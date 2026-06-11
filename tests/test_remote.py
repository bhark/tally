from __future__ import annotations

import pytest

from tally import remote
from tally.model import InstallTarget
from tally.runner import CommandResult


def _result(stdout: str) -> CommandResult:
    return CommandResult(cmd=["lsblk"], returncode=0, stdout=stdout, stderr="")


_RESCUE = (
    '{"blockdevices":[{"name":"nvme0n1","mountpoint":null,'
    '"children":[{"name":"nvme0n1p1","mountpoint":null}]}]}'
)
_INSTALLED = (
    '{"blockdevices":[{"name":"nvme0n1","mountpoint":null,'
    '"children":[{"name":"nvme0n1p1","mountpoint":"/boot"},{"name":"nvme0n1p2","mountpoint":"/"}]}]}'
)


def test_root_mounted_walks_children():
    import json

    assert remote._root_mounted(json.loads(_INSTALLED)["blockdevices"]) is True
    assert remote._root_mounted(json.loads(_RESCUE)["blockdevices"]) is False


def test_verify_rescue_passes_when_disks_unmounted(monkeypatch):
    monkeypatch.setattr(remote, "exec", lambda *a, **k: _result(_RESCUE))
    remote.verify_rescue(remote.Session(host="1.2.3.4", tmp="/tmp/x"))  # no raise


def test_verify_rescue_aborts_on_installed_os(monkeypatch):
    monkeypatch.setattr(remote, "exec", lambda *a, **k: _result(_INSTALLED))
    with pytest.raises(remote.RemoteError, match="not a rescue ramdisk"):
        remote.verify_rescue(remote.Session(host="1.2.3.4", tmp="/tmp/x"))


def _disk(name: str, tran: str, rota: str) -> dict:
    return {"name": name, "type": "disk", "tran": tran, "rota": rota}


def test_select_disk_ssd_excludes_nvme():
    # talos: type=ssd is sata/sas only - the dd target must skip the nvme and the hdd
    disks = [_disk("nvme0n1", "nvme", "0"), _disk("sda", "sata", "0"), _disk("sdb", "sata", "1")]
    assert remote.select_disk(InstallTarget(selector={"type": "ssd"}), disks) == "/dev/sda"


def test_select_disk_ssd_no_match_when_only_nvme():
    disks = [_disk("nvme0n1", "nvme", "0")]
    with pytest.raises(remote.RemoteError, match="no disk matched"):
        remote.select_disk(InstallTarget(selector={"type": "ssd"}), disks)


def test_select_disk_nvme_lowest_name_wins():
    disks = [_disk("nvme1n1", "nvme", "0"), _disk("nvme0n1", "nvme", "0")]
    assert remote.select_disk(InstallTarget(selector={"type": "nvme"}), disks) == "/dev/nvme0n1"


def test_select_disk_explicit_disk_short_circuits():
    assert remote.select_disk(InstallTarget(disk="/dev/sdz"), []) == "/dev/sdz"


def test_uplink_mac_normalizes(monkeypatch):
    monkeypatch.setattr(remote, "exec", lambda *a, **k: _result("9C:6B:00:E7:84:16\n"))
    assert remote.uplink_mac(remote.Session(host="1.2.3.4", tmp="/t")) == "9c:6b:00:e7:84:16"


def test_uplink_mac_rejects_garbage(monkeypatch):
    monkeypatch.setattr(remote, "exec", lambda *a, **k: _result("cat: no such file\n"))
    assert remote.uplink_mac(remote.Session(host="1.2.3.4", tmp="/t")) is None
