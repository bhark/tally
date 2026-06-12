from __future__ import annotations

import json

from tally import observe
from tally.model import Cluster, CpuVendor, Node, NodeRole
from tally.observe import NodeState, parse_orphans, snapshot
from tally.paths import Paths


def _node(name, role=NodeRole.WORKER, ip=""):
    return Node(name=name, role=role, cpu=CpuVendor.AMD, ip=ip)


def _k8s_payload():
    return json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "cp1", "labels": {}},
                    "status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.1"}]},
                },
                {
                    "metadata": {
                        "name": "old-cp",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {
                        "addresses": [
                            {"type": "InternalIP", "address": "10.0.0.9"},
                            {"type": "Hostname", "address": "old-cp"},
                            {"type": "ExternalIP", "address": "1.2.3.9"},
                        ]
                    },
                },
                {
                    "metadata": {"name": "old-worker", "labels": {}},
                    "status": {
                        "addresses": [
                            {"type": "ExternalIP", "address": "1.2.3.10"},
                            {"type": "InternalIP", "address": "10.0.0.10"},
                        ]
                    },
                },
            ]
        }
    )


def test_parse_orphans_names_addresses_and_cp_label():
    orphans = parse_orphans(_k8s_payload(), {"cp1"}, set())
    names = [o.name for o in orphans]
    assert names == ["old-cp", "old-worker"]  # cp1 is desired, excluded

    by_name = {o.name: o for o in orphans}
    # external first then internal, hostname dropped
    assert by_name["old-cp"].addresses == ["1.2.3.9", "10.0.0.9"]
    assert by_name["old-cp"].control_plane is True
    assert by_name["old-worker"].addresses == ["1.2.3.10", "10.0.0.10"]
    assert by_name["old-worker"].control_plane is False


def test_parse_orphans_tolerates_missing_keys():
    raw = json.dumps({"items": [{"metadata": {"name": "x"}}, {}, {"metadata": {}}]})
    orphans = parse_orphans(raw, set(), set())
    assert [o.name for o in orphans] == ["x"]
    assert orphans[0].addresses == []
    assert orphans[0].control_plane is False


def test_parse_orphans_bad_json():
    assert parse_orphans("not json", set(), set()) == []


def test_parse_orphans_excludes_live_node_by_address():
    """A live desired node registered under a different k8s name (rename / hostname drift) is
    matched by address and never treated as an orphan to be reset."""
    # old-cp's InternalIP 10.0.0.9 belongs to a desired node whose name differs ("cpX")
    orphans = parse_orphans(_k8s_payload(), {"cp1"}, {"10.0.0.9"})
    assert [o.name for o in orphans] == ["old-worker"]  # old-cp excluded by address match


def test_snapshot_classifies_per_node(monkeypatch, tmp_path):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    paths.talosconfig.write_text("ctx")  # enables the secure probe

    cluster = Cluster(
        nodes=[
            _node("joined-n", NodeRole.CONTROLPLANE, ip="10.0.0.1"),
            _node("maint-n", ip="10.0.0.2"),
            _node("absent-n", ip="10.0.0.3"),
        ],
        name="c",
    )

    monkeypatch.setattr(observe.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def fake_check(cmd, env, *, timeout=None):
        ip = cmd[cmd.index("-n") + 1]
        secure = "-e" in cmd
        if ip == "10.0.0.1":
            return secure  # only answers mTLS -> joined
        if ip == "10.0.0.2":
            return not secure  # only insecure -> maintenance
        return False  # absent

    monkeypatch.setattr(observe.probe, "check", fake_check)
    monkeypatch.setattr(observe.probe, "read", lambda *a, **k: None)

    snap = snapshot(cluster, paths, {})
    assert snap.states == {
        "joined-n": NodeState.JOINED,
        "maint-n": NodeState.MAINTENANCE,
        "absent-n": NodeState.ABSENT,
    }
    assert snap.etcd_bootstrapped is True  # joined CP answers `etcd members`


def test_snapshot_etcd_not_bootstrapped_without_joined_cp(monkeypatch, tmp_path):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    paths.talosconfig.write_text("ctx")
    cluster = Cluster(nodes=[_node("cp1", NodeRole.CONTROLPLANE, ip="10.0.0.1")], name="c")

    monkeypatch.setattr(observe.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    # secure probe fails -> cp classified absent -> etcd never probed
    monkeypatch.setattr(observe.probe, "check", lambda cmd, env, **k: False)
    monkeypatch.setattr(observe.probe, "read", lambda *a, **k: None)

    snap = snapshot(cluster, paths, {})
    assert snap.states == {"cp1": NodeState.ABSENT}
    assert snap.etcd_bootstrapped is False


def test_snapshot_fresh_repo_all_absent(monkeypatch, tmp_path):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()  # no talosconfig, no kubeconfig
    cluster = Cluster(nodes=[_node("cp1", NodeRole.CONTROLPLANE, ip="10.0.0.1")], name="c")

    monkeypatch.setattr(observe.shutil, "which", lambda tool: None)

    snap = snapshot(cluster, paths, {})
    assert snap.states == {"cp1": NodeState.ABSENT}
    assert snap.k8s_reachable is False
    assert snap.cilium_installed is False
    assert snap.etcd_bootstrapped is False
    assert snap.orphans == []


def test_snapshot_k8s_reachable_with_orphans(monkeypatch, tmp_path):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    paths.kubeconfig.write_text("kc")
    cluster = Cluster(nodes=[_node("cp1", NodeRole.CONTROLPLANE, ip="10.0.0.1")], name="c")

    monkeypatch.setattr(observe.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(observe.probe, "check", lambda cmd, env, **k: "cilium" in cmd)
    monkeypatch.setattr(observe.probe, "read", lambda *a, **k: _k8s_payload())

    snap = snapshot(cluster, paths, {})
    assert snap.k8s_reachable is True
    assert snap.cilium_installed is True
    assert [o.name for o in snap.orphans] == ["old-cp", "old-worker"]
