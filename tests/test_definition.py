from __future__ import annotations

import yaml

from tally import definition
from tally.model import CpuVendor, InstallTarget, NodeRole, ProfileKey, Vswitch, default_cluster


def test_load_missing_returns_none(tmp_path):
    assert definition.load(tmp_path) is None


def test_roundtrip_preserves_topology(tmp_path):
    cluster = default_cluster()
    cp, worker = cluster.nodes
    cp.cpu = CpuVendor.INTEL
    cp.ip, cp.gateway = "65.109.108.72", "65.108.123.1"
    cp.nic_firmware_ext = "ghcr.io/example/fw:v1"
    cp.link_mac = "9c:6b:00:e7:84:16"
    worker.profile = ProfileKey.DB
    worker.install = InstallTarget(selector={"size": "<= 4TB", "type": "nvme"})
    worker.extra_patches = ["custom-patch.yaml"]
    worker.ip, worker.gateway = "65.109.108.73", "65.108.123.1"

    definition.save(cluster, tmp_path)
    loaded = definition.load(tmp_path)

    lcp, lworker = loaded.nodes
    assert [n.name for n in loaded.nodes] == ["cp1", "worker1"]
    assert lcp.role is NodeRole.CONTROLPLANE and lcp.cpu is CpuVendor.INTEL
    assert lcp.ip == "65.109.108.72"
    assert lcp.nic_firmware_ext == "ghcr.io/example/fw:v1"
    assert lcp.link_mac == "9c:6b:00:e7:84:16"
    assert lworker.link_mac == ""
    assert lworker.profile is ProfileKey.DB
    assert lworker.install.selector == {"size": "<= 4TB", "type": "nvme"}
    assert lworker.install.disk is None
    assert lworker.extra_patches == ["custom-patch.yaml"]


def test_definition_carries_no_progress_state(tmp_path):
    definition.save(default_cluster(), tmp_path)
    raw = yaml.safe_load((tmp_path / "tally.yaml").read_text())
    assert set(raw) == {"schema", "nodes"}  # no vswitch block when unset
    for node in raw["nodes"]:
        assert "stages" not in node and "status" not in node


def test_cluster_name_roundtrip(tmp_path):
    cluster = default_cluster()
    cluster.name = "hetzner"

    definition.save(cluster, tmp_path)
    raw = yaml.safe_load((tmp_path / "tally.yaml").read_text())
    assert raw["name"] == "hetzner"

    assert definition.load(tmp_path).name == "hetzner"


def test_unset_name_omits_key(tmp_path):
    definition.save(default_cluster(), tmp_path)
    raw = yaml.safe_load((tmp_path / "tally.yaml").read_text())
    assert "name" not in raw
    assert definition.load(tmp_path).name == ""


def test_vswitch_and_vlan_ip_roundtrip(tmp_path):
    cluster = default_cluster()
    cluster.vswitch = Vswitch(vlan_id=4001, subnet="10.10.0.0/24")
    cp, worker = cluster.nodes
    cp.ip, cp.gateway, cp.vlan_ip = "1.2.3.4", "1.2.3.1", "10.10.0.1"
    worker.ip, worker.gateway, worker.vlan_ip = "1.2.3.5", "1.2.3.1", "10.10.0.2"

    definition.save(cluster, tmp_path)
    raw = yaml.safe_load((tmp_path / "tally.yaml").read_text())
    assert raw["schema"] == 2
    assert raw["vswitch"] == {"vlan_id": 4001, "subnet": "10.10.0.0/24", "mtu": 1400}

    loaded = definition.load(tmp_path)
    assert loaded.vswitch == Vswitch(vlan_id=4001, subnet="10.10.0.0/24")
    assert [n.vlan_ip for n in loaded.nodes] == ["10.10.0.1", "10.10.0.2"]
