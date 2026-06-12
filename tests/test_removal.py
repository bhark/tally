from __future__ import annotations

import pytest

from tally import removal
from tally.constants import K8S_API_PORT
from tally.model import Cluster, CpuVendor, Node, NodeRole
from tally.observe import Orphan
from tally.paths import Paths
from tally.runner import CommandError, CommandResult
from tally.stages.base import StageError


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
    assert _flag(cmds, "--graceful")  # node performs its own etcd leave
    assert _flag(cmds, "--wait=false")  # node powers down mid-stream
    assert _flag(cmds, "-e", "1.1.1.1", "-n", "3.3.3.3")  # proxied via a live CP
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
    assert _flag(cmds, "--graceful")
    assert _flag(cmds, "--wait=false")


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


def test_reset_failure_raises_stage_error(tmp_path, monkeypatch):
    def boom(cmd, *, label, env=None, check=True):
        if cmd[:2] == ["talosctl", "reset"]:
            raise CommandError(label, cmd, 1, ["reset boom"])
        return CommandResult(cmd, 0, "", "")

    monkeypatch.setattr(removal, "run", boom)
    monkeypatch.setattr(removal.probe, "check", lambda cmd, env, **k: True)
    monkeypatch.setattr(Paths, "purge_node", lambda self, name: [])

    orphan = Orphan(name="w", addresses=["3.3.3.3"], control_plane=False)
    with pytest.raises(StageError):
        removal.remove_orphan(_cluster(), _paths(tmp_path), {}, orphan, wipe_all=False)


def test_reset_api_still_answering_warns_and_continues(tmp_path, monkeypatch):
    cmds = _record(monkeypatch)
    monkeypatch.setattr(removal.probe, "check", lambda cmd, env, **k: True)
    monkeypatch.setattr(removal.probe, "wait_gone", lambda *a, **k: False)  # never goes down
    warns: list[str] = []
    monkeypatch.setattr(removal, "warn", lambda msg: warns.append(msg))
    monkeypatch.setattr(Paths, "purge_node", lambda self, name: [])

    orphan = Orphan(name="w", addresses=["3.3.3.3"], control_plane=False)
    removal.remove_orphan(_cluster(), _paths(tmp_path), {}, orphan, wipe_all=False)

    verbs = [_verb(cmd) for cmd in cmds]
    assert "reset" in verbs and "delete" in verbs  # continues to delete despite API up
    assert any("still answering" in w for w in warns)


def test_no_live_cp_skips_reset_and_confirms_delete(tmp_path, monkeypatch):
    """No desired CP answers the secure API: the etcd-leave proxy is unavailable, so reset is
    skipped and removal falls to the manual confirm path rather than proxying through a dead CP."""
    cmds = _record(monkeypatch)
    monkeypatch.setattr(removal.probe, "check", lambda cmd, env, **k: False)  # no CP answers
    monkeypatch.setattr(removal, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(Paths, "purge_node", lambda self, name: [])

    orphan = Orphan(name="w", addresses=["3.3.3.3"], control_plane=False)
    removal.remove_orphan(_cluster(), _paths(tmp_path), {}, orphan, wipe_all=False)

    verbs = [_verb(cmd) for cmd in cmds]
    assert "reset" not in verbs and "delete" in verbs


def test_protect_kubeconfig_rewrites_off_orphan(tmp_path):
    paths = _paths(tmp_path)
    paths.kubeconfig.write_text(f"server: https://3.3.3.3:{K8S_API_PORT}\n")
    orphan = Orphan(name="w", addresses=["3.3.3.3"], control_plane=False)
    removal.protect_kubeconfig(_cluster(), paths, [orphan])
    text = paths.kubeconfig.read_text()
    assert "3.3.3.3" not in text
    assert "1.1.1.1" in text  # repointed to bootstrap_cp
