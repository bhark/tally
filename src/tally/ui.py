"""Shared presentation helpers over griphtui."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griphtui import note

if TYPE_CHECKING:
    from .model import Cluster
    from .observe import Snapshot


def gap() -> None:
    # status lines carry no trailing bar; emit one so the next prompt/header doesn't abut
    note("")


def node_line(node) -> str:
    role = f"{node.role.value}/{node.cpu.value}/{node.profile.value}"
    net = f"{node.ip or '?'} → {node.gateway or '?'}"
    vlan = f"  vlan {node.vlan_ip}" if node.vlan_ip else ""
    extra = f"  +{len(node.extra_patches)} patch" if node.extra_patches else ""
    return f"{role:<22}  {net}{vlan}  {node.install.describe()}{extra}"


def inventory_lines(cluster: Cluster, snap: Snapshot | None = None) -> list[str]:
    width = max((len(n.name) for n in cluster.nodes), default=0)
    lines = []
    for n in cluster.nodes:
        line = f"{n.name.ljust(width)}  {node_line(n)}"
        if snap is not None:
            state = snap.states.get(n.name)
            line += f"  [{state}]" if state else "  [?]"
        lines.append(line)
    return lines
