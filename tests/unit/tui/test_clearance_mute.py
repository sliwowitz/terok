# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Muting a task's clearance prompts, from the clearance screen and the task menu.

Mute silences the question, never the shield: these tests pin that terok asks
the hub for an explicit state, addresses it by the short container id the hub
keys on — a distinct string from the podman name and the full id, so passing
either of those cannot pass — and reports honestly when it could not.  The
last section pins the other half of ``services.clearance: off``: with nothing
to prompt anywhere, the clearance screen is not opened on an empty promise.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import MethodType, SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from terok.lib.api.clearance import MuteResult
from terok.tui.clearance_screen import ClearanceScreen, _PendingRequest
from terok.tui.task_actions import TaskActionsMixin
from tests.testnet import HTTPS_PORT, TEST_IP
from tests.unit.tui.tui_test_helpers import import_app

_CONTAINER_NAME = "terok-proj-cli-task1"
"""The podman name — what the notice shows, never what the hub is asked about."""
_FULL_ID = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
"""The full container id — not the hub's key either."""
_SHORT_ID = _FULL_ID[:12]
"""The 12-character short id: the key the hub's events carry and its mutes match."""


# ── the clearance screen's one-click mute ────────────────────────────────


@pytest.fixture
def app_double() -> Iterator[SimpleNamespace]:
    """Stand in for ``Screen.app``, which is a read-only property on the class."""
    double = SimpleNamespace(notify=MagicMock())
    with mock.patch.object(ClearanceScreen, "app", double):
        yield double


def _request(**overrides: str) -> _PendingRequest:
    """A pending request from task ``proj/task1``, carrying all three identities apart."""
    fields = {
        "container_id": _SHORT_ID,
        "container_name": _CONTAINER_NAME,
        "project": "proj",
        "task_id": "task1",
    }
    fields.update(overrides)
    return _PendingRequest(nid=1, summary="s", body="b", **fields)


def _screen_stub(request: _PendingRequest | None, subscriber: object) -> ClearanceScreen:
    """A screen with one highlighted request and a stub subscriber."""
    screen = ClearanceScreen.__new__(ClearanceScreen)
    screen._subscriber = subscriber
    screen._pending = {request.nid: request} if request else {}
    screen._highlighted_request = MethodType(lambda _self: request, screen)  # type: ignore[assignment]
    screen.run_worker = MagicMock()  # type: ignore[method-assign]
    return screen


def test_mute_asks_the_hub_for_the_highlighted_task(app_double: SimpleNamespace) -> None:
    """``m`` routes to the container the highlighted request came from."""
    screen = _screen_stub(_request(), subscriber=MagicMock())

    screen.action_mute_selected()

    screen.run_worker.assert_called_once()
    assert screen.run_worker.call_args.kwargs["group"] == "clearance-mute"
    assert screen.run_worker.call_args.kwargs["name"] == f"clearance-mute:{_SHORT_ID}"
    screen.run_worker.call_args.args[0].close()  # the coroutine is the worker's to run


def test_mute_without_a_subscriber_says_so(app_double: SimpleNamespace) -> None:
    """No hub connection means no mute, and the operator is told rather than ignored."""
    screen = _screen_stub(_request(), subscriber=None)

    screen.action_mute_selected()

    screen.run_worker.assert_not_called()
    app_double.notify.assert_called_once()


def test_mute_without_a_selection_does_nothing(app_double: SimpleNamespace) -> None:
    """Nothing highlighted, nothing muted."""
    screen = _screen_stub(None, subscriber=MagicMock())

    screen.action_mute_selected()

    screen.run_worker.assert_not_called()


@pytest.mark.asyncio
async def test_mute_addresses_the_hub_by_the_short_id(app_double: SimpleNamespace) -> None:
    """The subscriber is asked under the short id alone — the name is for the notice."""
    subscriber = SimpleNamespace(set_mute=AsyncMock(return_value=True))
    screen = _screen_stub(None, subscriber=subscriber)

    with mock.patch("terok.lib.api.clearance.task_audit_enabled", return_value=True):
        await ClearanceScreen._mute(screen, _request())

    subscriber.set_mute.assert_awaited_once_with(_SHORT_ID, True)
    message = app_double.notify.call_args.args[0]
    assert _CONTAINER_NAME in message
    assert "still enforced and logged" in message


@pytest.mark.asyncio
async def test_an_unknown_mute_state_is_reported_as_unknown(app_double: SimpleNamespace) -> None:
    """``None`` means the call did not land — the notice must not claim either state."""
    screen = _screen_stub(None, subscriber=SimpleNamespace(set_mute=AsyncMock(return_value=None)))

    await ClearanceScreen._mute(screen, _request())

    assert "Could not mute" in app_double.notify.call_args.args[0]
    assert app_double.notify.call_args.kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_a_hub_that_kept_asking_is_reported(app_double: SimpleNamespace) -> None:
    """The notice reflects the hub's answer, not the request."""
    screen = _screen_stub(None, subscriber=SimpleNamespace(set_mute=AsyncMock(return_value=False)))

    await ClearanceScreen._mute(screen, _request())

    assert app_double.notify.call_args.kwargs["severity"] == "error"


@pytest.mark.asyncio
async def test_mute_warns_when_the_tasks_audit_is_off(app_double: SimpleNamespace) -> None:
    """With audit off, a muted task's refusals are neither prompted nor recorded."""
    screen = _screen_stub(None, subscriber=SimpleNamespace(set_mute=AsyncMock(return_value=True)))

    with mock.patch(
        "terok.lib.api.clearance.task_audit_enabled", return_value=False
    ) as audit_probe:
        await ClearanceScreen._mute(screen, _request())

    audit_probe.assert_called_once_with("proj", "task1")
    assert "neither prompted nor recorded" in app_double.notify.call_args.args[0]
    assert app_double.notify.call_args.kwargs["severity"] == "warning"


@pytest.mark.asyncio
async def test_a_request_without_a_task_skips_the_audit_probe(
    app_double: SimpleNamespace,
) -> None:
    """A container terok did not launch carries no dossier; the mute still lands."""
    screen = _screen_stub(None, subscriber=SimpleNamespace(set_mute=AsyncMock(return_value=True)))

    with mock.patch("terok.lib.api.clearance.task_audit_enabled") as audit_probe:
        await ClearanceScreen._mute(screen, _request(project="", task_id=""))

    audit_probe.assert_not_called()
    assert "still enforced and logged" in app_double.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_m_mutes_the_highlighted_requests_task_on_the_mounted_screen() -> None:
    """End to end on the real screen: requests arrive, one is highlighted, ``m`` mutes its task.

    Nothing here stands in for the highlight lookup: two requests from two
    containers are queued and the second is highlighted, so a lookup that
    finds nothing (``m`` silently doing nothing) or the wrong request cannot
    pass.  The hub is asked under the short id the event carried.
    """
    from textual.app import App

    from terok.tui.clearance_screen import NotificationPosted

    subscriber = MagicMock()
    subscriber.start = AsyncMock()
    subscriber.stop = AsyncMock()
    subscriber.set_mute = AsyncMock(return_value=True)
    notifier = MagicMock()
    notifier.disconnect = AsyncMock()
    other_id = "ffeeddccbbaa"

    class _Host(App):
        """Minimal host that pushes the clearance screen so it mounts and subscribes."""

        def on_mount(self) -> None:
            self.push_screen(ClearanceScreen())

    def _blocked(nid: int, container_id: str) -> NotificationPosted:
        """A blocked connection from *container_id*, queued with Allow/Deny actions."""
        return NotificationPosted(
            nid=nid,
            summary=f"Blocked: {TEST_IP}:{HTTPS_PORT}",
            body="Protocol: TCP",
            actions=[("allow", "Allow"), ("deny", "Deny")],
            replaces_id=0,
            container_id=container_id,
            container_name=_CONTAINER_NAME,
            project="proj",
            task_id="task1",
        )

    with (
        mock.patch("terok.lib.api.clearance.MultiSocketSubscriber", return_value=subscriber),
        mock.patch("terok.lib.api.clearance.CallbackNotifier", return_value=notifier),
        mock.patch("terok.lib.api.clearance.task_audit_enabled", return_value=True),
    ):
        app = _Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ClearanceScreen)
            screen.post_message(_blocked(7, other_id))
            screen.post_message(_blocked(8, _SHORT_ID))
            await pilot.pause()
            screen.query_one("#pending-list").index = 1  # the operator moves to the second
            await pilot.pause()
            await pilot.press("m")
            await app.workers.wait_for_complete()
            await pilot.pause()

    subscriber.set_mute.assert_awaited_once_with(_SHORT_ID, True)


# ── the task action list ─────────────────────────────────────────────────


def _menu_stub(*, project: str | None = "proj") -> SimpleNamespace:
    """App double with task ``task1`` selected (or nothing) and a recording worker runner."""
    stub = SimpleNamespace(notify=MagicMock(), run_worker=MagicMock())
    stub._set_clearance_mute = MethodType(TaskActionsMixin._set_clearance_mute, stub)
    stub.current_project_name = project
    stub.current_task = SimpleNamespace(task_id="task1", mode="cli") if project else None
    return stub


@pytest.mark.parametrize(
    ("action", "muted"),
    [
        pytest.param("_action_clearance_mute", True, id="mute"),
        pytest.param("_action_clearance_unmute", False, id="unmute"),
    ],
)
def test_menu_actions_send_an_explicit_state(action: str, muted: bool) -> None:
    """Mute and unmute are separate verbs — a toggle would drift from the hub's memory."""
    stub = _menu_stub()

    getattr(TaskActionsMixin, action)(stub)

    stub.run_worker.assert_called_once()
    assert stub.run_worker.call_args.kwargs["group"] == "clearance-mute"
    work = stub.run_worker.call_args.args[0]
    answer = MuteResult(muted, "done")
    with mock.patch(
        "terok.lib.api.clearance.set_task_prompts_muted", return_value=answer
    ) as set_muted:
        assert work() is answer
    set_muted.assert_called_once_with("proj", "task1", muted=muted)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(RuntimeError("boom"), id="exception"),
        pytest.param(SystemExit("Project 'proj' not found"), id="broken-project"),
    ],
)
def test_menu_worker_folds_a_failure_into_an_error_result(failure: BaseException) -> None:
    """Whatever the library raises comes back as a result the handler announces."""
    stub = _menu_stub()
    TaskActionsMixin._action_clearance_mute(stub)
    work = stub.run_worker.call_args.args[0]

    with mock.patch("terok.lib.api.clearance.set_task_prompts_muted", side_effect=failure):
        result = work()

    assert result.muted is None
    assert result.severity == "error"
    assert str(failure) in result.message


def test_menu_action_needs_a_task() -> None:
    """With nothing selected the action says so instead of guessing."""
    stub = _menu_stub(project=None)

    TaskActionsMixin._action_clearance_mute(stub)

    stub.run_worker.assert_not_called()
    stub.notify.assert_called_once_with("No task selected.")


# ── the app announces what the worker found ──────────────────────────────


def _worker_event(state: object, *, result: object = None) -> MagicMock:
    """A finished ``clearance-mute`` worker in *state* carrying *result*."""
    worker = MagicMock()
    worker.group = "clearance-mute"
    worker.name = "clearance-mute:proj:task1"
    worker.result = result
    worker.error = RuntimeError("escaped")
    event = MagicMock()
    event.worker = worker
    event.state = state
    return event


def test_app_announces_the_mute_result_with_its_severity() -> None:
    """Success and refusal alike reach the operator, at the result's own severity."""
    import asyncio

    app_mod, app_class = import_app()
    instance = app_class()
    instance.notify = MagicMock()
    event = _worker_event(
        app_mod.WorkerState.SUCCESS,
        result=MuteResult(None, "Clearance is off for proj", "warning"),
    )

    asyncio.run(app_class.handle_worker_state_changed(instance, event))

    instance.notify.assert_called_once_with("Clearance is off for proj", severity="warning")


def test_app_announces_a_mute_worker_that_errored() -> None:
    """A worker that still ends in ERROR is announced, never silently dropped."""
    import asyncio

    app_mod, app_class = import_app()
    instance = app_class()
    instance.notify = MagicMock()

    asyncio.run(
        app_class.handle_worker_state_changed(instance, _worker_event(app_mod.WorkerState.ERROR))
    )

    message = instance.notify.call_args.args[0]
    assert "escaped" in message
    assert instance.notify.call_args.kwargs["severity"] == "error"


# ── the clearance screen's entry points under ``services.clearance: off`` ──


def _entry_stub() -> SimpleNamespace:
    """App double with project ``proj`` selected, a notifier and a screen stack."""
    stub = SimpleNamespace(current_project_name="proj", notify=MagicMock(), push_screen=AsyncMock())
    stub._open_clearance_screen = MethodType(TaskActionsMixin._open_clearance_screen, stub)
    return stub


@pytest.mark.asyncio
async def test_the_clearance_screen_opens_while_something_may_prompt() -> None:
    """No notice — a hub listens, or clearance is on somewhere relevant — opens the screen."""
    stub = _entry_stub()

    with mock.patch("terok.lib.api.clearance.clearance_off_notice", return_value=None) as probe:
        await stub._open_clearance_screen()

    probe.assert_called_once_with("proj")
    assert isinstance(stub.push_screen.await_args.args[0], ClearanceScreen)
    stub.notify.assert_not_called()


@pytest.mark.asyncio
async def test_clearance_off_everywhere_says_so_instead_of_an_empty_screen() -> None:
    """A screen that can never fill would read as broken; the notice says why it stays shut."""
    stub = _entry_stub()
    notice = "Clearance is off globally and for proj (services.clearance: off)"

    with mock.patch("terok.lib.api.clearance.clearance_off_notice", return_value=notice):
        await stub._open_clearance_screen()

    stub.push_screen.assert_not_awaited()
    stub.notify.assert_called_once_with(notice, severity="warning", timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["_action_shield_interactive", "_action_shield_watch"])
async def test_every_task_menu_entry_goes_through_the_gate(action: str) -> None:
    """``v`` / ``W`` and the task menu's clearance entry open the screen the same way."""
    stub = SimpleNamespace(_open_clearance_screen=AsyncMock())

    await getattr(TaskActionsMixin, action)(stub)

    stub._open_clearance_screen.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_app_wide_entry_goes_through_the_gate() -> None:
    """``C`` on the main screen and the palette's *Shield Clearance (live)* too."""
    _app_mod, app_class = import_app()
    stub = SimpleNamespace(_open_clearance_screen=AsyncMock())

    await app_class.action_show_clearance(stub)

    stub._open_clearance_screen.assert_awaited_once()
