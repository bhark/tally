"""Observed cluster state: per-desired-node classification + k8s orphan/cilium facts.

The Talos probes mirror the wizard's secure/insecure shapes but run spinner-free on a
worker pool, the whole survey wrapped in one spinner (the sole terminal writer). k8s
reachability gates orphan knowledge: a fresh repo or a down API yields no orphans, not
an empty truth.
"""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

from griphtui import spinner

from . import probe
from .model import Cluster
from .paths import Paths

_PROBE_TIMEOUT = 5


class NodeState(StrEnum):
    JOINED = "joined"
    MAINTENANCE = "maintenance"
    ABSENT = "absent"


@dataclass(slots=True)
class Orphan:
    name: str  # k8s node name
    addresses: list[str]  # ExternalIP first then InternalIP, from status.addresses
    control_plane: bool  # node-role.kubernetes.io/control-plane label present


@dataclass(slots=True)
class Snapshot:
    states: dict[str, NodeState]  # desired node.name -> state
    orphans: list[Orphan]
    k8s_reachable: bool  # False => orphans unknowable (fresh repo / api down)
    cilium_installed: bool


def parse_orphans(raw_json: str, desired_names: set[str]) -> list[Orphan]:
    """Pure: k8s nodes absent from the desired set, with addresses ExternalIP-then-InternalIP."""
    try:
        payload = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[Orphan] = []
    for item in payload.get("items") or []:
        meta = item.get("metadata") or {}
        name = meta.get("name")
        if not name or name in desired_names:
            continue
        labels = meta.get("labels") or {}
        status = item.get("status") or {}
        addrs = status.get("addresses") or []
        external = [
            a["address"] for a in addrs if a.get("type") == "ExternalIP" and a.get("address")
        ]
        internal = [
            a["address"] for a in addrs if a.get("type") == "InternalIP" and a.get("address")
        ]
        out.append(
            Orphan(
                name=name,
                addresses=external + internal,
                control_plane="node-role.kubernetes.io/control-plane" in labels,
            )
        )
    return out


def _classify(ip: str, env: dict[str, str], secure_ok: bool) -> NodeState:
    if not ip:
        return NodeState.ABSENT
    if secure_ok and probe.check(
        ["talosctl", "-e", ip, "-n", ip, "version"], env, timeout=_PROBE_TIMEOUT
    ):
        return NodeState.JOINED
    if probe.check(
        ["talosctl", "-n", ip, "get", "disks", "--insecure"], env, timeout=_PROBE_TIMEOUT
    ):
        return NodeState.MAINTENANCE
    return NodeState.ABSENT


def snapshot(cluster: Cluster, paths: Paths, env: dict[str, str]) -> Snapshot:
    have_talosctl = shutil.which("talosctl") is not None
    secure_ok = have_talosctl and paths.talosconfig.exists()

    def classify(node):
        if not have_talosctl:
            return node.name, NodeState.ABSENT
        return node.name, _classify(node.ip, env, secure_ok)

    with spinner("Surveying cluster state"):
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = dict(pool.map(classify, cluster.nodes))

    orphans, k8s_reachable, cilium_installed = _k8s_facts(cluster, paths, env)
    return Snapshot(
        states=states,
        orphans=orphans,
        k8s_reachable=k8s_reachable,
        cilium_installed=cilium_installed,
    )


def _k8s_facts(
    cluster: Cluster, paths: Paths, env: dict[str, str]
) -> tuple[list[Orphan], bool, bool]:
    if not paths.kubeconfig.exists() or shutil.which("kubectl") is None:
        return [], False, False
    kc = str(paths.kubeconfig)
    out = probe.read(["kubectl", "--kubeconfig", kc, "get", "nodes", "-o", "json"], env)
    if out is None:
        return [], False, False
    orphans = parse_orphans(out, {n.name for n in cluster.nodes})
    cilium = probe.check(
        ["kubectl", "--kubeconfig", kc, "get", "ds", "-n", "kube-system", "cilium"], env
    )
    return orphans, True, cilium
