"""Workload-shaped, hardware-agnostic config fragments keyed by ProfileKey.

A profile fixes the workload bits - kernel modules, kubelet mounts, genuine node
sysctls, node labels - so picking `db`/`storage` yields a complete node with no
external files. The OS install target is the operator's per-run answer, so `db`
works on any DB box and `storage` on any Ceph box without code change. Data and
OSD disks are never referenced here; TopoLVM/Rook claim them clean post-bootstrap.
Genuine per-deployment overrides ride bring-your-own --config-patch files.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import TOPOLVM_MOUNT
from .model import ProfileKey


@dataclass(frozen=True, slots=True)
class Profile:
    key: ProfileKey
    kernel_modules: tuple[str, ...] = ()
    kubelet_mounts: tuple[dict, ...] = ()
    sysctls: dict[str, str] = field(default_factory=dict)  # genuine node sysctls only
    node_labels: dict[str, str] = field(default_factory=dict)
    install_hint: str = ""


PROFILES: dict[ProfileKey, Profile] = {
    ProfileKey.GENERIC: Profile(
        key=ProfileKey.GENERIC,
        install_hint="OS on the install disk; no workload-specific kernel/mount tuning.",
    ),
    ProfileKey.DB: Profile(
        key=ProfileKey.DB,
        kernel_modules=("dm_mod",),  # thick lvm; in base, load explicit
        kubelet_mounts=(TOPOLVM_MOUNT,),
        node_labels={"workload": "db"},  # CNPG/TopoLVM nodeSelector target
        install_hint=(
            "OS on ONE NVMe; leave the other data NVMe(s) untouched for TopoLVM's VG. "
            "Prefer a diskSelector size range (e.g. size '<= 4TB') over a /dev path."
        ),
    ),
    ProfileKey.STORAGE: Profile(
        key=ProfileKey.STORAGE,
        kernel_modules=("rbd",),  # ceph block, in-tree on the v1.13 kernel
        sysctls={"fs.aio-max-nr": "1048576"},  # bluestore async io contexts
        node_labels={"workload": "storage"},  # Rook nodeSelector target
        install_hint=(
            "OS on an NVMe; leave the 2nd NVMe (BlueStore DB/WAL) and HDDs raw for Rook. "
            "Prefer a diskSelector (e.g. type nvme). nofile is a pod limit, not a sysctl - "
            "set it in Rook."
        ),
    ),
}


def profile_for(key: ProfileKey) -> Profile:
    return PROFILES[key]
