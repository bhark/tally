"""Artifact paths, split by sensitivity.

`defn` (default ./talos) holds the git-safe definition: tally.yaml plus the
generated patch/hostname/net fragments - committable, no secrets. `secret`
(default ./talos-secrets) holds everything that embeds CA keys or tokens - the
rendered configs, talosconfig, kubeconfig, secrets bundle - plus build images.
The secret dir is chmod 0700, its files 0600, and a .gitignore beside it keeps
it out of git wherever tally runs.
"""

from __future__ import annotations

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

    # per-node fragments, node- prefixed so they can't collide with the singular
    # fragments or role bases for any DNS-label node name
    def patch_for(self, node: Node) -> Path:
        return self.defn / f"node-{node.name}-patch.yaml"

    # standalone networking docs (alias/link/resolver/hostname) - see s1_config.net_docs
    def net_file(self, node: Node) -> Path:
        return self.defn / f"node-{node.name}-net.yaml"

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

    def out_dir(self, node: Node) -> Path:
        return self.secret / "_out" / node.name

    def image(self, node: Node) -> Path:
        return self.out_dir(node) / f"{node.name}.raw.zst"

    def config_for(self, node: Node) -> Path:
        return self.secret / f"node-{node.name}.yaml"

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
