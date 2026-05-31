"""Stage 6 - install Cilium (CNI + kube-proxy replacement) via Helm."""

from __future__ import annotations

from griphtui import confirm, is_cancel, note

from .. import probe
from ..constants import CILIUM_HELM_SETS, CILIUM_VERSION, HELM_REPO_NAME, HELM_REPO_URL
from ..model import Node, Stage
from ..runner import run
from .base import Ctx, StageCancelled, StageDef

_APISERVER_TIMEOUT = 180


def run_cilium(ctx: Ctx, _node: Node | None) -> None:
    cp = ctx.cluster.bootstrap_cp
    env = ctx.talos_env()

    _await_apiserver(ctx, cp)

    run(
        ["helm", "repo", "add", HELM_REPO_NAME, HELM_REPO_URL, "--force-update"],
        label="Add cilium helm repo",
        env=env,
    )
    run(["helm", "repo", "update"], label="Update helm repos", env=env)

    # no MTU set: Cilium ≥1.19 auto-detects 1400 as min-over-devices (vSwitch VLAN at
    # 1400, net0 at 1500); a hardcoded MTU would mask a future regression. devices left auto.
    cmd = [
        "helm",
        "upgrade",
        "--install",
        "cilium",
        "cilium/cilium",
        "--version",
        CILIUM_VERSION,
        "-n",
        "kube-system",
    ]
    for s in CILIUM_HELM_SETS:
        cmd += ["--set", s]

    run(cmd, label="Install cilium", env=env)


def _await_apiserver(ctx: Ctx, cp: Node) -> None:
    # apiserver binds :6443 tens of seconds after bootstrap returns; /readyz needs no cni
    cmd = ["kubectl", "get", "--raw=/readyz"]
    label = f"Waiting for {cp.name} kube-apiserver to serve (≤{_APISERVER_TIMEOUT // 60}m)"
    if probe.wait_until(cmd, ctx.talos_env(), label, timeout=_APISERVER_TIMEOUT):
        note(f"{cp.name} kube-apiserver ready → installing Cilium")
        return
    answer = confirm(
        f"{cp.name} kube-apiserver not reachable after {_APISERVER_TIMEOUT // 60}m; "
        "check control-plane health. Install anyway?",
        default=False,
    )
    if is_cancel(answer) or not answer:
        raise StageCancelled("Kube-apiserver unreachable; Cilium install would fail")


STAGE = StageDef(
    key=Stage.CILIUM,
    title="Install Cilium",
    run=run_cilium,
)
