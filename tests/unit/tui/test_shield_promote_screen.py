# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The promotion form: its rules, its radio, the break-glass confirm, and the engine's answer.

The rules are a pure function — ``read_promotion_form`` — and are pinned
directly: a destination must be chosen and open, ``set`` needs a name, a
break-glass needs a reason and an expiry that has not passed, and no other
destination ever sends either.  The modals run in a minimal host app with
the engine's ``promote_egress`` patched, so the tests see exactly what the
form asks of it and what it does with the answer.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest
from textual.app import App
from textual.widgets import Button, Input, Label, RadioButton, Static

from terok.lib.api.shield import (
    REMEDY_OVERRIDE,
    REMEDY_PROJECT,
    REMEDY_SET,
    REMEDY_TASK,
    TIER_ALLOWLIST,
    TIER_OPERATOR_DENY,
    TIER_PRIVATE,
    TIER_SECURITY,
    EgressCandidate,
    PromotionRefused,
    PromotionResult,
    SetSuggestion,
)
from terok.tui import shield_promote_screen as promote_mod
from terok.tui.screens import ConfirmDestructiveScreen
from terok.tui.shield_promote_screen import (
    PromotionRequest,
    ShieldEnableSetScreen,
    ShieldPromoteScreen,
    destination_hint,
    read_promotion_form,
)
from tests.testnet import HTTPS_PORT, PRIVATE_LAN_IP

_PROJECT = "proj"
_TASK = "7"
_TODAY = date(2026, 10, 27)
"""A fixed today, so expiry rules never depend on when the suite runs."""
_REASON = "vendor outage, debugging the direct endpoint"
_PENDING = object()
_HOLD_TIMEOUT_SECONDS = 10
"""How long a held engine waits for its release before giving up, so a failed test cannot hang."""

_REMEDIES: dict[str, tuple[str, ...]] = {
    TIER_ALLOWLIST: (REMEDY_TASK, REMEDY_PROJECT, REMEDY_SET),
    TIER_OPERATOR_DENY: (REMEDY_TASK, REMEDY_PROJECT),
    TIER_SECURITY: (REMEDY_OVERRIDE,),
    TIER_PRIVATE: (REMEDY_OVERRIDE,),
}


def _candidate(
    target: str = "svc.example.org", tier: str = TIER_ALLOWLIST, **kwargs: Any
) -> EgressCandidate:
    """One merged target, classified like the engine classifies *tier*."""
    fields: dict[str, Any] = {
        "target": target,
        "tier": tier,
        "remedies": _REMEDIES[tier],
        "actions": ("blocked",),
        "count": 3,
        "first_seen": "2026-10-01T10:00:00+00:00",
        "last_seen": "2026-10-02T11:00:00+00:00",
        "ports": (HTTPS_PORT,),
        "addresses": (),
    }
    fields.update(kwargs)
    return EgressCandidate(**fields)


# ── The form's rules ─────────────────────────────────────────────────


def test_a_destination_must_be_chosen() -> None:
    """Nothing is promoted nowhere."""
    candidate = _candidate()

    with pytest.raises(ValueError, match="Choose where"):
        read_promotion_form(candidate, None, today=_TODAY)


@pytest.mark.parametrize(
    ("tier", "destination"),
    [
        pytest.param(TIER_ALLOWLIST, REMEDY_OVERRIDE, id="t40-no-break-glass"),
        pytest.param(TIER_SECURITY, REMEDY_PROJECT, id="security-no-allow"),
        pytest.param(TIER_OPERATOR_DENY, REMEDY_SET, id="operator-deny-no-set"),
    ],
)
def test_a_closed_destination_is_refused_with_the_engines_reason(
    tier: str, destination: str
) -> None:
    """The tier decides; the form repeats the engine's reason rather than inventing one."""
    candidate = _candidate(tier=tier)

    with pytest.raises(ValueError) as refused:
        read_promotion_form(
            candidate, destination, reason=_REASON, expires="2026-11-01", today=_TODAY
        )

    assert str(refused.value) == candidate.refusal(destination)


def test_a_set_needs_a_name_and_takes_it_stripped() -> None:
    """An empty name is refused; a padded one is sent clean."""
    candidate = _candidate()

    with pytest.raises(ValueError, match="Name the egress set"):
        read_promotion_form(candidate, REMEDY_SET, set_name="  ", today=_TODAY)

    request = read_promotion_form(candidate, REMEDY_SET, set_name=" corp ", today=_TODAY)

    assert request == PromotionRequest(REMEDY_SET, set_name="corp")


@pytest.mark.parametrize(
    ("reason", "expires", "message"),
    [
        pytest.param("", "2026-11-01", "needs a reason", id="no-reason"),
        pytest.param("   ", "2026-11-01", "needs a reason", id="blank-reason"),
        pytest.param(_REASON, "", "needs an expiry", id="no-expiry"),
        pytest.param(_REASON, "next tuesday", "is no date", id="not-a-date"),
        pytest.param(_REASON, "2026-10-26", "has passed", id="yesterday"),
    ],
)
def test_a_break_glass_needs_a_reason_and_an_expiry_not_yet_passed(
    reason: str, expires: str, message: str
) -> None:
    """Both are required: the audit trail for the hole, and the day it closes."""
    candidate = _candidate(tier=TIER_SECURITY)

    with pytest.raises(ValueError, match=message):
        read_promotion_form(
            candidate,
            REMEDY_OVERRIDE,
            reason=reason,
            expires=expires,
            today=_TODAY,
        )


@pytest.mark.parametrize("expires", [_TODAY, _TODAY + timedelta(days=30)], ids=["today", "later"])
def test_an_expiry_of_today_or_later_holds(expires: date) -> None:
    """An override holds through its expiry day, so today is a valid last day."""
    request = read_promotion_form(
        _candidate(tier=TIER_PRIVATE),
        REMEDY_OVERRIDE,
        reason=f"  {_REASON}  ",
        expires=expires.isoformat(),
        today=_TODAY,
    )

    assert request == PromotionRequest(REMEDY_OVERRIDE, reason=_REASON, expires=expires)


@pytest.mark.parametrize("destination", [REMEDY_TASK, REMEDY_PROJECT])
def test_a_plain_allow_never_sends_a_reason_or_an_expiry(destination: str) -> None:
    """Hidden fields stay home: the engine refuses them anywhere but on an override."""
    request = read_promotion_form(
        _candidate(),
        destination,
        set_name="corp",
        reason=_REASON,
        expires="2026-11-01",
        today=_TODAY,
    )

    assert request == PromotionRequest(destination)


def test_the_hint_says_when_a_task_grant_lands() -> None:
    """Live while the task runs; from its next start when it is stopped."""
    candidate = _candidate(tier=TIER_OPERATOR_DENY)

    running = destination_hint(
        REMEDY_TASK, candidate, project=_PROJECT, task_id=_TASK, running=True
    )
    stopped = destination_hint(
        REMEDY_TASK, candidate, project=_PROJECT, task_id=_TASK, running=False
    )

    assert "live now" in running
    assert "applies at its next start" in stopped
    assert "lifts your own runtime deny" in running


# ── The modal ────────────────────────────────────────────────────────


class _Host(App):
    """Minimal host: pushes a modal and records what it dismisses with."""

    def __init__(self, screen: Any) -> None:
        super().__init__()
        self._screen = screen
        self.result: object = _PENDING

    def on_mount(self) -> None:
        self.push_screen(self._screen, self._capture)

    def _capture(self, result: object) -> None:
        self.result = result


@pytest.fixture
def engine() -> Iterator[MagicMock]:
    """Stand in for ``promote_egress``; it answers with a plain project result."""
    with mock.patch.object(promote_mod, "promote_egress") as promote:
        promote.return_value = PromotionResult("svc.example.org", REMEDY_PROJECT, next_start=True)
        yield promote


def _form(candidate: EgressCandidate | None = None, **kwargs: Any) -> ShieldPromoteScreen:
    """The form for *candidate*, harvested from task ``proj/7``, with a fixed today."""
    kwargs.setdefault("running", True)
    return ShieldPromoteScreen(
        candidate or _candidate(), project=_PROJECT, task_id=_TASK, today=_TODAY, **kwargs
    )


def _radio(screen: Any, destination: str) -> RadioButton:
    return screen.query_one(f"#promote-to-{destination}", RadioButton)


def _error(screen: Any) -> str:
    return str(screen.query_one("#promote-error", Label).render())


async def _settle(pilot: Any) -> None:
    """Let a promotion worker finish and its answer land."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.asyncio
async def test_closed_destinations_are_disabled_and_say_why() -> None:
    """A security-tier host: only break glass is open; the others stay visible, explained."""
    candidate = _candidate("vault.example.org", TIER_SECURITY)
    app = _Host(_form(candidate))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        disabled = {d: _radio(screen, d).disabled for d in (REMEDY_TASK, REMEDY_SET)}
        override = _radio(screen, REMEDY_OVERRIDE)
        closed = str(screen.query_one("#promote-closed", Static).render())
        override_fields = screen.query_one("#promote-override-fields").display

    assert disabled == {REMEDY_TASK: True, REMEDY_SET: True}
    assert not override.disabled and override.value
    assert candidate.refusal(REMEDY_PROJECT) in closed.replace("\n", " ")
    assert override_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asked", "selected"),
    [
        pytest.param(REMEDY_SET, REMEDY_SET, id="open-shortcut-kept"),
        pytest.param(REMEDY_OVERRIDE, REMEDY_TASK, id="closed-falls-back-to-first-open"),
        pytest.param(None, REMEDY_TASK, id="enter-takes-first-open"),
    ],
)
async def test_the_shortcut_preselects_an_open_destination(
    asked: str | None, selected: str
) -> None:
    """The key the operator pressed wins when the tier allows it."""
    app = _Host(_form(destination=asked))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        pressed = {d for d in (REMEDY_TASK, REMEDY_PROJECT, REMEDY_SET) if _radio(screen, d).value}
        set_fields = screen.query_one("#promote-set-fields").display

    assert pressed == {selected}
    assert set_fields is (selected == REMEDY_SET)


@pytest.mark.asyncio
async def test_choosing_a_destination_shows_its_fields() -> None:
    """The set field appears with ``set``; the break-glass fields never do for a t40 target."""
    app = _Host(_form())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        _radio(screen, REMEDY_SET).value = True
        await pilot.pause()
        shown = (
            screen.query_one("#promote-set-fields").display,
            screen.query_one("#promote-override-fields").display,
        )

    assert shown == (True, False)


@pytest.mark.asyncio
async def test_a_missing_set_name_is_said_in_the_form_and_nothing_runs(
    engine: MagicMock,
) -> None:
    """The rule is checked before the engine is asked anything."""
    app = _Host(_form(destination=REMEDY_SET))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#promote-submit")
        await _settle(pilot)
        error = _error(app.screen)

    engine.assert_not_called()
    assert "Name the egress set" in error
    assert app.result is _PENDING


@pytest.mark.asyncio
async def test_a_promotion_dismisses_with_the_engines_result(engine: MagicMock) -> None:
    """The form asks for exactly its destination, and hands the engine's answer back."""
    app = _Host(_form(destination=REMEDY_PROJECT))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#promote-submit")
        await _settle(pilot)

    engine.assert_called_once_with(
        _PROJECT,
        _TASK,
        "svc.example.org",
        REMEDY_PROJECT,
        set_name=None,
        reason=None,
        expires=None,
    )
    assert app.result is engine.return_value


@pytest.mark.asyncio
async def test_a_set_name_typed_in_is_what_the_engine_gets(engine: MagicMock) -> None:
    """Enter in the set-name field submits; the name goes to the engine as typed."""
    app = _Host(_form(destination=REMEDY_SET, set_names=("corp",)))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#promote-set", Input).value = "team-mirrors"
        await pilot.press("enter")
        await _settle(pilot)

    assert engine.call_args.kwargs["set_name"] == "team-mirrors"
    assert app.result is engine.return_value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(PromotionRefused("svc.example.org is not in the built-in set"), id="refused"),
        pytest.param(SystemExit("project.yml does not load"), id="broken-project"),
        pytest.param(RuntimeError("shield is disabled"), id="kill-switch"),
    ],
)
async def test_an_engine_refusal_stays_in_the_form(
    engine: MagicMock, refusal: BaseException
) -> None:
    """Refused, the form stays open with the reason under it, ready to fix or cancel."""
    engine.side_effect = refusal
    app = _Host(_form(destination=REMEDY_PROJECT))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#promote-submit")
        await _settle(pilot)
        screen = app.screen
        error = _error(screen)
        submit_disabled = screen.query_one("#promote-submit", Button).disabled

    assert isinstance(screen, ShieldPromoteScreen)
    assert str(refusal) in error
    assert error.startswith("Not promoted")
    assert not submit_disabled
    assert app.result is _PENDING


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_written_stops_the_promotion_and_says_so(
    engine: MagicMock,
) -> None:
    """A failed write is not a refusal: the form warns that earlier writes stay, and stays open."""
    engine.side_effect = OSError("no space left on device")
    app = _Host(_form(destination=REMEDY_PROJECT))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#promote-submit")
        await _settle(pilot)
        screen = app.screen
        error = _error(screen)
        submit_disabled = screen.query_one("#promote-submit", Button).disabled

    assert isinstance(screen, ShieldPromoteScreen)
    assert error.startswith("Stopped: a file could not be written (no space left on device)")
    assert "anything written before it stays" in error
    assert "reload the review (r)" in error
    assert not submit_disabled
    assert app.result is _PENDING


@pytest.mark.asyncio
async def test_an_unexpected_engine_error_is_shown_and_the_form_is_released(
    engine: MagicMock,
) -> None:
    """An error nobody expected leaves the outcome unknown, so the form says to reload the review."""
    engine.side_effect = KeyError("engine state")
    app = _Host(_form(destination=REMEDY_PROJECT))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#promote-submit")
        await _settle(pilot)
        screen = app.screen
        error = _error(screen)
        submit_disabled = screen.query_one("#promote-submit", Button).disabled

    assert isinstance(screen, ShieldPromoteScreen)
    assert error.startswith("The promotion stopped on an unexpected error")
    assert "engine state" in error
    assert "reload the review (r) to see what took effect" in error
    assert not submit_disabled
    assert app.result is _PENDING


@pytest.fixture
def held_engine(engine: MagicMock) -> Iterator[threading.Event]:
    """Hold the engine mid-promotion until the test sets the returned event."""
    release = threading.Event()
    answer = engine.return_value

    def _hold(*_args: Any, **_kwargs: Any) -> PromotionResult:
        release.wait(_HOLD_TIMEOUT_SECONDS)
        return answer

    engine.side_effect = _hold
    try:
        yield release
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_second_enter_while_the_promotion_runs_does_not_start_another(
    engine: MagicMock, held_engine: threading.Event
) -> None:
    """Enter in the set field twice asks the engine once; the first answer is the result."""
    app = _Host(_form(destination=REMEDY_SET))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#promote-set", Input).value = "corp"
        await pilot.press("enter")
        busy = app.screen.query_one("#promote-submit", Button).disabled
        await pilot.press("enter")
        held_engine.set()
        await _settle(pilot)

    assert busy
    engine.assert_called_once()
    assert app.result is engine.return_value


@pytest.mark.asyncio
async def test_enabling_a_set_twice_while_it_runs_asks_the_engine_once(
    engine: MagicMock, held_engine: threading.Event
) -> None:
    """A second Enable that slips past the disabled button is dropped, never queued."""
    suggestion = SetSuggestion("python", "built-in", ("pypi.org",), 1, 1)
    app = _Host(ShieldEnableSetScreen(suggestion, project=_PROJECT, task_id=_TASK, running=False))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        submit = screen.query_one("#promote-submit", Button)
        await pilot.click("#promote-submit")
        busy = submit.disabled
        screen.post_message(Button.Pressed(submit))
        await pilot.pause()
        held_engine.set()
        await _settle(pilot)

    assert busy
    engine.assert_called_once()
    assert app.result is engine.return_value


async def _break_glass(pilot: Any, app: _Host) -> None:
    """Fill a valid break-glass in and submit it."""
    await pilot.pause()
    screen = app.screen
    screen.query_one("#promote-reason", Input).value = _REASON
    screen.query_one("#promote-expires", Input).value = (_TODAY + timedelta(days=7)).isoformat()
    await pilot.click("#promote-submit")
    await pilot.pause()


@pytest.mark.asyncio
async def test_a_break_glass_is_confirmed_before_anything_is_written(engine: MagicMock) -> None:
    """The confirm spells out what opens, until when and why; only a yes runs it."""
    candidate = _candidate("vault.example.org", TIER_SECURITY)
    app = _Host(_form(candidate))
    async with app.run_test() as pilot:
        await _break_glass(pilot, app)
        confirm = app.screen
        message = str(confirm.query_one("#confirm-message", Static).render())
        called_before = engine.called
        await pilot.click("#btn-confirm")
        await _settle(pilot)

    assert isinstance(confirm, ConfirmDestructiveScreen)
    assert not called_before
    assert "above the security tier" in message
    assert (_TODAY + timedelta(days=7)).isoformat() in message
    assert _REASON in message
    assert engine.call_args.args[3] == REMEDY_OVERRIDE
    assert engine.call_args.kwargs == {
        "set_name": None,
        "reason": _REASON,
        "expires": _TODAY + timedelta(days=7),
    }
    assert app.result is engine.return_value


@pytest.mark.asyncio
async def test_a_declined_break_glass_writes_nothing(engine: MagicMock) -> None:
    """Cancel on the confirm leaves the form open and the policy untouched."""
    app = _Host(_form(_candidate(PRIVATE_LAN_IP, TIER_PRIVATE)))
    async with app.run_test() as pilot:
        await _break_glass(pilot, app)
        await pilot.click("#btn-cancel")
        await pilot.pause()
        screen = app.screen

    engine.assert_not_called()
    assert isinstance(screen, ShieldPromoteScreen)
    assert app.result is _PENDING


@pytest.mark.asyncio
async def test_escape_promotes_nothing(engine: MagicMock) -> None:
    """Cancel is ``None`` — the harvest screen then changes nothing."""
    app = _Host(_form())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    engine.assert_not_called()
    assert app.result is None


@pytest.mark.asyncio
async def test_enabling_a_suggested_set_promotes_the_set_without_a_target(
    engine: MagicMock,
) -> None:
    """A suggestion enables its set for everything it covers: no target, the set's name."""
    suggestion = SetSuggestion("python", "built-in", ("pypi.org", "files.pythonhosted.org"), 3, 4)
    app = _Host(ShieldEnableSetScreen(suggestion, project=_PROJECT, task_id=_TASK, running=False))
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(app.screen.query_one("#enable-set-text", Static).render())
        await pilot.click("#promote-submit")
        await _settle(pilot)

    assert "covers 2 of the 3 targets" in text
    assert "every host in the set" in text
    engine.assert_called_once_with(
        _PROJECT, _TASK, None, REMEDY_SET, set_name="python", reason=None, expires=None
    )
    assert app.result is engine.return_value
