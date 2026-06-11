from __future__ import annotations

import pytest

from tally import wizard
from tally.menu import BACK, Exit
from tally.model import Cluster, CpuVendor, Node, NodeRole, Vswitch, default_cluster, next_vlan_ip
from tally.observe import NodeState, Orphan, Snapshot
from tally.paths import Paths
from tally.reconcile import Action, ActionKind
from tally.stages import Ctx, StageCancelled
from tally.stages.s4_apply import DryRunVerdict


def _ctx(tmp_path, cluster=None):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    return Ctx(cluster=cluster or default_cluster(), paths=paths)


def _snap(states, *, cilium=False, orphans=None, k8s=True) -> Snapshot:
    return Snapshot(
        states=states, orphans=orphans or [], k8s_reachable=k8s, cilium_installed=cilium
    )


def _record(monkeypatch):
    """Record stage phases; stub the live pin probe so sequencing stays hermetic."""
    seq: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        wizard, "_run_phase", lambda ctx, sd, node: seq.append((sd.key.value, node and node.name))
    )
    monkeypatch.setattr(wizard, "_pin_and_render", lambda ctx, node: None)
    return seq


# next_vlan_ip --------------------------------------------------------------


def test_vlan_ip_auto_assigns_by_role_range():
    """CPs fill from .1, workers from .100; each role's range advances independently."""
    cluster = Cluster(nodes=[], vswitch=Vswitch(vlan_id=4001, subnet="10.10.0.0/24"))

    def add(role):
        ip = next_vlan_ip(cluster, role)
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
    assert next_vlan_ip(cluster, NodeRole.CONTROLPLANE) == "10.20.0.1"
    assert next_vlan_ip(cluster, NodeRole.WORKER) is None


# plan-driven bring-up ------------------------------------------------------


def test_fresh_cluster_full_walk_then_bootstrap_and_cilium(tmp_path, monkeypatch):
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: default)
    snap = _snap({"cp1": NodeState.ABSENT, "worker1": NodeState.ABSENT})
    wizard._bring_up(_ctx(tmp_path), snap)  # no kubeconfig → fresh
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
    """Regression gate: a joined node converges (in-sync skip, no apply); bootstrap+cilium skip."""
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: True)
    ctx = _ctx(tmp_path)
    ctx.paths.kubeconfig.write_text("kube\n")  # already bootstrapped → cluster_up
    snap = _snap({"cp1": NodeState.JOINED, "worker1": NodeState.ABSENT}, cilium=True)

    wizard._bring_up(ctx, snap)

    assert ("apply", "cp1") not in seq and ("rescue", "cp1") not in seq  # joined → no re-image
    keys = [k for k, _ in seq]
    assert "bootstrap" not in keys and "cilium" not in keys
    assert seq == [
        ("config", None),
        ("rescue", "worker1"),
        ("apply", "worker1"),
    ]


def test_bootstrapped_but_cilium_absent_reinstalls(tmp_path, monkeypatch):
    """Decoupled gate: a run that bootstrapped then died at cilium must reinstall on rerun.

    kubeconfig present (etcd up) so bootstrap skips, but the snapshot says CNI absent → cilium runs.
    """
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: True)
    ctx = _ctx(tmp_path)
    ctx.paths.kubeconfig.write_text("kube\n")
    snap = _snap({"cp1": NodeState.JOINED, "worker1": NodeState.JOINED}, cilium=False)

    wizard._bring_up(ctx, snap)

    keys = [k for k, _ in seq]
    assert "bootstrap" not in keys  # etcd already up
    assert keys[-1] == "cilium"  # reinstalls despite cluster being up


def test_maintenance_node_applies_only(tmp_path, monkeypatch):
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: default)
    snap = _snap({"cp1": NodeState.MAINTENANCE, "worker1": NodeState.MAINTENANCE})
    wizard._bring_up(_ctx(tmp_path), snap)
    assert seq == [
        ("config", None),
        ("apply", "cp1"),
        ("apply", "worker1"),
        ("bootstrap", None),
        ("cilium", None),
    ]


def test_configured_pre_bootstrap_reapplies(tmp_path, monkeypatch):
    """Joined node with no kubeconfig (bootstrap not yet run) re-applies, not re-images.

    Regression gate for the time-sync bailout state: config applied but etcd never
    bootstrapped. Must converge via apply (so config fixes land), then bootstrap.
    """
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: default)
    snap = _snap({"cp1": NodeState.JOINED, "worker1": NodeState.JOINED})
    wizard._bring_up(_ctx(tmp_path), snap)  # no kubeconfig → pre-bootstrap
    assert seq == [
        ("config", None),
        ("apply", "cp1"),
        ("apply", "worker1"),
        ("bootstrap", None),
        ("cilium", None),
    ]


def test_declined_bootstrap_skips_but_continues_to_cilium(tmp_path, monkeypatch):
    seq = _record(monkeypatch)
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: False)
    ctx = _ctx(tmp_path)
    ctx.cluster.nodes = ctx.cluster.nodes[:1]
    snap = _snap({"cp1": NodeState.ABSENT})
    wizard._bring_up(ctx, snap)
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
    monkeypatch.setattr(wizard, "_pin_and_render", lambda ctx, node: None)
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: False)
    ctx = _ctx(tmp_path)
    ctx.cluster.nodes = ctx.cluster.nodes[:1]
    snap = _snap({"cp1": NodeState.ABSENT})

    with pytest.raises(StageCancelled):
        wizard._bring_up(ctx, snap)
    assert "bootstrap" not in seq and "cilium" not in seq


# converge dispatch ---------------------------------------------------------


def _converge_node(tmp_path, monkeypatch):
    """A joined node reachable enough for a dry-run: ip + talosconfig + talosctl on PATH."""
    ctx = _ctx(tmp_path)
    ctx.paths.talosconfig.write_text("cfg\n")
    node = ctx.cluster.bootstrap_cp
    node.ip = "1.2.3.4"
    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    phases: list[str] = []
    applied: list[str] = []
    monkeypatch.setattr(wizard, "_run_phase", lambda ctx, sd, node: phases.append(sd.key.value))
    monkeypatch.setattr(wizard, "_apply_and_verify", lambda ctx, node: applied.append(node.name))
    return ctx, node, phases, applied


def test_converge_in_sync_does_not_apply(tmp_path, monkeypatch):
    ctx, node, phases, applied = _converge_node(tmp_path, monkeypatch)
    monkeypatch.setattr(wizard, "dry_run", lambda ctx, node: (DryRunVerdict.IN_SYNC, ""))
    wizard._converge(ctx, node)
    assert phases == [] and applied == []


def test_converge_no_reboot_applies_without_wait(tmp_path, monkeypatch):
    ctx, node, phases, applied = _converge_node(tmp_path, monkeypatch)
    monkeypatch.setattr(wizard, "dry_run", lambda ctx, node: (DryRunVerdict.NO_REBOOT, "diff"))
    wizard._converge(ctx, node)
    assert phases == ["apply"] and applied == []  # secure apply, no reboot wait


def test_converge_reboot_confirmed_applies_and_verifies(tmp_path, monkeypatch):
    ctx, node, phases, applied = _converge_node(tmp_path, monkeypatch)
    monkeypatch.setattr(wizard, "dry_run", lambda ctx, node: (DryRunVerdict.REBOOT, "diff"))
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: True)
    wizard._converge(ctx, node)
    assert applied == [node.name]  # reboot path waits via _apply_and_verify


def test_converge_reboot_declined_skips(tmp_path, monkeypatch):
    ctx, node, phases, applied = _converge_node(tmp_path, monkeypatch)
    monkeypatch.setattr(wizard, "dry_run", lambda ctx, node: (DryRunVerdict.REBOOT, "diff"))
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: False)
    wizard._converge(ctx, node)
    assert phases == [] and applied == []


# orphan removal ------------------------------------------------------------


def test_bring_up_removes_orphan_after_cilium(tmp_path, monkeypatch):
    _record(monkeypatch)
    ctx = _ctx(tmp_path)
    ctx.paths.kubeconfig.write_text("kube\n")  # cluster_up
    orphan = Orphan(name="oldworker", addresses=["5.6.7.8"], control_plane=False)
    snap = _snap(
        {"cp1": NodeState.JOINED, "worker1": NodeState.JOINED}, cilium=True, orphans=[orphan]
    )
    monkeypatch.setattr(wizard, "missing_for_stage", lambda stage: [])
    monkeypatch.setattr(wizard.removal, "repoint_endpoints", lambda cluster, env: None)
    monkeypatch.setattr(wizard.removal, "protect_kubeconfig", lambda cluster, paths, orphans: None)
    removed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        wizard.removal,
        "remove_orphan",
        lambda cluster, paths, env, orphan, *, wipe_all: removed.append((orphan.name, wipe_all)),
    )
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: True)
    monkeypatch.setattr(wizard, "_ask_wipe_scope", lambda: True)

    wizard._bring_up(ctx, snap)

    assert removed == [("oldworker", True)]


def test_confirm_and_remove_honors_refusal_hint(tmp_path, monkeypatch):
    """A refusal hint (would strand the control-plane) short-circuits before any prompt."""
    ctx = _ctx(tmp_path)
    orphan = Orphan(name="oldcp", addresses=["1.1.1.1"], control_plane=True)
    action = Action(
        ActionKind.REMOVE, orphan=orphan, hint="refusing: would leave zero control-planes"
    )
    called: list[int] = []
    asked: list[int] = []
    monkeypatch.setattr(wizard.removal, "remove_orphan", lambda *a, **k: called.append(1))
    monkeypatch.setattr(wizard.prompts, "ask", lambda label, *, default: asked.append(1) or True)

    wizard._confirm_and_remove(ctx, action, {})

    assert called == [] and asked == []


# run loop ------------------------------------------------------------------


def test_run_brings_up_once_then_refreshes_and_quits(tmp_path, monkeypatch):
    """Tally Up runs bring-up against the current snapshot, then re-surveys and re-renders."""
    ctx = _ctx(tmp_path)
    snaps = [_snap({"cp1": NodeState.ABSENT}), _snap({"cp1": NodeState.JOINED})]
    taken = iter(snaps)
    monkeypatch.setattr(wizard.observe, "snapshot", lambda cluster, paths, env: next(taken))
    monkeypatch.setattr(wizard, "_show_preflight", lambda: None)
    monkeypatch.setattr(wizard, "intro", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "outro", lambda *a, **k: None)

    bring_ups: list[Snapshot] = []
    monkeypatch.setattr(wizard, "_bring_up", lambda ctx, snap: bring_ups.append(snap))

    verdicts = iter([Exit("up"), BACK])  # tally up, then quit

    class _FakeMenu:
        def run(self):
            return next(verdicts)

    monkeypatch.setattr(wizard.screens, "main_menu", lambda state: _FakeMenu())

    wizard.run(ctx)

    assert bring_ups == [snaps[0]]  # bring-up saw the pre-survey snapshot, ran exactly once
