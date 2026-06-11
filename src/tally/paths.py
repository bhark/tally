"""Artifact paths, split by sensitivity.

`defn` (default ./talos) holds the git-safe definition: tally.yaml plus the
generated patch/hostname/net fragments - committable, no secrets. `secret`
(default ./talos-secrets) holds everything that embeds CA keys or tokens - the
rendered configs, talosconfig, kubeconfig, secrets bundle - plus build images.
The secret dir is chmod 0700, its files 0600, and a .gitignore beside it keeps
it out of git wherever tally runs.
"""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .model import Node, NodeRole


@dataclass(slots=True)
class Paths:
    defn: Path  # git-safe definition + fragments
    secret: Path  # secret-bearing configs + build scratch

    # git-safe definition (defn) -------------------------------------------
    @property
    def tally_yaml(self) -> Path:
        return self.defn / "tally.yaml"

    @property
    def patch_common(self) -> Path:
        return self.defn / "patch-common.yaml"

    # CP-role cluster section only (cni/proxy/scheduling); merged into the CP base
    @property
    def patch_cp_cluster(self) -> Path:
        return self.defn / "patch-cp-cluster.yaml"

    # cluster-wide TimeSyncConfig (Hetzner NTP); merged into every role base at gen time
    @property
    def patch_time(self) -> Path:
        return self.defn / "patch-timesync.yaml"

    # cluster-wide discovery disable (vSwitch only); keeps trustd/apid off the firewalled public IP
    @property
    def patch_discovery(self) -> Path:
        return self.defn / "patch-discovery.yaml"

    # name-keyed so the removal flow can enumerate by name without a Node
    def _patch_for(self, name: str) -> Path:
        return self.defn / f"node-{name}-patch.yaml"

    def _net_file(self, name: str) -> Path:
        return self.defn / f"node-{name}-net.yaml"

    # per-node fragments, node- prefixed so they can't collide with the singular
    # fragments or role bases for any DNS-label node name
    def patch_for(self, node: Node) -> Path:
        return self._patch_for(node.name)

    # standalone networking docs (alias/link/resolver/hostname) - see s1_config.net_docs
    def net_file(self, node: Node) -> Path:
        return self._net_file(node.name)

    # secret-bearing (secret) ----------------------------------------------
    @property
    def secrets_yaml(self) -> Path:
        return self.secret / "secrets.yaml"

    @property
    def talosconfig(self) -> Path:
        return self.secret / "talosconfig"

    @property
    def kubeconfig(self) -> Path:
        return self.secret / "kubeconfig"

    # role bases: output of `gen config`, patched per-node into config_for(node)
    @property
    def controlplane_yaml(self) -> Path:
        return self.secret / "controlplane.yaml"

    @property
    def worker_yaml(self) -> Path:
        return self.secret / "worker.yaml"

    def base_for(self, node: Node) -> Path:
        return self.controlplane_yaml if node.role is NodeRole.CONTROLPLANE else self.worker_yaml

    def _out_dir(self, name: str) -> Path:
        return self.secret / "_out" / name

    def _config_for(self, name: str) -> Path:
        return self.secret / f"node-{name}.yaml"

    def out_dir(self, node: Node) -> Path:
        return self._out_dir(node.name)

    def image(self, node: Node) -> Path:
        return self.out_dir(node) / f"{node.name}.raw.zst"

    def config_for(self, node: Node) -> Path:
        return self._config_for(node.name)

    def node_artifacts(self, name: str) -> list[Path]:
        """every per-node artifact path (existing or not): patch, net, config, _out/<name>."""
        return [
            self._patch_for(name),
            self._net_file(name),
            self._config_for(name),
            self._out_dir(name),
        ]

    def purge_node(self, name: str) -> list[Path]:
        """remove existing per-node artifacts; return those actually removed."""
        removed: list[Path] = []
        for p in self.node_artifacts(name):
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                else:
                    continue
            except OSError:
                # _out/<name> may hold root-owned docker artifacts; same caveat as harden()
                continue
            removed.append(p)
        return removed

    # lifecycle ------------------------------------------------------------
    def ensure(self) -> None:
        self.defn.mkdir(parents=True, exist_ok=True)
        self.secret.mkdir(parents=True, exist_ok=True)
        self.secret.chmod(stat.S_IRWXU)
        self._ignore_secrets()

    def harden(self) -> None:
        """chmod 0700 the secret dir and 0600 every top-level file in it.

        Covers role bases, all per-node configs, secrets/talosconfig/kubeconfig
        without an enumerated list. _out/ images stay in a subdir, untouched -
        avoids chmod'ing possibly root-owned docker artifacts.
        """
        if not self.secret.exists():
            return
        self.secret.chmod(stat.S_IRWXU)
        for p in self.secret.iterdir():
            if p.is_file():
                p.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _ignore_secrets(self) -> None:
        entry = self.secret.name + "/"
        gitignore = self.secret.parent / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""
        if entry in existing.splitlines():
            return
        prefix = "" if existing == "" or existing.endswith("\n") else "\n"
        with gitignore.open("a") as fh:
            fh.write(f"{prefix}{entry}\n")
