from __future__ import annotations

import yaml

from tally.constants import TOPOLVM_MOUNT
from tally.model import CpuVendor, InstallTarget, Node, NodeRole, ProfileKey, Vswitch
from tally.stages.s1_config import (
    _cp_cluster_patch,
    _discovery_patch,
    _machine_patch,
    dump_yaml,
    dump_yaml_all,
    hostname_doc,
    link_docs,
    net_docs,
    vlan_doc,
)


def _vswitch() -> Vswitch:
    return Vswitch(vlan_id=4001, subnet="10.10.0.0/24")


def _node(**overrides) -> Node:
    kw = dict(
        name="cp1",
        role=NodeRole.CONTROLPLANE,
        cpu=CpuVendor.AMD,
        ip="65.109.108.72",
        gateway="65.108.123.1",
    )
    kw.update(overrides)
    return Node(**kw)


def _by_kind(docs: list[dict]) -> dict:
    return {d["kind"]: d for d in docs}


def test_link_alias_selects_single_physical_nic():
    alias, _link = link_docs(_node())
    assert alias["kind"] == "LinkAliasConfig"
    assert alias["selector"]["match"] is True  # single physical link, no MAC
    assert alias["name"] == "net0"


def test_link_vs_default_route_shape():
    _alias, link = link_docs(_node())
    assert link["kind"] == "LinkConfig" and link["name"] == "net0"
    assert link["addresses"] == [{"address": "65.109.108.72/32"}]
    routes = link["routes"]
    assert routes[0] == {"destination": "65.108.123.1/32"}  # link-scope host route, no gateway
    assert "gateway" not in routes[0]
    assert routes[1] == {"gateway": "65.108.123.1"}  # default route, no destination


def test_net_docs_are_standalone_and_carry_hostname():
    docs = net_docs(_node(), None)
    by_kind = _by_kind(docs)
    assert set(by_kind) == {"LinkAliasConfig", "LinkConfig", "ResolverConfig", "HostnameConfig"}
    assert all(d["apiVersion"] == "v1alpha1" for d in docs)  # standalone docs, not machine.*
    assert by_kind["HostnameConfig"]["hostname"] == "cp1"  # embed carries the hostname
    assert by_kind["ResolverConfig"]["nameservers"][0] == {"address": "185.12.64.1"}


def test_machine_patch_carries_no_network():
    # host networking left v1alpha1 in v1.13 → it rides the standalone net docs, not the patch
    assert "network" not in _machine_patch(_node(), None)["machine"]


def test_hostname_doc_is_hostnameconfig_with_auto_off():
    doc = hostname_doc(_node())
    assert doc["kind"] == "HostnameConfig"
    assert doc["hostname"] == "cp1"
    assert doc["auto"] == "off"  # auto off to coexist with the base default HostnameConfig
    assert "'off'" in dump_yaml(doc)  # string, not YAML 1.1 bool


def test_yaml_roundtrip_and_v6_quoting():
    docs = net_docs(_node(), None)
    text = dump_yaml_all(docs)
    assert list(yaml.safe_load_all(text)) == docs
    assert '"2a01:4ff:ff00::add:1"' in text  # v6 quoted
    assert "match: true" in text  # CEL bool, not quoted


def test_machine_patch_carries_install_disk():
    n = _node()
    assert _machine_patch(n, None)["machine"]["install"]["disk"] == n.install.disk


def test_cp_cluster_patch_is_cluster_only():
    cp = _cp_cluster_patch(None)
    assert cp["cluster"]["network"]["cni"]["name"] == "none"
    assert cp["cluster"]["proxy"]["disabled"] is True
    assert cp["cluster"]["allowSchedulingOnControlPlanes"] is True
    assert "machine" not in cp  # cluster section only; per-node machine patch is separate
    assert "cluster" not in _machine_patch(_node(), None)


def test_selector_target_emits_quoted_diskselector():
    n = _node(install=InstallTarget(selector={"type": "nvme", "size": ">= 1TB"}))
    install = _machine_patch(n, None)["machine"]["install"]
    assert install["disk"] == {"$patch": "delete"}  # strip base gen-config disk:/dev/sda leftover
    assert install["diskSelector"] == {"type": "nvme", "size": ">= 1TB"}
    text = dump_yaml(_machine_patch(n, None))
    assert "'>= 1TB'" in text  # leading > is a YAML indicator → must be quoted
    assert yaml.safe_load(text) == _machine_patch(n, None)


def test_generic_profile_adds_no_fragments():
    machine = _machine_patch(_node(profile=ProfileKey.GENERIC), None)["machine"]
    assert "kernel" not in machine and "kubelet" not in machine and "sysctls" not in machine
    assert "nodeLabels" not in machine


def test_db_profile_fragments_land_in_role_patch():
    machine = _machine_patch(_node(profile=ProfileKey.DB), None)["machine"]
    assert machine["kernel"]["modules"] == [{"name": "dm_mod"}]
    assert machine["kubelet"]["extraMounts"] == [TOPOLVM_MOUNT]
    assert machine["nodeLabels"] == {"workload": "db"}


def test_storage_profile_loads_rbd():
    machine = _machine_patch(_node(profile=ProfileKey.STORAGE), None)["machine"]
    assert machine["kernel"]["modules"] == [{"name": "rbd"}]
    assert machine["nodeLabels"] == {"workload": "storage"}
    assert machine["sysctls"] == {"fs.aio-max-nr": "1048576"}


# vswitch ---------------------------------------------------------------------


def test_vlan_doc_is_standalone_vlanconfig():
    n = _node(vlan_ip="10.10.0.1")
    doc = vlan_doc(n, _vswitch())
    assert doc["kind"] == "VLANConfig"  # standalone doc, not a vlans: key on LinkConfig
    assert doc["name"] == "net0.4001" and doc["parent"] == "net0"
    assert doc["vlanID"] == 4001 and doc["mtu"] == 1400
    assert doc["addresses"] == [{"address": "10.10.0.1/24"}]
    assert doc["routes"] == [{"destination": "10.10.0.0/24"}]  # on-link, no gateway
    assert "gateway" not in doc["routes"][0]


def test_net_docs_append_vlan_only_with_vswitch():
    n = _node(vlan_ip="10.10.0.1")
    assert all(d["kind"] != "VLANConfig" for d in net_docs(n, None))
    docs = net_docs(n, _vswitch())
    assert [d["kind"] for d in docs][-1] == "VLANConfig"  # appended last, base order intact


def test_machine_patch_pins_kubelet_node_ip():
    n = _node(vlan_ip="10.10.0.1")
    assert "kubelet" not in _machine_patch(n, None)["machine"]
    kubelet = _machine_patch(n, _vswitch())["machine"]["kubelet"]
    assert kubelet["nodeIP"]["validSubnets"] == ["10.10.0.0/24"]


def test_cp_cluster_patch_advertises_etcd_subnet():
    assert "etcd" not in _cp_cluster_patch(None)["cluster"]
    etcd = _cp_cluster_patch(_vswitch())["cluster"]["etcd"]
    assert etcd["advertisedSubnets"] == ["10.10.0.0/24"]


def test_discovery_patch_disables_discovery_cluster_only():
    patch = _discovery_patch()
    assert patch["cluster"]["discovery"]["enabled"] is False
    assert "machine" not in patch  # all-node cluster section, applied via --config-patch
