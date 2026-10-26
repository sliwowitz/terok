# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Exercise task naming in the live CLI launch dialog with real focus events."""

from unittest import mock

import pytest
from textual.app import App
from textual.widgets import Input, TextArea

from terok.tui import screens
from terok.tui.console_log import ConsoleLogEntry
from terok.tui.worker_log_screen import WorkerLogScreen

_PENDING = object()


class _LaunchHost(App[None]):
    """Keep the launch screen mounted and retain its dismissal result."""

    def __init__(self) -> None:
        super().__init__()
        self.launch = screens.TaskLaunchScreen(
            container_name="project-cli-7",
            project_name="project",
            task_id="7",
            task_name="initial-name",
            installed=frozenset({"claude"}),
        )
        self.result: object = _PENDING

    def on_mount(self) -> None:
        """Open the combined naming and instructions dialog."""
        self.push_screen(self.launch, self._capture)

    def _capture(self, result: object) -> None:
        """Retain the result without terminating the test host."""
        self.result = result


@pytest.fixture
def rename(monkeypatch: pytest.MonkeyPatch) -> mock.Mock:
    """Record persistence calls without allocating a container or task."""
    rename = mock.Mock()
    monkeypatch.setattr(screens, "task_rename", rename)
    return rename


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> _LaunchHost:
    """Provide an isolated launch dialog whose readiness never invokes Podman."""
    monkeypatch.setattr(
        screens.TaskLaunchScreen, "_probe_readiness", lambda self: ("running", True)
    )
    app = _LaunchHost()
    monkeypatch.setattr(app, "notify", mock.Mock())
    return app


@pytest.mark.parametrize("terminal_size", [(100, 40), (80, 24)])
async def test_name_and_prompt_are_visible_with_name_initially_focused(
    app, rename, terminal_size
) -> None:
    """Name, instructions, and action buttons fit together even in a standard terminal."""
    async with app.run_test(size=terminal_size) as pilot:
        await pilot.pause()
        name = app.launch.query_one("#launch-name", Input)
        status = app.launch.query_one("#launch-status")
        prompt = app.launch.query_one("#launch-prompt", TextArea)
        dialog = app.launch.query_one("#launch-dialog")
        controls = [name, prompt, *app.launch.query("#launch-buttons Button")]
        dimensions = "\n".join(
            f"{control.id}: {control.region}" for control in [dialog, status, *controls]
        )

        assert app.focused is name
        assert name.value == "initial-name"
        assert name.region.bottom <= status.region.y < prompt.region.y
        for control in controls:
            assert control.visible
            assert dialog.content_region.contains_region(control.region), dimensions
            assert app.launch.region.contains_region(control.region), dimensions
        assert dialog.border_title == "CLI Task 7"
        rename.assert_not_called()


@pytest.mark.parametrize("leave_name", ["tab", "click"])
async def test_blur_persists_normalized_name_once(app, rename, leave_name) -> None:
    """Keyboard and pointer focus changes save immediately without duplicate notices."""
    async with app.run_test(size=(100, 40)) as pilot:
        name = app.launch.query_one("#launch-name", Input)
        name.value = "  My Task!  "
        await pilot.pause()
        rename.assert_not_called()

        if leave_name == "tab":
            await pilot.press("tab")
        else:
            await pilot.click("#launch-prompt")
        await pilot.pause()

        rename.assert_called_once_with("project", "7", "my-task")
        assert name.value == "my-task"
        assert app.focused is app.launch.query_one("#launch-prompt", TextArea)
        app.notify.assert_called_once()
        assert app.notify.call_args.args == ("Task 7 renamed.",)

        await pilot.click("#launch-name")
        await pilot.press("tab", "escape")
        await pilot.pause()

    rename.assert_called_once()
    app.notify.assert_called_once()
    assert app.result == ("project", "7", "my-task", "project-cli-7", None, None)


@pytest.mark.parametrize("empty_name", ["", "   "])
async def test_empty_name_restores_latest_saved_name(app, rename, empty_name) -> None:
    """An empty name restores the latest rename, not the original suggestion."""
    async with app.run_test(size=(100, 40)) as pilot:
        name = app.launch.query_one("#launch-name", Input)
        name.value = "updated-name"
        await pilot.press("tab")
        await pilot.click("#launch-name")
        name.value = empty_name
        await pilot.press("tab")
        await pilot.pause()

        assert name.value == "updated-name"
        rename.assert_called_once_with("project", "7", "updated-name")
        app.notify.assert_called_once()
        assert app.notify.call_args.args == ("Task 7 renamed.",)


async def test_enter_in_name_advances_to_prompt_instead_of_logging_in(app, rename) -> None:
    """Even a ready container must not turn name-field Enter into Login."""
    async with app.run_test(size=(100, 40)) as pilot:
        await app.launch._poll_status()
        app.launch.query_one("#launch-name", Input).value = "updated-name"
        await pilot.press("enter")
        await pilot.pause()

        assert app.focused is app.launch.query_one("#launch-prompt", TextArea)
        assert app.result is _PENDING
        rename.assert_called_once_with("project", "7", "updated-name")


@pytest.mark.parametrize("exit_action", ["escape", "dismiss", "login", "login_action"])
async def test_leaving_dialog_saves_name_before_delivering_launch_context(
    app, rename, exit_action, monkeypatch
) -> None:
    """Dismissal and login retain name and prompt even without an earlier focus change."""
    capture_result = app._capture

    def capture_after_rename(result: object) -> None:
        """The dismissal callback must already observe the persisted name."""
        rename.assert_called_once_with("project", "7", "final-name")
        capture_result(result)

    monkeypatch.setattr(app, "_capture", capture_after_rename)
    async with app.run_test(size=(100, 40)) as pilot:
        await app.launch._poll_status()
        name = app.launch.query_one("#launch-name", Input)
        name.value = "  Final Name  "
        app.launch.query_one("#launch-prompt", TextArea).load_text("  Keep these instructions  ")
        assert app.focused is name
        rename.assert_not_called()

        if exit_action == "escape":
            await pilot.press("escape")
        elif exit_action == "login_action":
            app.launch.action_login()
        else:
            await pilot.click(f"#btn-{exit_action}")
        await pilot.pause()

    rename.assert_called_once_with("project", "7", "final-name")
    app.notify.assert_called_once()
    assert app.notify.call_args.args == ("Task 7 renamed.",)
    agent = "bash" if exit_action.startswith("login") else None
    assert app.result == (
        "project",
        "7",
        "final-name",
        "project-cli-7",
        agent,
        "Keep these instructions",
    )


async def test_showing_log_commits_name_without_dismissing_launch_dialog(app, rename) -> None:
    """Opening the startup log saves the name and leaves instructions available on return."""
    app.launch._console_entry = ConsoleLogEntry(
        id=1, title="Starting task", argv=[], command="terok task run-cli project 7"
    )
    async with app.run_test(size=(100, 40)) as pilot:
        app.launch.query_one("#launch-name", Input).value = "updated-name"
        await pilot.click("#btn-show-log")
        await pilot.pause()

        assert isinstance(app.screen, WorkerLogScreen)
        rename.assert_called_once_with("project", "7", "updated-name")
        assert app.result is _PENDING

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.launch
        assert app.launch.query_one("#launch-name", Input).value == "updated-name"
        rename.assert_called_once()
        app.notify.assert_called_once()


@pytest.mark.parametrize("invalid_name", ["-invalid", "!!!"])
async def test_invalid_name_restores_previous_name_without_persistence(
    app, rename, invalid_name
) -> None:
    """An invalid slug cannot replace the valid task name."""
    async with app.run_test(size=(100, 40)) as pilot:
        name = app.launch.query_one("#launch-name", Input)
        name.value = invalid_name
        await pilot.press("tab")
        await pilot.pause()

        assert name.value == "initial-name"
        rename.assert_not_called()
        app.notify.assert_called_once()
        assert app.notify.call_args.args[0].startswith("Invalid name:")


@pytest.mark.parametrize("failure", [SystemExit("missing task"), RuntimeError("write failed")])
async def test_failed_rename_restores_previous_name_and_dismisses_cleanly(
    app, rename, failure
) -> None:
    """Persistence failures report an error and never leak an unsaved name into login context."""
    rename.side_effect = failure
    async with app.run_test(size=(100, 40)) as pilot:
        name = app.launch.query_one("#launch-name", Input)
        name.value = "updated-name"
        await pilot.press("tab")
        await pilot.pause()

        assert name.value == "initial-name"
        assert app.notify.call_args.kwargs["severity"] == "error"
        await pilot.press("escape")
        await pilot.pause()

    rename.assert_called_once_with("project", "7", "updated-name")
    app.notify.assert_called_once()
    assert app.result == ("project", "7", "initial-name", "project-cli-7", None, None)
