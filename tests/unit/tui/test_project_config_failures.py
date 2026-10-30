# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Global configuration failures must not blame or edit an otherwise valid project file."""

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from terok.lib.api import BrokenProject
from terok.tui.app import TerokTUI
from terok.tui.widgets.project_state import render_broken_project


def _blocked_project(tmp_path: Path, name: str = "demo") -> BrokenProject:
    """A valid project blocked by a credential setting in the system config layer."""
    return BrokenProject(
        name=name,
        config_path=tmp_path / "projects" / name / "project.yml",
        error="Invalid credentials configuration: credentials.use_keyring: extra setting",
        global_config_paths=(tmp_path / "system" / "config.yml",),
    )


@pytest.mark.parametrize("global_error", [False, True], ids=["project-config", "global-config"])
def test_load_failure_details_identify_the_configuration_to_repair(
    tmp_path: Path, global_error: bool
) -> None:
    """Global blockers display only their global paths; local errors retain project.yml."""
    project_path = tmp_path / "demo" / "project.yml"
    global_paths = (tmp_path / "system" / "config.yml", tmp_path / "user" / "config.yml")
    failure = BrokenProject(
        name="demo",
        config_path=project_path,
        error="Configuration validation failed",
        global_config_paths=global_paths if global_error else (),
    )

    text = str(render_broken_project(failure))

    assert failure.error in text
    assert "Project:   demo" in text
    if global_error:
        assert "(blocked: global config)" in text
        assert "blocked by invalid global configuration" in text
        assert "broken" not in text
        assert str(project_path) not in text
        assert "project.yml" not in text
        assert all(f"Global config: {str(path)!r}" in text for path in global_paths)
    else:
        assert "(broken)" in text
        assert "This project's configuration could not be loaded" in text
        assert f"Config: {project_path}" in text
        assert "Global config" not in text


def test_enter_on_global_blocker_never_opens_project_editor(tmp_path: Path) -> None:
    """A system-layer error shows its real path, not an unrelated project/global editor."""
    instance = mock.Mock(spec=TerokTUI)
    instance.push_screen = mock.AsyncMock()
    instance.notify = mock.Mock()
    instance.current_project_name = "demo"
    blocked = _blocked_project(tmp_path)
    instance._broken_by_id = {"demo": blocked}

    asyncio.run(TerokTUI.action_show_project_actions(instance))

    instance._edit_project_yml.assert_not_called()
    instance._action_edit_global_config.assert_not_called()
    instance.push_screen.assert_not_called()
    instance.notify.assert_called_once()
    message = instance.notify.call_args.args[0]
    assert "blocked by invalid global configuration" in message
    assert blocked.error in message
    assert str(blocked.global_config_paths[0]) in message
    assert "project.yml" not in message
    assert instance.notify.call_args.kwargs["markup"] is False


def test_global_blocker_toast_is_shared_and_reappears_after_recovery(tmp_path: Path) -> None:
    """One global error blocking several projects produces one toast, until fixed."""
    instance = mock.Mock(spec=TerokTUI)
    instance.notify = mock.Mock()
    instance._announced_broken_ids = set()
    instance._announced_global_config_errors = set()
    blocked = [_blocked_project(tmp_path, name) for name in ("alpha", "beta")]

    TerokTUI._announce_newly_broken(instance, blocked)
    TerokTUI._announce_newly_broken(instance, blocked)
    TerokTUI._announce_newly_broken(instance, [*blocked, _blocked_project(tmp_path, "gamma")])

    instance.notify.assert_called_once()
    message = instance.notify.call_args.args[0]
    assert "Global config blocks projects" in message
    assert str(blocked[0].global_config_paths[0]) in message
    assert "Broken project" not in message
    assert "project.yml" not in message
    assert instance.notify.call_args.kwargs["markup"] is False

    TerokTUI._announce_newly_broken(instance, [])
    TerokTUI._announce_newly_broken(instance, blocked)
    assert instance.notify.call_count == 2


def test_local_and_global_failures_have_separate_toasts(tmp_path: Path) -> None:
    """An unrelated damaged project remains visible alongside a shared global blocker."""
    instance = mock.Mock(spec=TerokTUI)
    instance.notify = mock.Mock()
    instance._announced_broken_ids = set()
    instance._announced_global_config_errors = set()
    blocked = _blocked_project(tmp_path)
    broken = BrokenProject(
        name="damaged",
        config_path=tmp_path / "damaged" / "project.yml",
        error="Malformed project.yml",
    )

    TerokTUI._announce_newly_broken(instance, [blocked, broken])
    TerokTUI._announce_newly_broken(instance, [blocked, broken])

    messages = [call.args[0] for call in instance.notify.call_args_list]
    assert len(messages) == 2
    assert messages[0].startswith("Global config blocks projects")
    assert messages[1] == "Broken project config detected — damaged: Malformed project.yml"


def test_global_source_paths_escape_terminal_controls(tmp_path: Path) -> None:
    """Even a config filename containing terminal controls is rendered as literal text."""
    source_path = tmp_path / "config[bold]\x1b[31m\n.yml"
    blocked = BrokenProject(
        name="demo",
        config_path=tmp_path / "demo" / "project.yml",
        error="Invalid global credentials configuration",
        global_config_paths=(source_path,),
    )
    details = str(render_broken_project(blocked))
    instance = mock.Mock(spec=TerokTUI)
    instance.notify = mock.Mock()
    instance.current_project_name = "demo"
    instance._broken_by_id = {"demo": blocked}
    instance._announced_broken_ids = set()
    instance._announced_global_config_errors = set()

    TerokTUI._announce_newly_broken(instance, [blocked])
    asyncio.run(TerokTUI.action_show_project_actions(instance))

    messages = [details, *(call.args[0] for call in instance.notify.call_args_list)]
    assert len(messages) == 3
    for message in messages:
        assert repr(str(source_path)) in message
        assert "\x1b" not in message
        assert str(source_path) not in message
    assert all(call.kwargs["markup"] is False for call in instance.notify.call_args_list)
