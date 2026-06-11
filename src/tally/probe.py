"""Talos API reachability + readiness probes - single-shot and polled, spinner-wrapped.

The insecure probe answers only in maintenance mode (imaged, not yet applied); the
secure mTLS probe answers only from a joined member. Polled variants spin until a
condition holds or a deadline passes, so a dead or slow node falls back to guided
handling rather than hanging the wizard.
"""

from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Callable

from griphtui import spinner

PROBE_TIMEOUT = 10  # per-attempt cap


def check(cmd: list[str], env: dict[str, str], *, timeout: int = PROBE_TIMEOUT) -> bool:
    """one-shot reachability, no spinner - safe to call from worker threads."""
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _attempt(cmd: list[str], env: dict[str, str], timeout: int) -> bool:
    return check(cmd, env, timeout=timeout)


def _stdout(cmd: list[str], env: dict[str, str], timeout: int) -> str | None:
    # None ⇒ couldn't run or non-zero exit; distinct from an empty-but-successful read
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace").strip()


def _tcp_open(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _poll(check: Callable[[], bool], label: str, timeout: int, interval: int) -> bool:
    deadline = time.monotonic() + timeout
    with spinner(label):
        while True:
            if check():
                return True
            if time.monotonic() + interval >= deadline:
                return False
            time.sleep(interval)


def read(cmd: list[str], env: dict[str, str], *, timeout: int = PROBE_TIMEOUT) -> str | None:
    """One-shot value read: stripped stdout, or None on failure-to-run / non-zero exit."""
    return _stdout(cmd, env, timeout)


def reachable(cmd: list[str], env: dict[str, str], label: str) -> bool:
    # transient spinner; caller notes the outcome, so no done/fail line
    with spinner(label):
        return _attempt(cmd, env, PROBE_TIMEOUT)


def wait_until(
    cmd: list[str], env: dict[str, str], label: str, *, timeout: int, interval: int = 5
) -> bool:
    """Poll cmd until it exits 0 or timeout elapses. True ⇒ became reachable."""
    return _poll(lambda: _attempt(cmd, env, PROBE_TIMEOUT), label, timeout, interval)


def wait_gone(
    cmd: list[str], env: dict[str, str], label: str, *, timeout: int, interval: int = 5
) -> bool:
    """poll until cmd STOPS exiting 0 (api went away). True => gone before timeout."""
    return _poll(lambda: not _attempt(cmd, env, PROBE_TIMEOUT), label, timeout, interval)


def wait_port(host: str, port: int, label: str, *, timeout: int, interval: int = 5) -> bool:
    """Poll a tcp port until it accepts a connection or timeout elapses."""
    return _poll(lambda: _tcp_open(host, port, PROBE_TIMEOUT), label, timeout, interval)


def wait_for_value(
    cmd: list[str], env: dict[str, str], label: str, want: str, *, timeout: int, interval: int = 5
) -> bool:
    """Poll cmd until its stripped stdout equals want, or timeout elapses."""
    return _poll(lambda: _stdout(cmd, env, PROBE_TIMEOUT) == want, label, timeout, interval)
