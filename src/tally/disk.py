"""Resolve a node's actual boot disk, live in maintenance mode, to pin install to it.

The dd target (where the operator wrote the raw image) and the install diskSelector
are independent: on a multi-NVMe box they can diverge, so Talos installs its bootloader
to a disk the BIOS won't boot and the node comes up "not Talos". Talos' own SystemDisk
names the disk it actually booted from; we read that disk's stable wwid (serial as
fallback) and pin install.diskSelector to it. Resolved live and injected into the
rendered config only - tally.yaml keeps its portable selector and self-heals across
disk swaps.
"""

from __future__ import annotations

import shutil

from . import probe

# wwid is the hardware WWN (stablest); serial is the next-best stable identifier
_STABLE_FIELDS = ("wwid", "serial")


def resolve_system_selector(ip: str, env: dict[str, str]) -> dict[str, str] | None:
    """Field-based install.diskSelector bound to the disk Talos booted from, or None.

    Reads SystemDisk (the booted disk) over the insecure maintenance API, then that
    disk's stable identifier. None ⇒ no clean signal; the caller keeps the declarative
    selector. install==boot holds by construction - both name the same physical disk.
    """
    if not ip or shutil.which("talosctl") is None:
        return None
    disk_id = probe.read(_get(ip, "systemdisk", "diskID"), env)
    if not disk_id:
        return None
    for field in _STABLE_FIELDS:
        value = probe.read(_get(ip, f"disks {disk_id}", field), env)
        if value:
            return {field: value}
    return None


def _get(ip: str, resource: str, field: str) -> list[str]:
    return [
        "talosctl",
        "-n",
        ip,
        "get",
        *resource.split(),
        "-o",
        f"jsonpath={{.spec.{field}}}",
        "--insecure",
    ]
