# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Obsolete task inputs warn on resume; only explicit recreation discards retained work."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from terok.lib.integrations.sandbox import RuntimeInspectionError
from terok.lib.orchestration.task_runners import restart
from terok.lib.util.host_cmd import WORKSPACE_DANGEROUS_DIRNAME
from tests.testfs import WORKSPACE_ROOT


@pytest.fixture
def upgrade_task(tmp_path: Path, mock_runtime: MagicMock) -> Iterator[SimpleNamespace]:
    """Isolate restart policy from launch, token and firewall implementation details."""
    project = SimpleNamespace(
        name="protocol-upgrade",
        tasks_root=tmp_path / "tasks",
        shutdown_timeout=10,
        hook_post_stop=None,
        is_sealed=False,
    )
    mock_runtime.container.return_value.mounts = [
        (str(project.tasks_root / "1" / WORKSPACE_DANGEROUS_DIRNAME), str(WORKSPACE_ROOT))
    ]
    meta = {"mode": "cli", "unrestricted": False, "debug": True}
    with (
        patch.object(restart, "load_project", return_value=project),
        patch.object(restart, "validate_host_setup"),
        patch.object(restart, "load_task_meta", return_value=(meta, tmp_path / "dossier.json")),
        patch.object(restart, "_validate_restart_preconditions") as preconditions,
        patch.object(restart, "require_image_input_protocol", create=True) as image_protocol,
        patch.object(restart, "_print_reach_footer"),
        patch.object(restart, "run_hook"),
        patch.object(restart, "_sandbox") as sandbox,
        patch.object(restart, "task_run_cli") as cli,
        patch.object(restart, "task_run_toad") as toad,
    ):
        yield SimpleNamespace(
            project=project,
            meta=meta,
            container=mock_runtime.container.return_value,
            sandbox=sandbox.return_value,
            cli=cli,
            toad=toad,
            preconditions=preconditions,
            image_protocol=image_protocol,
        )


@pytest.mark.parametrize("entrypoint", [restart.ensure_task_running, restart.task_restart])
@pytest.mark.parametrize("mode", ["cli", "toad", "run"])
@pytest.mark.parametrize("state", ["running", "exited"])
@pytest.mark.parametrize("obsolete", ["container-env", "image-label"])
@pytest.mark.parametrize("configured_sealed", [False, True])
def test_legacy_container_warns_and_resumes_without_recreation(
    upgrade_task: SimpleNamespace,
    entrypoint: Callable[..., None],
    mode: str,
    state: str,
    obsolete: str,
    configured_sealed: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Attach and restart preserve obsolete containers so their work can be salvaged."""
    task = upgrade_task
    task.project.is_sealed = configured_sealed
    task.meta["mode"] = mode
    task.container.state = state
    if obsolete == "container-env":
        task.container.env = {"TEROK_CONTAINER_PROTOCOL": "3"}
    else:
        task.container.image.labels.return_value = {}

    with (
        patch.object(restart, "_validate_shield_bundle_version"),
        patch.object(restart, "_refresh_shield_tiers"),
        patch.object(restart, "_refresh_clearance_mode"),
        patch.object(restart, "_start_and_report_restart") as resume,
    ):
        entrypoint(task.project.name, "1")

    assert resume.called is (entrypoint is restart.task_restart or state == "exited")
    if entrypoint is restart.task_restart and state == "running":
        task.container.stop.assert_called_once_with(timeout=task.project.shutdown_timeout)
    else:
        task.container.stop.assert_not_called()
    task.image_protocol.assert_not_called()
    task.sandbox.rm.assert_not_called()
    task.cli.assert_not_called()
    task.toad.assert_not_called()
    output = capsys.readouterr()
    assert output.err.count("obsolete input protocol") == 1
    if mode == "run":
        assert "new headless task with an explicit prompt" in output.err
        assert "--recreate" not in output.err
    else:
        assert "--recreate" in output.err
    assert "container-only workspace changes" not in output.err
    assert "Recreating" not in output.out


@pytest.mark.parametrize("mode", ["cli", "toad"])
@pytest.mark.parametrize("state", ["running", "exited"])
def test_explicit_recreation_does_not_check_obsolete_retained_inputs(
    upgrade_task: SimpleNamespace,
    mode: str,
    state: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fresh recreation checks its replacement, not the container it will discard."""
    task = upgrade_task
    task.meta["mode"] = mode
    task.container.state = state
    task.container.env = {}
    with patch.object(restart, "require_task_input_protocol") as retained_protocol:
        restart.task_restart(task.project.name, "1", fresh=True)

    retained_protocol.assert_not_called()
    task.image_protocol.assert_called_once()
    task.sandbox.rm.assert_called_once_with([f"{task.project.name}-{mode}-1"])
    runner = task.cli if mode == "cli" else task.toad
    runner.assert_called_once_with(task.project.name, "1", unrestricted=False, debug=True)
    output = capsys.readouterr()
    assert "recreate requested" in output.out
    assert "obsolete input protocol" not in output.out + output.err


@pytest.mark.parametrize("backing", ["other-source", "nested-only", "duplicate"])
def test_recreation_warns_about_unverified_workspace_backing(
    upgrade_task: SimpleNamespace,
    tmp_path: Path,
    backing: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unverified workspace persistence warns without changing the recreation workflow."""
    task = upgrade_task
    task.container.state = "running"
    source, destination = task.container.mounts[0]
    task.container.mounts = {
        "other-source": [(str(tmp_path / "unrelated"), destination)],
        "nested-only": [(source, str(WORKSPACE_ROOT / "child"))],
        "duplicate": [(source, destination), (source, destination)],
    }[backing]

    restart.task_restart(task.project.name, "1", fresh=True)

    task.image_protocol.assert_called_once()
    task.container.stop.assert_called_once_with(timeout=0)
    task.sandbox.rm.assert_called_once_with([f"{task.project.name}-cli-1"])
    task.cli.assert_called_once_with(task.project.name, "1", unrestricted=False, debug=True)
    task.toad.assert_not_called()
    warning = capsys.readouterr().err
    assert "container-only workspace changes" in warning
    assert "discard" in warning


def test_current_running_container_is_not_recreated(upgrade_task: SimpleNamespace) -> None:
    """The secure input protocol preserves the ordinary already-running fast path."""
    task = upgrade_task
    task.container.state = "running"

    restart.ensure_task_running(task.project.name, "1")

    task.container.start.assert_not_called()
    task.container.stop.assert_not_called()
    task.sandbox.rm.assert_not_called()
    task.cli.assert_not_called()
    task.toad.assert_not_called()


@pytest.mark.parametrize(
    ("entrypoint", "fresh", "state"),
    [
        (restart.ensure_task_running, False, None),
        (restart.task_restart, False, None),
        (restart.task_restart, True, "running"),
        (restart.task_restart, True, "exited"),
    ],
)
@pytest.mark.parametrize("mode", ["cli", "toad"])
def test_recreation_requires_replacement_image_before_teardown(
    upgrade_task: SimpleNamespace,
    entrypoint: Callable[..., None],
    fresh: bool,
    mode: str,
    state: str | None,
) -> None:
    """Missing containers and explicit recreation both require a compatible replacement."""
    task = upgrade_task
    task.meta["mode"] = mode
    task.container.state = state
    task.container.env = {}
    task.image_protocol.side_effect = RuntimeError("Rebuild replacement image first")

    with pytest.raises(SystemExit, match="Rebuild replacement image"):
        entrypoint(task.project.name, "1", **({"fresh": True} if fresh else {}))

    task.image_protocol.assert_called_once()
    task.preconditions.assert_not_called()
    task.container.start.assert_not_called()
    task.container.stop.assert_not_called()
    task.sandbox.rm.assert_not_called()
    task.cli.assert_not_called()
    task.toad.assert_not_called()


@pytest.mark.parametrize(
    ("entrypoint", "state"),
    [
        (restart.ensure_task_running, "exited"),
        (restart.task_restart, "exited"),
        (restart.task_restart, "running"),
    ],
)
@pytest.mark.parametrize("mode", ["cli", "toad", "run"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("sealed", [False, True])
def test_failed_resume_preserves_container_even_with_a_valid_replacement(
    upgrade_task: SimpleNamespace,
    entrypoint: Callable[..., None],
    state: str,
    mode: str,
    legacy: bool,
    sealed: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed restart never authorizes discarding work, regardless of protocol or storage."""
    task = upgrade_task
    task.meta["mode"] = mode
    task.container.state = state
    if legacy:
        task.container.env = {}
    if sealed:
        task.container.mounts = []
    with (
        patch.object(restart, "_validate_shield_bundle_version"),
        patch.object(restart, "_refresh_shield_tiers"),
        patch.object(restart, "_refresh_clearance_mode"),
        patch.object(restart, "_start_and_report_restart", side_effect=SystemExit("start failed")),
        pytest.raises(SystemExit, match="start failed"),
    ):
        entrypoint(task.project.name, "1")

    task.image_protocol.assert_not_called()
    assert task.container.stop.call_count == int(state == "running")
    task.sandbox.rm.assert_not_called()
    task.cli.assert_not_called()
    task.toad.assert_not_called()
    output = capsys.readouterr()
    assert "Recreating" not in output.out
    assert "container-only workspace changes" not in output.err


@pytest.mark.parametrize("legacy", [False, True])
def test_protocol_preflight_reuses_the_resolved_runtime(
    upgrade_task: SimpleNamespace, mock_runtime: MagicMock, legacy: bool
) -> None:
    """Protocol and refresh checks do not repeat expensive runtime resolution."""
    task = upgrade_task
    task.container.state = "exited"
    if legacy:
        task.container.env = {}
    with (
        patch.object(restart._rt, "resolve_runtime", return_value=mock_runtime) as resolve,
        patch.object(restart, "_validate_shield_bundle_version"),
        patch.object(restart, "_refresh_shield_tiers"),
        patch.object(restart, "_refresh_clearance_mode"),
        patch.object(restart, "_start_and_report_restart") as resume,
        patch.object(restart, "_recreate_in_place") as recreate,
    ):
        restart.task_restart(task.project.name, "1")

    resolve.assert_called_once_with(task.project)
    resume.assert_called_once()
    recreate.assert_not_called()
    task.image_protocol.assert_not_called()


@pytest.mark.parametrize("mode", ["cli", "toad"])
@pytest.mark.parametrize("state", ["running", "exited"])
@pytest.mark.parametrize("configured_sealed", [False, True])
def test_retained_sealed_workspace_recreates_with_warning(
    upgrade_task: SimpleNamespace,
    mode: str,
    state: str,
    configured_sealed: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Actual retained storage selects the warning, never a separate workflow gate."""
    task = upgrade_task
    task.project.is_sealed = configured_sealed
    task.meta["mode"] = mode
    task.container.state = state
    task.container.mounts = []
    task.container.env = {}

    restart.task_restart(task.project.name, "1", fresh=True)

    task.image_protocol.assert_called_once()
    task.preconditions.assert_called_once()
    task.container.start.assert_not_called()
    if state == "running":
        task.container.stop.assert_called_once_with(timeout=0)
    else:
        task.container.stop.assert_not_called()
    task.sandbox.rm.assert_called_once_with([f"{task.project.name}-{mode}-1"])
    runner = task.cli if mode == "cli" else task.toad
    other = task.toad if mode == "cli" else task.cli
    runner.assert_called_once_with(task.project.name, "1", unrestricted=False, debug=True)
    other.assert_not_called()
    warning = capsys.readouterr().err
    assert "container-only workspace changes" in warning
    assert "discard" in warning


@pytest.mark.parametrize("state", ["running", "exited"])
@pytest.mark.parametrize("bounce", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_retained_sealed_container_can_still_resume(
    upgrade_task: SimpleNamespace,
    state: str,
    bounce: bool,
    legacy: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nondestructive attach or resume needs no discarded-workspace warning."""
    task = upgrade_task
    task.container.state = state
    task.container.mounts = []
    if legacy:
        task.container.env = {}
    entrypoint = restart.task_restart if bounce else restart.ensure_task_running
    with (
        patch.object(restart, "_refresh_before_resume"),
        patch.object(restart, "_start_and_report_restart") as resume,
    ):
        entrypoint(task.project.name, "1")

    assert resume.called is (bounce or state == "exited")
    task.image_protocol.assert_not_called()
    assert task.container.stop.call_count == int(bounce and state == "running")
    task.sandbox.rm.assert_not_called()
    assert "container-only workspace changes" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("stage", "fresh"),
    [("container", False), ("container", True), ("image", False), ("missing-image", False)],
)
def test_uncertain_inspection_never_authorizes_recreation(
    upgrade_task: SimpleNamespace, stage: str, fresh: bool
) -> None:
    """Inspection failure and unavailable images are not confirmed obsolete protocols."""
    task = upgrade_task
    task.container.state = "exited"
    if stage == "container":
        task.container.state = None  # The old display probe falsely suggested absence.
        task.container.inspect.side_effect = RuntimeInspectionError("inspection failed")
    elif stage == "image":
        task.container.image.inspect.side_effect = RuntimeInspectionError("inspection failed")
    else:
        task.container.image = None

    with pytest.raises(SystemExit, match="inspection failed|originating image"):
        restart.task_restart(task.project.name, "1", fresh=fresh)

    task.image_protocol.assert_not_called()
    task.preconditions.assert_not_called()
    task.container.start.assert_not_called()
    task.container.stop.assert_not_called()
    task.sandbox.rm.assert_not_called()
    task.cli.assert_not_called()
    task.toad.assert_not_called()


def test_recreation_aborts_on_final_inspection_failure(
    upgrade_task: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable final snapshot aborts independently of the workspace warning policy."""
    task = upgrade_task
    task.container.state = "running"
    task.container.env = {}
    observed = task.container.inspect()
    task.container.inspect.side_effect = [observed, RuntimeInspectionError("inspection failed")]

    with pytest.raises(SystemExit, match="inspection failed"):
        restart.task_restart(task.project.name, "1", fresh=True)

    task.container.stop.assert_not_called()
    task.sandbox.rm.assert_not_called()
    task.cli.assert_not_called()
    task.toad.assert_not_called()
    assert "container-only workspace changes" not in capsys.readouterr().err


def test_recreation_warns_from_final_workspace_snapshot(
    upgrade_task: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lost backing in the final snapshot changes the warning, not the recreation workflow."""
    task = upgrade_task
    task.container.state = "running"
    task.container.env = {}
    observed = task.container.inspect()
    task.container.inspect.side_effect = [observed, replace(observed, mounts=())]

    restart.task_restart(task.project.name, "1", fresh=True)

    task.container.stop.assert_called_once_with(timeout=0)
    task.sandbox.rm.assert_called_once_with([f"{task.project.name}-cli-1"])
    task.cli.assert_called_once_with(task.project.name, "1", unrestricted=False, debug=True)
    task.toad.assert_not_called()
    warning = capsys.readouterr().err
    assert "container-only workspace changes" in warning
    assert "discard" in warning
