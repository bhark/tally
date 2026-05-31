"""The single shell-out path.

The spinner thread is the sole terminal writer, so the subprocess MUST use
captured pipes - never the TTY, or its output escapes the griphtui frame.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from griphtui import note, spinner, success

_TAIL_LINES = 50


@dataclass(slots=True)
class CommandResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def out(self) -> str:
        return self.stdout.strip()


class CommandError(RuntimeError):
    def __init__(self, label: str, cmd: list[str], returncode: int, stderr_tail: list[str]) -> None:
        self.label = label
        self.cmd = cmd
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        super().__init__(f"{label} failed (exit {returncode})")


def run(
    cmd: list[str],
    *,
    label: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> CommandResult:
    with spinner(label):
        proc = subprocess.run(cmd, env=env, capture_output=True)
    stdout = proc.stdout.decode(errors="replace") if proc.stdout else ""
    stderr = proc.stderr.decode(errors="replace") if proc.stderr else ""
    if check and proc.returncode != 0:
        tail = (stderr.splitlines() or stdout.splitlines())[-_TAIL_LINES:]
        raise CommandError(label, cmd, proc.returncode, tail)
    success(label)
    return CommandResult(cmd, proc.returncode, stdout, stderr)


def run_interactive(cmd: list[str], *, label: str) -> int:
    """TTY-owning shell-out: no spinner, no capture, stdio inherited.

    The one sanctioned path where the subprocess talks to the terminal directly -
    used for ssh's interactive auth (passphrase / hardware-key touch / password),
    which the spinner's captured-pipe model can't carry.
    """
    note(label)
    return subprocess.run(cmd).returncode
