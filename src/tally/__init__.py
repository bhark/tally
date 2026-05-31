"""tally - terminal wizard driving the Talos-on-Hetzner bring-up runbook."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("tally")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.0.0+unknown"
