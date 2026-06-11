"""Stage 3 - rescue + build/write image.

The operator activates Hetzner rescue and resets the box themselves (Robot UI), then
confirms here; tally polls until rescue ssh answers, opens an ssh master connection,
verifies it's a rescue ramdisk (not the installed OS), then drives the write: pin the
uplink MAC, (re)build the image, probe the target disk, upload, wipe every disk, dd the
image, register the UEFI boot entry, reboot. ssh handles auth.

The image is built here, not as a standalone earlier step: its embedded net docs depend
on the uplink MAC, a hardware fact first knowable over this very ssh session.
"""

from __future__ import annotations

import shutil

from griphtui import Option, confirm, error, is_cancel, note, select, text, warn

from .. import definition, probe, remote
from ..model import Node, Stage
from ..ui import gap
from .base import Ctx, StageCancelled, StageDef
from .s1_config import render_node
from .s2_image import run_image

_BOOT_TIMEOUT = 900  # some firmware walks every network-boot option before the disk entry
_SSH_PORT = 22
_RESCUE_TIMEOUT = 300  # rescue ramdisk boot can lag well behind the operator's confirm


def _require(value):
    if is_cancel(value):
        raise StageCancelled("Backed out of rescue")
    return value


def _nonempty(label: str):
    def validate(v: str) -> str | None:
        return None if v.strip() else f"{label} is required"

    return validate


def run_rescue(ctx: Ctx, node: Node | None) -> None:
    assert node is not None
    note(
        [
            "In the Hetzner Robot panel for this server:",
            "  1. Rescue tab → activate Linux (64-bit). Note the root password.",
            "  2. Reset tab → trigger a hardware reset to boot rescue.",
            "  3. Confirm below once triggered - we wait for it to come up.",
        ],
        title=f"Rescue {node.name}",
    )
    if not _require(
        confirm("Activated rescue (noted root password) and triggered the reset?", default=False)
    ):
        raise StageCancelled("Rescue not started")

    session = _open(node)
    try:
        try:
            remote.verify_rescue(session)
        except remote.RemoteError as e:
            raise StageCancelled(str(e)) from e
        _ensure_image(ctx, node, rebuild=_pin_uplink(ctx, node, session))
        disk = _resolve_disk(session, node)
        gap()
        if not _require(
            confirm(
                f"Write {node.name} → {disk} on {session.target}? (wipes ALL disks)",
                default=False,
            )
        ):
            disk = _require(
                text("dd target disk", default=disk, validate=_nonempty("disk"))
            ).strip()

        remote_image = f"/tmp/{node.name}.raw.zst"
        remote.upload(
            session, str(ctx.paths.image(node)), remote_image, f"Uploading image to {node.name}"
        )
        remote.exec(session, _write_script(remote_image, disk), f"Writing image to {disk}")
        remote.exec(session, "reboot", f"Rebooting {node.name}", check=False)
    finally:
        remote.disconnect(session)

    _await_talos(ctx, node)


def _pin_uplink(ctx: Ctx, node: Node, session: remote.Session) -> bool:
    """Pin the link alias to the rescue uplink's MAC. True ⇒ pin changed (image is stale)."""
    mac = remote.uplink_mac(session)
    if not mac:
        warn(f"{node.name}: uplink MAC unresolved; keeping structural link match")
        return False
    if mac == node.link_mac:
        return False
    node.link_mac = mac
    definition.save(ctx.cluster, ctx.paths.defn)
    note(f"{node.name}: link alias pinned to uplink mac {mac}")
    return True


def _ensure_image(ctx: Ctx, node: Node, *, rebuild: bool) -> None:
    image = ctx.paths.image(node)
    if image.exists() and not rebuild:
        note(f"Reusing existing image {image.name}")
        return
    render_node(ctx, node)  # net docs must embed the pin before the imager snapshots them
    run_image(ctx, node)


def _open(node: Node) -> remote.Session:
    """Wait for rescue ssh to answer, then authenticate the master connection.

    Polls tcp/22 first - the box may still be booting when the operator confirms - then does the
    one interactive auth (the spinner-polled probe can't carry ssh's passphrase/password prompt).
    On timeout or auth failure, drops to a prompt defaulting to the same IP: Enter re-polls, an
    edit retargets a failover/additional IP, Esc cancels. So a slow/wrong host guides, not aborts.
    """
    host = node.ip
    while True:
        label = f"Waiting for rescue ssh on {host} (≤{_RESCUE_TIMEOUT // 60}m)"
        if probe.wait_port(host, _SSH_PORT, label, timeout=_RESCUE_TIMEOUT):
            try:
                return remote.connect(host)
            except remote.RemoteError as e:
                error(str(e))
        else:
            error(f"{host}:{_SSH_PORT} not reachable after {_RESCUE_TIMEOUT // 60}m")
        host = _require(
            text("Rescue host IP", default=host, validate=_nonempty("rescue host"))
        ).strip()


def _resolve_disk(session: remote.Session, node: Node) -> str:
    try:
        return remote.select_disk(node.install, remote.probe_disks(session))
    except remote.RemoteError as e:
        raise StageCancelled(str(e)) from e


def _write_script(image: str, disk: str) -> str:
    # wipe EVERY disk first so no stale OS wins the boot order; pipefail makes a failed
    # zstd|dd abort the script (and surface) rather than silently leaving a half-written disk
    #
    # the NVRAM entry is required, not a nicety: UEFI only mandates the \EFI\BOOT fallback
    # for removable media, and some firmware never tries it on fixed disks. appended LAST in
    # BootOrder so Hetzner's network-boot interception (rescue activation) still wins.
    return f"""set -eo pipefail
mdadm --stop --scan 2>/dev/null || true
for d in /dev/nvme[0-9]n[0-9] /dev/sd[a-z]; do
  [ -b "$d" ] || continue
  wipefs -a "$d" || true
  sgdisk --zap-all "$d" 2>/dev/null || true
  blkdiscard -f "$d" 2>/dev/null || dd if=/dev/zero of="$d" bs=1M count=64 oflag=direct
done
zstd -d -c {image} | dd of={disk} bs=4M iflag=fullblock status=progress oflag=direct
sync
if [ -d /sys/firmware/efi ]; then
  partprobe {disk} 2>/dev/null || true
  for b in $(efibootmgr | grep ' talos' | cut -c5-8); do efibootmgr -q -b "$b" -B; done
  order=$(efibootmgr | grep '^BootOrder:' | cut -d' ' -f2 || true)
  efibootmgr -q -c -d {disk} -p 1 -L talos -l '\\EFI\\BOOT\\BOOTX64.EFI'
  new=$(efibootmgr | grep ' talos' | cut -c5-8 | head -1)
  if [ -n "$order" ] && [ -n "$new" ]; then efibootmgr -q -o "$order,$new"; fi
fi"""


def _await_talos(ctx: Ctx, node: Node) -> None:
    """Poll the insecure Talos API until the node answers in maintenance mode.

    A slow bare-metal POST can outlast one window, so a timeout loops back to re-poll rather
    than abort: re-running would re-classify the node as absent and re-image it (see _open).
    """
    if not node.ip or shutil.which("talosctl") is None:
        note("Cannot probe; assuming the node comes up, apply will confirm")
        return
    cmd = ["talosctl", "-n", node.ip, "get", "disks", "--insecure"]
    while True:
        label = f"Waiting for {node.name} to boot into Talos (≤{_BOOT_TIMEOUT // 60}m)"
        if probe.wait_until(cmd, ctx.talos_env(), label, timeout=_BOOT_TIMEOUT):
            note(f"{node.name} up in maintenance mode → ready to apply")
            return
        gap()
        choice = _require(
            select(
                f"{node.name} unreachable after {_BOOT_TIMEOUT // 60}m",
                [
                    Option(label="Keep waiting", value="wait"),
                    Option(label="Continue anyway (apply will confirm)", value="go"),
                    Option(label="Abort", value="abort"),
                ],
            )
        )
        if choice == "go":
            return
        if choice == "abort":
            raise StageCancelled("Node did not reach maintenance mode")


STAGE = StageDef(
    key=Stage.RESCUE,
    title="Rescue + write image",
    run=run_rescue,
)
