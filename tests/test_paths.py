from __future__ import annotations

import shutil
import stat

from tally.model import default_cluster
from tally.paths import Paths


def _paths(tmp_path) -> Paths:
    return Paths(tmp_path / "talos", tmp_path / "talos-secrets")


def test_gitignore_excludes_secrets_and_files_hardened(tmp_path):
    p = _paths(tmp_path)
    p.ensure()

    gitignore = (tmp_path / ".gitignore").read_text().splitlines()
    assert "talos-secrets/" in gitignore
    assert stat.S_IMODE((tmp_path / "talos-secrets").stat().st_mode) == 0o700

    cp = default_cluster().nodes[0]
    p.secrets_yaml.write_text("secret")
    p.talosconfig.write_text("cfg")
    p.config_for(cp).write_text("node config")  # per-node configs locked too
    p.harden()
    assert stat.S_IMODE(p.secrets_yaml.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.talosconfig.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.config_for(cp).stat().st_mode) == 0o600


def test_gitignore_appends_without_duplicating(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    p = _paths(tmp_path)
    p.ensure()
    p.ensure()  # idempotent - must not append twice

    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines.count("talos-secrets/") == 1
    assert "node_modules/" in lines


def test_sensitivity_split(tmp_path):
    p = _paths(tmp_path)
    cp = default_cluster().nodes[0]
    # git-safe definition under the defn dir
    assert p.tally_yaml.parent == tmp_path / "talos"
    assert p.patch_common.parent == tmp_path / "talos"
    assert p.patch_for(cp).parent == tmp_path / "talos"
    assert p.net_file(cp).parent == tmp_path / "talos"
    # secret-bearing + build artifacts under the secret dir
    assert p.secrets_yaml.parent == tmp_path / "talos-secrets"
    assert p.controlplane_yaml.parent == tmp_path / "talos-secrets"  # role base


def test_config_for_is_per_node_under_secret(tmp_path):
    p = _paths(tmp_path)
    cp, worker = default_cluster().nodes
    assert p.config_for(cp) != p.config_for(worker)
    assert p.config_for(cp).name == "node-cp1.yaml"
    assert p.config_for(cp).parent == tmp_path / "talos-secrets"
    assert p.base_for(cp) == p.controlplane_yaml
    assert p.base_for(worker) == p.worker_yaml


def test_image_path_is_per_node_under_secret(tmp_path):
    p = _paths(tmp_path)
    cp, worker = default_cluster().nodes
    assert p.image(cp) != p.image(worker)
    assert p.image(cp).name == "cp1.raw.zst"
    assert p.out_dir(cp).parent.name == "_out"
    assert (tmp_path / "talos-secrets") in p.image(cp).parents


def test_node_artifacts_enumerates_four_paths(tmp_path):
    p = _paths(tmp_path)
    defn, secret = tmp_path / "talos", tmp_path / "talos-secrets"
    patch, net, config, out = p.node_artifacts("cp1")
    assert (patch.parent, patch.name) == (defn, "node-cp1-patch.yaml")
    assert (net.parent, net.name) == (defn, "node-cp1-net.yaml")
    assert (config.parent, config.name) == (secret, "node-cp1.yaml")
    assert (out.parent, out.name) == (secret / "_out", "cp1")


def test_purge_node_removes_existing_and_skips_absent(tmp_path):
    p = _paths(tmp_path)
    p.ensure()
    cp = default_cluster().nodes[0]
    p.patch_for(cp).write_text("patch")
    p.config_for(cp).write_text("config")
    p.out_dir(cp).mkdir(parents=True)
    p.image(cp).write_text("image")  # file inside _out/<name>, removed via rmtree
    # net_file deliberately absent

    removed = set(p.purge_node(cp.name))

    assert removed == {p.patch_for(cp), p.config_for(cp), p.out_dir(cp)}
    assert p.net_file(cp) not in removed
    assert not p.patch_for(cp).exists()
    assert not p.config_for(cp).exists()
    assert not p.out_dir(cp).exists()


def test_purge_node_tolerates_oserror(tmp_path, monkeypatch):
    p = _paths(tmp_path)
    p.ensure()
    cp = default_cluster().nodes[0]
    p.patch_for(cp).write_text("patch")
    p.out_dir(cp).mkdir(parents=True)

    def _boom(_):
        raise OSError("root-owned docker artifact")

    monkeypatch.setattr(shutil, "rmtree", _boom)

    removed = p.purge_node(cp.name)

    assert removed == [p.patch_for(cp)]  # file removed; undeletable dir skipped
    assert p.out_dir(cp).exists()
