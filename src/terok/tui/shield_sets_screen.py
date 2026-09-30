# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Picks a project's egress sets (``shield.sets``) via a TUI modal.

Dismisses with a tuple of set names, or ``None`` on cancel.  The sets come
in two groups:

- **Built-in** sets sit under the master "Recommended" checkbox.  Armed, it
  stands for [`RECOMMENDED_SET`][terok.lib.core.egress_sets.RECOMMENDED_SET]
  — every built-in set, including sets added in future releases; otherwise
  the checked built-in sets are an explicit, frozen selection.  Mirrors the
  agents picker's master-checkbox cascade: turning any built-in item off
  un-arms master.  Checking every item by hand leaves master off, because
  an enumeration is a different commitment than "recommended, including
  future sets".
- **Your sets** — the operator's own files in the egress-sets directory —
  sit outside that cascade: ``recommended`` never covers them, so master
  neither checks nor clears them and each stays an explicit choice.  A set
  whose file does not parse is shown disabled, with its error.

Saving with master armed returns ``(recommended, *checked user sets)``;
otherwise the checked built-in sets followed by the checked user sets
(empty = no sets at all).  Names the picker has no choice for — a user set
that is broken or gone — are carried over at the end, so the write refuses
them by name instead of dropping them from ``shield.sets`` unseen.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Rule

_MASTER_ID = "shield-sets-recommended"
_ITEM_PREFIX = "shield-sets-item-"
_USER_PREFIX = "shield-sets-user-"


def _item_id(slug: str) -> str:
    """DOM id of a built-in set's checkbox."""
    return f"{_ITEM_PREFIX}{slug}"


def _user_item_id(name: str) -> str:
    """DOM id of a user set's checkbox — a separate prefix keeps it out of the cascade."""
    return f"{_USER_PREFIX}{name}"


class ShieldSetsScreen(ModalScreen[tuple[str, ...] | None]):
    """Modal picker for a project's egress sets.

    *initial* is the project's current ``shield.sets``: a selection that
    holds ``recommended`` arms master and checks every built-in set; any
    other selection checks exactly its sets; ``None`` or an empty selection
    checks nothing.  User sets it names are checked in their own group.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    ShieldSetsScreen {
        align: center middle;
    }

    #shield-sets-dialog {
        width: 78;
        max-width: 100%;
        height: 90%;
        border: heavy $primary;
        border-title-align: right;
        background: $surface;
        padding: 1 2;
    }

    #shield-sets-scroll {
        height: 1fr;
    }

    .shield-sets-list {
        border: round $primary-darken-2;
        padding: 0 1;
        height: auto;
    }

    .shield-sets-master {
        color: $accent;
    }

    .shield-sets-sep {
        margin: 0 1;
    }

    .shield-sets-help {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }

    .shield-sets-group {
        margin-top: 1;
        height: auto;
    }

    #shield-sets-buttons {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    #shield-sets-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, *, initial: tuple[str, ...] | None, title: str = "Egress sets") -> None:
        """Build the modal; the set catalog loads in [`compose`][terok.tui.shield_sets_screen.ShieldSetsScreen.compose]."""
        super().__init__()
        self._initial = initial
        self._title = title
        self._choices: tuple[str, ...] = ()
        self._user_choices: tuple[str, ...] = ()
        self._master_cb: Checkbox | None = None
        self._item_cbs: dict[str, Checkbox] = {}
        self._user_cbs: dict[str, Checkbox] = {}

    def compose(self) -> ComposeResult:
        """Render the built-in group (master + per-set), the user group, and footer buttons."""
        from terok.lib.api import (
            EGRESS_SETS,
            OS_PACKAGES_SUMMARY,
            RECOMMENDED_SET,
            egress_sets_dir,
            selected_egress_sets,
            user_egress_sets,
        )

        self._choices = tuple(EGRESS_SETS)
        user_sets = user_egress_sets()
        self._user_choices = tuple(s.name for s in user_sets if s.error is None)
        is_recommended = RECOMMENDED_SET in (self._initial or ())
        preset = set(selected_egress_sets(self._initial))

        dialog = Vertical(id="shield-sets-dialog")
        dialog.border_title = self._title
        with dialog:
            yield Label(
                "Egress allowlists granted to this project's tasks while the shield is up.  "
                "Recommended grants every built-in set, including sets added in future "
                "releases; an explicit selection freezes the set; unchecking everything "
                "grants none.  Your own sets are never part of Recommended — pick each one.",
                classes="shield-sets-help",
            )
            with VerticalScroll(id="shield-sets-scroll"):
                with Vertical(classes="shield-sets-list"):
                    yield Checkbox(
                        "Recommended (every built-in set, including future ones)",
                        value=is_recommended,
                        id=_MASTER_ID,
                        classes="shield-sets-master",
                        name=RECOMMENDED_SET,
                    )
                    yield Rule(line_style="dashed", classes="shield-sets-sep")
                    for slug, hosts in EGRESS_SETS.items():
                        label = slug if hosts else f"{slug} ({OS_PACKAGES_SUMMARY})"
                        yield Checkbox(label, value=slug in preset, id=_item_id(slug), name=slug)
                yield Label(
                    f"Your sets  ({egress_sets_dir()})", classes="shield-sets-group", markup=False
                )
                with Vertical(classes="shield-sets-list", id="shield-sets-user-list"):
                    if not user_sets:
                        yield Label(
                            "None yet — a <name>.txt file there, one host per line, is a set.",
                            classes="shield-sets-help",
                        )
                    for egress_set in user_sets:
                        if egress_set.error is not None:
                            label = f"{egress_set.name} — invalid: {egress_set.error}"
                        else:
                            count = len(egress_set.hosts)
                            label = f"{egress_set.name} ({count} host{'' if count == 1 else 's'})"
                        yield Checkbox(
                            Content(label),  # literal: a file's error text is not markup
                            value=egress_set.error is None and egress_set.name in preset,
                            id=_user_item_id(egress_set.name),
                            name=egress_set.name,
                            disabled=egress_set.error is not None,
                        )
            with Horizontal(id="shield-sets-buttons"):
                yield Button("Cancel", id="shield-sets-cancel", variant="default")
                yield Button("Save", id="shield-sets-save", variant="primary")

    def on_mount(self) -> None:
        """Cache widget refs once so cascade + read don't re-query the DOM per item."""
        self._master_cb = self.query_one(f"#{_MASTER_ID}", Checkbox)
        self._item_cbs = {
            slug: self.query_one(f"#{_item_id(slug)}", Checkbox) for slug in self._choices
        }
        self._user_cbs = {
            name: self.query_one(f"#{_user_item_id(name)}", Checkbox) for name in self._user_choices
        }

    # ``prevent`` short-circuits Checkbox.Changed synchronously so the
    # cascade-from-master writes below don't recurse into this handler.
    # User-set checkboxes match neither branch: they stay out of the cascade.
    @on(Checkbox.Changed)
    def _on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cb_id = event.checkbox.id or ""
        if cb_id == _MASTER_ID:
            for item in self._item_cbs.values():
                with item.prevent(Checkbox.Changed):
                    item.value = event.checkbox.value
            return
        if cb_id.startswith(_ITEM_PREFIX) and not event.checkbox.value:
            master = self._master_cb
            if master is not None and master.value:
                with master.prevent(Checkbox.Changed):
                    master.value = False

    def action_cancel(self) -> None:
        """Dismiss with ``None`` — caller treats as no change."""
        self.dismiss(None)

    @on(Button.Pressed, "#shield-sets-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def _unshown_names(self) -> tuple[str, ...]:
        """Names in the initial selection that no live checkbox stands for, once each, in order."""
        from terok.lib.api import RECOMMENDED_SET

        shown = {RECOMMENDED_SET, *self._item_cbs, *self._user_cbs}
        return tuple(name for name in dict.fromkeys(self._initial or ()) if name not in shown)

    @on(Button.Pressed, "#shield-sets-save")
    def _on_save(self) -> None:
        """Dismiss with the recommended meta-set or the built-in sets, then the user sets.

        The names the picker could not show come last, unchanged: the write
        that follows refuses a broken or missing set by name, where leaving
        it out would silently rewrite the operator's ``shield.sets``.
        """
        from terok.lib.api import RECOMMENDED_SET

        user = tuple(name for name, cb in self._user_cbs.items() if cb.value)
        unshown = self._unshown_names()
        master = self._master_cb
        if master is not None and master.value:
            self.dismiss((RECOMMENDED_SET, *user, *unshown))
            return
        built_in = tuple(slug for slug, cb in self._item_cbs.items() if cb.value)
        self.dismiss((*built_in, *user, *unshown))


__all__ = ["ShieldSetsScreen"]
