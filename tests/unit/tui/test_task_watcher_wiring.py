# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The polling mixin debounces inotify activity into a single reconcile.

Covers the reaction logic on the app side — draining, debounce coalescing, and
the empty-drain no-op — without a real event loop.  The watcher mechanism itself
is covered against real inotify in ``test_task_watcher``.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from tests.unit.tui.tui_test_helpers import import_app


def _instance(app_class: type, *, drains: bool) -> Any:
    """App wired with a fake watcher and spies for the debounce timer."""
    instance = app_class()
    instance._task_watcher = mock.Mock()
    instance._task_watcher.drain.return_value = drains
    instance._watch_debounce = None
    instance._poll_container_status = mock.Mock()
    instance.set_timer = mock.Mock(return_value=mock.Mock())
    return instance


def test_change_schedules_a_debounced_reconcile() -> None:
    _app_mod, app_class = import_app()
    instance = _instance(app_class, drains=True)
    instance._on_task_dir_changed()
    instance.set_timer.assert_called_once()
    # The debounce fires the same reconcile the timer would, not an eager one.
    assert instance.set_timer.call_args.args[1] is instance._poll_container_status
    instance._poll_container_status.assert_not_called()


def test_empty_drain_is_a_noop() -> None:
    _app_mod, app_class = import_app()
    instance = _instance(app_class, drains=False)
    instance._on_task_dir_changed()
    instance.set_timer.assert_not_called()


def test_burst_collapses_restarting_the_window() -> None:
    _app_mod, app_class = import_app()
    instance = _instance(app_class, drains=True)
    instance._on_task_dir_changed()
    first = instance._watch_debounce
    instance._on_task_dir_changed()
    # The pending window is cancelled and replaced rather than stacking timers.
    first.stop.assert_called_once()
    assert instance.set_timer.call_count == 2


def test_stop_cancels_pending_debounce() -> None:
    _app_mod, app_class = import_app()
    instance = _instance(app_class, drains=True)
    instance._on_task_dir_changed()
    pending = instance._watch_debounce
    instance._stop_task_watcher()
    pending.stop.assert_called_once()
    assert instance._watch_debounce is None
    assert instance._task_watcher is None
