from __future__ import annotations

from tally import cluster_ops
from tally.constants import K8S_API_PORT
from tally.model import Cluster, CpuVendor, Node, NodeRole


def _cluster():
    return Cluster(
        name="c",
        nodes=[
            Node(name="cp1", role=NodeRole.CONTROLPLANE, cpu=CpuVendor.AMD, ip="1.1.1.1"),
            Node(name="cp2", role=NodeRole.CONTROLPLANE, cpu=CpuVendor.AMD, ip="1.1.1.2"),
            Node(name="worker1", role=NodeRole.WORKER, cpu=CpuVendor.AMD, ip="2.2.2.2"),
        ],
    )


def test_set_endpoints_lists_all_cp_ips(monkeypatch):
    cmds: list[list[str]] = []
    monkeypatch.setattr(cluster_ops, "run", lambda cmd, **k: cmds.append(cmd))
    cluster_ops.set_endpoints(_cluster(), {})
    assert cmds == [["talosctl", "config", "endpoint", "1.1.1.1", "1.1.1.2"]]  # CPs only


def test_rewrite_kubeconfig_server_swaps_on_match(tmp_path):
    kc = tmp_path / "kubeconfig"
    kc.write_text(f"    server: https://9.9.9.9:{K8S_API_PORT}\n")
    assert cluster_ops.rewrite_kubeconfig_server(kc, "9.9.9.9", "1.1.1.1") is True
    text = kc.read_text()
    assert f"https://1.1.1.1:{K8S_API_PORT}" in text
    assert "9.9.9.9" not in text


def test_rewrite_kubeconfig_server_no_match(tmp_path):
    kc = tmp_path / "kubeconfig"
    original = f"    server: https://8.8.8.8:{K8S_API_PORT}\n"
    kc.write_text(original)
    assert cluster_ops.rewrite_kubeconfig_server(kc, "9.9.9.9", "1.1.1.1") is False
    assert kc.read_text() == original  # untouched
