"""Entry point: parse args, load or seed the cluster, run the wizard."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__, definition
from .model import Cluster
from .paths import Paths
from .stages import Ctx
from .wizard import run as run_wizard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tally", description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("talos"),
        help="git-safe definition + generated manifests (default: ./talos)",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        default=None,
        help="secret-bearing configs + build artifacts (default: <dir>-secrets)",
    )
    parser.add_argument("--debug", action="store_true", help="verbose errors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    defn = args.dir.resolve()
    secret = (args.secrets or args.dir.parent / f"{args.dir.name}-secrets").resolve()
    paths = Paths(defn=defn, secret=secret)
    paths.ensure()

    cluster = definition.load(defn) or Cluster(nodes=[])
    ctx = Ctx(cluster=cluster, paths=paths, debug=args.debug)
    run_wizard(ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
