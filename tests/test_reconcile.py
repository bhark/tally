from __future__ import annotations

from tally.model import Cluster, CpuVendor, Node, NodeRole
from tally.observe import NodeState, Orphan, Snapshot
from tally.reconcile import ActionKind, compute, describe, locked_fields


def _node(name, role=NodeRole.WORKER, ip="10.0.0.1"):
    return Node(name=name, role=role, cpu=CpuVendor.AMD, ip=ip)


def _snap(states, orphans=None, *, cilium=False):
    return Snapshot(
        states=states,
        orphans=orphans or [],
        k8s_reachable=True,
        cilium_installed=cilium,
    )


def _cluster():
    return Cluster(
        nodes=[
            _node("cp1", NodeRole.CONTROLPLANE),
            _node("worker1"),
        ],
        name="c",
    )


def test_fresh_all_absent():
    cluster = _cluster()
    snap = _snap({"cp1": NodeState.ABSENT, "worker1": NodeState.ABSENT})
    plan = compute(cluster, snap, cluster_up=False)

    assert plan.bootstrap is True
    assert plan.cilium is True
    assert [a.kind for a in plan.actions] == [ActionKind.BRING_UP, ActionKind.BRING_UP]
    assert plan.actions[0].node.name == "cp1"  # bootstrap_cp first


def test_joined_with_orphan_converges_then_removes():
    cluster = _cluster()
    orphan = Orphan(name="old", addresses=["1.2.3.4"], control_plane=False)
    snap = _snap(
        {"cp1": NodeState.JOINED, "worker1": NodeState.JOINED},
        orphans=[orphan],
        cilium=True,
    )
    plan = compute(cluster, snap, cluster_up=True)

    assert plan.bootstrap is False
    assert plan.cilium is False
    kinds = [a.kind for a in plan.actions]
    assert kinds == [ActionKind.CONVERGE, ActionKind.CONVERGE, ActionKind.REMOVE]
    assert plan.actions[-1].orphan is orphan  # removes trail


def test_maintenance_applies():
    cluster = _cluster()
    snap = _snap({"cp1": NodeState.MAINTENANCE, "worker1": NodeState.ABSENT})
    plan = compute(cluster, snap, cluster_up=True)
    assert [a.kind for a in plan.actions] == [ActionKind.APPLY, ActionKind.BRING_UP]


def test_pre_bootstrap_joined_reapplies():
    cluster = _cluster()
    snap = _snap({"cp1": NodeState.JOINED, "worker1": NodeState.JOINED})
    plan = compute(cluster, snap, cluster_up=False)
    assert [a.kind for a in plan.actions] == [ActionKind.REAPPLY, ActionKind.REAPPLY]


def test_last_cp_orphan_refused():
    cluster = _cluster()  # cp1 absent -> no joined desired CP
    cp_orphan = Orphan(name="old-cp", addresses=["1.2.3.4"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.ABSENT, "worker1": NodeState.ABSENT},
        orphans=[cp_orphan],
    )
    plan = compute(cluster, snap, cluster_up=True)

    remove = [a for a in plan.actions if a.kind is ActionKind.REMOVE]
    assert len(remove) == 1  # action still present
    assert remove[0].hint == "refusing: would leave zero control-planes"


def test_cp_orphan_quorum_warning_three_to_two():
    # 2 joined desired CPs + 1 CP orphan = 3 live; removing the orphan drops to 2 (even) -> warn
    cluster = Cluster(
        nodes=[
            _node("cp1", NodeRole.CONTROLPLANE),
            _node("cp2", NodeRole.CONTROLPLANE),
            _node("worker1"),
        ],
        name="c",
    )
    cp_orphan = Orphan(name="old-cp", addresses=["1.2.3.4"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.JOINED, "cp2": NodeState.JOINED, "worker1": NodeState.JOINED},
        orphans=[cp_orphan],
        cilium=True,
    )
    plan = compute(cluster, snap, cluster_up=True)

    remove = [a for a in plan.actions if a.kind is ActionKind.REMOVE][0]
    assert remove.hint == "warning: control-plane quorum will drop to 2"


def test_cp_orphan_two_to_one_no_warning():
    # 1 joined desired CP + 1 CP orphan = 2 live; removal leaves 1 (odd) -> no hint
    cluster = _cluster()
    cp_orphan = Orphan(name="old-cp", addresses=["1.2.3.4"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.JOINED, "worker1": NodeState.JOINED},
        orphans=[cp_orphan],
        cilium=True,
    )
    plan = compute(cluster, snap, cluster_up=True)

    remove = [a for a in plan.actions if a.kind is ActionKind.REMOVE][0]
    assert remove.hint is None


def test_ordering_bootstrap_cp_first_removes_last():
    cluster = Cluster(
        nodes=[
            _node("worker1"),  # definition order puts a worker first
            _node("cp1", NodeRole.CONTROLPLANE),
            _node("worker2"),
        ],
        name="c",
    )
    orphan = Orphan(name="old", addresses=["1.2.3.4"], control_plane=False)
    snap = _snap(
        {n.name: NodeState.JOINED for n in cluster.nodes},
        orphans=[orphan],
        cilium=True,
    )
    plan = compute(cluster, snap, cluster_up=True)

    node_names = [a.node.name for a in plan.actions if a.node is not None]
    assert node_names == ["cp1", "worker1", "worker2"]  # bootstrap_cp hoisted, no dup
    assert plan.actions[-1].orphan is orphan


def test_describe_lines():
    cluster = _cluster()
    orphan = Orphan(name="old", addresses=["1.2.3.4"], control_plane=False)
    snap = _snap(
        {"cp1": NodeState.JOINED, "worker1": NodeState.MAINTENANCE},
        orphans=[orphan],
        cilium=True,
    )
    lines = describe(compute(cluster, snap, cluster_up=True))
    assert "cp1  joined -> converge" in lines
    assert "worker1  maintenance -> apply" in lines
    assert "orphan old (1.2.3.4) -> remove" in lines


def test_locked_fields():
    node = _node("cp1", NodeRole.CONTROLPLANE)
    live = _snap({"cp1": NodeState.JOINED})
    locked = locked_fields(node, live)
    assert set(locked) == {"name", "role", "cpu", "ip", "gateway", "vlan_ip"}
    assert "profile" not in locked  # workload-shape fields stay editable

    absent = _snap({"cp1": NodeState.ABSENT})
    assert locked_fields(node, absent) == {}
