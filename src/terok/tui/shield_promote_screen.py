# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Promoting one harvested target: where it goes, and what a break-glass takes.

The harvest screen opens one of two modals, and each runs its promotion itself
before dismissing with what it did:

- [`ShieldPromoteScreen`][terok.tui.shield_promote_screen.ShieldPromoteScreen]
  — the form for one target: a radio of the four destinations, with those the
  target's tier closes disabled and the reason each is closed spelled out; an
  egress-set name for ``set``; a reason and an expiry for a break-glass
  ``override``, which a confirmation spells out once more before anything is
  written.
- [`ShieldEnableSetScreen`][terok.tui.shield_promote_screen.ShieldEnableSetScreen]
  — a suggested egress set, enabled for everything it covers: one confirm.

Running the promotion inside the modal keeps a refusal next to the fields that
caused it.  The engine is the authority on what may be promoted where — a
built-in set that does not name the target, a set name that cannot be one —
and when it refuses, the form stays open with the reason under it rather than
closing on a toast that outlives the fields.  The form checks only what it can
see for itself: that a destination is chosen and open, and that the fields
that destination takes are filled in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static
from textual.worker import Worker, WorkerState

from terok.lib.api.shield import (
    PROMOTION_DESTINATIONS,
    REMEDY_OVERRIDE,
    REMEDY_PROJECT,
    REMEDY_SET,
    REMEDY_TASK,
    TIER_OPERATOR_DENY,
    TIER_PRIVATE,
    TIER_SECURITY,
    promote_egress,
)

from .screens import ConfirmDestructiveScreen

if TYPE_CHECKING:
    from terok.lib.api.shield import EgressCandidate, PromotionResult, SetSuggestion

DESTINATION_LABELS: dict[str, str] = {
    REMEDY_TASK: "This task",
    REMEDY_PROJECT: "The project — shield.allow",
    REMEDY_SET: "An egress set — built-in, or one of yours",
    REMEDY_OVERRIDE: "Break glass — shield.override, above the security tier",
}
"""How the form names each destination."""

_PROMOTE_GROUP = "shield-promote"
_ERROR_ID = "promote-error"
_SUBMIT_ID = "promote-submit"
_SUGGESTED_EXPIRY_DAYS = 7
"""How far ahead the expiry field's placeholder points — an example, never a default."""


@dataclass(frozen=True)
class PromotionRequest:
    """What the form asks of the engine: the destination and the fields that go with it."""

    destination: str
    set_name: str | None = None
    """The egress set — for ``set`` only."""
    reason: str | None = None
    """Why the break-glass exists — for ``override`` only."""
    expires: date | None = None
    """The last day the break-glass holds — for ``override`` only."""


def read_promotion_form(
    candidate: EgressCandidate,
    destination: str | None,
    *,
    set_name: str = "",
    reason: str = "",
    expires: str = "",
    today: date,
) -> PromotionRequest:
    """Turn the form's fields into a request for *destination*; ``ValueError`` says what to fix.

    Only the fields the destination takes are read — a set name for ``set``,
    a reason and an expiry for ``override`` — and the others are never sent,
    since the engine refuses a reason or an expiry anywhere but on an
    override.  A destination the target's tier closes is refused with the
    engine's own reason.  An expiry is a calendar day the override holds
    through, so it must be *today* or later.
    """
    if destination is None:
        raise ValueError("Choose where to promote it.")
    if (closed := candidate.refusal(destination)) is not None:
        raise ValueError(closed)
    if destination == REMEDY_SET:
        name = set_name.strip()
        if not name:
            raise ValueError(
                "Name the egress set: a built-in set that names the target, or one of "
                "yours — created when it is new."
            )
        return PromotionRequest(destination, set_name=name)
    if destination == REMEDY_OVERRIDE:
        why = reason.strip()
        if not why:
            raise ValueError(
                "A break-glass override needs a reason: the audit trail for the hole it opens."
            )
        return PromotionRequest(destination, reason=why, expires=_expiry(expires, today))
    return PromotionRequest(destination)


def _expiry(text: str, today: date) -> date:
    """The expiry field as a date, today or later."""
    text = text.strip()
    if not text:
        raise ValueError(
            "A break-glass override needs an expiry: the last day it holds, as YYYY-MM-DD."
        )
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{text!r} is no date: give the last day it holds as YYYY-MM-DD."
        ) from None
    if day < today:
        raise ValueError(
            f"{day.isoformat()} has passed: an override holds through its expiry day, so the "
            f"expiry is today ({today.isoformat()}) or later."
        )
    return day


def destination_hint(
    destination: str, candidate: EgressCandidate, *, project: str, task_id: str, running: bool
) -> str:
    """What promoting *candidate* to an open *destination* does — when, and for whom.

    One wording for the form and the harvest screen's detail pane.  Whether
    the task runs decides what happens now: a running task takes a grant
    live, a stopped one at its next start.
    """
    task = f"task {project}/{task_id}"
    lifts = (
        " — and lifts your own runtime deny of it" if candidate.tier == TIER_OPERATOR_DENY else ""
    )
    if destination == REMEDY_TASK:
        when = (
            "live now, and it stays with the task across its restarts"
            if running
            else "the task is not running, so the grant waits in its runtime overlay and "
            "applies at its next start"
        )
        return f"Allow it for {task} alone: {when}{lifts}."
    now = f", and {task} takes it now" if running else ""
    if destination == REMEDY_PROJECT:
        return (
            f"Add it to shield.allow in {project}'s project.yml: every task of {project} gets "
            f"it from its next start{now}{lifts}."
        )
    if destination == REMEDY_SET:
        return (
            "Enable a built-in egress set that names it, or add it to one of your own sets "
            f"(created when new) and enable that: every task of {project} gets it from its next "
            f"start{now}{lifts}."
        )
    vault = (
        "  Agents keep relaying keys through the vault; the override only adds the direct route."
        if candidate.tier == TIER_SECURITY
        else ""
    )
    return (
        "Break glass: a shield.override above the security tier, with a reason and an expiry — "
        f"every task of {project} from its next start{now}, until the end of the expiry day.{vault}"
    )


def _break_glass_message(
    candidate: EgressCandidate, request: PromotionRequest, *, project: str, running: bool
) -> str:
    """The confirmation a break-glass needs: what opens, for whom, until when, and why."""
    until = request.expires.isoformat() if request.expires else "?"
    now = (
        " — and on the running task now, closed by the kernel at the end of that day"
        if running
        else ""
    )
    lines = [
        f"Open {candidate.target} above the security tier for every task of {project}, from "
        f"its next start{now}, until the end of {until} (host local time).",
        "",
        f"Reason: {request.reason}",
    ]
    if candidate.tier == TIER_SECURITY:
        lines += [
            "",
            "The security tier refuses it so that agents reach it only through the vault, "
            "which relays their keys.  They still will: the override adds a direct route past it.",
        ]
    elif candidate.tier == TIER_PRIVATE:
        lines += ["", "It is in a private network range: the override opens every port on it."]
    lines += [
        "",
        "The override is written to project.yml with its reason and expiry.",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class _Outcome:
    """What one promotion attempt came to: the engine's result, or why there is none."""

    result: PromotionResult | None = None
    error: str = ""


def _attempt(project: str, task_id: str, target: str | None, request: PromotionRequest) -> _Outcome:
    """Run one promotion, folding every refusal into words for the form (runs in a thread).

    ``SystemExit`` is caught too: a broken ``project.yml`` or user set is
    the operator's to fix, and a worker thread must not die on it.
    """
    try:
        result = promote_egress(
            project,
            task_id,
            target,
            request.destination,
            set_name=request.set_name,
            reason=request.reason,
            expires=request.expires,
        )
    except ValueError as exc:  # PromotionRefused among them: nothing was changed
        return _Outcome(error=f"Not promoted: {exc}")
    except SystemExit as exc:
        return _Outcome(error=f"Not promoted — a file you own needs fixing first: {exc}")
    except RuntimeError as exc:
        return _Outcome(error=f"Not promoted: {exc}")
    except OSError as exc:
        return _Outcome(
            error=f"Stopped: a file could not be written ({exc}) — anything written before it "
            "stays; cancel and reload the review (r) to see where things stand."
        )
    return _Outcome(result=result)


class _PromotionModal(ModalScreen["PromotionResult | None"]):
    """A modal that runs one promotion in a worker, then dismisses with its result.

    Until the engine answers, the submit button is disabled; a refusal is
    shown under the fields and the modal stays open, so the operator can fix
    what was refused or cancel.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, project: str, task_id: str, *, running: bool) -> None:
        """Bind the modal to the task whose harvest the promotion comes from."""
        super().__init__()
        self._project = project
        self._task_id = task_id
        self._task_running = running
        self._promoting = False

    def _promote(self, target: str | None, request: PromotionRequest) -> None:
        """Ask the engine for *request* in a worker; the answer arrives in the state handler.

        One at a time: a second submit while the first is in flight is
        ignored, never queued behind it.
        """
        if self._promoting:
            return
        self._set_busy(True)
        self._show_error("")
        self.run_worker(
            lambda: _attempt(self._project, self._task_id, target, request),
            name=f"{_PROMOTE_GROUP}:{target or request.set_name}",
            group=_PROMOTE_GROUP,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Dismiss with the engine's result, or show why there is none."""
        if event.worker.group != _PROMOTE_GROUP:
            return
        if event.state == WorkerState.ERROR:
            # Nothing the engine is known to raise gets here, so nothing can
            # be said about what it changed before it stopped.
            self._set_busy(False)
            self._show_error(
                f"The promotion stopped on an unexpected error ({event.worker.error}) — cancel "
                "and reload the review (r) to see what took effect."
            )
        elif event.state == WorkerState.SUCCESS:
            outcome = event.worker.result
            if isinstance(outcome, _Outcome) and outcome.result is not None:
                self.dismiss(outcome.result)
                return
            self._set_busy(False)
            self._show_error(outcome.error if isinstance(outcome, _Outcome) else "Not promoted.")

    def _set_busy(self, busy: bool) -> None:
        """Disable the submit button while a promotion is in flight."""
        self._promoting = busy
        try:
            self.query_one(f"#{_SUBMIT_ID}", Button).disabled = busy
        except NoMatches:  # pragma: no cover — dismissed meanwhile
            pass

    def _show_error(self, message: str) -> None:
        """Put *message* under the fields; an empty one clears it."""
        try:
            self.query_one(f"#{_ERROR_ID}", Label).update(message)
        except NoMatches:  # pragma: no cover — dismissed meanwhile
            pass

    def action_cancel(self) -> None:
        """Dismiss without promoting anything."""
        self.dismiss(None)


class ShieldPromoteScreen(_PromotionModal):
    """The promotion form for one harvested target; dismisses with the ``PromotionResult``.

    *destination* preselects one — the shortcut key the operator pressed —
    when the target's tier allows it; without one, a target the review
    suggests a set for starts on ``set``, with that set's name filled in, and
    anything else on the first open destination.  The closed ones stay on the
    radio, disabled, with the reason each is closed listed under it: an
    operator who sees why a host cannot go to the project learns where it
    can go.  *set_names* feed the set-name field's suggestions (the target's
    suggested set first).
    """

    CSS = """
    ShieldPromoteScreen {
        align: center middle;
    }

    #promote-dialog {
        width: 88;
        max-width: 100%;
        height: 90%;
        border: heavy $primary;
        border-title-align: right;
        background: $surface;
        padding: 1 2;
    }

    #promote-scroll {
        height: 1fr;
    }

    .promote-help {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }

    .promote-fields {
        height: auto;
    }

    #promote-error {
        color: $error;
        height: auto;
    }

    #promote-buttons {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    #promote-buttons Button {
        margin-left: 1;
    }

    #promote-destination {
        border: round $primary-darken-2;
        padding: 0 1;
        width: 100%;
    }
    """

    def __init__(
        self,
        candidate: EgressCandidate,
        *,
        project: str,
        task_id: str,
        running: bool,
        set_names: tuple[str, ...] = (),
        destination: str | None = None,
        today: date | None = None,
    ) -> None:
        """Prepare the form for *candidate*, harvested from task *task_id* of *project*."""
        super().__init__(project, task_id, running=running)
        self._candidate = candidate
        self._set_names = set_names
        self._today = today or date.today()
        open_ = candidate.destinations
        # The key pressed wins; plain enter takes the review's own advice — a
        # suggested set that covers the target — before the first open one.
        wanted = destination or (REMEDY_SET if candidate.suggested_set else None)
        first_open = open_[0] if open_ else None
        self._destination: str | None = wanted if wanted in open_ else first_open

    def compose(self) -> ComposeResult:
        """Lay out the destinations, the fields they take, and the buttons."""
        candidate = self._candidate
        dialog = Vertical(id="promote-dialog")
        dialog.border_title = f"Promote {candidate.target}"
        dialog.border_subtitle = "Esc to cancel"
        with dialog:
            # The fields scroll; the error line and the buttons stay in view
            # below them, however short the terminal.
            with VerticalScroll(id="promote-scroll"):
                yield Static(self._summary(), id="promote-summary", markup=False)
                with RadioSet(id="promote-destination"):
                    for destination in PROMOTION_DESTINATIONS:
                        yield RadioButton(
                            DESTINATION_LABELS[destination],
                            value=destination == self._destination,
                            name=destination,
                            id=f"promote-to-{destination}",
                            disabled=destination not in candidate.destinations,
                        )
                yield Static(
                    self._closed_reasons(),
                    id="promote-closed",
                    classes="promote-help",
                    markup=False,
                )
                yield Static("", id="promote-hint", classes="promote-help", markup=False)
                with Vertical(id="promote-set-fields", classes="promote-fields"):
                    yield Label(
                        "Egress set — a built-in set that names it, or one of yours", markup=False
                    )
                    yield Input(
                        value=candidate.suggested_set or "",
                        placeholder="set name",
                        suggester=SuggestFromList(self._set_names) if self._set_names else None,
                        id="promote-set",
                    )
                    if self._set_names:
                        yield Static(
                            f"Known here: {', '.join(self._set_names)}",
                            classes="promote-help",
                            markup=False,
                        )
                with Vertical(id="promote-override-fields", classes="promote-fields"):
                    yield Label("Reason — the audit trail for the hole it opens", markup=False)
                    yield Input(placeholder="why this break-glass exists", id="promote-reason")
                    yield Label(
                        "Expires — the last day it holds, YYYY-MM-DD, today or later",
                        markup=False,
                    )
                    example = self._today + timedelta(days=_SUGGESTED_EXPIRY_DAYS)
                    yield Input(placeholder=f"e.g. {example.isoformat()}", id="promote-expires")
            yield Label("", id=_ERROR_ID, markup=False)
            with Horizontal(id="promote-buttons"):
                yield Button("Cancel", id="promote-cancel")
                yield Button("Promote", id=_SUBMIT_ID, variant="primary")

    def on_mount(self) -> None:
        """Show the preselected destination's fields and put the cursor where typing starts."""
        self._sync_fields()
        if self._destination == REMEDY_SET:
            self.query_one("#promote-set", Input).focus()
        elif self._destination == REMEDY_OVERRIDE:
            self.query_one("#promote-reason", Input).focus()
        else:
            self.query_one(f"#{_SUBMIT_ID}", Button).focus()

    def _summary(self) -> str:
        """One line on the target: what the shield did about it, how often, on which ports."""
        candidate = self._candidate
        tries = "1 try" if candidate.count == 1 else f"{candidate.count} tries"
        ports = ",".join(str(port) for port in candidate.ports) or "-"
        return (
            f"{candidate.target} — {tries} ({', '.join(candidate.actions)}) on port(s) {ports}, "
            f"harvested from task {self._project}/{self._task_id}."
        )

    def _closed_reasons(self) -> str:
        """Why each closed destination is closed — empty when every one is open."""
        closed = [
            f"{DESTINATION_LABELS[destination].split(' — ')[0]}: {reason}"
            for destination in PROMOTION_DESTINATIONS
            if (reason := self._candidate.refusal(destination)) is not None
        ]
        return "Closed for this target —\n" + "\n".join(closed) if closed else ""

    def _sync_fields(self) -> None:
        """Show the fields the selected destination takes, and say what it does."""
        destination = self._destination
        self.query_one("#promote-set-fields").display = destination == REMEDY_SET
        self.query_one("#promote-override-fields").display = destination == REMEDY_OVERRIDE
        hint = ""
        if destination is not None:
            hint = destination_hint(
                destination,
                self._candidate,
                project=self._project,
                task_id=self._task_id,
                running=self._task_running,
            )
        self.query_one("#promote-hint", Static).update(hint)
        submit = self.query_one(f"#{_SUBMIT_ID}", Button)
        submit.label = "Break glass…" if destination == REMEDY_OVERRIDE else "Promote"
        submit.disabled = destination is None

    @on(RadioSet.Changed, "#promote-destination")
    def _on_destination_changed(self, event: RadioSet.Changed) -> None:
        """Follow the radio: the fields, the hint and the button track the destination."""
        self._destination = event.pressed.name
        self._show_error("")
        self._sync_fields()

    @on(Input.Submitted, "#promote-reason")
    def _on_reason_submitted(self) -> None:
        """Enter on the reason moves on to the expiry, the other field a break-glass takes."""
        self.query_one("#promote-expires", Input).focus()

    @on(Input.Submitted, "#promote-set")
    @on(Input.Submitted, "#promote-expires")
    @on(Button.Pressed, f"#{_SUBMIT_ID}")
    def _on_submit(self) -> None:
        """Check the fields, confirm a break-glass, then run the promotion."""
        if self._promoting:
            return
        try:
            request = read_promotion_form(
                self._candidate,
                self._destination,
                set_name=self.query_one("#promote-set", Input).value,
                reason=self.query_one("#promote-reason", Input).value,
                expires=self.query_one("#promote-expires", Input).value,
                today=self._today,
            )
        except ValueError as exc:
            self._show_error(str(exc))
            return
        target = self._candidate.target
        if request.destination != REMEDY_OVERRIDE:
            self._promote(target, request)
            return
        message = _break_glass_message(
            self._candidate, request, project=self._project, running=self._task_running
        )

        def _confirmed(confirmed: bool | None) -> None:
            """Break glass only on an explicit yes."""
            if confirmed:
                self._promote(target, request)

        self.app.push_screen(
            ConfirmDestructiveScreen(message, title="Break glass?", confirm_label="Break glass"),
            _confirmed,
        )

    @on(Button.Pressed, "#promote-cancel")
    def _on_cancel(self) -> None:
        """Dismiss without promoting anything."""
        self.dismiss(None)


class ShieldEnableSetScreen(_PromotionModal):
    """Enable a suggested egress set for everything it covers; dismisses with the result.

    A suggestion is already a whole decision — the set, and the targets it
    covers that nothing allows yet — so it takes one confirm, not a form.
    The text says what else comes with it: every host in the set, for every
    task of the project.
    """

    CSS = """
    ShieldEnableSetScreen {
        align: center middle;
    }

    #enable-set-dialog {
        width: 80;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        border: heavy $primary;
        border-title-align: right;
        background: $surface;
        padding: 1 2;
    }

    #promote-error {
        color: $error;
        height: auto;
    }

    #enable-set-scroll {
        height: auto;
        max-height: 60vh;
    }

    #enable-set-buttons {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    #enable-set-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(
        self, suggestion: SetSuggestion, *, project: str, task_id: str, running: bool
    ) -> None:
        """Prepare the confirm for *suggestion*, made for task *task_id* of *project*."""
        super().__init__(project, task_id, running=running)
        self._set_suggestion = suggestion

    def compose(self) -> ComposeResult:
        """Say what enabling the set grants, then ask."""
        dialog = Vertical(id="enable-set-dialog")
        dialog.border_title = f"Enable {self._set_suggestion.name}?"
        dialog.border_subtitle = "Esc to cancel"
        with dialog:
            with VerticalScroll(id="enable-set-scroll"):
                yield Static(self._text(), id="enable-set-text", markup=False)
            yield Label("", id=_ERROR_ID, markup=False)
            with Horizontal(id="enable-set-buttons"):
                yield Button("Cancel", id="enable-set-cancel")
                yield Button("Enable", id=_SUBMIT_ID, variant="primary")

    def on_mount(self) -> None:
        """Focus the confirm: the suggestion was the operator's pick already."""
        self.query_one(f"#{_SUBMIT_ID}", Button).focus()

    def _text(self) -> str:
        """What the set is, what it covers, and what else comes with it."""
        suggestion = self._set_suggestion
        noun = "host" if suggestion.hosts == 1 else "hosts"
        now = (
            f", and task {self._project}/{self._task_id} takes what it covers now"
            if self._task_running
            else ""
        )
        return (
            f"Enable the egress set {suggestion.name} ({suggestion.origin}, {suggestion.hosts} "
            f"{noun}) for project {self._project}.\n\n"
            f"It covers {len(suggestion.covers)} of the {suggestion.of} targets nothing allows "
            f"yet: {', '.join(suggestion.covers)}.\n\n"
            f"Every task of {self._project} gets every host in the set from its next "
            f"start{now}."
        )

    @on(Button.Pressed, f"#{_SUBMIT_ID}")
    def _on_enable(self) -> None:
        """Enable the set for what it covers in this task's harvest."""
        self._promote(None, PromotionRequest(REMEDY_SET, set_name=self._set_suggestion.name))

    @on(Button.Pressed, "#enable-set-cancel")
    def _on_cancel(self) -> None:
        """Dismiss without enabling anything."""
        self.dismiss(None)
