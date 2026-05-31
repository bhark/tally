"""Stage 4 - push the full machine config over the Talos API.

Insecure (maintenance) for a freshly-imaged node; secure (mTLS) for one already
carrying our config, where re-applying converges drift (no reboot unless required).
The reachability probe doubles as the mode selector.
"""

from __future__ import annotations

from .. import probe
from ..model import Node, Stage
from ..runner import run
from .base import Ctx, StageDef, StageError


def run_apply(ctx: Ctx, node: Node | None) -> None:
    assert node is not None
    config = ctx.paths.config_for(node)
    if not config.exists():
        raise StageError(f"{config.name} missing; run config first")

    env = ctx.talos_env()
    maintenance = probe.reachable(
        ["talosctl", "-n", node.ip, "get", "disks", "--insecure"],
        env,
        f"Reach {node.name} ({node.ip})",
    )
    if maintenance:
        cmd = ["talosctl", "apply-config", "--insecure", "-n", node.ip, "-f", str(config)]
    else:  # configured node answers only over mTLS; auto mode reboots solely if required
        cmd = ["talosctl", "apply-config", "-e", node.ip, "-n", node.ip, "-f", str(config)]
    mode = "maintenance" if maintenance else "secure"
    run(cmd, label=f"Apply config to {node.name} ({mode})", env=env)


STAGE = StageDef(
    key=Stage.APPLY,
    title="Apply full config",
    run=run_apply,
)
