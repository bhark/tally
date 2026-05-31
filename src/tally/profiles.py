"""Workload-shaped, hardware-agnostic config fragments keyed by ProfileKey.

A profile fixes only the workload bits - kernel modules, kubelet mounts, genuine
node sysctls. The OS install target is the operator's per-run answer, so `db`
works on any DB box and `storage` on any Ceph box without code change. Data and
OSD disks are never referenced here; TopoLVM/Rook claim them clean post-bootstrap.
Anything a profile doesn't cover comes from per-node extra --config-patch files
(presets/ holds editable examples).
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
        install_hint=(
            "OS on ONE NVMe; leave the other data NVMe(s) untouched for TopoLVM's VG. "
            "Prefer a diskSelector size range (e.g. size '<= 4TB') over a /dev path."
        ),
    ),
    ProfileKey.STORAGE: Profile(
        key=ProfileKey.STORAGE,
        kernel_modules=("rbd",),  # ceph block, in-tree on the v1.13 kernel
        install_hint=(
            "OS on an NVMe; leave the 2nd NVMe (BlueStore DB/WAL) and HDDs raw for Rook. "
            "Prefer a diskSelector (e.g. type nvme). Genuine ceph sysctls go via an extra "
            "patch (see presets/storage.yaml); nofile is a pod limit, not a sysctl."
        ),
    ),
}


def profile_for(key: ProfileKey) -> Profile:
    return PROFILES[key]
