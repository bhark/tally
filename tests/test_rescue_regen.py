from __future__ import annotations

from tally import wizard
from tally.model import InstallTarget, default_cluster
from tally.paths import Paths
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


def _stub_remote(monkeypatch):
    """Replace the rescue ssh surface so run_rescue never touches a real host."""
    monkeypatch.setattr(s3_rescue.probe, "wait_port", lambda *a, **k: True)
    monkeypatch.setattr(s3_rescue.probe, "wait_until", lambda *a, **k: True)
    monkeypatch.setattr(
        s3_rescue.remote, "connect", lambda host: s3_rescue.remote.Session(host, "")
    )
    monkeypatch.setattr(s3_rescue.remote, "verify_rescue", lambda session: None)
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
    monkeypatch.setattr(s3_rescue, "confirm", lambda *a, **k: True)  # gate + write
    _stub_remote(monkeypatch)

    s3_rescue.run_rescue(ctx, node)  # no StageCancelled
    assert node.install.selector == {"type": "nvme"} and node.install.disk is None


# driver pin behaviour ------------------------------------------------------


def _driver(tmp_path, monkeypatch, selector):
    """Single-node bring-up with the boot-disk resolver stubbed to `selector`."""
    ctx = _ctx(tmp_path)
    ctx.cluster.nodes = ctx.cluster.nodes[:1]
    node = ctx.cluster.nodes[0]
    node.ip = "1.2.3.4"
    calls: list[str] = []

    def fake_run_phase(ctx, sd, node):
        calls.append(sd.key.value)
        if sd.key.value == "image":
            ctx.paths.image(node).parent.mkdir(parents=True, exist_ok=True)
            ctx.paths.image(node).write_text("img")

    monkeypatch.setattr(wizard, "_run_phase", fake_run_phase)
    monkeypatch.setattr(wizard, "_configured", lambda ctx, node: False)
    monkeypatch.setattr(wizard, "_in_maintenance", lambda ctx, node: False)
    monkeypatch.setattr(wizard.disk, "resolve_system_selector", lambda ip, env: selector)
    return ctx, node, calls


def test_driver_pins_boot_disk_and_regens_without_rebuild(tmp_path, monkeypatch):
    ctx, node, calls = _driver(tmp_path, monkeypatch, {"wwid": "eui.0a"})

    wizard._bring_up_node(ctx, node, cluster_up=False)

    assert calls == ["image", "rescue", "config", "apply"]  # pin re-renders, no re-image
    assert calls.count("image") == 1  # match:true embed has no per-node id → no rebuild
    assert node.resolved_install.selector == {"wwid": "eui.0a"}
    assert node.install.disk == "/dev/nvme0n1"  # declarative target untouched (rendered-only pin)


def test_driver_unresolved_boot_disk_skips_regen(tmp_path, monkeypatch):
    ctx, node, calls = _driver(tmp_path, monkeypatch, None)  # no clean signal

    wizard._bring_up_node(ctx, node, cluster_up=False)

    assert calls == ["image", "rescue", "apply"]  # nothing to re-render
    assert node.resolved_install is None  # falls back to declarative selector


def test_driver_maintenance_node_pins_before_apply(tmp_path, monkeypatch):
    ctx, node, calls = _driver(tmp_path, monkeypatch, {"serial": "S1"})
    monkeypatch.setattr(wizard, "_in_maintenance", lambda ctx, node: True)  # imaged out-of-band

    wizard._bring_up_node(ctx, node, cluster_up=False)

    assert calls == ["config", "apply"]  # no image/rescue, but still pin + re-render
    assert node.resolved_install.selector == {"serial": "S1"}
