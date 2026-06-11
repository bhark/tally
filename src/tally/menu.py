"""Nav-stack-as-call-stack menu framework over griphtui."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from griphtui import Option, is_cancel, select


@dataclass(frozen=True, slots=True)
class _Stay: ...


@dataclass(frozen=True, slots=True)
class _Back: ...


STAY: Final = _Stay()
BACK: Final = _Back()


@dataclass(frozen=True, slots=True)
class Exit:
    value: str


Nav = _Stay | _Back | Exit
Handler = Callable[[], Nav]


@dataclass(frozen=True, slots=True)
class Item:
    label: str
    run: Handler
    hint: str | None = None
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class Menu:
    title: str
    items: Callable[[], Sequence[Item]]  # recomputed every render
    header: Callable[[], None] | None = None

    # invariant: items() must yield >=1 enabled item or select raises; callers add a Back/Quit
    def run(self) -> Exit | _Back:
        while True:
            if self.header is not None:
                self.header()
            options = [
                Option(label=it.label, value=it, hint=it.hint, disabled=it.disabled)
                for it in self.items()
            ]
            choice = select(self.title, options)
            if is_cancel(choice):
                return BACK
            nav = choice.run()
            if isinstance(nav, _Stay):
                continue
            return nav


def submenu(menu: Menu) -> Handler:
    def run() -> Nav:
        result = menu.run()
        return result if isinstance(result, Exit) else STAY

    return run


def item(label: str, action: Callable[[], object], *, hint: str | None = None) -> Item:
    def run() -> Nav:
        action()
        return STAY

    return Item(label=label, run=run, hint=hint)
