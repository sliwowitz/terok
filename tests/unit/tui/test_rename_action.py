# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""TUI task renames update open host tmux windows without blocking the event loop."""

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from terok.tui import task_actions


@pytest.fixture
def app() -> SimpleNamespace:
    """Selected task and the TUI callbacks used by the rename action."""
    return SimpleNamespace(
        current_project_name="proj",
        current_task=SimpleNamespace(task_id="t1", mode="cli", name="old-name"),
        notify=Mock(),
        refresh_tasks=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cli", "web", None])
async def test_successful_rename_updates_login_title_off_loop(app, monkeypatch, mode) -> None:
    """Persist first, then update the correct container's title on a worker thread."""
    app.current_task.mode = mode
    persist = Mock()
    monkeypatch.setattr(task_actions, "task_rename", persist)
    loop_thread = threading.get_ident()

    def rename_window(cname: str, title: str) -> None:
        persist.assert_called_once_with("proj", "t1", "new-name")
        assert cname == task_actions.container_name("proj", mode or "cli", "t1")
        assert title == "proj:t1:new-name"
        assert threading.get_ident() != loop_thread

    rename = Mock(side_effect=rename_window)
    monkeypatch.setattr(task_actions, "rename_login_window", rename)

    await task_actions.TaskActionsMixin._on_rename_task_result(app, "new-name")

    rename.assert_called_once()
    app.notify.assert_called_once_with("Task t1 renamed.")
    app.refresh_tasks.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancelled", "no-project", "no-task", "persist-failed"])
async def test_unsuccessful_rename_leaves_login_title_unchanged(app, monkeypatch, reason) -> None:
    """Cancellation, missing selection and failed persistence never change tmux titles."""
    persist = Mock()
    rename = Mock()
    monkeypatch.setattr(task_actions, "task_rename", persist)
    monkeypatch.setattr(task_actions, "rename_login_window", rename)
    if reason == "no-project":
        app.current_project_name = None
    elif reason == "no-task":
        app.current_task = None
    elif reason == "persist-failed":
        persist.side_effect = SystemExit("rename failed")

    await task_actions.TaskActionsMixin._on_rename_task_result(
        app, None if reason == "cancelled" else "new-name"
    )

    rename.assert_not_called()
    app.refresh_tasks.assert_not_awaited()
    if reason == "persist-failed":
        app.notify.assert_called_once_with("rename failed")
    else:
        persist.assert_not_called()
        app.notify.assert_not_called()
