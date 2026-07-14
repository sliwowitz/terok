# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the TUI login action's off-loop preflight.

The login preflight (``get_login_command``) asks podman for the
container's live state, and podman can sit on its lock for many seconds.
These tests pin that the blocking work runs on a worker thread — on the
event loop it would freeze every keystroke and timer until podman
answered (the "TUI froze on ``i``" bug).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from terok.tui import project_actions as project_actions_mod, task_actions as task_actions_mod
from terok.tui.project_actions import ProjectActionsMixin
from terok.tui.task_actions import TaskActionsMixin


def _record_loop_presence(seen: dict[str, bool]) -> None:
    """Record whether the calling frame runs on the asyncio event loop."""
    try:
        asyncio.get_running_loop()
        seen["on_loop"] = True
    except RuntimeError:
        seen["on_loop"] = False


def _login_app_stub() -> SimpleNamespace:
    """App double with a selected cli task, ready for ``_action_login``."""
    return SimpleNamespace(
        current_project_name="proj",
        current_task=SimpleNamespace(task_id="t1", mode="cli", name="mytask"),
        is_web=False,
        notify=MagicMock(),
        _launch_terminal_session=AsyncMock(),
    )


class TestActionLoginOffLoop:
    """``_action_login`` keeps the podman preflight off the event loop."""

    @pytest.mark.asyncio
    async def test_preflight_runs_off_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The blocking ``get_login_command`` call runs on a worker thread."""
        seen: dict[str, bool] = {}

        def fake_get_login_command(pid: str, tid: str) -> list[str]:
            _record_loop_presence(seen)
            return ["podman", "exec", "-it", "cname", "bash"]

        monkeypatch.setattr(task_actions_mod, "get_login_command", fake_get_login_command)
        stub = _login_app_stub()

        await TaskActionsMixin._action_login(stub)

        assert seen["on_loop"] is False
        stub._launch_terminal_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_preflight_refusal_still_notifies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``SystemExit`` from the threaded preflight surfaces as a notification."""

        def refuse(pid: str, tid: str) -> list[str]:
            raise SystemExit("Container cname is not running (state: exited).")

        monkeypatch.setattr(task_actions_mod, "get_login_command", refuse)
        stub = _login_app_stub()

        await TaskActionsMixin._action_login(stub)

        stub.notify.assert_called_once()
        assert "not running" in stub.notify.call_args[0][0]
        stub._launch_terminal_session.assert_not_awaited()


class TestLaunchTerminalSessionOffLoop:
    """``_launch_terminal_session`` keeps the tmux/terminal probes off the loop."""

    @pytest.mark.asyncio
    async def test_launch_login_runs_off_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The launch orchestrator (tmux/ps subprocess probes) runs on a worker thread."""
        seen: dict[str, bool] = {}

        def fake_launch_login(
            cmd: list[str], title: str | None = None, reuse_key: str | None = None
        ) -> tuple[str, None]:
            _record_loop_presence(seen)
            return ("tmux", None)

        monkeypatch.setattr(project_actions_mod, "launch_login", fake_launch_login)
        stub = SimpleNamespace(is_web=False, notify=MagicMock())

        await ProjectActionsMixin._launch_terminal_session(
            stub, ["podman", "exec", "-it", "cname", "bash"], title="login", cname="cname"
        )

        assert seen["on_loop"] is False
        stub.notify.assert_called_once()
