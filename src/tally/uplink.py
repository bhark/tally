"""Resolve a node's uplink NIC MAC, live in maintenance mode, to pin the link alias.

A fixed-string LinkAliasConfig must match exactly one link, so the structural
`match: true` breaks on multi-NIC boxes: the alias errors out and the node boots
with no network at all. The uplink is the link already carrying the node's public
address; its permanent MAC pins the alias (see s1_config.link_docs). Discovered,
never operator-typed; persisted to tally.yaml so image embeds stay reproducible.
"""

from __future__ import annotations

import shutil

from . import probe
from .model import is_mac


def resolve_uplink_mac(ip: str, env: dict[str, str]) -> str | None:
    """Permanent MAC of the link holding the node's address, or None on no clean signal."""
    if not ip or shutil.which("talosctl") is None:
        return None
    ids = probe.read(_get(ip, "addresses", "metadata.id"), env)
    link = None
    for line in (ids or "").splitlines():
        parts = line.strip().split("/")  # id: linkName/address/prefix
        if len(parts) >= 2 and parts[1] == ip:
            link = parts[0]
            break
    if not link:
        return None
    mac = probe.read(_get(ip, f"links {link}", "spec.permanentAddr"), env)
    if not mac:  # some drivers expose no permanent addr; the live one is the next-best pin
        mac = probe.read(_get(ip, f"links {link}", "spec.hardwareAddr"), env)
    mac = (mac or "").lower()
    return mac if is_mac(mac) else None


def _get(ip: str, resource: str, field: str) -> list[str]:
    return [
        "talosctl",
        "-n",
        ip,
        "get",
        *resource.split(),
        "-o",
        f"jsonpath={{.{field}}}",
        "--insecure",
    ]
