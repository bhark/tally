"""Stage 1 - generate node configs, reproducibly, from pinned secrets."""

from __future__ import annotations

from pathlib import Path

import yaml
from griphtui import note

from ..constants import (
    K8S_API_PORT,
    K8S_VERSION,
    NAMESERVERS,
    NTP_SERVERS,
    TALOS_VERSION,
    USERNS_SYSCTL,
)
from ..model import Node, Stage, Vswitch
from ..profiles import profile_for
from ..runner import run
from .base import Ctx, StageDef, StageError

# v1.13 retired host networking from v1alpha1 (machine.network is deprecated) in
# favour of standalone docs; net_docs() emits the replacement set.
LINK_ALIAS = "net0"  # single physical NIC, aliased structurally - no MAC, no per-node id


def link_docs(node: Node) -> list[dict]:
    """Alias the one physical NIC, then statically address + route it.

    /32 address: a link-scope host route (no gateway key) reaches the gateway,
    and the default route rides it. Mirrors Hetzner's single-IP layout. match:
    true selects the sole physical link; logical links are auto-excluded.
    """
    return [
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkAliasConfig",
            "name": LINK_ALIAS,
            "selector": {"match": True},
        },
        {
            "apiVersion": "v1alpha1",
            "kind": "LinkConfig",
            "name": LINK_ALIAS,
            "addresses": [{"address": f"{node.ip}/32"}],
            "routes": [
                {"destination": f"{node.gateway}/32"},  # link-scope host route, no gateway
                {"gateway": node.gateway},  # default route, no destination
            ],
        },
    ]


def resolver_doc() -> dict:
    return {
        "apiVersion": "v1alpha1",
        "kind": "ResolverConfig",
        "nameservers": [{"address": ns} for ns in NAMESERVERS],
    }


def time_sync_doc() -> dict:
    """Override the NTP server set: the Talos default (time.cloudflare.com) is filtered
    on Hetzner, so etcd never clears its time-sync gate without on-network servers."""
    return {
        "apiVersion": "v1alpha1",
        "kind": "TimeSyncConfig",
        "ntp": {"servers": list(NTP_SERVERS)},
    }


def hostname_doc(node: Node) -> dict:
    """Static hostname as a HostnameConfig doc; auto off to coexist with the base default."""
    return {
        "apiVersion": "v1alpha1",
        "kind": "HostnameConfig",
        "auto": "off",
        "hostname": node.name,
    }


def vlan_doc(node: Node, vswitch: Vswitch) -> dict:
    """Standalone v1.13 VLANConfig over the link alias - NOT a vlans: key on LinkConfig.

    The on-link /24 route (no gateway) plus the private address give the device a
    global-unicast connected route and the NodeInternalIP - the two signals Cilium's
    min-over-devices MTU auto-detection keys on to size the overlay off 1400. Depends
    on Cilium ≥1.19 and devices/direct-routing-device left auto (see s6_cilium).
    """
    return {
        "apiVersion": "v1alpha1",
        "kind": "VLANConfig",
        "name": f"{LINK_ALIAS}.{vswitch.vlan_id}",
        "vlanID": vswitch.vlan_id,
        "parent": LINK_ALIAS,
        "mtu": vswitch.mtu,
        "addresses": [{"address": f"{node.vlan_ip}/{vswitch.subnet_prefixlen}"}],
        "routes": [{"destination": vswitch.subnet}],  # on-link, no gateway
    }


def net_docs(node: Node, vswitch: Vswitch | None) -> list[dict]:
    """Standalone v1.13 networking docs: link alias + static link + resolver + hostname,
    plus the vSwitch VLAN doc when one is configured.

    One artifact, two consumers: embedded verbatim in the image (so the node has
    its IP/hostname before it can fetch config) and applied as a machineconfig patch.
    """
    docs = [*link_docs(node), resolver_doc(), hostname_doc(node)]
    if vswitch is not None:
        docs.append(vlan_doc(node, vswitch))
    return docs


def _machine_patch(node: Node, vswitch: Vswitch | None) -> dict:
    target = node.resolved_install or node.install  # live boot-disk pin wins over declarative
    install = target.install_block()
    if "diskSelector" in install:  # base gen-config seeds disk:/dev/sda; drop it, selector wins
        install["disk"] = {"$patch": "delete"}
    machine: dict = {"install": install}
    profile = profile_for(node.profile)
    if profile.kernel_modules:
        machine["kernel"] = {"modules": [{"name": m} for m in profile.kernel_modules]}
    if profile.kubelet_mounts:
        machine["kubelet"] = {"extraMounts": [dict(m) for m in profile.kubelet_mounts]}
    if profile.sysctls:  # merges with the common userns sysctl (talosctl unions maps)
        machine["sysctls"] = dict(profile.sysctls)
    if vswitch is not None:  # pin kubelet InternalIP onto the vSwitch (all roles)
        machine.setdefault("kubelet", {})["nodeIP"] = {"validSubnets": [vswitch.subnet]}
    return {"machine": machine}


def _common_patch() -> dict:
    key, value = USERNS_SYSCTL
    return {"machine": {"sysctls": {key: value}}}


def _cp_cluster_patch(vswitch: Vswitch | None) -> dict:
    """CP-role cluster section only - merged into the controlplane base at gen time.

    allowSchedulingOnControlPlanes stays true for the experiment; tainting real CP
    nodes is out of scope here.
    """
    cluster: dict = {
        "allowSchedulingOnControlPlanes": True,
        "network": {"cni": {"name": "none"}},  # Cilium owns CNI
        "proxy": {"disabled": True},  # Cilium kube-proxy replacement via KubePrism
    }
    if vswitch is not None:  # etcd peers on the vSwitch; listenSubnets defaults to advertised
        cluster["etcd"] = {"advertisedSubnets": [vswitch.subnet]}
    return {"cluster": cluster}


def _discovery_patch() -> dict:
    """Disable cluster discovery so a joining node reaches trustd/apid via the configured
    vSwitch endpoint, not the discovery-advertised public IP the Robot firewall drops."""
    return {"cluster": {"discovery": {"enabled": False}}}


class _Dumper(yaml.SafeDumper):
    pass


def _quote_colon_scalars(dumper: yaml.SafeDumper, value: str):
    # quote IPv6 addresses; a bare colon-bearing scalar risks YAML 1.1 base-60 /
    # ambiguous parsing in stricter downstream parsers.
    style = '"' if ":" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _quote_colon_scalars)


def dump_yaml(doc: dict) -> str:
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False)


def dump_yaml_all(docs: list[dict]) -> str:
    return yaml.dump_all(docs, Dumper=_Dumper, sort_keys=False)


def _dump(path, doc: dict) -> None:
    path.write_text(dump_yaml(doc))


def _dump_all(path, docs: list[dict]) -> None:
    path.write_text(dump_yaml_all(docs))


def run_config(ctx: Ctx, _node: Node | None) -> None:
    cluster = ctx.cluster

    problems = cluster.problems() + [p for n in cluster.nodes for p in n.problems()]
    if problems:
        raise StageError("Cluster not ready for config:\n  " + "\n  ".join(problems))

    missing_patches = [p for n in cluster.nodes for p in n.extra_patches if not Path(p).exists()]
    if missing_patches:
        raise StageError("Extra config-patch files not found:\n  " + "\n  ".join(missing_patches))

    paths = ctx.paths
    cp = cluster.bootstrap_cp
    cp_ips = [n.ip for n in cluster.control_planes]
    vswitch = cluster.vswitch

    # private VLAN endpoint keeps in-cluster API traffic on the vSwitch; SANs union
    # covers both so the public-rewritten kubeconfig still validates TLS
    endpoint = f"https://{cp.ip}:{K8S_API_PORT}"
    sans = ",".join(cp_ips)
    if vswitch is not None:
        endpoint = f"https://{cp.vlan_ip}:{K8S_API_PORT}"
        cp_vlan_ips = [n.vlan_ip for n in cluster.control_planes]
        sans = ",".join(dict.fromkeys([*cp_ips, *cp_vlan_ips]))

    if not paths.secrets_yaml.exists():
        run(
            ["talosctl", "gen", "secrets", "-o", str(paths.secrets_yaml)],
            label="Mint cluster secrets",
        )
        paths.harden()
    else:
        note("Reusing existing secrets.yaml (resumable)")

    _dump(paths.patch_common, _common_patch())
    _dump(paths.patch_cp_cluster, _cp_cluster_patch(vswitch))
    _dump(paths.patch_time, time_sync_doc())

    # all-node patches; discovery off only with a vSwitch (else trustd/apid leak to the public IP)
    all_node_patches = [paths.patch_common, paths.patch_time]
    if vswitch is not None:
        _dump(paths.patch_discovery, _discovery_patch())
        all_node_patches.append(paths.patch_discovery)
    patch_flags = [arg for p in all_node_patches for arg in ("--config-patch", f"@{p}")]

    # base role configs, shared across nodes of a role; certSANs cover the CPs
    # present now so a client can validate TLS against any current-CP IP
    run(
        [
            "talosctl",
            "gen",
            "config",
            cluster.name,
            endpoint,
            "--with-secrets",
            str(paths.secrets_yaml),
            "--talos-version",
            TALOS_VERSION,
            "--kubernetes-version",
            K8S_VERSION,
            "--additional-sans",
            sans,
            *patch_flags,
            "--config-patch-control-plane",
            f"@{paths.patch_cp_cluster}",
            "--output-dir",
            str(paths.secret),
            "--force",
        ],
        label="Gen config",
    )

    for node in cluster.nodes:
        _render_node(ctx, node)

    paths.harden()


def _render_node(ctx: Ctx, node: Node) -> None:
    """Derive the per-node applied config from its role base via machineconfig patch."""
    paths = ctx.paths
    vswitch = ctx.cluster.vswitch
    _dump_all(paths.net_file(node), net_docs(node, vswitch))
    _dump(paths.patch_for(node), _machine_patch(node, vswitch))

    cmd = [
        "talosctl",
        "machineconfig",
        "patch",
        str(paths.base_for(node)),
        "-p",
        f"@{paths.patch_for(node)}",
        "-p",
        f"@{paths.net_file(node)}",
    ]
    for path in node.extra_patches:
        cmd += ["-p", f"@{path}"]
    cmd += ["-o", str(paths.config_for(node))]

    run(cmd, label=f"Render {node.name} config")


STAGE = StageDef(
    key=Stage.CONFIG,
    title="Generate node configs",
    run=run_config,
)
