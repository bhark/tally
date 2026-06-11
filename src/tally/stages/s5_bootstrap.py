"""Stage 5 - point talosconfig at the CP and bootstrap etcd (once)."""

from __future__ import annotations

from griphtui import confirm, is_cancel, note

from .. import probe
from ..constants import K8S_API_PORT
from ..model import Node, Stage
from ..runner import run
from .base import Ctx, StageCancelled, StageDef

_SYNC_TIMEOUT = 150  # first NTP sync on a freshly-booted bare-metal CP; generous


def run_bootstrap(ctx: Ctx, _node: Node | None) -> None:
    cp = ctx.cluster.bootstrap_cp
    cp_ips = [n.ip for n in ctx.cluster.control_planes]
    # rpcs relay via an arbitrary cp endpoint; hetzner firewalls inter-node public
    # :50000, so the relay's dial must ride the vswitch when there is one
    node_ip = cp.vlan_ip if ctx.cluster.vswitch else cp.ip
    env = ctx.talos_env()
    paths = ctx.paths

    # endpoints stay public CP IPs - the operator hop (admin → :50000 is firewall-allowed)
    run(["talosctl", "config", "endpoint", *cp_ips], label="Set talosconfig endpoints", env=env)
    run(["talosctl", "config", "node", node_ip], label="Set talosconfig node", env=env)

    # etcd answers member list only once bootstrapped; skip re-bootstrap on resume
    already = probe.reachable(
        ["talosctl", "-n", node_ip, "etcd", "members"],
        env,
        f"Checking whether {cp.name} etcd is already bootstrapped",
    )
    if already:
        note(f"{cp.name} etcd already bootstrapped → skipping bootstrap")
    else:
        _await_timesync(ctx, cp, node_ip)
        run(["talosctl", "bootstrap"], label="Bootstrap etcd (once)", env=env)

    run(["talosctl", "kubeconfig", str(paths.secret)], label="Fetch kubeconfig", env=env)
    _rewrite_kubeconfig_server(ctx, cp)
    paths.harden()


def _rewrite_kubeconfig_server(ctx: Ctx, cp: Node) -> None:
    """vSwitch clusters bake the private VLAN endpoint into the kubeconfig; rewrite it to
    the CP public IP so the operator's workstation kubectl/helm reach the API off-cluster.

    Public IP is in the cert SANs, so TLS still validates. No-op without a vswitch.
    """
    vswitch = ctx.cluster.vswitch
    if vswitch is None:
        return
    private = f"https://{cp.vlan_ip}:{K8S_API_PORT}"
    public = f"https://{cp.ip}:{K8S_API_PORT}"
    kubeconfig = ctx.paths.kubeconfig
    text = kubeconfig.read_text()
    if private not in text:
        note(f"Kubeconfig server is not {private}; left unchanged")
        return
    kubeconfig.write_text(text.replace(private, public))
    note(f"Rewrote kubeconfig server {private} → {public}")


def _await_timesync(ctx: Ctx, cp: Node, node_ip: str) -> None:
    """etcd refuses to bootstrap until the CP clock is NTP-synced (FailedPrecondition).

    The operator confirms the moment they can, but a freshly-booted node may not have
    finished its first SNTP sync - so spin on TimeStatus.synced rather than letting
    bootstrap fail and forcing a manual retry.
    """
    cmd = ["talosctl", "-n", node_ip, "get", "timestatus", "-o", "jsonpath={.spec.synced}"]
    label = f"Waiting for {cp.name} clock to NTP-sync (≤{_SYNC_TIMEOUT // 60}m)"
    if probe.wait_for_value(cmd, ctx.talos_env(), label, "true", timeout=_SYNC_TIMEOUT):
        note(f"{cp.name} clock synced → bootstrapping etcd")
        return
    answer = confirm(
        f"{cp.name} clock not synced after {_SYNC_TIMEOUT // 60}m; check NTP reachability. "
        "Bootstrap anyway?",
        default=False,
    )
    if is_cancel(answer) or not answer:
        raise StageCancelled("Control-plane clock not in sync; bootstrap would fail")


STAGE = StageDef(
    key=Stage.BOOTSTRAP,
    title="Bootstrap control-plane",
    run=run_bootstrap,
)
