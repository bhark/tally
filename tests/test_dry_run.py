from __future__ import annotations

from tally.stages.s4_apply import DryRunVerdict, parse_dry_run


def test_no_changes_is_in_sync():
    stderr = "Dry run summary:\nNo changes.\n"
    assert parse_dry_run(stderr) is DryRunVerdict.IN_SYNC


def test_without_reboot_is_no_reboot():
    stderr = "Dry run summary:\nApplied configuration without a reboot (skipped in dry-run).\n"
    assert parse_dry_run(stderr) is DryRunVerdict.NO_REBOOT


def test_with_reboot_is_reboot():
    stderr = (
        "Config diff:\n"
        "  machine:\n-   foo: a\n+   foo: b\n"
        "Dry run summary:\n"
        "Applied configuration with a reboot (skipped in dry-run).\n"
    )
    assert parse_dry_run(stderr) is DryRunVerdict.REBOOT


def test_unrecognized_is_reboot():
    assert parse_dry_run("") is DryRunVerdict.REBOOT
    assert parse_dry_run("totally unexpected output") is DryRunVerdict.REBOOT


def test_no_changes_wins_over_without_reboot():
    stderr = "No changes.\nApplied configuration without a reboot (skipped in dry-run).\n"
    assert parse_dry_run(stderr) is DryRunVerdict.IN_SYNC
