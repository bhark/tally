from __future__ import annotations

import pytest

from tally import wizard
from tally.model import InstallTarget, default_cluster
from tally.paths import Paths
from tally.reconcile import Action, ActionKind
from tally.stages import Ctx, s3_rescue


def _ctx(tmp_path):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    return Ctx(cluster=default_cluster(), paths=paths)


def _seed_cp(ctx):
    node = ctx.cluster.nodes[0]
    node.ip, node.gateway = "65.109.108.72", "65.108.123.1"
    img = ctx.paths.image(node)
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_text("img")
    return node


# real s3 correction logic --------------------------------------------------


def _stub_remote(monkeypatch, uplink_mac=None):
    """Replace the rescue ssh surface so run_rescue never touches a real host."""
    monkeypatch.setattr(s3_rescue.probe, "wait_port", lambda *a, **k: True)
    monkeypatch.setattr(s3_rescue.probe, "wait_until", lambda *a, **k: True)
    monkeypatch.setattr(
        s3_rescue.remote, "connect", lambda host: s3_rescue.remote.Session(host, "")
    )
    monkeypatch.setattr(s3_rescue.remote, "verify_rescue", lambda session: None)
    monkeypatch.setattr(s3_rescue.remote, "uplink_mac", lambda session: uplink_mac)
    monkeypatch.setattr(s3_rescue.remote, "probe_disks", lambda session: [])
    monkeypatch.setattr(s3_rescue.remote, "select_disk", lambda install, disks: "/dev/nvme0n1")
    monkeypatch.setattr(s3_rescue.remote, "upload", lambda *a, **k: None)
    monkeypatch.setattr(s3_rescue.remote, "exec", lambda *a, **k: None)
    monkeypatch.setattr(s3_rescue.remote, "disconnect", lambda session: None)


def test_s3_diskselector_node_continues_without_regen(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    node = _seed_cp(ctx)
    node.install = InstallTarget(selector={"type": "nvme"})

    monkeypatch.setattr(s3_rescue, "note", lambda *a, **k: None)
    monkeypatch.setattr(s3_rescue, "warn", lambda *a, **k: None)
    monkeypatch.setattr(s3_rescue, "confirm", lambda *a, **k: True)  # gate + write
    _stub_remote(monkeypatch)

    s3_rescue.run_rescue(ctx, node)  # no StageCancelled
    assert node.install.selector == {"type": "nvme"} and node.install.disk is None


def test_s3_pins_uplink_mac_and_rebuilds_image(tmp_path, monkeypatch):
    """A newly discovered uplink MAC stales the image: re-render + rebuild before upload."""
    ctx = _ctx(tmp_path)
    node = _seed_cp(ctx)  # image exists, but the pin invalidates it

    monkeypatch.setattr(s3_rescue, "note", lambda *a, **k: None)
    monkeypatch.setattr(s3_rescue, "confirm", lambda *a, **k: True)
    _stub_remote(monkeypatch, uplink_mac="9c:6b:00:e7:84:16")
    rebuilt: list[str] = []
    monkeypatch.setattr(s3_rescue, "render_node", lambda ctx, node: rebuilt.append("render"))
    monkeypatch.setattr(s3_rescue, "run_image", lambda ctx, node: rebuilt.append("image"))

    s3_rescue.run_rescue(ctx, node)

    assert node.link_mac == "9c:6b:00:e7:84:16"
    assert rebuilt == ["render", "image"]
    from tally import definition

    assert definition.load(ctx.paths.defn).nodes[0].link_mac == "9c:6b:00:e7:84:16"


def test_s3_unchanged_mac_reuses_image(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    node = _seed_cp(ctx)
    node.link_mac = "9c:6b:00:e7:84:16"  # already pinned in a previous run

    monkeypatch.setattr(s3_rescue, "note", lambda *a, **k: None)
    monkeypatch.setattr(s3_rescue, "confirm", lambda *a, **k: True)
    _stub_remote(monkeypatch, uplink_mac="9c:6b:00:e7:84:16")
    monkeypatch.setattr(
        s3_rescue, "run_image", lambda ctx, node: pytest.fail("image must be reused")
    )

    s3_rescue.run_rescue(ctx, node)
    assert node.link_mac == "9c:6b:00:e7:84:16"


# driver pin behaviour ------------------------------------------------------


def _driver(tmp_path, monkeypatch, selector, mac=None):
    """Single-node action with the live pin resolvers stubbed to `selector` / `mac`."""
    ctx = _ctx(tmp_path)
    ctx.cluster.nodes = ctx.cluster.nodes[:1]
    node = ctx.cluster.nodes[0]
    node.ip = "1.2.3.4"
    calls: list[str] = []

    monkeypatch.setattr(wizard, "_run_phase", lambda ctx, sd, node: calls.append(sd.key.value))
    monkeypatch.setattr(wizard.disk, "resolve_system_selector", lambda ip, env: selector)
    monkeypatch.setattr(wizard.uplink, "resolve_uplink_mac", lambda ip, env: mac)
    return ctx, node, calls


def test_driver_pins_boot_disk_and_regens(tmp_path, monkeypatch):
    ctx, node, calls = _driver(tmp_path, monkeypatch, {"wwid": "eui.0a"})

    wizard._run_node_action(ctx, Action(ActionKind.BRING_UP, node=node))

    assert calls == ["rescue", "config", "apply"]  # pin re-renders post-rescue
    assert node.resolved_install.selector == {"wwid": "eui.0a"}
    assert node.install.disk == "/dev/nvme0n1"  # declarative target untouched (rendered-only pin)


def test_driver_unresolved_pins_skip_regen(tmp_path, monkeypatch):
    ctx, node, calls = _driver(tmp_path, monkeypatch, None)  # no clean signal

    wizard._run_node_action(ctx, Action(ActionKind.BRING_UP, node=node))

    assert calls == ["rescue", "apply"]  # nothing to re-render
    assert node.resolved_install is None  # falls back to declarative selector


def test_driver_maintenance_node_pins_before_apply(tmp_path, monkeypatch):
    ctx, node, calls = _driver(tmp_path, monkeypatch, {"serial": "S1"})

    wizard._run_node_action(ctx, Action(ActionKind.APPLY, node=node))

    assert calls == ["config", "apply"]  # no rescue, but still pin + re-render
    assert node.resolved_install.selector == {"serial": "S1"}


def test_driver_maintenance_node_pins_uplink_mac(tmp_path, monkeypatch):
    """A maintenance-path node (imaged out-of-band) still gets its alias pinned + persisted."""
    ctx, node, calls = _driver(tmp_path, monkeypatch, None, mac="9c:6b:00:e7:84:16")

    wizard._run_node_action(ctx, Action(ActionKind.APPLY, node=node))

    assert calls == ["config", "apply"]  # mac pin alone re-renders
    assert node.link_mac == "9c:6b:00:e7:84:16"
    from tally import definition

    assert definition.load(ctx.paths.defn).nodes[0].link_mac == "9c:6b:00:e7:84:16"
