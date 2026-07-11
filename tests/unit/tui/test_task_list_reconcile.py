# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The container-state poll reconciles the task set with what's on disk.

Each batch query — fired by the inotify/podman-event push sources, with the
slow resync as insurance — re-reads every task from disk, so its fresh
``TaskMeta`` snapshots reveal tasks created or deleted *outside* the TUI.  The
handler diffs that membership against the displayed rows (level-triggered, so it
cannot miss a change) and reloads the list only when it actually differs —
otherwise it syncs the live lifecycle fields (container state plus the
``ready_at`` init marker, work status and exit code) onto the existing rows so a
running container's badge can move from "init" to "running" without a reload.
A poll the runtime didn't answer arrives as ``None`` and must leave every row
untouched (#1134).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

from terok.lib.api import TaskMeta
from tests.unit.tui.tui_test_helpers import import_app


def run(coro: object) -> object:
    """Run an async test coroutine."""
    return asyncio.run(coro)


def _meta(
    task_id: str,
    *,
    container_state: str | None = None,
    initialized: bool = False,
    work_status: str | None = None,
    web_port: int | None = None,
) -> TaskMeta:
    """A real ``TaskMeta`` row carrying the live fields the poll reconciles."""
    return TaskMeta(
        task_id=task_id,
        mode="cli",
        workspace="",
        web_port=web_port,
        container_state=container_state,
        initialized=initialized,
        work_status=work_status,
    )


def _instance(app_class: type, displayed: list[Any]) -> Any:
    """App wired with a fake task list and an async ``refresh_tasks`` spy."""
    instance = app_class()
    instance.current_project_name = "p1"
    instance.current_task = None
    instance.refresh_tasks = mock.AsyncMock()
    task_list = mock.Mock()
    task_list.tasks = displayed
    instance.query_one = mock.Mock(return_value=task_list)
    instance._task_list = task_list  # handle for assertions
    return instance


def _poll(instance: Any, app_mod: Any, metas: list[TaskMeta], pid: str = "p1") -> None:
    """Drive a completed container-state poll through the worker handler."""
    worker = mock.Mock()
    worker.group = "container-state"
    worker.result = (pid, metas)
    event = mock.Mock()
    event.worker = worker
    event.state = app_mod.WorkerState.SUCCESS
    run(instance.handle_worker_state_changed(event))


class TestMembershipReconcile:
    """Membership drift between disk and the displayed rows forces a reload."""

    def test_external_create_triggers_refresh(self) -> None:
        app_mod, app_class = import_app()
        instance = _instance(app_class, [_meta("1")])
        _poll(instance, app_mod, [_meta("1"), _meta("2")])  # task 2 appeared on disk
        instance.refresh_tasks.assert_awaited_once()

    def test_external_delete_triggers_refresh(self) -> None:
        app_mod, app_class = import_app()
        instance = _instance(app_class, [_meta("1"), _meta("2")])
        _poll(instance, app_mod, [_meta("1")])  # task 2 vanished from disk
        instance.refresh_tasks.assert_awaited_once()


class TestStateOnlyUpdate:
    """Stable membership keeps the in-place state update — no reload."""

    def test_state_change_updates_in_place(self) -> None:
        app_mod, app_class = import_app()
        instance = _instance(app_class, [_meta("1", initialized=True)])
        _poll(instance, app_mod, [_meta("1", container_state="running", initialized=True)])
        instance.refresh_tasks.assert_not_awaited()
        assert instance._task_list.tasks[0].container_state == "running"
        instance._task_list.refresh_labels.assert_called_once()

    def test_running_marker_flips_init_to_running(self) -> None:
        """The ``ready_at`` init marker landing must move "init" → "running".

        Regression: an externally-created task appears with a running container
        before it has initialised, so its badge is "init".  The marker is
        persisted moments later; the steady-state poll must sync ``initialized``
        (not just the container state) or the row stays yellow forever.
        """
        app_mod, app_class = import_app()
        row = _meta("1", container_state="running", initialized=False)
        assert row.status == "init"  # precondition: yellow before the marker
        instance = _instance(app_class, [row])
        _poll(instance, app_mod, [_meta("1", container_state="running", initialized=True)])
        instance.refresh_tasks.assert_not_awaited()
        assert instance._task_list.tasks[0].status == "running"
        instance._task_list.refresh_labels.assert_called_once()

    def test_no_change_is_a_noop(self) -> None:
        app_mod, app_class = import_app()
        instance = _instance(app_class, [_meta("1")])
        _poll(instance, app_mod, [_meta("1")])
        instance.refresh_tasks.assert_not_awaited()
        instance._task_list.refresh_labels.assert_not_called()


class TestProjectGuard:
    """A result for a project the user already switched away from is dropped."""

    def test_stale_project_result_ignored(self) -> None:
        app_mod, app_class = import_app()
        instance = _instance(app_class, [_meta("1")])
        _poll(instance, app_mod, [_meta("1"), _meta("2")], pid="other")
        instance.refresh_tasks.assert_not_awaited()
        instance.query_one.assert_not_called()


class TestFailedQueryKeepsLastKnown:
    """A poll the runtime didn't answer must not move any status (#1134).

    ``podman ps`` failing (e.g. storage-lock contention with a concurrent
    image build) used to read as "all containers gone", flipping every
    initialised task to ❓ "not found" for the duration of the build.
    """

    def test_none_result_keeps_rows_untouched(self) -> None:
        app_mod, app_class = import_app()
        row = _meta("1", container_state="exited", initialized=True)
        instance = _instance(app_class, [row])
        _poll(instance, app_mod, None)
        instance.refresh_tasks.assert_not_awaited()
        instance.query_one.assert_not_called()
        assert row.status == "stopped"  # unchanged: 🔴, not ❓

    def test_successful_poll_records_last_known_states(self) -> None:
        """A successful poll snapshots states for seeding later reloads."""
        app_mod, app_class = import_app()
        instance = _instance(app_class, [_meta("1", initialized=True)])
        _poll(instance, app_mod, [_meta("1", container_state="running", initialized=True)])
        assert instance._last_container_states["p1"] == {"1": "running"}


class TestSeedTaskRows:
    """Freshly loaded rows inherit the TUI-held launch flag and last-known state."""

    def test_seeds_last_known_state_and_launching_flag(self) -> None:
        _app_mod, app_class = import_app()
        instance = app_class()
        instance._last_container_states["p1"] = {"1": "exited"}
        instance._launching_tasks.add(("p1", "2"))
        rows = [_meta("1", initialized=True), _meta("2")]
        instance._seed_task_rows("p1", rows)
        assert rows[0].status == "stopped"  # 🔴 from the first render — no ❓ flash
        assert rows[1].starting is True

    def test_container_state_comes_only_from_the_poll_cache(self) -> None:
        """Rows carry exactly the cached state — never an invented one."""
        _app_mod, app_class = import_app()
        instance = app_class()
        rows = [_meta("1", container_state="running", initialized=True)]
        instance._seed_task_rows("p1", rows)  # no successful poll recorded yet
        assert rows[0].container_state is None
