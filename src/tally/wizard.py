"""Linear, stateless bring-up: define the cluster, then walk the runbook.

No progress is stored. Topology comes from tally.yaml (or operator prompts);
idempotency comes from the workdir artifacts (secrets reused, images on disk,
talosctl apply/helm being declarative) plus three-state probe discovery - each
node is classified joined (skip), maintenance (apply only), or absent (image +
rescue + apply). Adding to an already-bootstrapped cluster skips bootstrap+cilium.
"""

from __future__ import annotations

import shutil
import traceback

from griphtui import (
    Option,
    error,
    intro,
    is_cancel,
    note,
    outro,
    select,
    spinner,
    success,
    warn,
)

from . import definition, disk, probe, prompts, uplink
from .constants import (
    DEFAULT_INSTALL_DISK,
    VSWITCH_MTU_DEFAULT,
    VSWITCH_SUBNET_DEFAULT,
)
from .model import (
    CpuVendor,
    InstallTarget,
    Node,
    NodeRole,
    ProfileKey,
    Stage,
    Vswitch,
    next_vlan_ip,
)
from .preflight import check_tools, missing_for_stage, summary_lines
from .runner import CommandError
from .stages import BY_KEY, Ctx, StageCancelled, StageDef, StageError
from .ui import gap, inventory_lines, node_line

_CONFIG = BY_KEY[Stage.CONFIG]
_RESCUE = BY_KEY[Stage.RESCUE]
_APPLY = BY_KEY[Stage.APPLY]
_BOOTSTRAP = BY_KEY[Stage.BOOTSTRAP]
_CILIUM = BY_KEY[Stage.CILIUM]

_APPLY_VERIFY_TIMEOUT = 900  # apply → install → reboot → secure API; firmware may walk PXE first
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
            note(inventory_lines(cluster), title="Nodes")
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
        if prompts.ask("Bootstrap etcd now? once per cluster", default=True):
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
    note(node_line(node), title=f"Bring up {node.name}")

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

    _run_phase(ctx, _RESCUE, node)  # builds the image post-pin; ends reachable in maintenance
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
    while not probe.wait_until(cmd, ctx.talos_env(), label, timeout=_APPLY_VERIFY_TIMEOUT):
        gap()
        choice = select(
            f"{node.name} not back on the secure API after {_APPLY_VERIFY_TIMEOUT // 60}m",
            [
                Option(label="Keep waiting", value="wait"),
                Option(label="Abort", value="abort"),
            ],
        )
        if is_cancel(choice) or choice == "abort":
            raise StageError(
                f"{node.name} did not answer the secure Talos API after apply "
                f"(apid/trustd or install/boot failure) - check the node console"
            )
    success(f"{node.name} rebooted into configured Talos")


def _pin_and_render(ctx: Ctx, node: Node) -> None:
    """Bind install to the booted disk and the link alias to the uplink, re-render on change.

    Both pins read live maintenance-mode state. The disk pin stays rendered-only (see
    disk.resolve_system_selector); the MAC pin persists to tally.yaml - the image embed
    depends on it, so reruns must see it before any rescue contact. A node with no clean
    signal keeps its declarative values; the operator is told.
    """
    before = node.resolved_install
    before_mac = node.link_mac
    selector = disk.resolve_system_selector(node.ip, ctx.talos_env())
    if selector:
        node.resolved_install = InstallTarget(selector=selector)
        note(f"{node.name}: install pinned to boot disk → {node.resolved_install.describe()}")
    else:
        warn(f"{node.name}: boot disk unresolved; keeping {node.install.describe()}")
    mac = uplink.resolve_uplink_mac(node.ip, ctx.talos_env())
    if mac and mac != node.link_mac:
        node.link_mac = mac
        definition.save(ctx.cluster, ctx.paths.defn)
        note(f"{node.name}: link alias pinned to uplink mac {mac}")
    if node.resolved_install != before or node.link_mac != before_mac:
        _run_phase(ctx, _CONFIG, None)


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
    vlan = prompts.ask_text(
        "vSwitch VLAN ID",
        default=str(current.vlan_id) if current else "",
        validate=prompts.vlan_id_validator,
    )
    if vlan is None:
        return
    subnet = prompts.ask_text(
        "vSwitch subnet (CIDR, pick whatever you like)",
        default=current.subnet if current else VSWITCH_SUBNET_DEFAULT,
        validate=prompts.cidr_validator,
    )
    if subnet is None:
        return
    cluster.vswitch = Vswitch(vlan_id=int(vlan), subnet=subnet, mtu=VSWITCH_MTU_DEFAULT)
    definition.save(cluster, ctx.paths.defn)
    success(f"vSwitch configured: VLAN {vlan}, {subnet}")


def _set_cluster_name(ctx: Ctx) -> None:
    """Prompt the cluster name once; baked into gen config, so fix later by hand-editing."""
    cluster = ctx.cluster
    name = prompts.ask_text("Cluster name", validate=prompts.cluster_name_validator)
    if name is None:
        return
    cluster.name = name
    definition.save(cluster, ctx.paths.defn)
    success(f"Cluster name set to {name!r}")


# node prompts --------------------------------------------------------------


def _add_node(ctx: Ctx) -> None:
    """Prompt a full node (role first), append on success. Any cancel aborts cleanly."""
    cluster = ctx.cluster
    role = prompts.ask_role()
    if role is None:
        return
    label = "control-plane" if role is NodeRole.CONTROLPLANE else "worker"
    name = prompts.ask_text(f"{label.capitalize()} name", validate=prompts.name_validator(cluster))
    if name is None:
        return
    cpu = prompts.ask_cpu(CpuVendor.AMD)
    if cpu is None:
        return
    profile = prompts.ask_profile(ProfileKey.GENERIC)
    if profile is None:
        return
    ip = prompts.ask_text("IPv4 address", validate=prompts.ipv4_validator)
    if ip is None:
        return
    gateway = prompts.ask_text("Gateway", validate=prompts.ipv4_validator)
    if gateway is None:
        return
    vlan_ip = ""
    if cluster.vswitch is not None:  # auto-assigned, not prompted: CP from .1, worker from .100
        vlan_ip = next_vlan_ip(cluster, role)
        if vlan_ip is None:
            error(f"No free vSwitch IP for a {label} in {cluster.vswitch.subnet}")
            return
        note(f"vSwitch IP auto-assigned: {vlan_ip}")
    install = prompts.ask_install_target(InstallTarget(disk=DEFAULT_INSTALL_DISK), profile)
    if install is None:
        return
    fw = prompts.ask_text("Extra NIC firmware extension ref (blank if none)", default="")
    if fw is None:
        return
    extra = prompts.ask_extra_patches([])
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


# small helpers -------------------------------------------------------------


def _show_preflight() -> None:
    with spinner("Checking required tools"):
        statuses = check_tools()
    lines = summary_lines(statuses)
    if lines:
        note(lines, title="Preflight (missing tools, not installed)")


def _label(sd: StageDef, node: Node | None) -> str:
    return f"{sd.title} ({node.name})" if node is not None else sd.title
