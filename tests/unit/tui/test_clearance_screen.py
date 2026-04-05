# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the TUI clearance screen, TuiNotifier, and CLI integration."""

from __future__ import annotations

import argparse
import asyncio
from unittest import mock

from terok.cli.commands.clearance import dispatch, register
from tests.unit.tui.tui_test_helpers import (
    _import_with_stubs,
    import_app,
    import_screens,
    make_key_event,
)


def _import_clearance():
    """Import clearance_screen module with Textual stubs."""
    return _import_with_stubs(None, "terok.tui.clearance_screen")[0]


def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# TuiNotifier
# ---------------------------------------------------------------------------


class TestTuiNotifier:
    """Tests for the TuiNotifier Notifier-protocol implementation."""

    def test_notify_returns_monotonic_ids(self) -> None:
        """Each call to notify() returns a unique incrementing ID."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        id1 = _run(notifier.notify("A"))
        id2 = _run(notifier.notify("B"))
        assert id1 < id2

    def test_notify_replaces_id_returns_same(self) -> None:
        """When replaces_id is given, that ID is returned."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        nid = _run(notifier.notify("A", replaces_id=42))
        assert nid == 42

    def test_notify_posts_message(self) -> None:
        """notify() posts a _NotificationPosted message to the screen."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        _run(notifier.notify("Title", "Body", actions=[("accept", "Allow")]))
        screen.post_message.assert_called_once()

    def test_on_action_stores_callback(self) -> None:
        """on_action() stores a callback for later invocation."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        cb = mock.Mock()
        _run(notifier.on_action(1, cb))
        assert 1 in notifier._callbacks

    def test_invoke_action_calls_and_removes(self) -> None:
        """invoke_action() calls the stored callback and removes it."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        cb = mock.Mock()
        _run(notifier.on_action(5, cb))
        notifier.invoke_action(5, "accept")
        cb.assert_called_once_with("accept")
        assert 5 not in notifier._callbacks

    def test_invoke_action_noop_for_unknown(self) -> None:
        """invoke_action() is a no-op for unknown notification IDs."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        notifier.invoke_action(999, "deny")  # should not raise

    def test_close_removes_callback(self) -> None:
        """close() removes the callback for a notification."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        _run(notifier.on_action(3, mock.Mock()))
        _run(notifier.close(3))
        assert 3 not in notifier._callbacks

    def test_disconnect_clears_all(self) -> None:
        """disconnect() removes all callbacks."""
        mod = _import_clearance()
        screen = mock.Mock()
        notifier = mod.TuiNotifier(screen)
        _run(notifier.on_action(1, mock.Mock()))
        _run(notifier.on_action(2, mock.Mock()))
        _run(notifier.disconnect())
        assert len(notifier._callbacks) == 0

    def test_satisfies_notifier_protocol(self) -> None:
        """TuiNotifier is structurally compatible with the Notifier protocol."""
        from terok_dbus._protocol import Notifier

        mod = _import_clearance()
        notifier = mod.TuiNotifier(mock.Mock())
        assert isinstance(notifier, Notifier)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestClearanceCLI:
    """Tests for the terok clearance CLI command."""

    def test_register_creates_subparser(self) -> None:
        """The clearance subcommand registers without error."""
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest="cmd"))
        args = parser.parse_args(["clearance"])
        assert args.cmd == "clearance"

    def test_dispatch_returns_false_for_other_commands(self) -> None:
        """Dispatch ignores non-clearance commands."""
        assert not dispatch(argparse.Namespace(cmd="project"))

    def test_dispatch_returns_true_for_clearance(self) -> None:
        """Dispatch launches the clearance app for cmd=clearance."""
        with mock.patch("terok.tui.clearance_screen.main"):
            assert dispatch(argparse.Namespace(cmd="clearance"))


# ---------------------------------------------------------------------------
# TUI integration
# ---------------------------------------------------------------------------


class TestClearanceTUIIntegration:
    """Tests for clearance wiring into the existing TUI."""

    def test_task_action_handlers_includes_show_clearance(self) -> None:
        """TASK_ACTION_HANDLERS maps show_clearance to the correct method."""
        app_mod, _ = import_app()
        assert "show_clearance" in app_mod.TASK_ACTION_HANDLERS
        assert app_mod.TASK_ACTION_HANDLERS["show_clearance"] == "action_show_clearance"

    def test_task_details_shift_c_dismisses_show_clearance(self) -> None:
        """Pressing C on TaskDetailsScreen dismisses with show_clearance."""
        screens, widgets = import_screens()
        task = widgets.TaskMeta(
            task_id="1", mode="cli", workspace="/w", web_port=None, container_state="running"
        )
        screen = screens.TaskDetailsScreen(task=task, has_tasks=True, project_id="p")
        screen.dismiss = mock.Mock()
        screen.on_key(make_key_event("C"))
        screen.dismiss.assert_called_once_with("show_clearance")

    def test_task_details_shift_c_noop_without_tasks(self) -> None:
        """Pressing C without tasks does nothing."""
        screens, _ = import_screens()
        screen = screens.TaskDetailsScreen(task=None, has_tasks=False, project_id="p")
        screen.dismiss = mock.Mock()
        screen.on_key(make_key_event("C"))
        screen.dismiss.assert_not_called()
