# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The shield posture actions of ``TaskActionsMixin``: down, up, disengage, and the kill-switch guard.

Driven unbound on a ``SimpleNamespace`` app double, the way
``test_login_action`` drives ``_action_login`` — no Textual app boots.
"""

from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import pytest

from terok.tui import task_actions as task_actions_mod
from terok.tui.task_actions import TaskActionsMixin
from tests.testfs import MOCK_BASE

_POSTURE_ACTIONS = [
    pytest.param("_action_shield_down", "down", id="down"),
    pytest.param("_action_shield_up", "up", id="up"),
    pytest.param("_action_shield_disengaged", "down", id="disengaged"),
    pytest.param("action_shield_down_from_main", "down", id="down-from-main"),
    pytest.param("action_shield_up_from_main", "up", id="up-from-main"),
    pytest.param("action_shield_disengaged_from_main", "down", id="disengaged-from-main"),
]


def _app_stub(monkeypatch: pytest.MonkeyPatch, *, disabled: bool) -> SimpleNamespace:
    """App double whose config reports the kill-switch as *disabled*.

    ``_action_shield_toggle`` is recorded, not run; the guard and the
    disengage action are the real mixin methods bound onto the double so
    the actions reach them the way the app does.
    """
    cfg = SimpleNamespace(
        shield_disable_firewall_no_protection=disabled, shield_security_hint="see the docs"
    )
    monkeypatch.setattr(task_actions_mod, "get_config", lambda: cfg)
    stub = SimpleNamespace(notify=MagicMock(), _action_shield_toggle=MagicMock())
    for name in ("_notify_shield_disabled", "_action_shield_disengaged"):
        setattr(stub, name, MethodType(getattr(TaskActionsMixin, name), stub))
    return stub


@pytest.mark.parametrize(("action", "posture"), _POSTURE_ACTIONS)
def test_posture_action_toggles_when_firewall_enabled(
    monkeypatch: pytest.MonkeyPatch, action: str, posture: str
) -> None:
    """With the kill-switch off, each action hands its posture to ``_action_shield_toggle``."""
    stub = _app_stub(monkeypatch, disabled=False)

    getattr(TaskActionsMixin, action)(stub)

    stub._action_shield_toggle.assert_called_once()
    assert stub._action_shield_toggle.call_args.args[0] == posture
    stub.notify.assert_not_called()


@pytest.mark.parametrize(("action", "_posture"), _POSTURE_ACTIONS)
def test_posture_action_refuses_when_firewall_disabled(
    monkeypatch: pytest.MonkeyPatch, action: str, _posture: str
) -> None:
    """With the kill-switch set, each action notifies the operator and never toggles."""
    stub = _app_stub(monkeypatch, disabled=True)

    getattr(TaskActionsMixin, action)(stub)

    stub._action_shield_toggle.assert_not_called()
    stub.notify.assert_called_once()
    assert "disable_firewall_no_protection" in stub.notify.call_args.args[0]


def test_disengage_takes_the_shield_down_disengaged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disengage action's shield function calls ``ShieldManager.down(..., disengaged=True)``."""
    stub = _app_stub(monkeypatch, disabled=False)
    TaskActionsMixin._action_shield_disengaged(stub)
    shield_fn = stub._action_shield_toggle.call_args.args[1]
    manager = MagicMock()

    with (
        mock.patch.object(task_actions_mod, "task_shield_manager", return_value=manager),
        mock.patch(
            "terok.lib.orchestration.task_runners.resolve_container_uuid", return_value="cafef00d"
        ),
    ):
        shield_fn("ctr", Path(MOCK_BASE) / "task")

    manager.down.assert_called_once_with("ctr", "cafef00d", disengaged=True)


# ── The degraded-tier warning rides the posture's toast ──────────────


def _risk(*lines: str) -> MagicMock:
    """A rotation risk double whose lines are *lines*, whatever the posture."""
    risk = MagicMock()
    risk.lines.return_value = list(lines)
    return risk


@pytest.mark.parametrize(
    ("call", "posture"),
    [
        pytest.param(lambda d: task_actions_mod._shield_up("ctr", d), "UP", id="up"),
        pytest.param(lambda d: task_actions_mod._shield_down("ctr", d), "DOWN", id="down"),
        pytest.param(
            lambda d: task_actions_mod._shield_down("ctr", d, disengaged=True),
            "DISENGAGED",
            id="disengaged",
        ),
    ],
)
def test_a_posture_change_returns_the_warning_worded_for_it(call, posture: str) -> None:
    """The lines are asked for the posture the action set — and come back one per line."""
    from terok.lib.api.shield import ShieldState

    manager = MagicMock()
    manager.rotation_risk = _risk("first line", "second line")
    with (
        mock.patch.object(task_actions_mod, "task_shield_manager", return_value=manager),
        mock.patch(
            "terok.lib.orchestration.task_runners.resolve_container_uuid", return_value="cafef00d"
        ),
    ):
        warning = call(Path(MOCK_BASE) / "task")

    manager.rotation_risk.lines.assert_called_once_with(ShieldState[posture])
    assert warning == "first line\nsecond line"


@pytest.mark.parametrize(
    "risk",
    [
        pytest.param(None, id="nothing-at-risk"),
        pytest.param(_risk(), id="no-line-for-this-posture"),
    ],
)
def test_no_warning_when_nothing_is_at_risk(risk: object) -> None:
    """No risk, or none that matters in this posture, means a plain confirmation."""
    from terok.lib.api.shield import ShieldState

    manager = SimpleNamespace(rotation_risk=risk)

    assert task_actions_mod._rotation_warning(manager, ShieldState.UP) is None


def test_an_unreadable_warning_never_fails_the_posture_change() -> None:
    """The posture already changed: a record that cannot be read costs only the warning."""
    from terok.lib.api.shield import ShieldState

    class _Unreadable:
        @property
        def rotation_risk(self) -> object:
            raise OSError("gone")

    assert task_actions_mod._rotation_warning(_Unreadable(), ShieldState.UP) is None


def _reporter(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """App double running the real reporter against a recording ``notify``."""
    cfg = SimpleNamespace(shield_security_hint="see the docs")
    monkeypatch.setattr(task_actions_mod, "get_config", lambda: cfg)
    stub = SimpleNamespace(notify=MagicMock())
    stub._report_shield_action = MethodType(TaskActionsMixin._report_shield_action, stub)
    return stub


@pytest.mark.parametrize(
    ("action", "confirmation"),
    [
        pytest.param("up", "Shield up for task task1", id="up"),
        pytest.param("down", "Shield dropped for task task1. see the docs", id="down"),
    ],
)
def test_the_posture_toast_carries_the_warning_as_a_warning(
    monkeypatch: pytest.MonkeyPatch, action: str, confirmation: str
) -> None:
    """Confirmation first, then the lines — a warning toast, held long enough to read."""
    stub = _reporter(monkeypatch)

    stub._report_shield_action(action, "task1", None, "DNS tier lookup pins 2 names")

    message = stub.notify.call_args.args[0]
    kwargs = stub.notify.call_args.kwargs
    assert message == f"{confirmation}\nDNS tier lookup pins 2 names"
    assert kwargs["severity"] == "warning"
    assert kwargs["timeout"] == task_actions_mod._ROTATION_NOTICE_TIMEOUT_S


def test_a_posture_toast_without_a_warning_stays_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing at risk: the confirmation alone, at the default severity."""
    stub = _reporter(monkeypatch)

    stub._report_shield_action("up", "task1", None, None)

    stub.notify.assert_called_once_with("Shield up for task task1")


# ── The background worker behind every shield action ─────────────────


_TASKS_ROOT = Path(MOCK_BASE) / "tasks"
_CONTAINER = "proj-cli-task1"


def _worker_stub(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """App double with ``proj/task1`` selected and a recording worker runner.

    ``load_project`` is replaced by a project rooted under ``_TASKS_ROOT``, so
    the worker body never reads a real project from disk.
    """
    project = SimpleNamespace(tasks_root=_TASKS_ROOT)
    monkeypatch.setattr(task_actions_mod, "load_project", lambda _pid: project)
    return SimpleNamespace(
        notify=MagicMock(),
        run_worker=MagicMock(),
        current_project_name="proj",
        current_task=SimpleNamespace(task_id="task1", mode="cli"),
    )


def test_the_worker_hands_back_what_the_shield_action_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On success the worker reports no error and passes the action's detail on to the toast."""
    stub = _worker_stub(monkeypatch)
    shield_fn = MagicMock(return_value="DNS tier lookup pins 2 names")
    TaskActionsMixin._action_shield_toggle(stub, "up", shield_fn)
    work = stub.run_worker.call_args.args[0]

    result = work()

    shield_fn.assert_called_once_with(_CONTAINER, _TASKS_ROOT / "task1")
    assert result == ("proj", "task1", None, "DNS tier lookup pins 2 names")


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        pytest.param(SystemExit("posture is down"), "posture is down", id="refusal"),
        pytest.param(RuntimeError("podman is gone"), "podman is gone", id="crash"),
    ],
)
def test_the_worker_folds_a_failure_into_the_result(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException, message: str
) -> None:
    """A refusal or a crash in the shield action comes back as error text, never as a raise."""
    stub = _worker_stub(monkeypatch)
    TaskActionsMixin._action_shield_toggle(stub, "down", MagicMock(side_effect=failure))
    work = stub.run_worker.call_args.args[0]

    result = work()

    assert result == ("proj", "task1", message, None)
