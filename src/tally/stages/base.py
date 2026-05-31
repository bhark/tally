"""Stage definition, run context, and the control-flow exceptions stages raise."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from ..model import Cluster, Node, Stage
from ..paths import Paths


class StageError(RuntimeError):
    """A stage cannot proceed (bad input, missing tool, unmet assumption)."""


class StageCancelled(RuntimeError):
    """Operator backed out of a guided step; the driver aborts the run cleanly.

    Nothing is persisted - the workdir artifacts are the only record, so a later
    re-run picks up from whatever is already on disk.
    """


@dataclass(slots=True)
class Ctx:
    cluster: Cluster
    paths: Paths
    debug: bool = False

    def talos_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "TALOSCONFIG": str(self.paths.talosconfig),
            "KUBECONFIG": str(self.paths.kubeconfig),
        }


# run(ctx, node) - node is None for cluster-scope stages
StageRun = Callable[[Ctx, Node | None], None]


@dataclass(frozen=True, slots=True)
class StageDef:
    key: Stage
    title: str
    run: StageRun
