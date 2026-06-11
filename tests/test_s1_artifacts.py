from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tally.model import (
    Cluster,
    CpuVendor,
    InstallTarget,
    Node,
    NodeRole,
    ProfileKey,
    Vswitch,
    default_cluster,
)
from tally.paths import Paths
from tally.stages import s1_config
from tally.stages.base import Ctx


def _paths(tmp_path) -> Paths:
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    return paths


def _fake_run(paths):
    calls: list[tuple[str, list[str]]] = []

    def run(cmd, *, label, **_kw):
        calls.append((label, cmd))
        if "secrets" in label:
            paths.secrets_yaml.write_text("secret\n")
        if label == "Gen config":
            for p in (paths.controlplane_yaml, paths.worker_yaml, paths.talosconfig):
                p.write_text("placeholder\n")
        if label.startswith("Render"):  # machineconfig patch emits the -o target
            Path(cmd[cmd.index("-o") + 1]).write_text("rendered\n")
        return None

    return run, calls


def _ready_cluster():
    cluster = default_cluster()
    cluster.name = "hetzner"
    cp, worker = cluster.nodes
    cp.ip, cp.gateway = "65.109.108.72", "65.108.123.1"
    worker.ip, worker.gateway = "65.109.108.73", "65.108.123.1"
    return cluster


def _node(name, role, ip, **overrides) -> Node:
    kw = dict(name=name, role=role, cpu=CpuVendor.AMD, ip=ip, gateway="65.108.123.1")
    kw.update(overrides)
    return Node(**kw)


def test_config_artifacts(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = _ready_cluster()
    cp, worker = cluster.nodes

    run, calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)

    s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)

    common = yaml.safe_load(paths.patch_common.read_text())
    assert common["machine"]["sysctls"]["user.max_user_namespaces"] == "11255"

    cp_cluster = yaml.safe_load(paths.patch_cp_cluster.read_text())
    assert cp_cluster["cluster"]["network"]["cni"]["name"] == "none"
    assert cp_cluster["cluster"]["proxy"]["disabled"] is True
    assert "machine" not in cp_cluster

    cp_patch = yaml.safe_load(paths.patch_for(cp).read_text())
    assert cp_patch["machine"]["install"]["disk"] == "/dev/nvme0n1"
    assert "cluster" not in cp_patch
    assert "network" not in cp_patch["machine"]  # host networking rides the net docs

    worker_patch = yaml.safe_load(paths.patch_for(worker).read_text())
    assert worker_patch["machine"]["install"]["disk"] == "/dev/nvme0n1"
    assert "network" not in worker_patch["machine"]

    for node in cluster.nodes:
        net = {d["kind"]: d for d in yaml.safe_load_all(paths.net_file(node).read_text())}
        assert set(net) == {"LinkAliasConfig", "LinkConfig", "ResolverConfig", "HostnameConfig"}
        assert net["HostnameConfig"]["hostname"] == node.name  # embed carries the hostname
        assert net["LinkConfig"]["addresses"] == [{"address": f"{node.ip}/32"}]

    gen = next(cmd for label, cmd in calls if label == "Gen config")
    assert "--with-secrets" in gen
    assert "v1.13.3" in gen and "1.36.1" in gen
    assert "--force" in gen
    endpoint = f"https://{cp.ip}:6443"
    assert endpoint in gen  # endpoint = first-CP IP
    assert gen[gen.index(endpoint) - 1] == cluster.name  # configured name is the positional
    sans = gen[gen.index("--additional-sans") + 1]
    assert cp.ip in sans  # certSANs cover the CPs present at gen time
    assert str(paths.secret) in gen  # rendered bases land in the secret dir

    # exactly one machineconfig patch per node, each deriving its own config
    renders = [cmd for label, cmd in calls if label.startswith("Render")]
    assert len(renders) == len(cluster.nodes)
    for node, cmd in zip(cluster.nodes, renders, strict=True):
        assert str(paths.base_for(node)) in cmd
        assert f"@{paths.patch_for(node)}" in cmd
        assert f"@{paths.net_file(node)}" in cmd
        assert cmd[cmd.index("-o") + 1] == str(paths.config_for(node))
        assert paths.config_for(node).exists()


def test_link_alias_pinned_to_uplink_mac(tmp_path, monkeypatch):
    """A discovered uplink MAC pins the alias; unpinned nodes keep the structural match."""
    paths = _paths(tmp_path)
    cluster = _ready_cluster()
    cp, worker = cluster.nodes
    cp.link_mac = "9c:6b:00:e7:84:16"

    run, _calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)
    s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)

    cp_net = {d["kind"]: d for d in yaml.safe_load_all(paths.net_file(cp).read_text())}
    match = cp_net["LinkAliasConfig"]["selector"]["match"]
    assert match == 'mac(link.permanent_addr) == "9c:6b:00:e7:84:16"'

    worker_net = {d["kind"]: d for d in yaml.safe_load_all(paths.net_file(worker).read_text())}
    assert worker_net["LinkAliasConfig"]["selector"]["match"] is True


def test_resolved_install_overrides_declarative_in_render(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = _ready_cluster()
    cp = cluster.nodes[0]
    cp.resolved_install = InstallTarget(selector={"wwid": "eui.0a"})  # live boot-disk pin

    run, _calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)

    s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)

    cp_patch = yaml.safe_load(paths.patch_for(cp).read_text())
    assert cp_patch["machine"]["install"]["diskSelector"] == {"wwid": "eui.0a"}
    # base gen-config disk:/dev/sda stripped so only the selector survives the merge
    assert cp_patch["machine"]["install"]["disk"] == {"$patch": "delete"}


def test_discovery_patch_present_only_with_vswitch(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = _ready_cluster()
    cluster.vswitch = Vswitch(vlan_id=4000, subnet="10.10.0.0/24")
    cp, worker = cluster.nodes
    cp.vlan_ip, worker.vlan_ip = "10.10.0.1", "10.10.0.100"

    run, calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)
    s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)

    disco = yaml.safe_load(paths.patch_discovery.read_text())
    assert disco["cluster"]["discovery"]["enabled"] is False
    gen = next(cmd for label, cmd in calls if label == "Gen config")
    assert f"@{paths.patch_discovery}" in gen  # applied to all nodes


def test_no_discovery_patch_without_vswitch(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = _ready_cluster()  # default_cluster has no vSwitch

    run, calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)
    s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)

    assert not paths.patch_discovery.exists()
    gen = next(cmd for label, cmd in calls if label == "Gen config")
    assert f"@{paths.patch_discovery}" not in gen


def test_multi_cp_and_multi_worker_render_distinct_configs(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = Cluster(
        name="hetzner",
        nodes=[
            _node("cp1", NodeRole.CONTROLPLANE, "65.109.108.72"),
            _node("cp2", NodeRole.CONTROLPLANE, "65.109.108.73"),
            _node("w1", NodeRole.WORKER, "65.109.108.74"),
            _node("w2", NodeRole.WORKER, "65.109.108.75"),
        ],
    )

    run, calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)

    s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)  # no count error

    gen = next(cmd for label, cmd in calls if label == "Gen config")
    sans = gen[gen.index("--additional-sans") + 1]
    assert "65.109.108.72" in sans and "65.109.108.73" in sans  # both CPs in certSANs
    assert "65.109.108.74" not in sans  # workers excluded

    targets = [cmd[cmd.index("-o") + 1] for label, cmd in calls if label.startswith("Render")]
    assert len(targets) == len(set(targets)) == 4  # one distinct config per node
    cp_base, worker_base = str(paths.controlplane_yaml), str(paths.worker_yaml)
    bases = [cmd[3] for label, cmd in calls if label.startswith("Render")]  # base is arg[3]
    assert bases == [cp_base, cp_base, worker_base, worker_base]


def test_secrets_minted_once(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = _ready_cluster()

    run, calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)

    ctx = Ctx(cluster=cluster, paths=paths)
    s1_config.run_config(ctx, None)
    s1_config.run_config(ctx, None)

    assert sum(1 for label, _ in calls if "secrets" in label) == 1


def test_profile_selector_and_extra_patch_flow(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = _ready_cluster()
    worker = cluster.nodes[1]
    worker.profile = ProfileKey.DB
    worker.install = InstallTarget(selector={"size": "<= 4TB"})
    patch_file = tmp_path / "extra.yaml"
    patch_file.write_text("machine: {}\n")
    worker.extra_patches = [str(patch_file)]

    run, calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)

    s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)

    worker_patch = yaml.safe_load(paths.patch_for(worker).read_text())
    assert worker_patch["machine"]["install"]["diskSelector"] == {"size": "<= 4TB"}
    assert worker_patch["machine"]["kernel"]["modules"] == [{"name": "dm_mod"}]
    mounts = {m["destination"] for m in worker_patch["machine"]["kubelet"]["extraMounts"]}
    assert "/run/topolvm" in mounts

    cp_patch = yaml.safe_load(paths.patch_for(cluster.nodes[0]).read_text())
    assert "kernel" not in cp_patch["machine"]  # generic CP must stay clean

    # extra patches ride the per-node machineconfig patch, not gen config
    worker_render = next(cmd for label, cmd in calls if label == "Render worker1 config")
    assert f"@{patch_file}" in worker_render
    gen = next(cmd for label, cmd in calls if label == "Gen config")
    assert f"@{patch_file}" not in gen


def test_missing_extra_patch_errors(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    cluster = _ready_cluster()
    cluster.nodes[1].extra_patches = [str(tmp_path / "nope.yaml")]

    run, _calls = _fake_run(paths)
    monkeypatch.setattr(s1_config, "run", run)

    with pytest.raises(s1_config.StageError):
        s1_config.run_config(Ctx(cluster=cluster, paths=paths), None)
