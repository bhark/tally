"""Shared talosctl/kubeconfig primitives used by both bootstrap and orphan removal.

`set_endpoints` points talosconfig at the operator-reachable CP public IPs;
`rewrite_kubeconfig_server` swaps a server host in the kubeconfig (private→public after
bootstrap, or off a soon-removed orphan). Both are one-liners that were duplicated across
s5_bootstrap and removal - factored here so a change to either reaches both call sites.
"""

from __future__ import annotations

from pathlib import Path

from .constants import K8S_API_PORT
from .model import Cluster
from .runner import run


def set_endpoints(cluster: Cluster, env: dict[str, str]) -> None:
    """Point talosconfig at the desired CP public IPs (the firewall-allowed operator hop)."""
    cp_ips = [n.ip for n in cluster.control_planes]
    run(["talosctl", "config", "endpoint", *cp_ips], label="Set talosconfig endpoints", env=env)


def rewrite_kubeconfig_server(kubeconfig: Path, old_addr: str, new_addr: str) -> bool:
    """Swap the kubeconfig server host old_addr→new_addr in place. True iff a swap was made."""
    old = f"https://{old_addr}:{K8S_API_PORT}"
    new = f"https://{new_addr}:{K8S_API_PORT}"
    text = kubeconfig.read_text()
    if old not in text:
        return False
    kubeconfig.write_text(text.replace(old, new))
    return True
