"""Navigable menu screens over the menu framework: the day-2 reconciler UI.

Pure presentation glue. Each screen is built fresh from a ScreenState; headers
and item lists recompute every render so they always reflect current topology +
the last snapshot. The reconcile plan shown here is computed, never executed -
selecting "Tally Up!" exits the root menu with "up" for the caller to act on.
"""

from __future__ import annotations

from dataclasses import dataclass

from griphtui import confirm, error, is_cancel, note, success, warn

from . import definition, observe, prompts
from .constants import DEFAULT_INSTALL_DISK, VSWITCH_MTU_DEFAULT, VSWITCH_SUBNET_DEFAULT
from .menu import BACK, STAY, Exit, Item, Menu, item, submenu
from .model import CpuVendor, InstallTarget, Node, NodeRole, ProfileKey, Vswitch, next_vlan_ip
from .observe import NodeState, Snapshot
from .reconcile import compute, describe, locked_fields
from .stages.base import Ctx
from .ui import inventory_lines, node_line


@dataclass(slots=True)
class ScreenState:
    ctx: Ctx
    snap: Snapshot


def _problems(cluster) -> list[str]:
    return cluster.problems() + [p for n in cluster.nodes for p in n.problems()]


def _save(state: ScreenState) -> None:
    definition.save(state.ctx.cluster, state.ctx.paths.defn)


# main ----------------------------------------------------------------------


def main_menu(state: ScreenState) -> Menu:
    def header() -> None:
        cluster = state.ctx.cluster
        problems = _problems(cluster)
        if cluster.name:
            note(cluster.name, title="Cluster")
        if cluster.nodes:
            note(inventory_lines(cluster, state.snap), title="Nodes")
        else:
            note("No nodes yet", title="Nodes")
        if problems:  # gates compute: bootstrap_cp would raise with zero control-planes
            note(problems, title="Resolve before bring-up")
        else:
            note(describe(compute(cluster, state.snap)), title="Cluster state")
        if state.snap.etcd_bootstrapped and not state.snap.k8s_reachable:
            note("k8s API unreachable - orphan detection skipped")

    def items() -> list[Item]:
        cluster = state.ctx.cluster
        problems = _problems(cluster)
        out: list[Item] = []
        if not cluster.name:
            out.append(item("Set cluster name", lambda: _set_name(state)))
        out.append(
            Item(
                label="Tally Up!",
                run=lambda: Exit("up"),
                hint=problems[0] if problems else None,
                disabled=bool(problems),
            )
        )
        out.append(Item("Edit nodes", submenu(nodes_menu(state))))
        out.append(Item("Edit vSwitch", submenu(vswitch_menu(state))))
        out.append(item("Refresh cluster state", lambda: _refresh(state)))
        out.append(Item("Quit", lambda: BACK))
        return out

    return Menu("Tally", items, header=header)


def _set_name(state: ScreenState) -> None:
    name = prompts.ask_text("Cluster name", validate=prompts.cluster_name_validator)
    if name is None:
        return
    state.ctx.cluster.name = name
    _save(state)
    success(f"Cluster name set to {name!r}")


def _refresh(state: ScreenState) -> None:
    state.snap = observe.snapshot(state.ctx.cluster, state.ctx.paths, state.ctx.talos_env())


# nodes ---------------------------------------------------------------------


def nodes_menu(state: ScreenState) -> Menu:
    def items() -> list[Item]:
        out = [
            Item(node.name, submenu(node_menu(state, node)), hint=node_line(node))
            for node in state.ctx.cluster.nodes
        ]
        out.append(item("Add node", lambda: _add_node(state)))
        out.append(Item("Back", lambda: BACK))
        return out

    return Menu("Edit nodes", items)


def node_menu(state: ScreenState, node: Node) -> Menu:
    def items() -> list[Item]:
        cluster = state.ctx.cluster
        locks = locked_fields(node, state.snap)

        def field_item(field: str, current: str, prompt) -> Item:
            def run():
                value = prompt()
                if value is None or value == getattr(node, field):
                    return STAY
                setattr(node, field, value)
                _save(state)
                success(f"{node.name}: {field} -> {value}")
                return STAY

            return Item(
                label=f"{field}: {current}",
                run=run,
                hint=locks.get(field),
                disabled=field in locks,
            )

        out = [
            field_item(
                "name",
                node.name,
                lambda: prompts.ask_text(
                    "Node name",
                    default=node.name,
                    validate=prompts.name_validator(cluster, except_node=node),
                ),
            ),
            field_item("role", node.role.value, lambda: prompts.ask_role(default=node.role)),
            field_item("cpu", node.cpu.value, lambda: prompts.ask_cpu(default=node.cpu)),
            field_item(
                "profile", node.profile.value, lambda: prompts.ask_profile(default=node.profile)
            ),
            field_item(
                "ip",
                node.ip or "?",
                lambda: prompts.ask_text(
                    "IPv4 address", default=node.ip, validate=prompts.ipv4_validator
                ),
            ),
            field_item(
                "gateway",
                node.gateway or "?",
                lambda: prompts.ask_text(
                    "Gateway", default=node.gateway, validate=prompts.ipv4_validator
                ),
            ),
            field_item(
                "install",
                node.install.describe(),
                lambda: prompts.ask_install_target(node.install, node.profile),
            ),
            _firmware_item(state, node),
            field_item(
                "extra_patches",
                f"{len(node.extra_patches)} file(s)",
                lambda: prompts.ask_extra_patches(node.extra_patches),
            ),
            Item(
                label=f"vlan_ip: {node.vlan_ip or '(none)'}",
                run=lambda: STAY,
                hint="auto-assigned",
                disabled=True,
            ),
            _delete_item(state, node),
            Item("Back", lambda: BACK),
        ]
        return out

    return Menu(f"Edit {node.name}", items)


# own item: empty input clears to None, which must stay distinct from cancel
def _firmware_item(state: ScreenState, node: Node) -> Item:
    def run():
        value = prompts.ask_text(
            "Extra NIC firmware extension ref (blank if none)",
            default=node.nic_firmware_ext or "",
        )
        if value is None:
            return STAY
        new = value or None
        if new != node.nic_firmware_ext:
            node.nic_firmware_ext = new
            _save(state)
            success(f"{node.name}: nic_firmware_ext -> {new or '(none)'}")
        return STAY

    return Item(f"nic_firmware_ext: {node.nic_firmware_ext or '(none)'}", run)


def _delete_item(state: ScreenState, node: Node) -> Item:
    live = state.snap.states.get(node.name) is not NodeState.ABSENT

    def run():
        cluster = state.ctx.cluster
        if node.role is NodeRole.CONTROLPLANE and len(cluster.control_planes) == 1:
            warn("cluster needs at least one control-plane; add another before removing this one")
            return STAY
        label = f"Delete {node.name}?"
        if live:
            label += " node is live; Tally Up will propose removing it from the cluster"
        answer = confirm(label, default=False)
        if is_cancel(answer) or not answer:
            return STAY
        cluster.nodes.remove(node)
        _save(state)
        success(f"Removed {node.name}")
        return BACK

    return Item("Delete node", run)


# vswitch -------------------------------------------------------------------


def vswitch_menu(state: ScreenState) -> Menu:
    def items() -> list[Item]:
        vs = state.ctx.cluster.vswitch
        if vs is None:
            return [
                item("Configure vSwitch", lambda: _configure_vswitch(state)),
                Item("Back", lambda: BACK),
            ]
        return [
            item(
                f"Reconfigure vSwitch (VLAN {vs.vlan_id}, {vs.subnet})",
                lambda: _configure_vswitch(state),
            ),
            Item("Disable vSwitch", _disable_vswitch(state)),
            Item("Back", lambda: BACK),
        ]

    return Menu("Edit vSwitch", items)


def _configure_vswitch(state: ScreenState) -> None:
    current = state.ctx.cluster.vswitch
    vlan = prompts.ask_text(
        "vSwitch VLAN ID",
        default=str(current.vlan_id) if current else "",
        validate=prompts.vlan_id_validator,
    )
    if vlan is None:
        return
    subnet = prompts.ask_text(
        "vSwitch subnet (CIDR, pick whatever you like)",
        default=current.subnet if current else VSWITCH_SUBNET_DEFAULT,
        validate=prompts.cidr_validator,
    )
    if subnet is None:
        return
    state.ctx.cluster.vswitch = Vswitch(vlan_id=int(vlan), subnet=subnet, mtu=VSWITCH_MTU_DEFAULT)
    _save(state)
    success(f"vSwitch configured: VLAN {vlan}, {subnet}")


def _disable_vswitch(state: ScreenState):
    def run():
        answer = confirm("Disable vSwitch?", default=False)
        if is_cancel(answer) or not answer:
            return STAY
        state.ctx.cluster.vswitch = None
        _save(state)
        success("vSwitch disabled")
        return STAY

    return run


# add node ------------------------------------------------------------------


def _add_node(state: ScreenState) -> None:
    """Full node prompt (role first), append on success. Any cancel aborts cleanly."""
    cluster = state.ctx.cluster
    role = prompts.ask_role()
    if role is None:
        return
    name = prompts.ask_text("Node name", validate=prompts.name_validator(cluster))
    if name is None:
        return
    cpu = prompts.ask_cpu(CpuVendor.AMD)
    if cpu is None:
        return
    profile = prompts.ask_profile(ProfileKey.GENERIC)
    if profile is None:
        return
    ip = prompts.ask_text("IPv4 address", validate=prompts.ipv4_validator)
    if ip is None:
        return
    gateway = prompts.ask_text("Gateway", validate=prompts.ipv4_validator)
    if gateway is None:
        return
    vlan_ip = ""
    if cluster.vswitch is not None:
        vlan_ip = next_vlan_ip(cluster, role)
        if vlan_ip is None:
            error(f"No free vSwitch IP for a {role.value} in {cluster.vswitch.subnet}")
            return
        note(f"vSwitch IP auto-assigned: {vlan_ip}")
    install = prompts.ask_install_target(InstallTarget(disk=DEFAULT_INSTALL_DISK), profile)
    if install is None:
        return
    fw = prompts.ask_text("Extra NIC firmware extension ref (blank if none)", default="")
    if fw is None:
        return
    extra = prompts.ask_extra_patches([])
    if extra is None:
        return
    cluster.nodes.append(
        Node(
            name=name,
            role=role,
            cpu=cpu,
            profile=profile,
            ip=ip,
            gateway=gateway,
            vlan_ip=vlan_ip,
            install=install,
            nic_firmware_ext=fw or None,
            extra_patches=extra,
        )
    )
    _save(state)
    success(f"Added {role.value} {name}")
