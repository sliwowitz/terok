# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The timed allow-all window in the TUI: the modal's answers and what each one does.

Driven unbound on a ``SimpleNamespace`` app double, the way
``test_shield_posture_actions`` drives the posture actions — no Textual app boots.
"""

from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from terok.tui import task_actions as task_actions_mod
from terok.tui.task_actions import TaskActionsMixin
from tests.testfs import MOCK_BASE

_CONTAINER = "terok-proj-cli-task1"
_TASK_DIR = Path(MOCK_BASE) / "tasks" / "task1"


def _app_stub() -> SimpleNamespace:
    """App double recording ``_action_shield_toggle`` instead of running a worker."""
    stub = SimpleNamespace(notify=MagicMock(), _action_shield_toggle=MagicMock())
    stub._on_shield_bypass_result = MethodType(TaskActionsMixin._on_shield_bypass_result, stub)
    return stub


def test_a_duration_opens_the_window_for_it() -> None:
    """The modal's duration reaches ``ShieldManager.bypass``, and the granted one comes back."""
    stub = _app_stub()

    stub._on_shield_bypass_result("30s")

    stub._action_shield_toggle.assert_called_once()
    action, shield_fn = stub._action_shield_toggle.call_args.args
    assert action == "bypass"
    with mock.patch.object(task_actions_mod, "task_shield_manager") as factory:
        factory.return_value.bypass.return_value = "30s"
        granted = shield_fn(_CONTAINER, _TASK_DIR)
    factory.assert_called_once_with(_TASK_DIR)
    factory.return_value.bypass.assert_called_once_with(_CONTAINER, "30s")
    assert granted == "30s"


def test_an_empty_duration_closes_the_window() -> None:
    """Clearing the field is how the modal says "close it now"."""
    stub = _app_stub()

    stub._on_shield_bypass_result("")

    action, shield_fn = stub._action_shield_toggle.call_args.args
    assert action == "bypass-off"
    with mock.patch.object(task_actions_mod, "task_shield_manager") as factory:
        shield_fn(_CONTAINER, _TASK_DIR)
    factory.return_value.bypass_off.assert_called_once_with(_CONTAINER)


def test_the_window_manager_carries_teroks_config() -> None:
    """A bare manager never sees the kill switch; the one the window uses is built with it."""
    from terok.lib.api.shield import SandboxConfig, task_shield_manager

    disabled = SandboxConfig(shield_disabled=True)
    with mock.patch(
        "terok.lib.orchestration.task_runners.shield.make_sandbox_config", return_value=disabled
    ):
        manager = task_shield_manager(_TASK_DIR)

    assert manager.disabled
    with pytest.raises(RuntimeError, match="no window to open"):
        manager.bypass(_CONTAINER, "5m")


def _reporter() -> SimpleNamespace:
    """App double running the real result reporter against a recording ``notify``."""
    stub = SimpleNamespace(notify=MagicMock())
    stub._report_shield_action = MethodType(TaskActionsMixin._report_shield_action, stub)
    return stub


def test_an_opened_window_is_announced_with_its_duration_and_limits() -> None:
    """Opening an allow-all is confirmed, with what it does not let through."""
    stub = _reporter()

    stub._report_shield_action("bypass", "task1", None, "5m")

    message = stub.notify.call_args.args[0]
    assert "open for task task1 for 5m" in message
    assert "security tier and private ranges stay refused" in message


def test_a_closed_window_is_announced() -> None:
    """Closing it early is confirmed too — silence would read as nothing happened."""
    stub = _reporter()

    stub._report_shield_action("bypass-off", "task1", None, None)

    assert stub.notify.call_args.args[0] == "Bypass window closed for task task1"


@pytest.mark.parametrize(
    ("action", "phrase"),
    [
        pytest.param("bypass", "open the bypass window", id="open"),
        pytest.param("bypass-off", "close the bypass window", id="close"),
        pytest.param("up", "raise the shield", id="up"),
    ],
)
def test_a_failed_shield_action_is_announced_as_an_error(action: str, phrase: str) -> None:
    """The failure is as loud as the success, and says which action failed."""
    stub = _reporter()

    stub._report_shield_action(action, "task1", "posture is down", None)

    message = stub.notify.call_args.args[0]
    assert phrase in message
    assert "posture is down" in message
    assert stub.notify.call_args.kwargs["severity"] == "error"


def test_the_app_routes_shield_results_through_the_reporter() -> None:
    """The worker's ``(project, task, error, detail)`` reaches the reporter by action name."""
    import asyncio

    from tests.unit.tui.tui_test_helpers import import_app

    app_mod, app_class = import_app()
    instance = app_class()
    instance.current_task = None
    instance._report_shield_action = MagicMock()
    worker = MagicMock()
    worker.group = "shield-action"
    worker.name = "shield-action:bypass:proj:task1"
    worker.result = ("proj", "task1", None, "5m")
    event = MagicMock(worker=worker, state=app_mod.WorkerState.SUCCESS)

    asyncio.run(app_class.handle_worker_state_changed(instance, event))

    instance._report_shield_action.assert_called_once_with("bypass", "task1", None, "5m")


def test_cancelling_touches_nothing() -> None:
    """Escape must leave the shield exactly as it was."""
    stub = _app_stub()

    stub._on_shield_bypass_result(None)

    stub._action_shield_toggle.assert_not_called()


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        pytest.param("5m", "5m", id="duration"),
        pytest.param("  2h  ", "2h", id="stripped"),
        pytest.param("", "", id="empty-closes"),
    ],
)
def test_modal_returns_the_submitted_duration(submitted: str, expected: str) -> None:
    """``ShieldBypassScreen`` dismisses with the duration, or ``""`` to close."""
    from terok.tui.shield_bypass_screen import ShieldBypassScreen

    screen = ShieldBypassScreen("5m")
    with mock.patch.object(ShieldBypassScreen, "dismiss") as dismiss:
        screen.on_input_submitted(SimpleNamespace(value=submitted))  # type: ignore[arg-type]
    dismiss.assert_called_once_with(expected)


def test_modal_cancel_dismisses_with_none() -> None:
    """Cancel is distinguishable from "close the window": ``None`` versus ``""``."""
    from terok.tui.shield_bypass_screen import ShieldBypassScreen

    screen = ShieldBypassScreen("5m")
    with mock.patch.object(ShieldBypassScreen, "dismiss") as dismiss:
        screen.action_cancel()
    dismiss.assert_called_once_with(None)


def test_bypass_action_refuses_while_the_kill_switch_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the firewall disabled there is no window to open, and the TUI says so."""
    cfg = SimpleNamespace(
        shield_disable_firewall_no_protection=True,
        shield_security_hint="see the docs",
        shield_bypass_duration="5m",
    )
    monkeypatch.setattr(task_actions_mod, "get_config", lambda: cfg)
    stub = SimpleNamespace(
        notify=MagicMock(),
        push_screen=AsyncMock(),
        current_project_name="proj",
        current_task=SimpleNamespace(task_id="task1"),
    )
    stub._notify_shield_disabled = MethodType(TaskActionsMixin._notify_shield_disabled, stub)

    import asyncio

    asyncio.run(TaskActionsMixin._action_shield_bypass(stub))

    stub.push_screen.assert_not_called()
    stub.notify.assert_called_once()


def _live_app_stub(monkeypatch: pytest.MonkeyPatch, *, task: object | None) -> SimpleNamespace:
    """App double with the kill-switch off and *task* selected (or nothing selected)."""
    cfg = SimpleNamespace(
        shield_disable_firewall_no_protection=False,
        shield_security_hint="see the docs",
        shield_bypass_duration="45s",
    )
    monkeypatch.setattr(task_actions_mod, "get_config", lambda: cfg)
    stub = SimpleNamespace(
        notify=MagicMock(),
        push_screen=AsyncMock(),
        current_project_name="proj" if task is not None else None,
        current_task=task,
    )
    stub._notify_shield_disabled = MethodType(TaskActionsMixin._notify_shield_disabled, stub)
    stub._on_shield_bypass_result = MethodType(TaskActionsMixin._on_shield_bypass_result, stub)
    return stub


def test_bypass_action_asks_with_the_configured_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The modal opens prefilled from ``shield.bypass_duration``, wired to the handler."""
    import asyncio

    from terok.tui.shield_bypass_screen import ShieldBypassScreen

    stub = _live_app_stub(monkeypatch, task=SimpleNamespace(task_id="task1"))

    asyncio.run(TaskActionsMixin._action_shield_bypass(stub))

    screen, callback = stub.push_screen.call_args.args
    assert isinstance(screen, ShieldBypassScreen)
    assert screen._duration == "45s"
    assert callback == stub._on_shield_bypass_result


def test_bypass_action_needs_a_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing selected the action says so instead of opening a modal."""
    import asyncio

    stub = _live_app_stub(monkeypatch, task=None)

    asyncio.run(TaskActionsMixin._action_shield_bypass(stub))

    stub.push_screen.assert_not_called()
    stub.notify.assert_called_once_with("No task selected.")


@pytest.mark.asyncio
async def test_modal_shows_the_consequence_above_the_prefilled_duration() -> None:
    """Opening an allow-all is stated, not implied — and the field starts at the default."""
    from textual.app import App
    from textual.widgets import Input, Static

    from terok.tui.shield_bypass_screen import ShieldBypassScreen

    class _Host(App):
        """Minimal host that pushes the screen so its widgets can mount."""

        def on_mount(self) -> None:
            self.push_screen(ShieldBypassScreen("2h"))

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ShieldBypassScreen)
        # What the mounted widget shows, not the constant it was built from:
        # the wording is the fix, so the screen must be what says it.
        shown = screen.query_one("#bypass-consequence", Static).render()
        consequence = " ".join(str(shown).split())
        box = screen.query_one("#bypass-box", Input)

    assert "accepted and logged" in consequence
    assert "every destination" not in consequence  # the window sits below the security tier
    assert "stay refused" in consequence
    assert "below the security tier" in consequence
    assert "hard-deny floor" in consequence and "always does" in consequence
    assert "only while the shield is up" in consequence
    assert box.value == "2h"
    assert "close it now" in str(box.border_subtitle)
