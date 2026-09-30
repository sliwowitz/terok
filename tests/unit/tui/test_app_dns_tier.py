# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""TerokTUI's shield-state worker: one pass that feeds the whole shield block of the task pane.

Exercises the App-side reader — posture, bypass countdown, DNS tier, prompt
state and rotation warning, from one ``ShieldManager.inspect`` listing and a
few file reads — plus what the app does with a reading: anchoring the
countdown, ticking it without asking the shield again, and asking again when
the container or the mute changes.  No Textual app boots.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import pytest

from terok.lib.api import DnsTier
from terok.lib.api.shield import ShieldState
from terok.tui.app import ShieldPaneReading, TerokTUI
from tests.testfs import MOCK_BASE
from tests.unit.tui.tui_test_helpers import import_app

_SHORT_ID = "0f1e2d3c4b5a"


def _task(*, state: str = "running", mode: str = "cli") -> SimpleNamespace:
    """The selected task as the pane holds it."""
    return SimpleNamespace(mode=mode, task_id="t1", container_state=state)


def _manager(
    state: ShieldState = ShieldState.UP, remaining: int | None = None, risk: object = None
) -> SimpleNamespace:
    """A task shield manager double: one reading, a recorded tier, a short id, a risk."""
    return SimpleNamespace(
        inspect=MagicMock(return_value=SimpleNamespace(state=state, bypass_remaining=remaining)),
        dns_tier=DnsTier.LOOKUP,
        container_short_id=_SHORT_ID,
        rotation_risk=risk,
    )


def _read(
    manager: SimpleNamespace,
    task: SimpleNamespace,
    *,
    clearance: str = "prompt",
    prompts: str = "asking",
):
    """Run the reader against *manager* in a project whose config says *clearance* now.

    *prompts* is what the running container answers — its hub, or its record.
    """
    project = SimpleNamespace(tasks_root=Path(MOCK_BASE), clearance=clearance)
    with (
        mock.patch("terok.tui.app.load_project", return_value=project),
        mock.patch("terok.tui.app.container_name", return_value="c-name"),
        mock.patch("terok.tui.app.task_shield_manager", return_value=manager) as factory,
        mock.patch(
            "terok.lib.api.clearance.container_prompt_state", return_value=prompts
        ) as container,
    ):
        reading = TerokTUI._load_shield_state("proj", task)
    factory.assert_called_once_with(Path(MOCK_BASE) / "t1")
    return reading, container


def test_one_listing_gives_the_posture_and_the_recorded_tier() -> None:
    """The happy path: the posture's ``.name`` and the launch tier, no window open."""
    reading, _ = _read(_manager(), _task())

    assert reading.project_name == "proj" and reading.task_id == "t1"
    assert reading.shield_state == "UP"
    assert reading.dns_tier is DnsTier.LOOKUP
    assert reading.bypass_closes_at is None


def test_an_open_window_is_anchored_to_the_wall_clock() -> None:
    """The kernel's seconds left become a close time, so the pane counts down alone."""
    before = datetime.now()
    reading, _ = _read(_manager(remaining=299), _task())
    after = datetime.now()

    assert reading.bypass_closes_at is not None
    assert before + timedelta(seconds=299) <= reading.bypass_closes_at
    assert reading.bypass_closes_at <= after + timedelta(seconds=299)


def test_the_rotation_warning_is_worded_for_the_posture_just_read() -> None:
    """The risk's lines are asked for the live posture — DOWN keeps only the deny side."""
    risk = MagicMock()
    risk.lines.return_value = ["Security: the deny holds only launch-time addresses"]

    reading, _ = _read(_manager(ShieldState.DOWN, risk=risk), _task())

    risk.lines.assert_called_once_with(ShieldState.DOWN)
    assert reading.rotation_lines == ("Security: the deny holds only launch-time addresses",)


@pytest.mark.parametrize("prompts", ["muted", "asking", "off", "unknown"])
def test_the_prompt_state_is_the_running_containers_answer(prompts: str) -> None:
    """Asked of the container by its name and the recorded short id — its hub, or its record."""
    reading, container = _read(_manager(), _task(), prompts=prompts)

    container.assert_called_once_with("c-name", _SHORT_ID)
    assert reading.prompts == prompts


def test_the_projects_config_now_is_not_the_answer() -> None:
    """A project switched to ``off`` while its task runs: the task still asks, and says so.

    The config reaches a container only at its next start; the pane shows
    what the running one does.
    """
    reading, container = _read(_manager(), _task(), clearance="off", prompts="asking")

    container.assert_called_once()
    assert reading.prompts == "asking"


def test_a_stopped_task_has_no_prompt_state() -> None:
    """No container, no hub, no mute to show — not even ``off``."""
    reading, container = _read(
        _manager(ShieldState.OFFLINE), _task(state="exited"), clearance="off"
    )

    container.assert_not_called()
    assert reading.prompts is None


def test_errors_degrade_to_an_unknown_reading() -> None:
    """Any failure leaves the shield fields unknown rather than failing the worker."""
    with mock.patch("terok.tui.app.load_project", side_effect=RuntimeError("boom")):
        reading = TerokTUI._load_shield_state("proj", _task())

    assert reading == ShieldPaneReading("proj", "t1")


def test_an_unreadable_rotation_record_costs_only_the_warning() -> None:
    """The warning is advisory: a risk that cannot be read leaves the rest of the pane."""

    class _Unreadable:
        @property
        def rotation_risk(self) -> object:
            raise OSError("gone")

    assert TerokTUI._task_rotation_lines(_Unreadable(), ShieldState.UP) == ()


# ── The countdown ─────────────────────────────────────────────────────


def _countdown_app(closes_at: datetime | None, *, state: str = "UP") -> SimpleNamespace:
    """App double with a selected task, a ticker factory and a details pane."""
    details = MagicMock()
    timer = MagicMock()
    app = SimpleNamespace(
        current_project_name="proj",
        current_task=SimpleNamespace(task_id="t1", shield_state=state, bypass_closes_at=closes_at),
        _bypass_countdown=None,
        _last_image_old=None,
        set_interval=MagicMock(return_value=timer),
        query_one=MagicMock(return_value=details),
        _query_shield_state=MagicMock(),
    )
    for name in ("_sync_bypass_countdown", "_tick_bypass_countdown"):
        setattr(app, name, MethodType(getattr(TerokTUI, name), app))
    return app


def test_an_open_window_starts_a_one_second_countdown() -> None:
    """A repaint a second while it is open — from the anchored time, never a re-query."""
    app = _countdown_app(datetime.now() + timedelta(minutes=5))

    app._sync_bypass_countdown()
    app._sync_bypass_countdown()  # already running: not started twice

    app.set_interval.assert_called_once_with(1.0, app._tick_bypass_countdown)
    app._tick_bypass_countdown()
    app._query_shield_state.assert_not_called()
    app.query_one.return_value.set_task.assert_called_once()


@pytest.mark.parametrize(
    ("closes_at", "state"),
    [
        pytest.param(None, "UP", id="no-window"),
        pytest.param(datetime.now() + timedelta(minutes=5), "DOWN", id="not-up"),
    ],
)
def test_no_open_window_stops_the_countdown(closes_at: datetime | None, state: str) -> None:
    """Another task selected, the window closed, the posture changed: the ticker goes."""
    app = _countdown_app(closes_at, state=state)
    running = MagicMock()
    app._bypass_countdown = running

    app._sync_bypass_countdown()

    running.stop.assert_called_once()
    assert app._bypass_countdown is None
    app.set_interval.assert_not_called()


def test_the_countdown_runs_out_into_one_real_query() -> None:
    """At zero the window reads closed, the ticker stops, and the shield is asked to confirm."""
    app = _countdown_app(datetime.now() - timedelta(seconds=1))
    running = MagicMock()
    app._bypass_countdown = running

    app._tick_bypass_countdown()

    assert app.current_task.bypass_closes_at is None
    running.stop.assert_called_once()
    app._query_shield_state.assert_called_once_with("proj", app.current_task)


# ── The worker's results in the app ──────────────────────────────────


def _app_instance() -> tuple[object, type]:
    """A stub-imported TerokTUI instance with a selected task and a recording pane."""
    app_mod, app_class = import_app()
    instance = app_class()
    instance.current_project_name = "proj"
    instance.current_task = SimpleNamespace(
        task_id="t1",
        mode="cli",
        shield_state=None,
        dns_tier=None,
        bypass_closes_at=None,
        prompts=None,
        rotation_lines=(),
    )
    instance.query_one = MagicMock()
    instance._sync_bypass_countdown = MagicMock()
    instance._query_shield_state = MagicMock()
    return app_mod, instance


def _event(app_mod: object, group: str, name: str, result: object) -> MagicMock:
    """A successful worker's state change."""
    worker = MagicMock()
    worker.group = group
    worker.name = name
    worker.result = result
    return MagicMock(worker=worker, state=app_mod.WorkerState.SUCCESS)


def test_a_reading_lands_on_the_selected_task_and_syncs_the_countdown() -> None:
    """Every field of the reading reaches the task the pane renders."""
    app_mod, instance = _app_instance()
    closes = datetime.now() + timedelta(minutes=2)
    reading = app_mod.ShieldPaneReading(
        "proj",
        "t1",
        shield_state="UP",
        dns_tier=DnsTier.LOOKUP,
        bypass_closes_at=closes,
        prompts="muted",
        rotation_lines=("a line",),
    )

    asyncio.run(
        type(instance).handle_worker_state_changed(
            instance, _event(app_mod, "shield-state", "shield-state:proj:t1", reading)
        )
    )

    task = instance.current_task
    assert (task.shield_state, task.dns_tier, task.bypass_closes_at) == (
        "UP",
        DnsTier.LOOKUP,
        closes,
    )
    assert (task.prompts, task.rotation_lines) == ("muted", ("a line",))
    instance._sync_bypass_countdown.assert_called_once()


def test_a_reading_for_another_task_is_dropped() -> None:
    """The cursor moved on while the worker ran: the reading must not paint the wrong task."""
    app_mod, instance = _app_instance()
    reading = app_mod.ShieldPaneReading("proj", "t2", shield_state="UP")

    asyncio.run(
        type(instance).handle_worker_state_changed(
            instance, _event(app_mod, "shield-state", "shield-state:proj:t2", reading)
        )
    )

    assert instance.current_task.shield_state is None


@pytest.mark.parametrize(
    ("name", "requeried"),
    [
        pytest.param("clearance-mute:proj:t1", True, id="selected-task"),
        pytest.param("clearance-mute:proj:t9", False, id="another-task"),
    ],
)
def test_a_mute_result_asks_the_hub_again_for_the_selected_task(name: str, requeried: bool) -> None:
    """The Prompts line is the hub's answer, so a mute or unmute re-reads it."""
    from terok.lib.api.clearance import MuteResult

    app_mod, instance = _app_instance()
    instance.notify = MagicMock()

    asyncio.run(
        type(instance).handle_worker_state_changed(
            instance, _event(app_mod, "clearance-mute", name, MuteResult(True, "Muted"))
        )
    )

    instance.notify.assert_called_once_with("Muted", severity="information")
    assert instance._query_shield_state.called is requeried


def test_a_container_state_change_re_reads_the_selected_tasks_shield() -> None:
    """A start or a stop makes posture, window and prompts stale: they are read again."""
    from terok.lib.api import TaskMeta

    app_mod, instance = _app_instance()
    row = TaskMeta(task_id="t1", mode="cli", workspace="", web_port=None, container_state="running")
    other = TaskMeta(
        task_id="t2", mode="cli", workspace="", web_port=None, container_state="running"
    )
    instance.current_task = row
    task_list = SimpleNamespace(tasks=[row, other], refresh_labels=MagicMock())
    panes = {"#task-list": task_list, "#task-details": MagicMock()}
    instance.query_one = MagicMock(side_effect=lambda selector, *_: panes[selector])
    fresh = [
        TaskMeta(task_id="t1", mode="cli", workspace="", web_port=None, container_state="exited"),
        TaskMeta(task_id="t2", mode="cli", workspace="", web_port=None, container_state="running"),
    ]

    asyncio.run(
        type(instance).handle_worker_state_changed(
            instance, _event(app_mod, "container-state", "container-state", ("proj", fresh))
        )
    )

    instance._query_shield_state.assert_called_once_with("proj", row)
