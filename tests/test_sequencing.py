from __future__ import annotations

import pytest

from tally import wizard
from tally.model import Cluster, CpuVendor, Node, NodeRole, Vswitch, default_cluster
from tally.paths import Paths
from tally.stages import Ctx, StageCancelled


def _ctx(tmp_path):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    return Ctx(cluster=default_cluster(), paths=paths)


def test_vlan_ip_auto_assigns_by_role_range():
    """CPs fill from .1, workers from .100; each role's range advances independently."""
    cluster = Cluster(nodes=[], vswitch=Vswitch(vlan_id=4001, subnet="10.10.0.0/24"))

    def add(role):
        ip = wizard._next_vlan_ip(cluster, role)
        cluster.nodes.append(
            Node(name=f"n{len(cluster.nodes)}", role=role, cpu=CpuVendor.AMD, vlan_ip=ip)
        )
        return ip

    assert add(NodeRole.CONTROLPLANE) == "10.10.0.1"
    assert add(NodeRole.WORKER) == "10.10.0.100"
    assert add(NodeRole.CONTROLPLANE) == "10.10.0.2"  # CP range continues past the worker
    assert add(NodeRole.WORKER) == "10.10.0.101"


def test_vlan_ip_returns_none_when_role_range_exhausted():
    # /26 has no .100, so the worker range can't be satisfied
    cluster = Cluster(nodes=[], vswitch=Vswitch(vlan_id=4002, subnet="10.20.0.0/26"))
    assert wizard._next_vlan_ip(cluster, NodeRole.CONTROLPLANE) == "10.20.0.1"
    assert wizard._next_vlan_ip(cluster, NodeRole.WORKER) is None


def _record(monkeypatch):
    seq: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        wizard, "_run_phase", lambda ctx, sd, node: seq.append((sd.key.value, node and node.name))
    )
    return seq


def _absent(monkeypatch):
    """Default discovery: nothing live - every node walks image/rescue/apply."""
    monkeypatch.setattr(wizard, "_configured", lambda ctx, node: False)
    monkeypatch.setattr(wizard, "_in_maintenance", lambda ctx, node: False)


def test_fresh_cluster_full_walk_then_bootstrap_and_cilium(tmp_path, monkeypatch):
    seq = _record(monkeypatch)
    _absent(monkeypatch)
    monkeypatch.setattr(wizard, "_cilium_installed", lambda ctx: False)
    monkeypatch.setattr(wizard, "_ask", lambda label, *, default: default)
    wizard._bring_up(_ctx(tmp_path))  # no kubeconfig → fresh
    assert seq == [
        ("config", None),
        ("rescue", "cp1"),  # image builds inside rescue, post uplink pin
        ("apply", "cp1"),
        ("rescue", "worker1"),
        ("apply", "worker1"),
        ("bootstrap", None),
        ("cilium", None),
    ]


def test_existing_cluster_skips_joined_nodes_and_bootstrap(tmp_path, monkeypatch):
    """Regression gate: a joined node must never re-image/apply; bootstrap+cilium skip."""
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard, "_configured", lambda ctx, node: node.name == "cp1")  # cp1 live
    monkeypatch.setattr(wizard, "_in_maintenance", lambda ctx, node: False)  # worker1 absent
    monkeypatch.setattr(wizard, "_ask", lambda label, *, default: True)
    monkeypatch.setattr(wizard, "_cilium_installed", lambda ctx: True)  # healthy cluster → CNI up
    ctx = _ctx(tmp_path)
    ctx.paths.kubeconfig.write_text("kube\n")  # already bootstrapped

    wizard._bring_up(ctx)

    assert ("apply", "cp1") not in seq and ("rescue", "cp1") not in seq  # joined → untouched
    keys = [k for k, _ in seq]
    assert "bootstrap" not in keys and "cilium" not in keys
    assert seq == [
        ("config", None),
        ("rescue", "worker1"),
        ("apply", "worker1"),
    ]


def test_bootstrapped_but_cilium_absent_reinstalls(tmp_path, monkeypatch):
    """Decoupled gate: a run that bootstrapped then died at cilium must reinstall on rerun.

    kubeconfig present (etcd up) so bootstrap is skipped, but CNI absent → cilium runs.
    """
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard, "_configured", lambda ctx, node: True)  # both joined
    monkeypatch.setattr(wizard, "_in_maintenance", lambda ctx, node: False)
    monkeypatch.setattr(wizard, "_ask", lambda label, *, default: True)
    monkeypatch.setattr(wizard, "_cilium_installed", lambda ctx: False)  # install failed last run
    ctx = _ctx(tmp_path)
    ctx.paths.kubeconfig.write_text("kube\n")

    wizard._bring_up(ctx)

    keys = [k for k, _ in seq]
    assert "bootstrap" not in keys  # etcd already up
    assert keys[-1] == "cilium"  # reinstalls despite cluster being up


def test_maintenance_node_applies_only(tmp_path, monkeypatch):
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard, "_configured", lambda ctx, node: False)
    monkeypatch.setattr(wizard, "_in_maintenance", lambda ctx, node: True)  # imaged, not applied
    monkeypatch.setattr(wizard, "_cilium_installed", lambda ctx: False)
    monkeypatch.setattr(wizard, "_ask", lambda label, *, default: default)
    wizard._bring_up(_ctx(tmp_path))
    assert seq == [
        ("config", None),
        ("apply", "cp1"),
        ("apply", "worker1"),
        ("bootstrap", None),
        ("cilium", None),
    ]


def test_configured_pre_bootstrap_reapplies(tmp_path, monkeypatch):
    """Configured node with no kubeconfig (bootstrap not yet run) re-applies, not re-images.

    Regression gate for the time-sync bailout state: config applied but etcd never
    bootstrapped. Must converge via apply (so config fixes land), then bootstrap.
    """
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard, "_in_maintenance", lambda ctx, node: False)
    monkeypatch.setattr(wizard, "_configured", lambda ctx, node: True)  # both already have config
    monkeypatch.setattr(wizard, "_cilium_installed", lambda ctx: False)
    monkeypatch.setattr(wizard, "_ask", lambda label, *, default: default)
    wizard._bring_up(_ctx(tmp_path))  # no kubeconfig → pre-bootstrap
    assert seq == [
        ("config", None),
        ("apply", "cp1"),
        ("apply", "worker1"),
        ("bootstrap", None),
        ("cilium", None),
    ]


def test_declined_bootstrap_skips_but_continues_to_cilium(tmp_path, monkeypatch):
    seq = _record(monkeypatch)
    _absent(monkeypatch)
    monkeypatch.setattr(wizard, "_ask", lambda label, *, default: False)
    ctx = _ctx(tmp_path)
    ctx.cluster.nodes = ctx.cluster.nodes[:1]
    wizard._bring_up(ctx)
    keys = [k for k, _ in seq]
    assert "bootstrap" not in keys
    assert keys[-1] == "cilium"


def test_cancel_aborts_remaining_phases(tmp_path, monkeypatch):
    seq: list[str] = []

    def fake(ctx, sd, node):
        seq.append(sd.key.value)
        if sd.key.value == "apply":
            raise StageCancelled("backed out")

    monkeypatch.setattr(wizard, "_run_phase", fake)
    _absent(monkeypatch)
    monkeypatch.setattr(wizard, "_ask", lambda label, *, default: False)
    ctx = _ctx(tmp_path)
    ctx.cluster.nodes = ctx.cluster.nodes[:1]

    with pytest.raises(StageCancelled):
        wizard._bring_up(ctx)
    assert "bootstrap" not in seq and "cilium" not in seq
