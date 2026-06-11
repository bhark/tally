from __future__ import annotations

import pytest
from griphtui import CANCEL

from tally import menu
from tally.menu import BACK, STAY, Exit, Item, Menu, item, submenu


class FakeSelect:
    """scripted select: each entry is a label to choose, or None for Esc/Cancel."""

    def __init__(self, script: list[str | None]) -> None:
        self._script = list(script)
        self.calls: list[list] = []  # captured options per call

    def __call__(self, label, options):
        self.calls.append(list(options))
        choice = self._script.pop(0)
        if choice is None:
            return CANCEL
        for opt in options:
            if opt.label == choice:
                return opt.value
        raise AssertionError(f"no option labelled {choice!r}")


@pytest.fixture
def fake(monkeypatch):
    def install(script):
        f = FakeSelect(script)
        monkeypatch.setattr(menu, "select", f)
        return f

    return install


def test_leaf_runs_action_and_rerenders(fake):
    renders = 0
    fired = 0

    def items():
        nonlocal renders
        renders += 1
        return [item("go", action), back_item()]

    def action():
        nonlocal fired
        fired += 1

    fake(["go", "back"])
    Menu("m", items).run()

    assert fired == 1
    assert renders == 2  # re-rendered after the leaf returned STAY


def test_back_item_pops(fake):
    fake(["back"])
    assert Menu("m", lambda: [back_item()]).run() is BACK


def test_esc_pops(fake):
    fake([None])
    assert Menu("m", lambda: [item("x", lambda: None), back_item()]).run() is BACK


def test_exit_unwinds_through_submenu(fake):
    child = Menu("child", lambda: [Item("quit", lambda: Exit("done"))])
    parent = Menu("parent", lambda: [Item("open", submenu(child))])

    fake(["open", "quit"])
    result = parent.run()

    assert result == Exit("done")


def test_child_back_returns_parent_to_stay(fake):
    parent_renders = 0

    def parent_items():
        nonlocal parent_renders
        parent_renders += 1
        return [Item("open", submenu(child)), back_item()]

    child = Menu("child", lambda: [back_item()])
    parent = Menu("parent", parent_items)

    fake(["open", "back", "back"])  # enter child, child Back, then parent Back
    assert parent.run() is BACK
    assert parent_renders == 2  # parent re-rendered after child popped


def test_dynamic_labels_reflected(fake):
    n = 0

    def items():
        nonlocal n
        n += 1
        return [item(f"count-{n}", lambda: None), back_item()]

    f = fake(["count-1", "count-2", "back"])
    Menu("m", items).run()

    assert [o.label for o in f.calls[0]] == ["count-1", "back"]
    assert [o.label for o in f.calls[1]] == ["count-2", "back"]


def test_disabled_item_builds_disabled_option(fake):
    f = fake(["back"])
    Menu("m", lambda: [Item("off", lambda: STAY, disabled=True), back_item()]).run()

    off = next(o for o in f.calls[0] if o.label == "off")
    assert off.disabled is True


def back_item() -> Item:
    return Item("back", lambda: BACK)
