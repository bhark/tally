from __future__ import annotations

import pytest

from tally import remote
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
