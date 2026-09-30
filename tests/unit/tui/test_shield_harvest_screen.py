# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The harvest screen: one task's harvest, grouped for promotion, and the keys that promote it.

The engine is a double — ``review_egress`` returns a hand-built review — so
these tests pin what the screen does with one: the groups and their order,
that no row is ever dropped, which destinations a row offers and why the
others are closed, the empty states, and what a promotion's result turns
into.  Screens run in a minimal host app, the ``run_test`` pattern of
``test_shield_sets_screen``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.app import App
from textual.widgets import OptionList, Static

from terok.lib.api.shield import (
    REMEDY_HOST_PORT,
    REMEDY_OVERRIDE,
    REMEDY_PROJECT,
    REMEDY_SET,
    REMEDY_TASK,
    TIER_ALLOWLIST,
    TIER_HARD_DENY,
    TIER_OPERATOR_DENY,
    TIER_PRIVATE,
    TIER_SECURITY,
    EgressCandidate,
    EgressReview,
    PromotionResult,
    SetSuggestion,
    tier_heading,
)
from terok.tui import shield_harvest_screen as harvest_mod
from terok.tui.shield_harvest_screen import ShieldHarvestScreen
from terok.tui.shield_promote_screen import ShieldEnableSetScreen, ShieldPromoteScreen
from terok.tui.task_actions import TaskActionsMixin
from tests.testnet import CLOUD_METADATA_IP, CONTAINER_HOSTNAME, HTTPS_PORT, PRIVATE_LAN_IP

_PROJECT = "proj"
_TASK = "7"
_FIRST = "2026-10-01T10:00:00+00:00"
_LAST = "2026-10-02T11:00:00+00:00"
_UNKNOWN_TIER = "t99_future"
"""A tier this terok does not know — it must still be listed, never dropped."""

_REMEDIES: dict[str, tuple[str, ...]] = {
    TIER_ALLOWLIST: (REMEDY_TASK, REMEDY_PROJECT, REMEDY_SET),
    TIER_OPERATOR_DENY: (REMEDY_TASK, REMEDY_PROJECT),
    TIER_SECURITY: (REMEDY_OVERRIDE,),
    TIER_PRIVATE: (REMEDY_OVERRIDE,),
    TIER_HARD_DENY: (),
    _UNKNOWN_TIER: (),
}
"""The remedies shield attaches to each tier."""


def _candidate(
    target: str,
    tier: str = TIER_ALLOWLIST,
    *,
    actions: tuple[str, ...] = ("blocked",),
    remedies: tuple[str, ...] | None = None,
    count: int = 1,
    covered_by: str | None = None,
    suggested_set: str | None = None,
) -> EgressCandidate:
    """One merged target, classified like the engine classifies *tier*."""
    return EgressCandidate(
        target=target,
        tier=tier,
        remedies=_REMEDIES[tier] if remedies is None else remedies,
        actions=actions,
        count=count,
        first_seen=_FIRST,
        last_seen=_LAST,
        ports=(HTTPS_PORT,),
        addresses=(),
        covered_by=covered_by,
        suggested_set=suggested_set,
    )


def _review(
    *candidates: EgressCandidate,
    suggestions: tuple[SetSuggestion, ...] = (),
    audit: bool = True,
    attribution: bool = True,
) -> EgressReview:
    """A review of *candidates*, as the engine hands it over."""
    return EgressReview(
        project=_PROJECT,
        task=_TASK,
        candidates=candidates,
        suggestions=suggestions,
        audit_enabled=audit,
        dns_tier="dnsmasq-live" if attribution else "lookup",
        domain_attribution=attribution,
        suffix_match=attribution,
    )


_PYTHON = SetSuggestion("python", "built-in", ("pypi.org",), 2, 3)
_EVERY_GROUP = _review(
    _candidate("refused.example.org"),
    _candidate("window.example.org", actions=("bypass",)),
    _candidate("down.example.org", actions=("down",)),
    _candidate("pypi.org", suggested_set="python"),
    _candidate("denied.example.org", TIER_OPERATOR_DENY, actions=("security_deny",)),
    _candidate("vault.example.org", TIER_SECURITY, actions=("security_deny",)),
    _candidate(PRIVATE_LAN_IP, TIER_PRIVATE, actions=("private_range",)),
    _candidate(CLOUD_METADATA_IP, TIER_HARD_DENY, actions=("private_range",)),
    _candidate("future.example.org", _UNKNOWN_TIER),
    suggestions=(_PYTHON,),
)
"""One target in every group the screen knows, plus one in a tier it does not."""


class _Host(App):
    """Minimal host: pushes the screen, records notices instead of showing them."""

    def __init__(self, screen: ShieldHarvestScreen) -> None:
        super().__init__()
        self._screen = screen
        self.notices: list[tuple[str, dict[str, Any]]] = []

    def on_mount(self) -> None:
        self.push_screen(self._screen)

    def notify(self, message: str, **kwargs: Any) -> None:  # type: ignore[override]
        self.notices.append((message, kwargs))


@pytest.fixture
def engine() -> Iterator[MagicMock]:
    """Stand in for the engine's review; the operator has one set of their own."""
    corp = SimpleNamespace(name="corp", origin="user", error=None)
    with (
        mock.patch.object(harvest_mod, "review_egress") as review,
        mock.patch.object(harvest_mod, "egress_set_catalog", return_value=(corp,)),
    ):
        review.return_value = _EVERY_GROUP
        yield review


def _screen(**kwargs: Any) -> ShieldHarvestScreen:
    """The screen for task ``proj/7``, running and UP unless told otherwise."""
    kwargs.setdefault("posture", "UP")
    kwargs.setdefault("running", True)
    return ShieldHarvestScreen(_PROJECT, _TASK, **kwargs)


async def _loaded(pilot: Any) -> ShieldHarvestScreen:
    """Wait for the review worker to land, and return the screen."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    screen = pilot.app.screen
    assert isinstance(screen, ShieldHarvestScreen)
    return screen


def _prompts(screen: ShieldHarvestScreen) -> list[str]:
    """Every option's text, headings included, in list order."""
    listing = screen.query_one("#harvest-list", OptionList)
    return [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)]


def _highlight(screen: ShieldHarvestScreen, target: str) -> None:
    """Put the cursor on *target*'s own row — not on a suggestion that mentions it."""
    listing = screen.query_one("#harvest-list", OptionList)
    for index in range(listing.option_count):
        row = screen._candidate_rows.get(listing.get_option_at_index(index).id or "")
        if row is not None and row.target == target:
            listing.highlighted = index
            return
    raise AssertionError(f"{target} is not listed")


def _detail(screen: ShieldHarvestScreen) -> str:
    """The detail pane's text."""
    return str(screen.query_one("#harvest-detail", Static).render())


# ── Grouping ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_groups_follow_what_the_shield_did_and_the_tier_in_the_way() -> None:
    """Suggestions first, the three t40 stories next, break-glass after, the floor last."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        prompts = _prompts(await _loaded(pilot))

    headings = [
        harvest_mod._SUGGESTED_HEADING,
        harvest_mod._GROUP_HEADINGS[harvest_mod._REFUSED],
        harvest_mod._GROUP_HEADINGS[harvest_mod._WINDOW],
        harvest_mod._GROUP_HEADINGS[harvest_mod._DOWN],
        tier_heading(TIER_OPERATOR_DENY),
        tier_heading(TIER_SECURITY),
        tier_heading(TIER_PRIVATE),
        tier_heading(TIER_HARD_DENY),
        tier_heading(_UNKNOWN_TIER),
    ]
    positions = [prompts.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert prompts.index(harvest_mod._GROUP_HEADINGS[harvest_mod._REFUSED]) < next(
        i for i, p in enumerate(prompts) if "refused.example.org" in p
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_no_row_is_ever_dropped() -> None:
    """Every candidate is listed exactly once — the unknown tier's under a heading of its own."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        prompts = _prompts(await _loaded(pilot))

    rows = [prompt for prompt in prompts if not prompt.lstrip().startswith("enable ")]
    for candidate in _EVERY_GROUP.candidates:
        assert sum(candidate.target in row for row in rows) == 1, candidate.target


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_headings_are_not_rows_and_the_cursor_starts_on_one() -> None:
    """Headings are disabled options; the first highlight is the best suggestion."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        listing = screen.query_one("#harvest-list", OptionList)
        heading = listing.get_option_at_index(0)
        highlighted = listing.highlighted_option

    assert heading.disabled
    assert highlighted is not None and "enable python" in str(highlighted.prompt)


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_a_row_carries_its_tries_ports_and_status() -> None:
    """What separates a stray probe from a dependency, and what already grants it."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        prompts = _prompts(await _loaded(pilot))

    row = next(prompt for prompt in prompts if "pypi.org" in prompt and "enable" not in prompt)
    assert "1×" in row
    assert str(HTTPS_PORT) in row
    assert "suggested set python" in row
    vault = next(prompt for prompt in prompts if "vault.example.org" in prompt)
    assert "break-glass only" in vault


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_the_footer_advertises_every_key_the_screen_answers_to() -> None:
    """The keys are the screen's interface — enter included, which the list itself binds."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        bindings = (await _loaded(pilot)).active_bindings

    shown = {key: bindings[key][1] for key in ("enter", "t", "p", "s", "o", "r", "q")}
    assert all(binding.show for binding in shown.values())
    assert shown["enter"].description == "Promote…"


# ── The detail pane and the destinations ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_the_detail_says_why_each_closed_destination_is_closed() -> None:
    """A security-tier host: task, project and set are closed, each with the engine's reason."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        _highlight(screen, "vault.example.org")
        await pilot.pause()
        detail = _detail(screen)

    vault = next(c for c in _EVERY_GROUP.candidates if c.target == "vault.example.org")
    assert "refused by the security tier" in detail
    assert detail.count("closed —") == 3
    assert vault.refusal(REMEDY_PROJECT) in detail.replace("\n", " ")
    assert "Agents keep relaying keys through the vault" in detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "key", "destination"),
    [
        pytest.param("refused.example.org", "t", REMEDY_TASK, id="task"),
        pytest.param("refused.example.org", "p", REMEDY_PROJECT, id="project"),
        pytest.param("pypi.org", "s", REMEDY_SET, id="set"),
        pytest.param("vault.example.org", "o", REMEDY_OVERRIDE, id="override"),
        pytest.param("denied.example.org", "enter", REMEDY_TASK, id="enter-first-open"),
        pytest.param("pypi.org", "enter", REMEDY_SET, id="enter-takes-the-suggested-set"),
    ],
)
@pytest.mark.usefixtures("engine")
async def test_a_key_opens_the_form_on_its_destination(
    target: str, key: str, destination: str
) -> None:
    """Each shortcut opens the form preselected; enter opens it on the first open destination."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        _highlight(screen, target)
        await pilot.press(key)
        await pilot.pause()
        form = app.screen

    assert isinstance(form, ShieldPromoteScreen)
    assert form._candidate.target == target
    assert form._destination == destination


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "key"),
    [
        pytest.param("refused.example.org", "o", id="t40-has-no-break-glass"),
        pytest.param("vault.example.org", "p", id="security-takes-only-break-glass"),
        pytest.param(CLOUD_METADATA_IP, "enter", id="hard-floor-takes-nothing"),
    ],
)
@pytest.mark.usefixtures("engine")
async def test_a_closed_destination_explains_itself_instead_of_opening_the_form(
    target: str, key: str
) -> None:
    """The refusal is the answer: a warning naming why, and no form."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        _highlight(screen, target)
        await pilot.press(key)
        await pilot.pause()
        on_top = app.screen

    assert on_top is screen
    message, kwargs = app.notices[-1]
    assert target in message
    assert kwargs["severity"] == "warning"


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_a_suggestion_opens_the_enable_confirm() -> None:
    """A suggested set is a whole decision already: one confirm, not the form."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        await _loaded(pilot)
        await pilot.press("enter")
        await pilot.pause()
        confirm = app.screen

    assert isinstance(confirm, ShieldEnableSetScreen)
    assert confirm._set_suggestion == _PYTHON


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_the_form_suggests_covering_sets_then_the_operators_own() -> None:
    """The set-name field offers the set that covers the target first, then the user's sets."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        _highlight(screen, "pypi.org")
        await pilot.press("s")
        await pilot.pause()
        form = app.screen

    assert isinstance(form, ShieldPromoteScreen)
    assert form._set_names == ("python", "corp")


# ── Empty and failed reviews ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_review_with_audit_off_is_no_all_clear(engine: MagicMock) -> None:
    """With audit off nothing was recorded: no posture note may suggest otherwise."""
    engine.return_value = _review(audit=False)
    app = _Host(_screen(posture="DOWN"))
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        detail = _detail(screen)
        listed = screen.query_one("#harvest-list", OptionList).display

    assert not listed
    assert "audit was off" in detail
    assert "shield is down" not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("posture", "running", "note"),
    [
        pytest.param("UP", True, "what it refuses from now on lands here", id="up"),
        pytest.param("DOWN", True, "as accepted only because the shield is down", id="down"),
        pytest.param("DISENGAGED", True, "it enforces nothing", id="disengaged"),
        pytest.param("OFFLINE", False, "not running", id="stopped"),
    ],
)
async def test_an_empty_review_says_what_the_posture_records(
    engine: MagicMock, posture: str, running: bool, note: str
) -> None:
    """Under DOWN nothing is refused — the empty state must not claim refusals are recorded."""
    engine.return_value = _review()
    app = _Host(_screen(posture=posture, running=running))
    async with app.run_test() as pilot:
        detail = _detail(await _loaded(pilot))

    assert "Nothing harvested" in detail
    assert note in detail


@pytest.mark.asyncio
async def test_no_domain_attribution_is_said_above_the_list(engine: MagicMock) -> None:
    """On a tier without dnsmasq the review names no sets, and the screen says why."""
    engine.return_value = _review(_candidate(PRIVATE_LAN_IP, TIER_PRIVATE), attribution=False)
    app = _Host(_screen())
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        note = screen.query_one("#harvest-note", Static)
        shown, text = note.display, str(note.render())

    assert shown
    assert "No domain attribution" in text


@pytest.mark.asyncio
async def test_a_review_that_cannot_be_read_is_shown_and_announced(engine: MagicMock) -> None:
    """A task gone or a project broken is an answer on the screen, not a dead worker."""
    engine.side_effect = SystemExit("Unknown task 7")
    app = _Host(_screen())
    async with app.run_test() as pilot:
        detail = _detail(await _loaded(pilot))

    assert "Could not read the harvest: Unknown task 7" in detail
    assert app.notices[-1][1]["severity"] == "error"


@pytest.mark.asyncio
@pytest.mark.usefixtures("engine")
async def test_the_header_shows_the_posture_and_an_open_window() -> None:
    """Who, the posture, the window and the review's own standing, in one line."""
    closes = datetime.now() + timedelta(minutes=5)
    app = _Host(_screen(bypass_closes_at=closes))
    async with app.run_test() as pilot:
        header = str((await _loaded(pilot)).query_one("#harvest-header", Static).render())

    assert f"{_PROJECT}/{_TASK}" in header
    assert f"bypass window open until {closes:%H:%M:%S}" in header
    assert "DNS tier dnsmasq-live" in header
    assert "audit on" in header


# ── Results ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_result_is_announced_and_the_review_reloads_on_its_target(
    engine: MagicMock,
) -> None:
    """What took effect and when, then a fresh review with the cursor where it was."""
    app = _Host(_screen())
    async with app.run_test() as pilot:
        screen = await _loaded(pilot)
        result = PromotionResult(
            "down.example.org",
            REMEDY_PROJECT,
            notes=("Added down.example.org to shield.allow",),
            next_start=True,
        )
        screen._on_promoted(result)
        screen = await _loaded(pilot)
        highlighted = screen.query_one("#harvest-list", OptionList).highlighted_option

    message, kwargs = app.notices[-1]
    assert message == "Added down.example.org to shield.allow"
    assert kwargs["title"] == ("down.example.org → the project — applies at next start")
    assert kwargs["severity"] == "information"
    assert engine.call_count == 2
    assert highlighted is not None and "down.example.org" in str(highlighted.prompt)


@pytest.mark.parametrize(
    ("result", "title"),
    [
        pytest.param(
            PromotionResult("a.example.org", REMEDY_TASK, live=True),
            "a.example.org → this task — live on this task now",
            id="task-live",
        ),
        pytest.param(
            PromotionResult("a.example.org", REMEDY_TASK, next_start=True),
            "a.example.org → this task — applies at next start",
            id="task-stopped",
        ),
        pytest.param(
            PromotionResult("a.example.org", REMEDY_PROJECT, live=True, next_start=True),
            "a.example.org → the project — live on this task now; applies to every "
            "task at its next start",
            id="project-live",
        ),
        pytest.param(
            PromotionResult("a.example.org", REMEDY_SET, set_name="corp", next_start=True),
            "a.example.org → the egress set corp — applies at next start",
            id="user-set",
        ),
        pytest.param(
            PromotionResult(None, REMEDY_SET, set_name="python", next_start=True),
            "Egress set python enabled — applies at next start",
            id="enabled-set",
        ),
        pytest.param(
            PromotionResult("a.example.org", REMEDY_OVERRIDE, live=True, next_start=True),
            "a.example.org → a break-glass override — live on this task now; "
            "applies to every task at its next start",
            id="override",
        ),
        pytest.param(
            PromotionResult("a.example.org", REMEDY_TASK, failed=True),
            "a.example.org: nothing granted",
            id="failed",
        ),
    ],
)
def test_a_result_title_says_what_moved_where_and_when(result: PromotionResult, title: str) -> None:
    """One line before the engine's own notes: the move, and when it takes effect."""
    assert harvest_mod._title(result) == title


def test_a_host_loopback_row_names_its_only_way_in() -> None:
    """The host's own loopback is reached by a per-port grant, never a promotion."""
    candidate = _candidate(CONTAINER_HOSTNAME, TIER_HARD_DENY, remedies=(REMEDY_HOST_PORT,))

    assert harvest_mod._status(candidate) == "a per-port localhost grant only"


# ── Opening the screen ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_action_opens_the_screen_with_the_tasks_shield_context() -> None:
    """The posture, the window and whether the container runs come from the selected task."""
    closes = datetime.now() + timedelta(minutes=3)
    stub = SimpleNamespace(
        notify=MagicMock(),
        push_screen=AsyncMock(),
        current_project_name=_PROJECT,
        current_task=SimpleNamespace(
            task_id=_TASK, shield_state="UP", container_state="running", bypass_closes_at=closes
        ),
    )

    await TaskActionsMixin._action_shield_harvest(stub)

    screen = stub.push_screen.call_args.args[0]
    assert isinstance(screen, ShieldHarvestScreen)
    assert (screen._project, screen._task_id) == (_PROJECT, _TASK)
    assert (screen._posture, screen._task_running, screen._bypass_closes_at) == ("UP", True, closes)


@pytest.mark.asyncio
async def test_the_action_needs_a_task() -> None:
    """With nothing selected there is no harvest to review."""
    stub = SimpleNamespace(
        notify=MagicMock(), push_screen=AsyncMock(), current_project_name=None, current_task=None
    )

    await TaskActionsMixin._action_shield_harvest(stub)

    stub.push_screen.assert_not_called()
    stub.notify.assert_called_once_with("No task selected.")
