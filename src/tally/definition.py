"""tally.yaml - the git-safe, declarative cluster definition.

Operator-authored input: loaded to seed the wizard, saved back after a node is
defined or corrected. Topology only (nodes and their fields) - no secrets, no
stage/progress state. Progress lives in the workdir artifacts and the live
cluster, never here, so this file is safe to commit and hand-edit.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .constants import DEFAULT_INSTALL_DISK
from .model import Cluster, CpuVendor, InstallTarget, Node, NodeRole, ProfileKey, Vswitch

FILENAME = "tally.yaml"
SCHEMA_VERSION = 2


def load(defn_dir: Path) -> Cluster | None:
    path = defn_dir / FILENAME
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text()) or {}
    return Cluster(
        nodes=[_node_from_dict(n) for n in raw.get("nodes", [])],
        name=raw.get("name", ""),
        vswitch=_vswitch_from_dict(raw.get("vswitch")),
    )


def save(cluster: Cluster, defn_dir: Path) -> None:
    payload: dict = {"schema": SCHEMA_VERSION}
    if cluster.name:
        payload["name"] = cluster.name
    if cluster.vswitch is not None:
        payload["vswitch"] = _vswitch_to_dict(cluster.vswitch)
    payload["nodes"] = [_node_to_dict(n) for n in cluster.nodes]
    (defn_dir / FILENAME).write_text(yaml.safe_dump(payload, sort_keys=False))


def _vswitch_to_dict(vswitch: Vswitch) -> dict:
    return {"vlan_id": vswitch.vlan_id, "subnet": vswitch.subnet, "mtu": vswitch.mtu}


def _vswitch_from_dict(raw: dict | None) -> Vswitch | None:
    if not raw:
        return None
    return Vswitch(vlan_id=raw["vlan_id"], subnet=raw["subnet"], mtu=raw["mtu"])


def _node_to_dict(node: Node) -> dict:
    return {
        "name": node.name,
        "role": node.role.value,
        "cpu": node.cpu.value,
        "profile": node.profile.value,
        "ip": node.ip,
        "gateway": node.gateway,
        "vlan_ip": node.vlan_ip,
        "install": _install_to_dict(node.install),
        "link_mac": node.link_mac,
        "nic_firmware_ext": node.nic_firmware_ext,
        "extra_patches": list(node.extra_patches),
    }


def _node_from_dict(raw: dict) -> Node:
    return Node(
        name=raw["name"],
        role=NodeRole(raw["role"]),
        cpu=CpuVendor(raw["cpu"]),
        profile=ProfileKey(raw.get("profile", ProfileKey.GENERIC.value)),
        ip=raw.get("ip", ""),
        gateway=raw.get("gateway", ""),
        vlan_ip=raw.get("vlan_ip", ""),
        install=_install_from_dict(raw.get("install")),
        link_mac=raw.get("link_mac", ""),
        nic_firmware_ext=raw.get("nic_firmware_ext"),
        extra_patches=list(raw.get("extra_patches", [])),
    )


def _install_to_dict(target: InstallTarget) -> dict:
    if target.disk:
        return {"disk": target.disk}
    return {"selector": dict(target.selector or {})}


def _install_from_dict(raw: dict | None) -> InstallTarget:
    if not raw:
        return InstallTarget(disk=DEFAULT_INSTALL_DISK)
    if raw.get("selector"):
        return InstallTarget(selector=dict(raw["selector"]))
    return InstallTarget(disk=raw.get("disk", DEFAULT_INSTALL_DISK))
