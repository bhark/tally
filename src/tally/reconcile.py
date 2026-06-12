"""Pure diff: desired topology + observed snapshot -> an ordered plan of actions.

Zero I/O. Desired nodes lead (bootstrap_cp first so the endpoint owner converges
before its peers); orphan removals trail (new CP members join etcd before old ones
leave). A CP-quorum guard refuses or warns on removals that would strand the
control-plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .model import Cluster, Node, NodeRole
from .observe import NodeState, Orphan, Snapshot


class ActionKind(StrEnum):
    BRING_UP = "bring-up"
    APPLY = "apply"
    CONVERGE = "converge"
    REAPPLY = "reapply"
    REMOVE = "remove"


@dataclass(slots=True)
class Action:
    kind: ActionKind
    node: Node | None = None
    orphan: Orphan | None = None
    hint: str | None = None
    state: NodeState | None = None  # observed state the action derives from (node actions)
    refuse: bool = False  # plan-level veto: surfaced but never executed


@dataclass(slots=True)
class Plan:
    actions: list[Action]
    bootstrap: bool
    cilium: bool


_LOCKED_FIELDS = ("name", "role", "cpu", "ip", "gateway", "vlan_ip")


def _ordered_nodes(cluster: Cluster) -> list[Node]:
    head = cluster.bootstrap_cp
    return [head, *(n for n in cluster.nodes if n is not head)]


def _node_action(node: Node, state: NodeState, *, etcd_bootstrapped: bool) -> Action:
    if state is NodeState.ABSENT:
        return Action(ActionKind.BRING_UP, node=node, state=state)
    if state is NodeState.MAINTENANCE:
        return Action(ActionKind.APPLY, node=node, state=state)
    kind = ActionKind.CONVERGE if etcd_bootstrapped else ActionKind.REAPPLY
    return Action(kind, node=node, state=state)


def _remove_actions(cluster: Cluster, snap: Snapshot) -> list[Action]:
    joined_cp = sum(
        1
        for n in cluster.nodes
        if n.role is NodeRole.CONTROLPLANE and snap.states.get(n.name) is NodeState.JOINED
    )
    cp_orphans = [o for o in snap.orphans if o.control_plane]
    live_cp = joined_cp + len(cp_orphans)

    out: list[Action] = []
    for orphan in snap.orphans:
        if not orphan.control_plane:
            out.append(Action(ActionKind.REMOVE, orphan=orphan))
            continue
        remaining = live_cp - 1
        if remaining <= 0:
            out.append(
                Action(
                    ActionKind.REMOVE,
                    orphan=orphan,
                    refuse=True,
                    hint="would leave zero control-planes",
                )
            )
            continue  # not removed; later orphans still see it as present
        # even remaining loses quorum-tolerance vs the next-lower odd; also covers 3->2
        hint = None
        if remaining % 2 == 0:
            hint = f"warning: control-plane quorum will drop to {remaining}"
        out.append(Action(ActionKind.REMOVE, orphan=orphan, hint=hint))
        live_cp = remaining
    return out


def compute(cluster: Cluster, snap: Snapshot) -> Plan:
    actions = [
        _node_action(
            node,
            snap.states.get(node.name, NodeState.ABSENT),  # unsurveyed edit ⇒ absent ⇒ bring-up
            etcd_bootstrapped=snap.etcd_bootstrapped,
        )
        for node in _ordered_nodes(cluster)
    ]
    actions += _remove_actions(cluster, snap)
    return Plan(
        actions=actions,
        bootstrap=not snap.etcd_bootstrapped,
        cilium=not snap.cilium_installed,
    )


def describe(plan: Plan) -> list[str]:
    lines: list[str] = []
    flags = [f for f, on in (("bootstrap", plan.bootstrap), ("cilium", plan.cilium)) if on]
    if flags:
        lines.append("cluster: " + " + ".join(flags))
    for a in plan.actions:
        if a.node is not None:
            lines.append(f"{a.node.name}  {a.state} -> {a.kind}")
        elif a.orphan is not None:
            addr = a.orphan.addresses[0] if a.orphan.addresses else "?"
            lines.append(f"orphan {a.orphan.name} ({addr}) -> {a.kind}")
        if a.refuse:
            lines.append(f"  [refusing: {a.hint}]")
        elif a.hint:
            lines.append(f"  [{a.hint}]")
    return lines


def locked_fields(node: Node, snap: Snapshot) -> dict[str, str]:
    """Live nodes lock identity/network fields; profile/install/firmware/patches stay editable."""
    if snap.states.get(node.name) is NodeState.ABSENT:
        return {}
    return {f: "locked: node is live" for f in _LOCKED_FIELDS}
