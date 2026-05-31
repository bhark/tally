"""SSH master-connection lifecycle for driving a Hetzner rescue host.

One connection is authenticated interactively (ssh owns the TTY, so it can prompt
for a key passphrase, hardware-key touch, or the rescue root password - any auth
style, no secret ever held here). That master is then multiplexed: upload/exec/probe
reuse the socket with captured pipes, never re-authenticating. UserKnownHostsFile is
/dev/null - a reimaged host's key legitimately changes, and we won't touch the
operator's known_hosts or stall on REMOTE HOST IDENTIFICATION HAS CHANGED.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from . import runner
from .model import InstallTarget
from .runner import CommandResult

_CONNECT_TIMEOUT = 10
_CONTROL_PERSIST = 60


class RemoteError(RuntimeError):
    pass


@dataclass(slots=True)
class Session:
    host: str
    tmp: str

    @property
    def target(self) -> str:
        return f"root@{self.host}"

    @property
    def socket(self) -> str:
        return os.path.join(self.tmp, "s")


_BASE_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    f"ConnectTimeout={_CONNECT_TIMEOUT}",
    "-o",
    "ControlMaster=auto",
    "-o",
    f"ControlPersist={_CONTROL_PERSIST}",
]


def _opts(socket: str) -> list[str]:
    return [*_BASE_OPTS, "-o", f"ControlPath={socket}"]


def connect(host: str) -> Session:
    """Authenticate the master connection interactively; raise if it can't be opened."""
    tmp = tempfile.mkdtemp(prefix="tally-cm-")  # short path stays under ControlPath's length cap
    session = Session(host=host, tmp=tmp)
    rc = runner.run_interactive(
        ["ssh", *_opts(session.socket), session.target, "true"],
        label=f"Connecting to {session.target} (ssh may prompt for a passphrase or password)",
    )
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RemoteError(f"could not open ssh session to {session.target}")
    return session


def exec(session: Session, command: str, label: str, *, check: bool = True) -> CommandResult:
    return runner.run(
        ["ssh", *_opts(session.socket), session.target, command], label=label, check=check
    )


def upload(session: Session, local: str, remote: str, label: str) -> CommandResult:
    return runner.run(
        ["scp", *_opts(session.socket), local, f"{session.target}:{remote}"], label=label
    )


def disconnect(session: Session) -> None:
    subprocess.run(
        ["ssh", "-O", "exit", *_opts(session.socket), session.target], capture_output=True
    )
    shutil.rmtree(session.tmp, ignore_errors=True)


def probe_disks(session: Session) -> list[dict]:
    res = exec(
        session,
        "lsblk -dJ -o NAME,TYPE,TRAN,ROTA,SIZE",
        f"Probing disks on {session.host}",
    )
    return json.loads(res.out).get("blockdevices", [])


def _root_mounted(blockdevices: list[dict]) -> bool:
    for row in blockdevices:
        if row.get("mountpoint") == "/":
            return True
        if _root_mounted(row.get("children") or []):
            return True
    return False


def verify_rescue(session: Session) -> None:
    """Refuse to proceed unless root is a rescue ramdisk.

    Hetzner rescue runs from a ramdisk with the physical disks unmounted, so '/' never appears
    as a block-device mountpoint. A '/' mount on a real disk means we reached the installed OS,
    where the all-disk wipe would be catastrophic - host-key change can't catch it since we pin
    UserKnownHostsFile=/dev/null.
    """
    res = exec(session, "lsblk -Jo NAME,MOUNTPOINT", f"Verifying rescue mode on {session.host}")
    if _root_mounted(json.loads(res.out).get("blockdevices", [])):
        raise RemoteError(f"root is mounted from a disk on {session.host} - not a rescue ramdisk")


def _is_rotational(row: dict) -> bool:
    rota = row.get("rota")
    return rota in (True, "1", 1)


def _matches(row: dict, dtype: str) -> bool:
    if dtype == "nvme":
        return row.get("tran") == "nvme"
    if dtype == "ssd":
        return not _is_rotational(row)
    if dtype == "hdd":
        return _is_rotational(row)
    if dtype == "sd":
        return str(row.get("name", "")).startswith("sd")
    return False


def select_disk(install: InstallTarget, disks: list[dict]) -> str:
    """Resolve the dd target /dev path: explicit disk wins, else first selector match.

    Filters lsblk's whole-disk rows by the install selector's type and returns the
    lowest-named match (nvme0n1 before nvme1n1). The dd target only needs to be a disk
    the BIOS will boot; install.diskSelector is re-pinned post-boot from Talos' own
    SystemDisk (see disk.resolve_system_selector).
    """
    if install.disk:
        return install.disk
    dtype = (install.selector or {}).get("type")
    if not dtype:
        raise RemoteError("install target has no disk and no selector type to probe")
    candidates = sorted(
        (str(r["name"]) for r in disks if r.get("type") == "disk" and _matches(r, dtype)),
    )
    if not candidates:
        seen = ", ".join(f"{r.get('name')}({r.get('tran') or '?'})" for r in disks) or "none"
        raise RemoteError(
            f"no disk matched selector type={dtype}; lsblk saw: {seen}. "
            "pin install.disk in tally.yaml"
        )
    return f"/dev/{candidates[0]}"
