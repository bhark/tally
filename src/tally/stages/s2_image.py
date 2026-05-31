"""Stage 2 - build the per-node custom metal image (imager)."""

from __future__ import annotations

import shutil

from ..constants import EXTENSIONS_REF, IMAGER_REF
from ..model import Node, Stage
from ..runner import run
from .base import Ctx, StageDef, StageError


def _resolve_ucode(node: Node) -> str:
    # the one place shell is genuinely needed: a pipeline whose multi-line match
    # is truncated to a single ref by `head -1` so it can't corrupt the arg.
    pipeline = (
        f"crane export {EXTENSIONS_REF} | tar x -O image-digests "
        f"| grep '/{node.cpu}-ucode' | head -1"
    )
    res = run(["bash", "-c", pipeline], label=f"Resolve {node.cpu}-ucode extension")
    ref = res.out
    if not ref:
        raise StageError(f"No {node.cpu}-ucode extension found in {EXTENSIONS_REF}")
    return ref


def run_image(ctx: Ctx, node: Node | None) -> None:
    assert node is not None
    paths = ctx.paths
    net_src = paths.net_file(node)
    if not net_src.exists():
        raise StageError(f"{net_src.name} missing; run config first")

    out_dir = paths.out_dir(node)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(net_src, out_dir / net_src.name)

    ucode = _resolve_ucode(node)

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{out_dir}:/out",
        IMAGER_REF,
        "metal",
        "--arch",
        "amd64",
        "--system-extension-image",
        ucode,
    ]
    if node.nic_firmware_ext:
        cmd += ["--system-extension-image", node.nic_firmware_ext]
    cmd += ["--embedded-config-path", f"/out/{net_src.name}"]

    run(cmd, label=f"Build metal image for {node.name}")

    produced = out_dir / "metal-amd64.raw.zst"
    if not produced.exists():
        contents = ", ".join(p.name for p in out_dir.iterdir()) or "(empty)"
        raise StageError(f"Imager did not emit metal-amd64.raw.zst; out dir: {contents}")
    produced.replace(paths.image(node))


STAGE = StageDef(
    key=Stage.IMAGE,
    title="Build custom metal image",
    run=run_image,
)
