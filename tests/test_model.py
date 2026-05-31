from __future__ import annotations

import pytest

from tally.model import Cluster, CpuVendor, Node, NodeRole


def _node(name, role) -> Node:
    return Node(name=name, role=role, cpu=CpuVendor.AMD)


def _cp(name="cp1") -> Node:
    return _node(name, NodeRole.CONTROLPLANE)


def _worker(name="w1") -> Node:
    return _node(name, NodeRole.WORKER)


def _named(nodes) -> Cluster:
    return Cluster(nodes=nodes, name="hetzner")


def test_problems_empty_cluster():
    assert _named([]).problems() == ["Add at least one node"]


def test_problems_no_control_plane():
    assert _named([_worker()]).problems() == ["Add at least one control-plane"]


def test_problems_unset_name():
    assert Cluster(nodes=[_cp()]).problems() == ["Set a cluster name"]


def test_problems_valid_topologies():
    assert _named([_cp()]).problems() == []
    assert _named([_cp(), _worker()]).problems() == []


def test_problems_allows_multi_cp_and_multi_worker():
    cluster = _named([_cp("cp1"), _cp("cp2"), _cp("cp3"), _worker("w1"), _worker("w2")])
    assert cluster.problems() == []  # no count caps
    assert len(cluster.control_planes) == 3
    assert len(cluster.workers) == 2


def test_bootstrap_cp_is_first_control_plane():
    cp1, cp2 = _cp("cp1"), _cp("cp2")
    cluster = Cluster(nodes=[_worker("w1"), cp1, cp2])
    assert cluster.bootstrap_cp is cp1  # first CP in definition order


def test_bootstrap_cp_raises_without_control_plane():
    with pytest.raises(ValueError):
        _ = Cluster(nodes=[_worker()]).bootstrap_cp
