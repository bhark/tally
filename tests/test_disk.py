from __future__ import annotations

from tally import disk


def _fake_read(values: dict[tuple[str, str], str | None]):
    """Stub probe.read keyed by (resource, spec-field) parsed out of the talosctl cmd."""

    def read(cmd, env, **_kw):
        resource = cmd[cmd.index("get") + 1]
        jsonpath = cmd[cmd.index("-o") + 1]  # jsonpath={.spec.<field>}
        field = jsonpath.rsplit(".", 1)[1].rstrip("}")
        return values.get((resource, field))

    return read


def _present(monkeypatch):
    monkeypatch.setattr(disk.shutil, "which", lambda _name: "/usr/bin/talosctl")


def test_resolves_wwid_from_system_disk(monkeypatch):
    _present(monkeypatch)
    monkeypatch.setattr(
        disk.probe,
        "read",
        _fake_read({("systemdisk", "diskID"): "nvme0n1", ("disks", "wwid"): "eui.0a"}),
    )
    assert disk.resolve_system_selector("1.2.3.4", {}) == {"wwid": "eui.0a"}


def test_falls_back_to_serial_when_no_wwid(monkeypatch):
    _present(monkeypatch)
    monkeypatch.setattr(
        disk.probe,
        "read",
        _fake_read(
            {
                ("systemdisk", "diskID"): "nvme0n1",
                ("disks", "wwid"): None,
                ("disks", "serial"): "S1",
            }
        ),
    )
    assert disk.resolve_system_selector("1.2.3.4", {}) == {"serial": "S1"}


def test_none_when_no_system_disk(monkeypatch):
    _present(monkeypatch)
    monkeypatch.setattr(disk.probe, "read", _fake_read({}))  # systemdisk unreadable
    assert disk.resolve_system_selector("1.2.3.4", {}) is None


def test_none_without_ip_or_talosctl(monkeypatch):
    monkeypatch.setattr(disk.shutil, "which", lambda _name: "/usr/bin/talosctl")
    assert disk.resolve_system_selector("", {}) is None
    monkeypatch.setattr(disk.shutil, "which", lambda _name: None)
    assert disk.resolve_system_selector("1.2.3.4", {}) is None
