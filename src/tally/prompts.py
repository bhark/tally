"""Operator prompts and field validators over griphtui."""

from __future__ import annotations

import ipaddress

from griphtui import Option, confirm, is_cancel, note, select, text, warn

from .constants import DEFAULT_INSTALL_DISK, VLAN_ID_MAX, VLAN_ID_MIN
from .model import CpuVendor, InstallTarget, NodeRole, ProfileKey, is_dns_label, is_ipv4
from .profiles import profile_for


def ask(label: str, *, default: bool) -> bool:
    answer = confirm(label, default=default)
    return False if is_cancel(answer) else answer


def ask_text(label: str, *, default: str = "", validate=None) -> str | None:
    value = text(label, default=default, validate=validate)
    if is_cancel(value):
        return None
    return value.strip()


def ask_role(default: NodeRole = NodeRole.CONTROLPLANE) -> NodeRole | None:
    choice = select(
        "Node role",
        [
            Option(
                label="Control plane",
                value=NodeRole.CONTROLPLANE,
                selected=default is NodeRole.CONTROLPLANE,
            ),
            Option(label="Worker", value=NodeRole.WORKER, selected=default is NodeRole.WORKER),
        ],
    )
    return None if is_cancel(choice) else choice


def ask_cpu(default: CpuVendor) -> CpuVendor | None:
    choice = select(
        "CPU vendor",
        [
            Option(label="amd", value=CpuVendor.AMD, selected=default is CpuVendor.AMD),
            Option(label="intel", value=CpuVendor.INTEL, selected=default is CpuVendor.INTEL),
        ],
    )
    return None if is_cancel(choice) else choice


def ask_profile(default: ProfileKey) -> ProfileKey | None:
    options = [
        Option(
            label=p.value,
            value=p,
            hint=profile_for(p).install_hint or None,
            selected=(p is default),
        )
        for p in ProfileKey
    ]
    choice = select("Workload profile", options)
    return None if is_cancel(choice) else choice


def ask_install_target(default: InstallTarget, profile: ProfileKey) -> InstallTarget | None:
    hint = profile_for(profile).install_hint
    if hint:
        note(hint, title="Install-target guidance")
    mode = select(
        "Install target",
        [
            Option(
                label="Disk selector (preferred, stable across NVMe reordering)",
                value="selector",
                selected=default.selector is not None,
            ),
            Option(label="Explicit /dev path", value="disk", selected=default.disk is not None),
        ],
    )
    if is_cancel(mode):
        return None
    if mode == "disk":
        disk = ask_text(
            "Install disk",
            default=default.disk or DEFAULT_INSTALL_DISK,
            validate=dev_path_validator,
        )
        return None if disk is None else InstallTarget(disk=disk)
    return ask_selector(default.selector or {})


def ask_selector(default: dict[str, str]) -> InstallTarget | None:
    dtype = select(
        "Disk type",
        [Option(label="any", value="", selected="type" not in default)]
        + [
            Option(label=t, value=t, selected=default.get("type") == t)
            for t in ("nvme", "ssd", "hdd", "sd")
        ],
    )
    if is_cancel(dtype):
        return None
    size = ask_text(
        "Size expression (e.g. '<= 4TB', blank to skip)", default=default.get("size", "")
    )
    if size is None:
        return None
    model = ask_text("Model substring (blank to skip)", default=default.get("model", ""))
    if model is None:
        return None
    selector = {k: v for k, v in (("type", dtype), ("size", size), ("model", model)) if v}
    if not selector:
        warn("A diskSelector needs at least one field; nothing changed")
        return None
    return InstallTarget(selector=selector)


def ask_extra_patches(default: list[str]) -> list[str] | None:
    """Opt-in bring-your-own --config-patch files. Cancel ⇒ None (abort add), no ⇒ []."""
    answer = confirm("Add bring-your-own config-patch files?", default=bool(default))
    if is_cancel(answer):
        return None
    if not answer:
        return []
    raw = ask_text(
        "Patch file path(s), comma-separated (relative to where you run tally)",
        default=", ".join(default),
    )
    if raw is None:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def ipv4_validator(v: str) -> str | None:
    return None if is_ipv4(v.strip()) else "Expected a dotted IPv4 address"


def dev_path_validator(v: str) -> str | None:
    return None if v.strip().startswith("/dev/") else "Expected a /dev path"


def vlan_id_validator(v: str) -> str | None:
    v = v.strip()
    if not v.isdigit():
        return "Expected a numeric VLAN ID"
    if not VLAN_ID_MIN <= int(v) <= VLAN_ID_MAX:
        return f"VLAN ID must be in [{VLAN_ID_MIN}, {VLAN_ID_MAX}]"
    return None


def cidr_validator(v: str) -> str | None:
    try:
        ipaddress.IPv4Network(v.strip(), strict=False)
    except (ipaddress.AddressValueError, ValueError):
        return "Expected an IPv4 CIDR, e.g. 10.10.0.0/24"
    return None


def name_validator(cluster, except_node=None):
    def validate(v: str) -> str | None:
        v = v.strip()
        if not v:
            return "Name is required"
        if not is_dns_label(v):
            return "Must be a DNS-1123 label (lowercase alphanumeric + -, ≤63 chars)"
        if any(n.name == v for n in cluster.nodes if n is not except_node):
            return f"Name {v!r} already in use"
        return None

    return validate


def cluster_name_validator(v: str) -> str | None:
    v = v.strip()
    if not v:
        return "Name is required"
    if not is_dns_label(v):
        return "Must be a DNS-1123 label (lowercase alphanumeric + -, ≤63 chars)"
    return None
