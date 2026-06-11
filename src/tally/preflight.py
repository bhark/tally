"""Tool presence checks. Reported, never auto-installed.

Top-level preflight covers every tool; the stage dispatcher re-checks just the
running stage's tools just-in-time, so a missing helm only blocks Cilium, not
config generation.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from .constants import REQUIRED_TOOLS
from .model import Stage

# which external tools each stage actually shells out to
STAGE_TOOLS: dict[Stage, tuple[str, ...]] = {
    Stage.CONFIG: ("talosctl",),
    Stage.IMAGE: ("docker", "crane"),
    Stage.RESCUE: ("docker", "crane"),  # image is (re)built mid-rescue, post uplink pin
    Stage.APPLY: ("talosctl",),
    Stage.BOOTSTRAP: ("talosctl",),
    Stage.CILIUM: ("helm", "kubectl"),
    Stage.REMOVE: ("talosctl", "kubectl"),
}


@dataclass(slots=True)
class ToolStatus:
    name: str
    path: str | None

    @property
    def present(self) -> bool:
        return self.path is not None


def check_tools(tools: tuple[str, ...] = REQUIRED_TOOLS) -> list[ToolStatus]:
    return [ToolStatus(tool, shutil.which(tool)) for tool in tools]


def missing_for_stage(stage: Stage) -> list[str]:
    return [t for t in STAGE_TOOLS.get(stage, ()) if shutil.which(t) is None]


def summary_lines(statuses: list[ToolStatus]) -> list[str]:
    missing = [s for s in statuses if not s.present]
    width = max((len(s.name) for s in missing), default=0)
    return [f"[MISS] {s.name.ljust(width)}  Not on PATH" for s in missing]
