"""Single source of truth: pinned versions and fixed Hetzner/Cilium facts.

Bump Talos by editing TALOS_VERSION here; everything downstream is derived.
"""

from __future__ import annotations

TALOS_VERSION = "v1.13.3"
K8S_VERSION = "1.36.1"  # shipped by v1.13.3; pinned into gen config for reproducibility

IMAGER_REF = f"ghcr.io/siderolabs/imager:{TALOS_VERSION}"
EXTENSIONS_REF = f"ghcr.io/siderolabs/extensions:{TALOS_VERSION}"

CILIUM_VERSION = "1.19.4"
HELM_REPO_NAME = "cilium"
HELM_REPO_URL = "https://helm.cilium.io/"

K8S_API_PORT = 6443  # kube-apiserver; talos api is 50000, not this
KUBEPRISM_PORT = 7445

# fallback boot disk when none is specified; re-pinned post-boot from Talos' SystemDisk
DEFAULT_INSTALL_DISK = "/dev/nvme0n1"

# Hetzner vSwitch: MTU is hard-capped at 1400; VLAN IDs are restricted to 4000-4091.
# The vSwitch is pure L2 - Hetzner assigns no addressing, so the subnet is operator's
# choice; default to an RFC1918 /24 the operator can accept as-is.
VSWITCH_MTU_DEFAULT = 1400
VSWITCH_SUBNET_DEFAULT = "10.10.0.0/24"
VLAN_ID_MIN, VLAN_ID_MAX = 4000, 4091

# vlan_ip auto-assignment within the one subnet: CPs from .1, workers from .100, so the
# role reads off the address. assumes a /24 or larger (the default and Hetzner norm).
VSWITCH_CP_HOST_START = 1
VSWITCH_WORKER_HOST_START = 100

NAMESERVERS = [
    "185.12.64.1",
    "185.12.64.2",
    "2a01:4ff:ff00::add:1",
    "2a01:4ff:ff00::add:2",
]

# Hetzner filters outbound NTP to off-network servers, so the Talos default
# (time.cloudflare.com) times out and etcd's time-sync gate never clears; their
# on-network NTP answers like the resolvers above.
NTP_SERVERS = [
    "ntp1.hetzner.de",
    "ntp2.hetzner.com",
    "ntp3.hetzner.net",
]

USERNS_SYSCTL = ("user.max_user_namespaces", "11255")

# lvmd<->node socket for TopoLVM; needs host mount propagation (rshared).
# carried by the db profile, never referenced for data disks.
TOPOLVM_MOUNT = {
    "destination": "/run/topolvm",
    "type": "bind",
    "source": "/run/topolvm",
    "options": ["bind", "rshared", "rw"],
}

# cilium helm --set list, kept as data. k8sServiceHost stays localhost (KubePrism).
CILIUM_HELM_SETS = [
    "ipam.mode=kubernetes",
    "kubeProxyReplacement=true",
    "securityContext.capabilities.ciliumAgent="
    "{CHOWN,KILL,NET_ADMIN,NET_RAW,IPC_LOCK,SYS_ADMIN,SYS_RESOURCE,DAC_OVERRIDE,FOWNER,SETGID,SETUID}",
    "securityContext.capabilities.cleanCiliumState={NET_ADMIN,SYS_ADMIN,SYS_RESOURCE}",
    "cgroup.autoMount.enabled=false",
    "cgroup.hostRoot=/sys/fs/cgroup",
    "k8sServiceHost=localhost",
    f"k8sServicePort={KUBEPRISM_PORT}",
]

# zstd lives on the rescue host, not the operator workstation, so it is not here.
REQUIRED_TOOLS = ("talosctl", "docker", "crane", "helm", "kubectl")
