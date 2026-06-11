from __future__ import annotations

from tally import removal
from tally.model import Cluster, CpuVendor, Node, NodeRole
from tally.observe import Orphan
from tally.paths import Paths
from tally.runner import CommandResult


def _cluster():
    return Cluster(
        name="c",
        nodes=[
            Node(name="cp1", role=NodeRole.CONTROLPLANE, cpu=CpuVendor.AMD, ip="1.1.1.1"),
            Node(name="worker1", role=NodeRole.WORKER, cpu=CpuVendor.AMD, ip="2.2.2.2"),
        ],
    )


def _paths(tmp_path):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    return paths


def _record(monkeypatch, *, rc: int = 0):
    cmds: list[list[str]] = []

    def fake_run(cmd, *, label, env=None, check=True):
        cmds.append(cmd)
        return CommandResult(cmd, rc, "", "")

    monkeypatch.setattr(removal, "run", fake_run)
    return cmds


def _flag(cmds, *parts):
    """True iff one recorded command contains parts as a contiguous subsequence."""
    for cmd in cmds:
        for i in range(len(cmd) - len(parts) + 1):
            if cmd[i : i + len(parts)] == list(parts):
                return True
    return False


def _verb(cmd):
    """the action token: kubectl verb sits after --kubeconfig <path>; talosctl's is cmd[1]."""
    if cmd[0] == "kubectl":
        return cmd[3]
    return cmd[1]


def test_worker_reachable_drain_reset_delete_order(tmp_path, monkeypatch):
    cmds = _record(monkeypatch)
    monkeypatch.setattr(removal.probe, "check", lambda cmd, env, **k: True)
    monkeypatch.setattr(removal.probe, "wait_gone", lambda *a, **k: True)
    purged: list[str] = []
    monkeypatch.setattr(Paths, "purge_node", lambda self, name: purged.append(name) or [])

    orphan = Orphan(name="old-w", addresses=["3.3.3.3"], control_plane=False)
    removal.remove_orphan(_cluster(), _paths(tmp_path), {}, orphan, wipe_all=False)

    seq = [_verb(cmd) for cmd in cmds]
    assert seq == ["drain", "reset", "delete"]
    assert _flag(cmds, "--wipe-mode", "system-disk")
    assert _flag(cmds, "-e", "1.1.1.1", "-n", "3.3.3.3")  # proxied via bootstrap CP
    assert purged == ["old-w"]
    assert not _flag(cmds, "etcd", "members")


def test_cp_reachable_no_drain_resets_checks_etcd_wipe_all(tmp_path, monkeypatch):
    cmds = _record(monkeypatch)
    monkeypatch.setattr(removal.probe, "check", lambda cmd, env, **k: True)
    monkeypatch.setattr(removal.probe, "wait_gone", lambda *a, **k: True)
    monkeypatch.setattr(Paths, "purge_node", lambda self, name: [])

    orphan = Orphan(name="old-cp", addresses=["4.4.4.4"], control_plane=True)
    removal.remove_orphan(_cluster(), _paths(tmp_path), {}, orphan, wipe_all=True)

    verbs = [_verb(cmd) for cmd in cmds]
    assert "drain" not in verbs
    assert "reset" in verbs
    assert _flag(cmds, "etcd", "members")
    assert _flag(cmds, "--wipe-mode", "all")


def test_unreachable_confirm_true_deletes(tmp_path, monkeypatch):
    cmds = _record(monkeypatch)
    monkeypatch.setattr(removal.probe, "check", lambda cmd, env, **k: False)
    monkeypatch.setattr(removal, "confirm", lambda *a, **k: True)
    purged: list[str] = []
    monkeypatch.setattr(Paths, "purge_node", lambda self, name: purged.append(name) or [])

    orphan = Orphan(name="dead", addresses=["5.5.5.5"], control_plane=False)
    removal.remove_orphan(_cluster(), _paths(tmp_path), {}, orphan, wipe_all=False)

    verbs = [_verb(cmd) for cmd in cmds]
    assert "reset" not in verbs
    assert "delete" in verbs
    assert purged == ["dead"]


def test_unreachable_confirm_false_leaves_node(tmp_path, monkeypatch):
    cmds = _record(monkeypatch)
    monkeypatch.setattr(removal.probe, "check", lambda cmd, env, **k: False)
    monkeypatch.setattr(removal, "confirm", lambda *a, **k: False)
    purged: list[str] = []
    monkeypatch.setattr(Paths, "purge_node", lambda self, name: purged.append(name) or [])

    orphan = Orphan(name="dead", addresses=["5.5.5.5"], control_plane=False)
    removal.remove_orphan(_cluster(), _paths(tmp_path), {}, orphan, wipe_all=False)

    verbs = [_verb(cmd) for cmd in cmds]
    assert "reset" not in verbs and "delete" not in verbs
    assert purged == []


def test_repoint_endpoints_lists_all_cp_ips(monkeypatch):
    cmds = _record(monkeypatch)
    cluster = Cluster(
        name="c",
        nodes=[
            Node(name="cp1", role=NodeRole.CONTROLPLANE, cpu=CpuVendor.AMD, ip="1.1.1.1"),
            Node(name="cp2", role=NodeRole.CONTROLPLANE, cpu=CpuVendor.AMD, ip="1.1.1.2"),
            Node(name="worker1", role=NodeRole.WORKER, cpu=CpuVendor.AMD, ip="2.2.2.2"),
        ],
    )
    removal.repoint_endpoints(cluster, {})
    assert cmds == [["talosctl", "config", "endpoint", "1.1.1.1", "1.1.1.2"]]
