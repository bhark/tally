from __future__ import annotations

from tally import uplink

_ADDRESSES = "\n".join(
    [
        "bond0/10.0.0.9/32",  # logical link must not win on position
        "enp1s0/1.2.3.4/26",
        "lo/127.0.0.1/8",
    ]
)


def _read(addresses=_ADDRESSES, permanent="9C:6B:00:E7:84:16", hardware=None):
    def read(cmd, env, **_kw):
        joined = " ".join(cmd)
        if " addresses " in f" {joined} ":
            return addresses
        if "permanentAddr" in joined:
            return permanent
        if "hardwareAddr" in joined:
            return hardware
        return None

    return read


def test_resolves_link_holding_node_ip(monkeypatch):
    monkeypatch.setattr(uplink.probe, "read", _read())
    monkeypatch.setattr(uplink.shutil, "which", lambda t: "/bin/talosctl")
    assert uplink.resolve_uplink_mac("1.2.3.4", {}) == "9c:6b:00:e7:84:16"


def test_falls_back_to_hardware_addr(monkeypatch):
    monkeypatch.setattr(uplink.probe, "read", _read(permanent=None, hardware="aa:bb:cc:dd:ee:ff"))
    monkeypatch.setattr(uplink.shutil, "which", lambda t: "/bin/talosctl")
    assert uplink.resolve_uplink_mac("1.2.3.4", {}) == "aa:bb:cc:dd:ee:ff"


def test_none_when_no_address_matches(monkeypatch):
    monkeypatch.setattr(uplink.probe, "read", _read())
    monkeypatch.setattr(uplink.shutil, "which", lambda t: "/bin/talosctl")
    assert uplink.resolve_uplink_mac("5.6.7.8", {}) is None


def test_none_without_ip(monkeypatch):
    assert uplink.resolve_uplink_mac("", {}) is None
