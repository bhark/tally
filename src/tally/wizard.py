"""Stateless reconciler driver: survey live state, show the diff, converge on demand.

No progress is stored. Desired topology comes from tally.yaml; observed state comes from a
live survey (observe.snapshot); every run recomputes the diff (reconcile.compute) and the
menu renders it. "Tally Up!" walks the plan: bring up absent nodes (image + rescue + apply),
apply to maintenance nodes, converge joined nodes (dry-run, apply only on real drift),
bootstrap + Cilium when the live state needs them, then remove orphans (nodes live in k8s but
absent from tally.yaml) after the new members have joined. Idempotency comes from the on-disk
artifacts plus talosctl/helm being declarative - never from stored flags.
"""

from __future__ import annotations

import shutil
import traceback

from griphtui import (
    Option,
    error,
    intro,
    is_cancel,
    note,
    outro,
    select,
    spinner,
    success,
    warn,
)

from . import definition, disk, observe, probe, prompts, removal, screens, uplink
from .menu import Exit
from .model import InstallTarget, Node, Stage
from .preflight import check_tools, missing_for_stage, summary_lines
from .reconcile import Action, ActionKind, compute
from .runner import CommandError
from .stages import BY_KEY, Ctx, StageCancelled, StageDef, StageError
from .stages.s4_apply import DryRunVerdict, dry_run
from .ui import gap, node_line

_CONFIG = BY_KEY[Stage.CONFIG]
_RESCUE = BY_KEY[Stage.RESCUE]
_APPLY = BY_KEY[Stage.APPLY]
_BOOTSTRAP = BY_KEY[Stage.BOOTSTRAP]
_CILIUM = BY_KEY[Stage.CILIUM]

_APPLY_VERIFY_TIMEOUT = 900  # apply → install → reboot → secure API; firmware may walk PXE first
_WORKER_READY_TIMEOUT = 300  # worker registers + Cilium schedules → node Ready


def run(ctx: Ctx) -> None:
    intro("Tally: Talos on Hetzner reconciler")
    _show_preflight()
    state = screens.ScreenState(
        ctx=ctx, snap=observe.snapshot(ctx.cluster, ctx.paths, ctx.talos_env())
    )
    while True:
        result = screens.main_menu(state).run()
        if not isinstance(result, Exit):  # Back/Esc at the root ⇒ quit
            outro("Definition saved in tally.yaml")
            return
        try:
            _bring_up(ctx, state.snap)
        except StageCancelled as e:
            warn(str(e))
            note("Re-run Tally Up to continue from the artifacts on disk")
        except StageError as e:
            error(str(e))
            if ctx.debug:
                note(traceback.format_exc(), title="Traceback")
        state.snap = observe.snapshot(ctx.cluster, ctx.paths, ctx.talos_env())  # re-entry refresh


# bring-up ------------------------------------------------------------------


def _bring_up(ctx: Ctx, snap: observe.Snapshot) -> None:
    cluster_up = ctx.paths.kubeconfig.exists()
    plan = compute(ctx.cluster, snap, cluster_up=cluster_up)
    _run_phase(ctx, _CONFIG, None)  # regen all per-node configs (idempotent)

    for action in plan.actions:
        if action.kind is not ActionKind.REMOVE:
            _run_node_action(ctx, action)

    if plan.bootstrap:
        gap()
        if prompts.ask("Bootstrap etcd now? once per cluster", default=True):
            _run_phase(ctx, _BOOTSTRAP, None)
        else:
            note("Skipped bootstrap")

    gap()
    if plan.cilium:
        _run_phase(ctx, _CILIUM, None)
    else:
        note("Cilium already installed → skipping")

    # removals trail bootstrap+cilium so new members join etcd before old ones leave
    removals = [a for a in plan.actions if a.kind is ActionKind.REMOVE]
    if removals:
        _remove_orphans(ctx, removals)

    _verify_workers_ready(ctx)


def _run_node_action(ctx: Ctx, action: Action) -> None:
    node = action.node
    assert node is not None
    gap()
    note(node_line(node), title=f"{action.kind}: {node.name}")
    if action.kind is ActionKind.BRING_UP:  # absent: image + rescue + apply
        _run_phase(ctx, _RESCUE, node)  # builds the image post-pin; ends reachable in maintenance
        _pin_and_render(ctx, node)
        _apply_and_verify(ctx, node)
    elif action.kind is ActionKind.APPLY:  # maintenance: pin + apply
        _pin_and_render(ctx, node)
        _apply_and_verify(ctx, node)
    elif action.kind is ActionKind.REAPPLY:  # configured pre-bootstrap: re-apply to converge drift
        _apply_and_verify(ctx, node)
    elif action.kind is ActionKind.CONVERGE:  # joined member of an up cluster
        _converge(ctx, node)


def _converge(ctx: Ctx, node: Node) -> None:
    """Joined node: dry-run, apply only on real drift, confirm a reboot.

    No pin/rescue - the maintenance API is gone, the rendered config already carries the
    persisted link_mac, and config just regenerated. A reboot-requiring apply is the
    operator's call; a no-reboot apply lands silently.
    """
    if not node.ip or not ctx.paths.talosconfig.exists() or shutil.which("talosctl") is None:
        return
    verdict, diff = dry_run(ctx, node)
    if verdict is DryRunVerdict.IN_SYNC:
        note(f"{node.name} in sync → skipping")
        return
    if verdict is DryRunVerdict.NO_REBOOT:
        _run_phase(ctx, _APPLY, node)
        success(f"{node.name} converged (no reboot)")
        return
    if diff:
        note(diff, title=f"{node.name} config diff")
    if prompts.ask(f"Apply to {node.name} with a reboot?", default=False):
        _apply_and_verify(ctx, node)
    else:
        warn(f"{node.name} apply skipped - node remains drifted")


def _remove_orphans(ctx: Ctx, removals: list[Action]) -> None:
    missing = missing_for_stage(Stage.REMOVE)
    if missing:
        warn(f"Skipping orphan removal: missing tools ({', '.join(missing)})")
        return
    env = ctx.talos_env()
    orphans = [a.orphan for a in removals if a.orphan is not None]
    removal.repoint_endpoints(ctx.cluster, env)  # operator hop, off any orphan endpoint
    removal.protect_kubeconfig(ctx.cluster, ctx.paths, orphans)
    for action in removals:
        gap()
        _confirm_and_remove(ctx, action, env)


def _confirm_and_remove(ctx: Ctx, action: Action, env: dict[str, str]) -> None:
    orphan = action.orphan
    assert orphan is not None
    addrs = ", ".join(orphan.addresses) or "no known address"
    role = "control-plane" if orphan.control_plane else "worker"
    note(f"{orphan.name}  {role}  {addrs}", title="Orphan (live, not in tally.yaml)")
    if action.hint and action.hint.startswith("refusing"):
        warn(action.hint)
        return
    if action.hint:
        warn(action.hint)
    if not prompts.ask(f"Remove orphan {orphan.name} from the cluster?", default=False):
        note(f"Keeping {orphan.name}")
        return
    wipe_all = _ask_wipe_scope()
    if wipe_all is None:
        note(f"Removal of {orphan.name} aborted")
        return
    removal.remove_orphan(ctx.cluster, ctx.paths, env, orphan, wipe_all=wipe_all)


def _ask_wipe_scope() -> bool | None:
    """True ⇒ wipe data disks too; None ⇒ operator backed out. Always explicit - reset's own
    default wipes everything, so the choice is never inferred."""
    choice = select(
        "Wipe scope for the removed node",
        [
            Option(label="System disk only (keep data disks)", value="system"),
            Option(label="Full wipe including data disks", value="all"),
        ],
    )
    if is_cancel(choice):
        return None
    return choice == "all"


def _verify_workers_ready(ctx: Ctx) -> None:
    """Final gate: every worker must register Ready in k8s, else bring-up was not a success.

    Runs after bootstrap+Cilium (a worker can't be Ready before the API exists and CNI
    schedules). Skipped without a kubeconfig (bootstrap declined) or for an IP-less node.
    """
    if not ctx.paths.kubeconfig.exists() or shutil.which("kubectl") is None:
        return
    for node in ctx.cluster.workers:
        if not node.ip:
            continue
        cmd = [
            "kubectl",
            "--kubeconfig",
            str(ctx.paths.kubeconfig),
            "get",
            "node",
            node.name,
            "-o",
            "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
        ]
        label = f"Waiting for worker {node.name} Ready (≤{_WORKER_READY_TIMEOUT // 60}m)"
        if not probe.wait_for_value(
            cmd, ctx.talos_env(), label, "True", timeout=_WORKER_READY_TIMEOUT
        ):
            raise StageError(
                f"worker {node.name} never became Ready in Kubernetes "
                f"(trustd/apid join or CNI failure) - check the node console"
            )
        success(f"worker {node.name} joined and Ready")


def _apply_and_verify(ctx: Ctx, node: Node) -> None:
    """Apply the config, then wait for the node to reboot into it and answer the secure API.

    apply-config returns the instant maintenance-mode apid ACCEPTS the config - before the
    node installs, reboots, and brings apid back over mTLS. A worker whose apid can't get its
    trustd-signed cert (e.g. cross-node path blocked) wedges here silently; this turns that into
    a loud, located failure. Reaches the node over its public IP (admin-IP firewall allows).
    """
    _run_phase(ctx, _APPLY, node)
    if not node.ip or not ctx.paths.talosconfig.exists() or shutil.which("talosctl") is None:
        return  # pre-config / unvalidated node (tests); nothing to verify against yet
    cmd = ["talosctl", "-e", node.ip, "-n", node.ip, "version"]
    label = f"Waiting for {node.name} to reboot into config (≤{_APPLY_VERIFY_TIMEOUT // 60}m)"
    while not probe.wait_until(cmd, ctx.talos_env(), label, timeout=_APPLY_VERIFY_TIMEOUT):
        gap()
        choice = select(
            f"{node.name} not back on the secure API after {_APPLY_VERIFY_TIMEOUT // 60}m",
            [
                Option(label="Keep waiting", value="wait"),
                Option(label="Abort", value="abort"),
            ],
        )
        if is_cancel(choice) or choice == "abort":
            raise StageError(
                f"{node.name} did not answer the secure Talos API after apply "
                f"(apid/trustd or install/boot failure) - check the node console"
            )
    success(f"{node.name} rebooted into configured Talos")


def _pin_and_render(ctx: Ctx, node: Node) -> None:
    """Bind install to the booted disk and the link alias to the uplink, re-render on change.

    Both pins read live maintenance-mode state. The disk pin stays rendered-only (see
    disk.resolve_system_selector); the MAC pin persists to tally.yaml - the image embed
    depends on it, so reruns must see it before any rescue contact. A node with no clean
    signal keeps its declarative values; the operator is told.
    """
    before = node.resolved_install
    before_mac = node.link_mac
    selector = disk.resolve_system_selector(node.ip, ctx.talos_env())
    if selector:
        node.resolved_install = InstallTarget(selector=selector)
        note(f"{node.name}: install pinned to boot disk → {node.resolved_install.describe()}")
    else:
        warn(f"{node.name}: boot disk unresolved; keeping {node.install.describe()}")
    mac = uplink.resolve_uplink_mac(node.ip, ctx.talos_env())
    if mac and mac != node.link_mac:
        node.link_mac = mac
        definition.save(ctx.cluster, ctx.paths.defn)
        note(f"{node.name}: link alias pinned to uplink mac {mac}")
    if node.resolved_install != before or node.link_mac != before_mac:
        _run_phase(ctx, _CONFIG, None)


def _run_phase(ctx: Ctx, sd: StageDef, node: Node | None) -> None:
    label = _label(sd, node)
    missing = missing_for_stage(sd.key)
    if missing:
        raise StageError(f"{label}: missing tools ({', '.join(missing)})")
    try:
        sd.run(ctx, node)
    except CommandError as e:
        error(f"{label} failed (exit {e.returncode})")
        if e.stderr_tail:
            gap()
            note(e.stderr_tail, title="Last output")
        raise StageCancelled(f"{label} failed") from e
    success(f"{label} complete")


# small helpers -------------------------------------------------------------


def _show_preflight() -> None:
    with spinner("Checking required tools"):
        statuses = check_tools()
    lines = summary_lines(statuses)
    if lines:
        note(lines, title="Preflight (missing tools, not installed)")


def _label(sd: StageDef, node: Node | None) -> str:
    return f"{sd.title} ({node.name})" if node is not None else sd.title
