"""Orphan removal: tear down live k8s nodes that the desired tally.yaml no longer declares.

Not a StageDef - orphans are k8s facts, not Nodes, so we never synthesise a Node for
them. The Talos API is reached by proxying through a desired CP (with a vSwitch the
orphan's k8s InternalIP is a private VLAN address unreachable from the operator), and
graceful reset performs the etcd leave itself, so member IDs are never auto-parsed.
"""

from __future__ import annotations

from griphtui import confirm, is_cancel, note, warn

from . import probe
from .constants import K8S_API_PORT
from .runner import CommandError, run
from .stages.base import StageError

_RESET_GONE_TIMEOUT = 300
_DRAIN_TIMEOUT = "120s"


def reachable_address(cluster, env, orphan) -> str | None:
    """first orphan address answering the secure api proxied through the bootstrap CP, else None."""
    cp = cluster.bootstrap_cp
    for addr in orphan.addresses:
        if probe.check(["talosctl", "-e", cp.ip, "-n", addr, "version"], env):
            return addr
    return None


def repoint_endpoints(cluster, env) -> None:
    """point talosconfig at the desired CP public IPs (operator hop); call once before removal."""
    cp_ips = [n.ip for n in cluster.control_planes]
    run(["talosctl", "config", "endpoint", *cp_ips], label="Repoint talosconfig endpoints", env=env)


def protect_kubeconfig(cluster, paths, orphans) -> None:
    """repoint the kubeconfig server off any orphan to the bootstrap CP - don't saw the branch."""
    if not paths.kubeconfig.exists():
        return
    text = paths.kubeconfig.read_text()
    public = f"https://{cluster.bootstrap_cp.ip}:{K8S_API_PORT}"
    for orphan in orphans:
        for addr in orphan.addresses:
            server = f"https://{addr}:{K8S_API_PORT}"
            if server in text:
                paths.kubeconfig.write_text(text.replace(server, public))
                note(f"Rewrote kubeconfig server {server} → {public}")
                return


def remove_orphan(cluster, paths, env, orphan, *, wipe_all: bool) -> None:
    """drain (workers) -> reset (proxied) -> wait gone -> etcd verify (CP) -> delete -> purge."""
    addr = reachable_address(cluster, env, orphan)
    cp = cluster.bootstrap_cp

    if not orphan.control_plane:
        _drain_worker(paths, env, orphan)

    if addr is not None:
        _reset_and_wait(cp, env, orphan, addr, wipe_all=wipe_all)
        if orphan.control_plane:
            _show_etcd_members(cp, env)
    else:
        if not _confirm_unreachable_delete(cluster, env, orphan):
            warn(f"left {orphan.name} in the cluster")
            return

    _delete_node(paths, env, orphan)
    _purge_artifacts(paths, orphan)


def _drain_worker(paths, env, orphan) -> None:
    # belt-and-braces: graceful reset cordons/drains node-side too. never block on this.
    result = run(
        [
            "kubectl",
            "--kubeconfig",
            str(paths.kubeconfig),
            "drain",
            orphan.name,
            "--ignore-daemonsets",
            "--delete-emptydir-data",
            f"--timeout={_DRAIN_TIMEOUT}",
        ],
        label=f"Drain {orphan.name}",
        env=env,
        check=False,
    )
    if result.returncode != 0:
        warn(f"drain of {orphan.name} failed; continuing")


def _reset_and_wait(cp, env, orphan, addr, *, wipe_all: bool) -> None:
    # wipe-mode is always explicit - default 'all' would wipe data/OSD disks too.
    mode = "all" if wipe_all else "system-disk"
    # --wait=false: the node powers down mid-stream, so a synchronous wait never returns.
    try:
        run(
            [
                "talosctl",
                "reset",
                "-e",
                cp.ip,
                "-n",
                addr,
                "--graceful",
                "--wipe-mode",
                mode,
                "--wait=false",
            ],
            label=f"Reset {orphan.name} ({addr})",
            env=env,
        )
    except CommandError as exc:
        raise StageError(f"reset of {orphan.name} failed: {exc}") from exc

    gone = probe.wait_gone(
        ["talosctl", "-e", cp.ip, "-n", addr, "version"],
        env,
        f"Waiting for {orphan.name} API to go down",
        timeout=_RESET_GONE_TIMEOUT,
    )
    if not gone:
        warn(f"{orphan.name} API still answering after reset; continuing to k8s delete")


def _show_etcd_members(cp, env) -> None:
    # quorum sanity after a CP leaves; informational only.
    result = run(
        ["talosctl", "-e", cp.ip, "-n", cp.ip, "etcd", "members"],
        label="Verify etcd membership",
        env=env,
        check=False,
    )
    if result.returncode == 0:
        note(result.out)


def _confirm_unreachable_delete(cluster, env, orphan) -> bool:
    if orphan.control_plane:
        remaining = cluster.bootstrap_cp.ip
        # broken-member path only: graceful etcd-leave couldn't run, so the operator
        # removes the dead member by hand. never auto-parse the member ID.
        note(
            f"{orphan.name} is an unreachable control-plane. To clear its etcd member:\n"
            f"  talosctl -n {remaining} etcd members   # find the member ID\n"
            "  talosctl etcd remove-member <ID>"
        )
    answer = confirm(
        f"Node unreachable; delete {orphan.name} from Kubernetes anyway?", default=False
    )
    return not is_cancel(answer) and bool(answer)


def _delete_node(paths, env, orphan) -> None:
    # idempotent: the node may already be gone post-reset.
    result = run(
        ["kubectl", "--kubeconfig", str(paths.kubeconfig), "delete", "node", orphan.name],
        label=f"Delete {orphan.name} from Kubernetes",
        env=env,
        check=False,
    )
    if result.returncode != 0:
        warn(f"kubectl delete node {orphan.name} failed; it may already be gone")


def _purge_artifacts(paths, orphan) -> None:
    removed = paths.purge_node(orphan.name)
    if removed:
        note("Removed artifacts:\n" + "\n".join(str(p) for p in removed))
    else:
        note(f"no artifacts to remove for {orphan.name}")
