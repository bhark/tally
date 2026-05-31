"""Ordered stage registry. Sequencing is the wizard's linear driver, not stored state."""

from __future__ import annotations

from ..model import Stage
from .base import Ctx, StageCancelled, StageDef, StageError
from .s1_config import STAGE as _CONFIG
from .s2_image import STAGE as _IMAGE
from .s3_rescue import STAGE as _RESCUE
from .s4_apply import STAGE as _APPLY
from .s5_bootstrap import STAGE as _BOOTSTRAP
from .s6_cilium import STAGE as _CILIUM

STAGES: tuple[StageDef, ...] = (_CONFIG, _IMAGE, _RESCUE, _APPLY, _BOOTSTRAP, _CILIUM)
BY_KEY: dict[Stage, StageDef] = {s.key: s for s in STAGES}

__all__ = [
    "STAGES",
    "BY_KEY",
    "Ctx",
    "StageDef",
    "StageError",
    "StageCancelled",
]
