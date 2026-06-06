"""Linear, stateless bring-up: define the cluster, then walk the runbook.

No progress is stored. Topology comes from tally.yaml (or operator prompts);
idempotency comes from the workdir artifacts (secrets reused, images on disk,
talosctl apply/helm being declarative) plus three-state probe discovery - each
node is classified joined (skip), maintenance (apply only), or absent (image +
rescue + apply). Adding to an already-bootstrapped cluster skips bootstrap+cilium.
"""

from __future__ import annotations

import ipaddress
import shutil
import traceback

from griphtui import (
    Option,
    confirm,
    error,
    intro,
    is_cancel,
    note,
    outro,
    select,
    spinner,
    success,
    text,
    warn,
)

from . import definition, disk, probe
from .constants import (
    DEFAULT_INSTALL_DISK,
    VLAN_ID_MAX,
    VLAN_ID_MIN,
    VSWITCH_CP_HOST_START,
    VSWITCH_MTU_DEFAULT,
    VSWITCH_SUBNET_DEFAULT,
    VSWITCH_WORKER_HOST_START,
)
from .model import (
    Cluster,
    CpuVendor,
    InstallTarget,
    Node,
    NodeRole,
    ProfileKey,
    Stage,
    Vswitch,
    is_dns_label,
    is_host_in_subnet,
    is_ipv4,
)
from .preflight import check_tools, missing_for_stage, summary_lines
from .profiles import profile_for
from .runner import CommandError
from .stages import BY_KEY, Ctx, StageCancelled, StageDef, StageError
from .ui import gap

_CONFIG = BY_KEY[Stage.CONFIG]
_IMAGE = BY_KEY[Stage.IMAGE]
_RESCUE = BY_KEY[Stage.RESCUE]
_APPLY = BY_KEY[Stage.APPLY]
_BOOTSTRAP = BY_KEY[Stage.BOOTSTRAP]
_CILIUM = BY_KEY[Stage.CILIUM]

_APPLY_VERIFY_TIMEOUT = 300  # apply → install to disk → reboot → secure API; generous
_WORKER_READY_TIMEOUT = 300  # worker registers + Cilium schedules → node Ready


def run(ctx: Ctx) -> None:
    intro("Tally: Talos on Hetzner bring-up")
    _show_preflight()
    if not _define_cluster(ctx):
        outro("Exited before bring-up")
        return
    try:
        _bring_up(ctx)
    except StageCancelled as e:
        warn(str(e))
        note("Re-run with the same --dir to continue from the artifacts on disk")
        return
    except StageError as e:
        error(str(e))
        if ctx.debug:
            note(traceback.format_exc(), title="Traceback")
        return
    outro("Cluster up; definition in tally.yaml, secrets in the secrets dir")


# define --------------------------------------------------------------------


def _define_cluster(ctx: Ctx) -> bool:
    """Add nodes until the topology validates, then proceed. Saves tally.yaml.

    Add-only: correcting or removing a node is done by hand-editing tally.yaml.
    A defined node may already be live, so local edit/delete would drift from the
    cluster - out of scope here.
    """
    cluster = ctx.cluster
    while True:
        gap()
        if cluster.name:
            note(cluster.name, title="Cluster")
        if cluster.nodes:
            note(_inventory_lines(cluster), title="Nodes")
        else:
            note("No nodes yet", title="Nodes")
        problems = cluster.problems() + [p for n in cluster.nodes for p in n.problems()]
        if problems:
            note(problems, title="Resolve before bring-up")

        options: list[Option[str]] = []
        if not problems:
            options.append(Option(label="Proceed with bring-up", value="__go__"))
        if not cluster.name:
            options.append(Option(label="Set cluster name", value="__name__"))
        options.append(Option(label="Add a node", value="__add__"))
        if cluster.vswitch is None:
            options.append(Option(label="Configure vSwitch", value="__vswitch__"))
        else:
            vs = cluster.vswitch
            label = f"Reconfigure vSwitch (VLAN {vs.vlan_id}, {vs.subnet})"
            options.append(Option(label=label, value="__vswitch__"))
            options.append(Option(label="Disable vSwitch", value="__novswitch__"))
        options.append(Option(label="Quit", value="__quit__"))

        choice = select("Define the cluster", options)
        if is_cancel(choice) or choice == "__quit__":
            return False
        if choice == "__go__":
            return True
        if choice == "__name__":
            _set_cluster_name(ctx)
        if choice == "__add__":
            _add_node(ctx)
        if choice == "__vswitch__":
            _configure_vswitch(ctx)
        if choice == "__novswitch__":
            cluster.vswitch = None
            definition.save(cluster, ctx.paths.defn)
            success("vSwitch disabled")


# bring-up ------------------------------------------------------------------


def _bring_up(ctx: Ctx) -> None:
    _run_phase(ctx, _CONFIG, None)  # regen all per-node configs (idempotent)
    cp = ctx.cluster.bootstrap_cp
    rest = [n for n in ctx.cluster.nodes if n is not cp]
    # kubeconfig present ⇒ already bootstrapped → join-only walk, secure probe enabled
    cluster_up = ctx.paths.kubeconfig.exists()
    for node in [cp, *rest]:  # bootstrap_cp first so BOOTSTRAP targets a live CP
        _bring_up_node(ctx, node, cluster_up=cluster_up)

    if not cluster_up:
        gap()
        if _ask("Bootstrap etcd now? once per cluster", default=True):
            _run_phase(ctx, _BOOTSTRAP, None)
        else:
            note("Skipped bootstrap")

    # gate on actual CNI presence, not cluster_up: a run that bootstrapped then died at
    # cilium must still install on rerun (helm upgrade --install makes it safe)
    gap()
    if _cilium_installed(ctx):
        note("Cilium already installed → skipping")
    else:
        _run_phase(ctx, _CILIUM, None)

    _verify_workers_ready(ctx)


def _verify_workers_ready(ctx: Ctx) -> None:
    """Final gate: every worker must register Ready in k8s, else bring-up was not a success.

    Runs after bootstrap+Cilium (a worker can't be Ready before the API exists and CNI
    schedules). Skipped without a kubeconfig (bootstrap declined) or for an IP-less node.
    """
    if not ctx.paths.kubeconfig.exists() or shutil.which("kubectl") is None:
        return
    for node in ctx.cluster.workers:
        if not node.ip:
            continue
        cmd = [
            "kubectl",
            "--kubeconfig",
            str(ctx.paths.kubeconfig),
            "get",
            "node",
            node.name,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
        ]
        label = f"Waiting for worker {node.name} Ready (≤{_WORKER_READY_TIMEOUT // 60}m)"
        if not probe.wait_for_value(
            cmd, ctx.talos_env(), label, "True", timeout=_WORKER_READY_TIMEOUT
        ):
            raise StageError(
                f"worker {node.name} never became Ready in Kubernetes "
                f"(trustd/apid join or CNI failure) - check the node console"
            )
        success(f"worker {node.name} joined and Ready")


def _cilium_installed(ctx: Ctx) -> bool:
    if not ctx.paths.kubeconfig.exists() or shutil.which("kubectl") is None:
        return False
    return probe.reachable(
        [
            "kubectl",
            "--kubeconfig",
            str(ctx.paths.kubeconfig),
            "get",
            "ds",
            "-n",
            "kube-system",
            "cilium",
        ],
        ctx.talos_env(),
        "Checking whether Cilium is installed",
    )


def _bring_up_node(ctx: Ctx, node: Node, *, cluster_up: bool) -> None:
    gap()
    note(_node_line(node), title=f"Bring up {node.name}")

    if _in_maintenance(ctx, node):
        note(f"{node.name} in maintenance → applying config")
        _pin_and_render(ctx, node)
        _apply_and_verify(ctx, node)
        return
    if _configured(ctx, node):  # answers mTLS ⇒ already carries our config
        if cluster_up:  # live member of an up cluster - leave it untouched
            note(f"{node.name} already joined ({node.ip}) → skipping")
        else:  # configured but pre-bootstrap → re-apply to converge config drift
            note(f"{node.name} already configured ({node.ip}) → re-applying config")
            _apply_and_verify(ctx, node)
        return

    _ensure_image(ctx, node)
    _run_phase(ctx, _RESCUE, node)  # ends with the node reachable in maintenance
    _pin_and_render(ctx, node)
    _apply_and_verify(ctx, node)


def _apply_and_verify(ctx: Ctx, node: Node) -> None:
    """Apply the config, then wait for the node to reboot into it and answer the secure API.

    apply-config returns the instant maintenance-mode apid ACCEPTS the config - before the
    node installs, reboots, and brings apid back over mTLS. A worker whose apid can't get its
    trustd-signed cert (e.g. cross-node path blocked) wedges here silently; this turns that into
    a loud, located failure. Reaches the node over its public IP (admin-IP firewall allows).
    """
    _run_phase(ctx, _APPLY, node)
    if not node.ip or not ctx.paths.talosconfig.exists() or shutil.which("talosctl") is None:
        return  # pre-config / unvalidated node (tests); nothing to verify against yet
    cmd = ["talosctl", "-e", node.ip, "-n", node.ip, "version"]
    label = f"Waiting for {node.name} to reboot into config (≤{_APPLY_VERIFY_TIMEOUT // 60}m)"
    if not probe.wait_until(cmd, ctx.talos_env(), label, timeout=_APPLY_VERIFY_TIMEOUT):
        raise StageError(
            f"{node.name} did not answer the secure Talos API after apply "
            f"(apid/trustd or install/boot failure) - check the node console"
        )
    success(f"{node.name} rebooted into configured Talos")


def _pin_and_render(ctx: Ctx, node: Node) -> None:
    """Bind install to the disk the node actually booted, then re-render if it changed.

    Resolved live from Talos' SystemDisk and kept only in the rendered config - the
    declarative tally.yaml is untouched (see disk.resolve_system_selector). A node
    with no clean signal keeps its declarative selector; the operator is told.
    """
    before = node.resolved_install
    selector = disk.resolve_system_selector(node.ip, ctx.talos_env())
    if selector:
        node.resolved_install = InstallTarget(selector=selector)
        note(f"{node.name}: install pinned to boot disk → {node.resolved_install.describe()}")
    else:
        warn(f"{node.name}: boot disk unresolved; keeping {node.install.describe()}")
    if node.resolved_install != before:  # pin changed the effective target → re-render
        _run_phase(ctx, _CONFIG, None)


def _ensure_image(ctx: Ctx, node: Node) -> None:
    if ctx.paths.image(node).exists():
        note(f"Reusing existing image {ctx.paths.image(node).name}")
        return
    _run_phase(ctx, _IMAGE, node)


def _run_phase(ctx: Ctx, sd: StageDef, node: Node | None) -> None:
    label = _label(sd, node)
    missing = missing_for_stage(sd.key)
    if missing:
        raise StageError(f"{label}: missing tools ({', '.join(missing)})")
    try:
        sd.run(ctx, node)
    except CommandError as e:
        error(f"{label} failed (exit {e.returncode})")
        if e.stderr_tail:
            gap()
            note(e.stderr_tail, title="Last output")
        raise StageCancelled(f"{label} failed") from e
    success(f"{label} complete")


def _in_maintenance(ctx: Ctx, node: Node) -> bool:
    """Insecure probe: the Talos API answers unauthenticated only in maintenance mode.

    True ⇒ imaged but not yet applied. Bounded; a no-answer falls back to guided rescue.
    """
    if not node.ip or shutil.which("talosctl") is None:
        return False
    return probe.reachable(
        ["talosctl", "-n", node.ip, "get", "disks", "--insecure"],
        ctx.talos_env(),
        f"Probing {node.name} ({node.ip})",
    )


def _configured(ctx: Ctx, node: Node) -> bool:
    """Secure mTLS probe: only a node already carrying our config answers with the client cert.

    True regardless of whether the cluster is bootstrapped - a maintenance or absent node
    fails the same way (no mTLS). The caller uses cluster_up to tell a live member (skip)
    from a configured-but-pre-bootstrap node (re-apply). Needs the generated talosconfig.
    """
    if not node.ip or shutil.which("talosctl") is None:
        return False
    if not ctx.paths.talosconfig.exists():
        return False
    return probe.reachable(
        ["talosctl", "-e", node.ip, "-n", node.ip, "version"],
        ctx.talos_env(),
        f"Checking whether {node.name} ({node.ip}) already has config",
    )


# cluster prompts -----------------------------------------------------------


def _configure_vswitch(ctx: Ctx) -> None:
    """Prompt VLAN ID + subnet (MTU defaulted to the Hetzner cap), persist tally.yaml."""
    cluster = ctx.cluster
    current = cluster.vswitch
    vlan = _ask_text(
        "vSwitch VLAN ID",
        default=str(current.vlan_id) if current else "",
        validate=_vlan_id_validator,
    )
    if vlan is None:
        return
    subnet = _ask_text(
        "vSwitch subnet (CIDR, pick whatever you like)",
        default=current.subnet if current else VSWITCH_SUBNET_DEFAULT,
        validate=_cidr_validator,
    )
    if subnet is None:
        return
    cluster.vswitch = Vswitch(vlan_id=int(vlan), subnet=subnet, mtu=VSWITCH_MTU_DEFAULT)
    definition.save(cluster, ctx.paths.defn)
    success(f"vSwitch configured: VLAN {vlan}, {subnet}")


def _set_cluster_name(ctx: Ctx) -> None:
    """Prompt the cluster name once; baked into gen config, so fix later by hand-editing."""
    cluster = ctx.cluster
    name = _ask_text("Cluster name", validate=_cluster_name_validator)
    if name is None:
        return
    cluster.name = name
    definition.save(cluster, ctx.paths.defn)
    success(f"Cluster name set to {name!r}")


# node prompts --------------------------------------------------------------


def _add_node(ctx: Ctx) -> None:
    """Prompt a full node (role first), append on success. Any cancel aborts cleanly."""
    cluster = ctx.cluster
    role = _ask_role()
    if role is None:
        return
    label = "control-plane" if role is NodeRole.CONTROLPLANE else "worker"
    name = _ask_text(f"{label.capitalize()} name", validate=_name_validator(cluster))
    if name is None:
        return
    cpu = _ask_cpu(CpuVendor.AMD)
    if cpu is None:
        return
    profile = _ask_profile(ProfileKey.GENERIC)
    if profile is None:
        return
    ip = _ask_text("IPv4 address", validate=_ipv4_validator)
    if ip is None:
        return
    gateway = _ask_text("Gateway", validate=_ipv4_validator)
    if gateway is None:
        return
    vlan_ip = ""
    if cluster.vswitch is not None:  # auto-assigned, not prompted: CP from .1, worker from .100
        vlan_ip = _next_vlan_ip(cluster, role)
        if vlan_ip is None:
            error(f"No free vSwitch IP for a {label} in {cluster.vswitch.subnet}")
            return
        note(f"vSwitch IP auto-assigned: {vlan_ip}")
    install = _ask_install_target(InstallTarget(disk=DEFAULT_INSTALL_DISK), profile)
    if install is None:
        return
    fw = _ask_text("Extra NIC firmware extension ref (blank if none)", default="")
    if fw is None:
        return
    extra = _ask_extra_patches([])
    if extra is None:
        return
    cluster.nodes.append(
        Node(
            name=name,
            role=role,
            cpu=cpu,
            profile=profile,
            ip=ip,
            gateway=gateway,
            vlan_ip=vlan_ip,
            install=install,
            nic_firmware_ext=fw or None,
            extra_patches=extra,
        )
    )
    definition.save(cluster, ctx.paths.defn)
    success(f"Added {label} {name}")


def _ask_role() -> NodeRole | None:
    choice = select(
        "Node role",
        [
            Option(label="Control plane", value=NodeRole.CONTROLPLANE),
            Option(label="Worker", value=NodeRole.WORKER),
        ],
    )
    return None if is_cancel(choice) else choice


def _ask_cpu(default: CpuVendor) -> CpuVendor | None:
    choice = select(
        "CPU vendor",
        [
            Option(label="amd", value=CpuVendor.AMD, selected=default is CpuVendor.AMD),
            Option(label="intel", value=CpuVendor.INTEL, selected=default is CpuVendor.INTEL),
        ],
    )
    return None if is_cancel(choice) else choice


def _ask_profile(default: ProfileKey) -> ProfileKey | None:
    options = [
        Option(
            label=p.value,
            value=p,
            hint=profile_for(p).install_hint or None,
            selected=(p is default),
        )
        for p in ProfileKey
    ]
    choice = select("Workload profile", options)
    return None if is_cancel(choice) else choice


def _ask_install_target(default: InstallTarget, profile: ProfileKey) -> InstallTarget | None:
    hint = profile_for(profile).install_hint
    if hint:
        note(hint, title="Install-target guidance")
    mode = select(
        "Install target",
        [
            Option(
                label="Disk selector (preferred, stable across NVMe reordering)",
                value="selector",
                selected=default.selector is not None,
            ),
            Option(label="Explicit /dev path", value="disk", selected=default.disk is not None),
        ],
    )
    if is_cancel(mode):
        return None
    if mode == "disk":
        disk = _ask_text(
            "Install disk",
            default=default.disk or DEFAULT_INSTALL_DISK,
            validate=_dev_path_validator,
        )
        return None if disk is None else InstallTarget(disk=disk)
    return _ask_selector(default.selector or {})


def _ask_selector(default: dict[str, str]) -> InstallTarget | None:
    dtype = select(
        "Disk type",
        [Option(label="any", value="", selected="type" not in default)]
        + [
            Option(label=t, value=t, selected=default.get("type") == t)
            for t in ("nvme", "ssd", "hdd", "sd")
        ],
    )
    if is_cancel(dtype):
        return None
    size = _ask_text(
        "Size expression (e.g. '<= 4TB', blank to skip)", default=default.get("size", "")
    )
    if size is None:
        return None
    model = _ask_text("Model substring (blank to skip)", default=default.get("model", ""))
    if model is None:
        return None
    selector = {k: v for k, v in (("type", dtype), ("size", size), ("model", model)) if v}
    if not selector:
        warn("A diskSelector needs at least one field; nothing changed")
        return None
    return InstallTarget(selector=selector)


def _ask_extra_patches(default: list[str]) -> list[str] | None:
    """Opt-in bring-your-own --config-patch files. Cancel ⇒ None (abort add), no ⇒ []."""
    answer = confirm("Add bring-your-own config-patch files?", default=bool(default))
    if is_cancel(answer):
        return None
    if not answer:
        return []
    raw = _ask_text(
        "Patch file path(s), comma-separated (relative to where you run tally)",
        default=", ".join(default),
    )
    if raw is None:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


# small helpers -------------------------------------------------------------


def _show_preflight() -> None:
    with spinner("Checking required tools"):
        statuses = check_tools()
    lines = summary_lines(statuses)
    if lines:
        note(lines, title="Preflight (missing tools, not installed)")


def _label(sd: StageDef, node: Node | None) -> str:
    return f"{sd.title} ({node.name})" if node is not None else sd.title


def _inventory_lines(cluster: Cluster) -> list[str]:
    width = max((len(n.name) for n in cluster.nodes), default=0)
    return [f"{n.name.ljust(width)}  {_node_line(n)}" for n in cluster.nodes]


def _node_line(node: Node) -> str:
    role = f"{node.role.value}/{node.cpu.value}/{node.profile.value}"
    net = f"{node.ip or '?'} → {node.gateway or '?'}"
    vlan = f"  vlan {node.vlan_ip}" if node.vlan_ip else ""
    extra = f"  +{len(node.extra_patches)} patch" if node.extra_patches else ""
    return f"{role:<22}  {net}{vlan}  {node.install.describe()}{extra}"


def _ask(label: str, *, default: bool) -> bool:
    answer = confirm(label, default=default)
    return False if is_cancel(answer) else answer


def _ask_text(label: str, *, default: str = "", validate=None) -> str | None:
    value = text(label, default=default, validate=validate)
    if is_cancel(value):
        return None
    return value.strip()


def _ipv4_validator(v: str) -> str | None:
    return None if is_ipv4(v.strip()) else "Expected a dotted IPv4 address"


def _dev_path_validator(v: str) -> str | None:
    return None if v.strip().startswith("/dev/") else "Expected a /dev path"


def _vlan_id_validator(v: str) -> str | None:
    v = v.strip()
    if not v.isdigit():
        return "Expected a numeric VLAN ID"
    if not VLAN_ID_MIN <= int(v) <= VLAN_ID_MAX:
        return f"VLAN ID must be in [{VLAN_ID_MIN}, {VLAN_ID_MAX}]"
    return None


def _cidr_validator(v: str) -> str | None:
    try:
        ipaddress.IPv4Network(v.strip(), strict=False)
    except (ipaddress.AddressValueError, ValueError):
        return "Expected an IPv4 CIDR, e.g. 10.10.0.0/24"
    return None


def _next_vlan_ip(cluster: Cluster, role: NodeRole) -> str | None:
    """Lowest free host in the vswitch subnet for the role: CPs from .1, workers from
    .100, so the role reads off the address. None if that role's range is exhausted."""
    vswitch = cluster.vswitch
    net = vswitch.subnet_network
    base = int(net.network_address)
    start = VSWITCH_CP_HOST_START if role is NodeRole.CONTROLPLANE else VSWITCH_WORKER_HOST_START
    used = {n.vlan_ip for n in cluster.nodes if n.vlan_ip}
    for host in range(start, net.num_addresses):
        candidate = str(ipaddress.IPv4Address(base + host))
        if is_host_in_subnet(candidate, vswitch.subnet) and candidate not in used:
            return candidate
    return None


def _name_validator(cluster: Cluster):
    def validate(v: str) -> str | None:
        v = v.strip()
        if not v:
            return "Name is required"
        if not is_dns_label(v):
            return "Must be a DNS-1123 label (lowercase alphanumeric + -, ≤63 chars)"
        if any(n.name == v for n in cluster.nodes):
            return f"Name {v!r} already in use"
        return None

    return validate


def _cluster_name_validator(v: str) -> str | None:
    v = v.strip()
    if not v:
        return "Name is required"
    if not is_dns_label(v):
        return "Must be a DNS-1123 label (lowercase alphanumeric + -, ≤63 chars)"
    return None
