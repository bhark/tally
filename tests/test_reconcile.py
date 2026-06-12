from __future__ import annotations

from tally.model import Cluster, CpuVendor, Node, NodeRole
from tally.observe import NodeState, Orphan, Snapshot
from tally.reconcile import ActionKind, compute, describe, locked_fields


def _node(name, role=NodeRole.WORKER, ip="10.0.0.1"):
    return Node(name=name, role=role, cpu=CpuVendor.AMD, ip=ip)


def _snap(states, orphans=None, *, cilium=False, etcd=False):
    return Snapshot(
        states=states,
        orphans=orphans or [],
        k8s_reachable=True,
        cilium_installed=cilium,
        etcd_bootstrapped=etcd,
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
    plan = compute(cluster, snap)

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
        etcd=True,
    )
    plan = compute(cluster, snap)

    assert plan.bootstrap is False
    assert plan.cilium is False
    kinds = [a.kind for a in plan.actions]
    assert kinds == [ActionKind.CONVERGE, ActionKind.CONVERGE, ActionKind.REMOVE]
    assert plan.actions[-1].orphan is orphan  # removes trail


def test_maintenance_applies():
    cluster = _cluster()
    snap = _snap({"cp1": NodeState.MAINTENANCE, "worker1": NodeState.ABSENT}, etcd=True)
    plan = compute(cluster, snap)
    assert [a.kind for a in plan.actions] == [ActionKind.APPLY, ActionKind.BRING_UP]


def test_pre_bootstrap_joined_reapplies():
    cluster = _cluster()
    snap = _snap({"cp1": NodeState.JOINED, "worker1": NodeState.JOINED})  # etcd not yet up
    plan = compute(cluster, snap)
    assert [a.kind for a in plan.actions] == [ActionKind.REAPPLY, ActionKind.REAPPLY]


def test_unsurveyed_node_treated_absent():
    """A node added/renamed after the survey is missing from snap.states -> absent -> bring-up,
    never a KeyError (the crash the menu edit flow could trigger)."""
    cluster = _cluster()
    snap = _snap({"cp1": NodeState.JOINED}, etcd=True)  # worker1 absent from the snapshot
    plan = compute(cluster, snap)
    by_name = {a.node.name: a for a in plan.actions if a.node is not None}
    assert by_name["worker1"].kind is ActionKind.BRING_UP


def test_last_cp_orphan_refused():
    cluster = _cluster()  # cp1 absent -> no joined desired CP
    cp_orphan = Orphan(name="old-cp", addresses=["1.2.3.4"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.ABSENT, "worker1": NodeState.ABSENT},
        orphans=[cp_orphan],
        etcd=True,
    )
    plan = compute(cluster, snap)

    remove = [a for a in plan.actions if a.kind is ActionKind.REMOVE]
    assert len(remove) == 1  # action still present
    assert remove[0].refuse is True
    assert remove[0].hint == "would leave zero control-planes"


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
        etcd=True,
    )
    plan = compute(cluster, snap)

    remove = [a for a in plan.actions if a.kind is ActionKind.REMOVE][0]
    assert remove.refuse is False
    assert remove.hint == "warning: control-plane quorum will drop to 2"


def test_cp_orphan_two_to_one_no_warning():
    # 1 joined desired CP + 1 CP orphan = 2 live; removal leaves 1 (odd) -> no hint
    cluster = _cluster()
    cp_orphan = Orphan(name="old-cp", addresses=["1.2.3.4"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.JOINED, "worker1": NodeState.JOINED},
        orphans=[cp_orphan],
        cilium=True,
        etcd=True,
    )
    plan = compute(cluster, snap)

    remove = [a for a in plan.actions if a.kind is ActionKind.REMOVE][0]
    assert remove.hint is None


def test_cp_orphan_quorum_decrements_across_orphans():
    # 1 joined CP + 2 CP orphans = 3 live; live_cp must decrement between orphans:
    # first removal 3->2 (even) warns, second 2->1 (odd) is clean
    cluster = _cluster()
    o1 = Orphan(name="old-cp1", addresses=["1.2.3.4"], control_plane=True)
    o2 = Orphan(name="old-cp2", addresses=["1.2.3.5"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.JOINED, "worker1": NodeState.JOINED},
        orphans=[o1, o2],
        cilium=True,
        etcd=True,
    )
    remove = [a for a in compute(cluster, snap).actions if a.kind is ActionKind.REMOVE]
    assert [a.refuse for a in remove] == [False, False]
    assert remove[0].hint == "warning: control-plane quorum will drop to 2"
    assert remove[1].hint is None


def test_cp_orphans_refuse_when_quorum_exhausted():
    # 0 joined CP + 2 CP orphans = 2 live; first removal leaves 1 (allowed), the second
    # would strand the control-plane and is refused (a refused orphan stays counted)
    cluster = _cluster()
    o1 = Orphan(name="old-cp1", addresses=["1.2.3.4"], control_plane=True)
    o2 = Orphan(name="old-cp2", addresses=["1.2.3.5"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.ABSENT, "worker1": NodeState.ABSENT},
        orphans=[o1, o2],
        etcd=True,
    )
    remove = [a for a in compute(cluster, snap).actions if a.kind is ActionKind.REMOVE]
    assert [a.refuse for a in remove] == [False, True]
    assert remove[1].hint == "would leave zero control-planes"


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
        etcd=True,
    )
    plan = compute(cluster, snap)

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
        etcd=True,
    )
    lines = describe(compute(cluster, snap))
    assert "cp1  joined -> converge" in lines
    assert "worker1  maintenance -> apply" in lines
    assert "orphan old (1.2.3.4) -> remove" in lines


def test_describe_refusal_prefixed():
    cluster = _cluster()
    cp_orphan = Orphan(name="old-cp", addresses=["9.9.9.9"], control_plane=True)
    snap = _snap(
        {"cp1": NodeState.ABSENT, "worker1": NodeState.ABSENT},
        orphans=[cp_orphan],
        etcd=True,
    )
    lines = describe(compute(cluster, snap))
    assert "  [refusing: would leave zero control-planes]" in lines


def test_locked_fields():
    node = _node("cp1", NodeRole.CONTROLPLANE)
    live = _snap({"cp1": NodeState.JOINED})
    locked = locked_fields(node, live)
    assert set(locked) == {"name", "role", "cpu", "ip", "gateway", "vlan_ip"}
    assert "profile" not in locked  # workload-shape fields stay editable

    absent = _snap({"cp1": NodeState.ABSENT})
    assert locked_fields(node, absent) == {}
