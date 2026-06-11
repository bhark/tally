"""Pure data: nodes, the cluster, and the enums that key everything else."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import StrEnum

from .constants import (
    DEFAULT_INSTALL_DISK,
    VLAN_ID_MAX,
    VLAN_ID_MIN,
    VSWITCH_CP_HOST_START,
    VSWITCH_MTU_DEFAULT,
    VSWITCH_WORKER_HOST_START,
)


class NodeRole(StrEnum):
    CONTROLPLANE = "controlplane"
    WORKER = "worker"


class CpuVendor(StrEnum):
    AMD = "amd"
    INTEL = "intel"


class ProfileKey(StrEnum):
    GENERIC = "generic"
    DB = "db"
    STORAGE = "storage"


class Stage(StrEnum):
    CONFIG = "config"
    IMAGE = "image"
    RESCUE = "rescue"
    APPLY = "apply"
    BOOTSTRAP = "bootstrap"
    CILIUM = "cilium"
    REMOVE = "remove"


_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
# Talos requires node names to be valid hostnames; the node- artifact prefix relies on it
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# normalized form only; discovery lowercases before storing
_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

# field-based install.diskSelector type values verified against Talos release-1.13
SELECTOR_TYPES = frozenset({"ssd", "hdd", "nvme", "sd"})


def is_ipv4(value: str) -> bool:
    if not _IPV4_RE.match(value):
        return False
    return all(0 <= int(octet) <= 255 for octet in value.split("."))


def is_dns_label(value: str) -> bool:
    return bool(_DNS_LABEL_RE.match(value))


def is_mac(value: str) -> bool:
    return bool(_MAC_RE.match(value))


def is_host_in_subnet(ip: str, subnet: str) -> bool:
    """True iff ip is a usable host address inside subnet (not the network/broadcast)."""
    try:
        net = ipaddress.IPv4Network(subnet, strict=False)
        addr = ipaddress.IPv4Address(ip)
    except (ipaddress.AddressValueError, ValueError):
        return False
    return addr in net and addr not in (net.network_address, net.broadcast_address)


@dataclass(slots=True)
class InstallTarget:
    """Where Talos installs the OS: an explicit /dev path or a field-based diskSelector.

    diskSelector is preferred - NVMe enumeration order is unstable. Only the OS disk
    is ever named; data/OSD disks stay unreferenced so TopoLVM/Rook claim them clean.
    """

    disk: str | None = None
    selector: dict[str, str] | None = None

    def install_block(self) -> dict:
        if self.disk:
            return {"disk": self.disk}
        if self.selector:
            return {"diskSelector": dict(self.selector)}
        raise ValueError("install target has neither disk nor selector")

    def describe(self) -> str:
        if self.disk:
            return self.disk
        if self.selector:
            return "selector[" + ", ".join(f"{k}={v}" for k, v in self.selector.items()) + "]"
        return "unset"

    def problems(self, name: str) -> list[str]:
        if bool(self.disk) == bool(self.selector):
            return [f"{name}: install needs exactly one of disk or diskSelector"]
        if self.disk and not self.disk.startswith("/dev/"):
            return [f"{name}: install disk must be a /dev path"]
        if self.selector:
            dtype = self.selector.get("type")
            if dtype is not None and dtype not in SELECTOR_TYPES:
                joined = ", ".join(sorted(SELECTOR_TYPES))
                return [f"{name}: diskSelector type must be one of {joined}"]
        return []


@dataclass(slots=True)
class Vswitch:
    """A Hetzner vSwitch (private VLAN): cluster traffic rides this private L2 net,
    keeping the public Robot firewall scoped to admin IPs and constant in node count."""

    vlan_id: int
    subnet: str  # CIDR, e.g. "10.10.0.0/24"
    mtu: int = VSWITCH_MTU_DEFAULT

    @property
    def subnet_network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.subnet, strict=False)

    @property
    def subnet_prefixlen(self) -> int:
        return self.subnet_network.prefixlen

    def problems(self) -> list[str]:
        out: list[str] = []
        if not VLAN_ID_MIN <= self.vlan_id <= VLAN_ID_MAX:
            out.append(f"vswitch: VLAN ID must be in [{VLAN_ID_MIN}, {VLAN_ID_MAX}]")
        try:
            ipaddress.IPv4Network(self.subnet, strict=False)
        except (ipaddress.AddressValueError, ValueError):
            out.append(f"vswitch: subnet {self.subnet!r} is not a valid IPv4 network")
        if self.mtu > VSWITCH_MTU_DEFAULT:
            out.append(f"vswitch: MTU must be ≤ {VSWITCH_MTU_DEFAULT} (Hetzner cap)")
        return out


def _default_install() -> InstallTarget:
    return InstallTarget(disk=DEFAULT_INSTALL_DISK)


@dataclass(slots=True)
class Node:
    name: str
    role: NodeRole
    cpu: CpuVendor
    profile: ProfileKey = ProfileKey.GENERIC
    ip: str = ""
    gateway: str = ""
    vlan_ip: str = ""  # private vSwitch address; unused (and ignored) when no vswitch
    install: InstallTarget = field(default_factory=_default_install)
    # uplink NIC MAC, auto-discovered (rescue ssh / maintenance API), never operator-typed;
    # pins the link alias on multi-NIC boxes where structural match would be ambiguous
    link_mac: str = ""
    nic_firmware_ext: str | None = None
    extra_patches: list[str] = field(default_factory=list)  # operator --config-patch files
    # transient: install pinned to the live boot disk; rendered into config, never
    # serialised to tally.yaml (declarative def stays portable). see disk.resolve_system_selector
    resolved_install: InstallTarget | None = None

    def problems(self) -> list[str]:
        """Field-level issues that would make config/image generation invalid."""
        out: list[str] = []
        if not is_ipv4(self.ip):
            out.append(f"{self.name}: IPv4 address missing or malformed")
        if not is_ipv4(self.gateway):
            out.append(f"{self.name}: gateway missing or malformed")
        if self.link_mac and not is_mac(self.link_mac):
            out.append(f"{self.name}: link_mac must be a lowercase colon-separated MAC")
        out += self.install.problems(self.name)
        return out


@dataclass(slots=True)
class Cluster:
    nodes: list[Node]
    name: str = ""
    vswitch: Vswitch | None = None

    @property
    def control_planes(self) -> list[Node]:
        return [n for n in self.nodes if n.role is NodeRole.CONTROLPLANE]

    @property
    def bootstrap_cp(self) -> Node:
        """First control-plane: endpoint owner + bootstrap target. The floor is ≥1 CP."""
        cps = self.control_planes
        if not cps:
            raise ValueError("cluster has no control-plane")
        return cps[0]

    @property
    def workers(self) -> list[Node]:
        return [n for n in self.nodes if n.role is NodeRole.WORKER]

    def problems(self) -> list[str]:
        """Topology issues that block bring-up. Never raises; multiple CPs/workers fine."""
        out: list[str] = []
        if not self.name:
            out.append("Set a cluster name")
        if not self.nodes:
            out.append("Add at least one node")
        elif not self.control_planes:
            out.append("Add at least one control-plane")
        return out + self._vswitch_problems()

    def _vswitch_problems(self) -> list[str]:
        if self.vswitch is None:  # no vswitch ⇒ vlan_ip is ignored, no checks
            return []
        out = list(self.vswitch.problems())
        seen: dict[str, str] = {}
        for n in self.nodes:
            if not is_host_in_subnet(n.vlan_ip, self.vswitch.subnet):
                shown = n.vlan_ip or "?"
                out.append(f"{n.name}: vlan_ip {shown} not a host in {self.vswitch.subnet}")
                continue
            if n.vlan_ip in seen:
                out.append(f"{n.name}: vlan_ip {n.vlan_ip} already used by {seen[n.vlan_ip]}")
            seen[n.vlan_ip] = n.name
        return out


def default_cluster() -> Cluster:
    """The runbook topology: one control-plane + one worker, network unset.

    Network fields are left blank deliberately - the operator fills IP/gateway
    from the Hetzner Robot panel before config generation rather than carrying
    misleading example values.
    """
    return Cluster(
        nodes=[
            Node(name="cp1", role=NodeRole.CONTROLPLANE, cpu=CpuVendor.AMD),
            Node(name="worker1", role=NodeRole.WORKER, cpu=CpuVendor.AMD),
        ]
    )


def next_vlan_ip(cluster: Cluster, role: NodeRole) -> str | None:
    """Lowest free host in the vswitch subnet for the role: CPs from .1, workers from
    .100, so the role reads off the address. None if that role's range is exhausted."""
    vswitch = cluster.vswitch
    net = vswitch.subnet_network
    base = int(net.network_address)
    start = VSWITCH_CP_HOST_START if role is NodeRole.CONTROLPLANE else VSWITCH_WORKER_HOST_START
    used = {n.vlan_ip for n in cluster.nodes if n.vlan_ip}
    for host in range(start, net.num_addresses):
        candidate = str(ipaddress.IPv4Address(base + host))
        if is_host_in_subnet(candidate, vswitch.subnet) and candidate not in used:
            return candidate
    return None
