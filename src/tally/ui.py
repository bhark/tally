"""Shared presentation helpers over griphtui."""

from __future__ import annotations

from griphtui import note


def gap() -> None:
    # status lines carry no trailing bar; emit one so the next prompt/header doesn't abut
    note("")
