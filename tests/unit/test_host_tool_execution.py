# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Host commands use the selected executable, never a cwd-relative PATH entry."""

import argparse
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from terok.cli.commands import clearance
from terok.lib.core import projects
from terok.lib.domain import review_lag
from terok.lib.orchestration.task_runners import shield
from terok.tui import app, shell_launch, tmux_session

_TOOLS = (
    "git",
    "gh",
    "glab",
    "ps",
    "tmux",
    "podman",
    "bash",
    "gnome-terminal",
    "konsole",
    "ptyxis",
    "terok-tui",
    "terok-clearance",
)


@pytest.fixture
def tool_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Provide two host installations and executable shadows in the launch cwd."""
    cwd = tmp_path / "cwd"
    relative = cwd / "relative"
    first, second = tmp_path / "first tools", tmp_path / "second tools"
    for directory in (cwd, relative, first, second):
        directory.mkdir(parents=True, exist_ok=True)
        for name in _TOOLS:
            (directory / name).touch(mode=0o755)
    monkeypatch.chdir(cwd)
    return first, second


@pytest.mark.parametrize("action", ["git", "gh", "glab", "ps", "tmux", "tmux-window"])
def test_host_subprocess_selection_is_fresh(
    action: str, tool_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each launch excludes cwd and sees the current absolute PATH ordering."""
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="[]"))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(review_lag, "_forge_command", lambda _: [action, "api"])
    tool = "tmux" if action == "tmux-window" else action
    for directory in tool_paths:
        monkeypatch.setenv("PATH", os.pathsep.join(("", "relative", str(directory))))
        run.reset_mock()
        if action == "git":
            projects._get_global_git_config("user.name")
        elif action in ("gh", "glab"):
            review_lag.fetch_open_reviews("unused")
        elif action == "ps":
            shell_launch._parent_process_has_name("[]")
        elif action == "tmux":
            tmux_session.session_exists()
        else:
            shell_launch.tmux_new_window(["operator-command"])
        run.assert_called_once()
        assert run.call_args.kwargs["executable"] == str(directory / tool)


@pytest.mark.parametrize("terminal", ["gnome-terminal", "konsole", "ptyxis"])
def test_terminal_and_its_shell_use_the_launch_path(
    terminal: str, tool_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal daemon cannot substitute a different bash from its own PATH."""
    popen = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv("TERM_PROGRAM", terminal)
    monkeypatch.delenv("GNOME_TERMINAL_SERVICE", raising=False)
    for directory in tool_paths:
        monkeypatch.setenv("PATH", os.pathsep.join(("", "relative", str(directory))))
        assert shell_launch.spawn_terminal_with_command(["operator-command"])
        assert popen.call_args.kwargs["executable"] == str(directory / terminal)
        assert str(directory / "bash") in popen.call_args.args[0]


def test_shield_inspect_uses_selected_podman(tool_paths, monkeypatch) -> None:
    """The direct UUID probe follows the same lookup as the runtime adapter."""
    probe = Mock(return_value="container-id")
    monkeypatch.setattr(subprocess, "check_output", probe)
    for directory in tool_paths:
        monkeypatch.setenv("PATH", os.pathsep.join(("", "relative", str(directory))))
        assert shield.resolve_container_uuid("container") == "container-id"
        assert probe.call_args.kwargs["executable"] == str(directory / "podman")


def test_clearance_exec_uses_selected_entrypoint(tool_paths, monkeypatch) -> None:
    """An entrypoint handoff does not reintroduce cwd lookup through execvp."""
    execute = Mock()
    monkeypatch.setattr(os, "execlp", execute)
    for directory in tool_paths:
        monkeypatch.setenv("PATH", os.pathsep.join(("", "relative", str(directory))))
        assert clearance.dispatch(argparse.Namespace(cmd="clearance"))
        assert execute.call_args.args[0] == str(directory / "terok-clearance")


@pytest.mark.parametrize("existing", [False, True])
def test_tmux_handoff_binds_both_tmux_and_tui(tool_paths, monkeypatch, existing) -> None:
    """New and revived windows use the launching user's tools, including spaces."""
    execute = Mock(side_effect=SystemExit(0))
    monkeypatch.setattr(os, "execvp", execute)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux_session, "session_exists", lambda: existing)
    monkeypatch.setattr(tmux_session, "find_main_window", lambda: None)
    monkeypatch.setattr(tmux_session, "revive_window_args", lambda: [])
    monkeypatch.setattr(tmux_session, "session_marker_args", lambda: [])
    for directory in tool_paths:
        monkeypatch.setenv("PATH", os.pathsep.join(("", "relative", str(directory))))
        with pytest.raises(SystemExit) as stopped:
            app._launch_in_tmux()
        assert stopped.value.code == 0
        assert execute.call_args.args[0] == str(directory / "tmux")
        assert shlex.quote(str(directory / "terok-tui")) in execute.call_args.args[1]


def test_optional_tmux_lookup_failure_stays_a_noop(monkeypatch) -> None:
    """Rejecting a cwd-only PATH does not turn optional TUI helpers into errors."""
    monkeypatch.setenv("PATH", ".:")
    assert not tmux_session.session_exists()
    assert not shell_launch.tmux_new_window(["operator-command"])
