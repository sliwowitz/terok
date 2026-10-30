# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for shield CLI commands (registry-driven dispatch)."""

from __future__ import annotations

import argparse
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from terok_shield import ExecError

from terok.cli.commands.shield import (
    _KillSwitchShield,
    _resolve_task,
    _resolved_commands,
    dispatch,
    register,
)
from terok.lib.api.shield import SandboxConfig, ShieldManager, ShieldReading, ShieldState
from tests.testfs import MOCK_TASK_DIR_1

MISSING: object = object()
# Sentinel used when an argparse attribute should be absent.


@pytest.fixture()
def shield_parser() -> argparse.ArgumentParser:
    """Return an argument parser with the shield subcommands registered."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="cmd"))
    return parser


@pytest.fixture(autouse=True)
def _stub_resolve_task_id() -> object:
    """Bypass filesystem-backed ``resolve_task_id`` — these tests mock ``_resolve_task``."""
    with patch(
        "terok.cli.commands.shield.resolve_task_id",
        side_effect=lambda _pid, tid: tid,
    ) as stub:
        yield stub


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(["shield", "status"], {"shield_cmd": "status"}, id="status-no-task"),
        pytest.param(
            ["shield", "status", "proj", "1"],
            {"shield_cmd": "status", "project_name": "proj", "task_id": "1"},
            id="status-with-task",
        ),
        pytest.param(
            ["shield", "allow", "proj", "task1", "example.com"],
            {
                "shield_cmd": "allow",
                "project_name": "proj",
                "task_id": "task1",
                "target": "example.com",
            },
            id="allow",
        ),
        pytest.param(
            ["shield", "deny", "proj", "task1", "example.com"],
            {
                "shield_cmd": "deny",
                "project_name": "proj",
                "task_id": "task1",
                "target": "example.com",
            },
            id="deny",
        ),
        pytest.param(
            ["shield", "down", "proj", "task1", "--disengage"],
            {
                "shield_cmd": "down",
                "project_name": "proj",
                "task_id": "task1",
                "disengaged": True,
            },
            id="down-disengage",
        ),
        pytest.param(
            ["shield", "up", "proj", "task1"],
            {"shield_cmd": "up", "project_name": "proj", "task_id": "task1"},
            id="up",
        ),
        pytest.param(
            ["shield", "rules", "proj", "task1"],
            {"shield_cmd": "rules", "project_name": "proj", "task_id": "task1"},
            id="rules",
        ),
        pytest.param(
            ["shield", "profiles"],
            {"shield_cmd": "profiles", "project_name": MISSING},
            id="profiles",
        ),
        pytest.param(
            ["shield", "install-hooks"],
            {"shield_cmd": "install-hooks"},
            id="install-hooks",
        ),
        pytest.param(
            ["shield", "watch", "proj", "task1"],
            {"shield_cmd": "watch", "project_name": "proj", "task_id": "task1"},
            id="watch",
        ),
        pytest.param(
            ["shield", "simple-clearance", "proj", "task1"],
            {"shield_cmd": "simple-clearance", "project_name": "proj", "task_id": "task1"},
            id="simple-clearance",
        ),
        pytest.param(
            ["shield", "logs", "proj", "task1"],
            {
                "shield_cmd": "logs",
                "project_name": "proj",
                "task_id": "task1",
                "n": 50,
                "container": MISSING,
            },
            id="logs",
        ),
        pytest.param(
            ["shield", "logs", "proj", "task1", "-n", "10"],
            {
                "shield_cmd": "logs",
                "project_name": "proj",
                "task_id": "task1",
                "n": 10,
                "container": MISSING,
            },
            id="logs-custom-n",
        ),
    ],
)
def test_register_parses_shield_subcommands(
    shield_parser: argparse.ArgumentParser,
    argv: list[str],
    expected: dict[str, object],
) -> None:
    """Registered shield subcommands parse the expected argument shapes."""
    args = shield_parser.parse_args(argv)
    for key, value in expected.items():
        if value is MISSING:
            assert not hasattr(args, key)
        else:
            assert getattr(args, key) == value


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            ["shield", "bypass", "proj", "task1"],
            {"duration": None, "off": False},
            id="bare-reports-the-countdown",
        ),
        pytest.param(
            ["shield", "bypass", "proj", "task1", "--for", "5m"],
            {"duration": "5m", "off": False},
            id="for-opens-the-window",
        ),
        pytest.param(
            ["shield", "bypass", "proj", "task1", "--off"],
            {"duration": None, "off": True},
            id="off-closes-it",
        ),
    ],
)
def test_bypass_comes_from_the_shield_registry(
    shield_parser: argparse.ArgumentParser,
    argv: list[str],
    expected: dict[str, object],
) -> None:
    """The window verb needs no terok-side plumbing — the registry generates it."""
    args = shield_parser.parse_args(argv)
    assert args.shield_cmd == "bypass"
    assert (args.project_name, args.task_id) == ("proj", "task1")
    for key, value in expected.items():
        assert getattr(args, key) == value


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(
            [
                "shield",
                "override",
                "proj",
                "task1",
                "api.example.test",
                "--expires",
                "2099-12-31",
                "--reason",
                "vendor outage",
            ],
            {
                "target": "api.example.test",
                "expires": date(2099, 12, 31),
                "reason": "vendor outage",
                "off": False,
            },
            id="grant",
        ),
        pytest.param(
            ["shield", "override", "proj", "task1", "api.example.test", "--off"],
            {"target": "api.example.test", "expires": None, "reason": None, "off": True},
            id="revoke",
        ),
    ],
)
def test_override_comes_from_the_shield_registry(
    shield_parser: argparse.ArgumentParser,
    argv: list[str],
    expected: dict[str, object],
) -> None:
    """Break-glass needs no terok-side plumbing either: the registry verb arrives whole."""
    args = shield_parser.parse_args(argv)
    assert args.shield_cmd == "override"
    assert (args.project_name, args.task_id) == ("proj", "task1")
    for key, value in expected.items():
        assert getattr(args, key) == value


def test_override_rejects_an_expiry_that_is_not_a_date(
    shield_parser: argparse.ArgumentParser,
) -> None:
    """``--expires`` takes a calendar date only, refused at parse time otherwise."""
    with patch("sys.stderr", new_callable=StringIO), pytest.raises(SystemExit):
        shield_parser.parse_args(
            ["shield", "override", "proj", "task1", "h.test", "--expires", "+30d"]
        )


def test_terok_own_subcommands_never_shadow_a_registry_verb() -> None:
    """``install-hooks``, ``sets``, ``promote`` and ``export`` are hand-registered beside the registry's verbs.

    A shield verb of the same name would make argparse refuse the second
    ``add_parser`` and break every ``terok shield`` invocation.
    """
    registry = {cmd.name for cmd in _resolved_commands()}
    assert {"install-hooks", "sets", "promote", "export"}.isdisjoint(registry)
    assert "override" in registry


@pytest.mark.parametrize("command", ["prepare", "run", "resolve"])
def test_register_excludes_standalone_only_commands(
    shield_parser: argparse.ArgumentParser,
    command: str,
) -> None:
    """Standalone-only commands are not available through the main CLI parser."""
    with pytest.raises(SystemExit):
        shield_parser.parse_args(["shield", command])


def test_dispatch_returns_false_for_non_shield_commands() -> None:
    """Dispatch ignores non-shield CLI namespaces."""
    assert not dispatch(argparse.Namespace(cmd="project"))


def test_dispatch_sets_lists_registry() -> None:
    """Bare ``shield sets`` prints every curated set and what ``recommended`` expands to."""
    from terok.lib.api import EGRESS_SETS, RECOMMENDED_SET

    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name=None, sets_selection=None
    )
    with patch("sys.stdout", new_callable=StringIO) as out:
        assert dispatch(args)
    lines = [line.strip() for line in out.getvalue().splitlines()]
    for name in EGRESS_SETS:
        assert any(line.startswith(f"{name}:") for line in lines)
    recommended_line = next(line for line in lines if line.startswith(f"{RECOMMENDED_SET}:"))
    assert all(name in recommended_line for name in EGRESS_SETS)


def _sets_listing() -> list[str]:
    """Run bare ``terok shield sets`` and return its stripped output lines."""
    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name=None, sets_selection=None
    )
    with patch("sys.stdout", new_callable=StringIO) as out:
        assert dispatch(args)
    return [line.strip() for line in out.getvalue().splitlines()]


def _write_user_set(name: str, text: str) -> Path:
    """Write a user egress set into the test-isolated egress-sets directory."""
    from terok.lib.api import egress_sets_dir

    directory = egress_sets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_dispatch_sets_says_recommended_is_built_in_only() -> None:
    """The ``recommended`` line spells out that it never covers the operator's own sets."""
    from terok.lib.api import RECOMMENDED_SET

    recommended = next(line for line in _sets_listing() if line.startswith(f"{RECOMMENDED_SET}:"))
    assert "built-in sets only" in recommended


def test_dispatch_sets_without_user_sets_points_at_the_directory() -> None:
    """No user sets yet: the listing names where one would go."""
    from terok.lib.api import egress_sets_dir

    lines = _sets_listing()
    header = next(line for line in lines if line.startswith("Your egress sets"))
    assert str(egress_sets_dir()) in header
    assert lines[lines.index(header) + 1] == "none yet"


def test_dispatch_sets_lists_user_sets_with_origin_file_and_users() -> None:
    """User sets come with their file and the projects naming them; broken files say why."""
    corp = _write_user_set("corp.txt", "artifactory.corp.example\npypi-mirror.corp.example\n")
    broken = _write_user_set("broken.txt", "*.corp.example\n")
    shadow = _write_user_set("python.txt", "evil.example\n")
    with patch("terok.lib.api.egress_set_users", return_value={"corp": ("proj-a", "proj-b")}):
        lines = _sets_listing()
    at = lines.index("corp: artifactory.corp.example, pypi-mirror.corp.example")
    assert lines[at + 1 : at + 3] == [f"file: {corp}", "used by: proj-a, proj-b"]
    broken_at = next(i for i, line in enumerate(lines) if line.startswith("broken: INVALID"))
    assert "line 1" in lines[broken_at]
    assert lines[broken_at + 1 : broken_at + 3] == [f"file: {broken}", "used by: no project"]
    assert any(line.startswith(f"ignored {shadow}:") for line in lines)
    built_in_python = next(line for line in lines if line.startswith("python:"))
    assert "evil.example" not in built_in_python


def test_dispatch_sets_shows_project_selection() -> None:
    """``shield sets <project>`` reports ``shield.sets`` as authored and the sets it grants."""
    from terok.lib.api import EGRESS_SETS, RECOMMENDED_SET

    project = MagicMock()
    project.shield_sets = (RECOMMENDED_SET,)
    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name="proj", sets_selection=None
    )
    with (
        patch("terok.lib.api.load_project", return_value=project),
        patch("sys.stdout", new_callable=StringIO) as out,
    ):
        assert dispatch(args)
    authored, active = out.getvalue().splitlines()
    assert authored.endswith(f": {RECOMMENDED_SET}")
    assert active.endswith(", ".join(EGRESS_SETS))


def test_dispatch_sets_unset_project_grants_no_sets() -> None:
    """An unset ``shield.sets`` lists no curated set, in the shared no-sets wording."""
    from terok.lib.api import EGRESS_SETS, describe_egress_sets

    project = MagicMock()
    project.shield_sets = None
    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name="proj", sets_selection=None
    )
    with (
        patch("terok.lib.api.load_project", return_value=project),
        patch("sys.stdout", new_callable=StringIO) as out,
    ):
        assert dispatch(args)
    assert describe_egress_sets(None) in out.getvalue()
    assert not any(name in out.getvalue() for name in EGRESS_SETS)


def test_dispatch_sets_writes_selection() -> None:
    """``shield sets <project> --set a,b`` writes through the validated writer."""
    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name="proj", sets_selection="python,git-hosting"
    )
    with (
        patch("terok.lib.api.set_project_shield_sets") as writer,
        patch("sys.stdout", new_callable=StringIO),
    ):
        assert dispatch(args)
    writer.assert_called_once_with("proj", ("python", "git-hosting"))


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        pytest.param("none", (), id="none-grants-nothing"),
        pytest.param("recommended", ("recommended",), id="recommended-by-name"),
    ],
)
def test_dispatch_sets_special_tokens(token: str, expected: object) -> None:
    """``--set none`` writes an empty list; ``--set recommended`` names the meta-set."""
    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name="proj", sets_selection=token
    )
    with (
        patch("terok.lib.api.set_project_shield_sets") as writer,
        patch("sys.stdout", new_callable=StringIO),
    ):
        assert dispatch(args)
    writer.assert_called_once_with("proj", expected)


def test_dispatch_sets_rejects_unknown_set_name() -> None:
    """``--set`` with an unknown set name fails before touching project.yml."""
    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name="proj", sets_selection="pythn"
    )
    with pytest.raises(SystemExit, match="Unknown shield.sets entry: 'pythn'"):
        dispatch(args)


def test_dispatch_sets_set_requires_project() -> None:
    """``--set`` without a project exits with a clean CLI error."""
    args = argparse.Namespace(
        cmd="shield", shield_cmd="sets", project_name=None, sets_selection="python"
    )
    with (
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc_info,
    ):
        dispatch(args)
    assert exc_info.value.code == 1
    assert "requires a project" in err.getvalue()


@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_status_without_task(mock_mgr_cls: MagicMock) -> None:
    """Bare ``shield status`` shows the runtime/available shield status."""
    mock_shield = MagicMock()
    mock_shield.status.return_value = {
        "mode": "hook",
        "profiles": ["dev-standard"],
        "audit_enabled": True,
    }
    mock_mgr_cls.return_value.shield = mock_shield

    with patch("sys.stdout", new_callable=StringIO) as out:
        assert dispatch(argparse.Namespace(cmd="shield", shield_cmd="status"))

    mock_shield.status.assert_called_once_with()
    assert "Mode" in out.getvalue()
    assert "hook" in out.getvalue()


def test_dispatch_partial_task_selector_exits() -> None:
    """Providing only one half of the project/task selector exits cleanly."""
    args = argparse.Namespace(cmd="shield", shield_cmd="status", project_name="proj", task_id=None)
    with (
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc_info,
    ):
        dispatch(args)

    assert exc_info.value.code == 1
    assert "both" in err.getvalue()


@pytest.mark.parametrize(
    ("shield_cmd", "shield_method", "shield_result", "expected_text"),
    [
        pytest.param(
            "status",
            "inspect",
            ShieldReading(state=ShieldState.UP, bypass_remaining=None),
            "up",
            id="task-status",
        ),
        pytest.param(
            "status",
            "inspect",
            ShieldReading(state=ShieldState.UP, bypass_remaining=299),
            "up (bypass window open, 4m59s left)",
            id="task-status-window-open",
        ),
    ],
)
@patch("terok.cli.commands.shield._resolve_task", return_value=("proj-cli-1", MOCK_TASK_DIR_1))
@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_task_scoped_commands(
    mock_mgr_cls: MagicMock,
    _resolve: MagicMock,
    shield_cmd: str,
    shield_method: str,
    shield_result: object,
    expected_text: str,
) -> None:
    """Task-scoped shield commands resolve the task and delegate to the shield object."""
    mock_shield = MagicMock()
    getattr(mock_shield, shield_method).return_value = shield_result
    mock_mgr_cls.return_value.shield = mock_shield

    args = argparse.Namespace(cmd="shield", shield_cmd=shield_cmd, project_name="proj", task_id="1")
    with patch("sys.stdout", new_callable=StringIO) as out:
        assert dispatch(args)

    getattr(mock_shield, shield_method).assert_called_once_with("proj-cli-1")
    assert expected_text in out.getvalue()


@patch("terok.cli.commands.shield._resolve_task", return_value=("proj-cli-1", MOCK_TASK_DIR_1))
@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_logs_resolves_container_and_tails(
    mock_mgr_cls: MagicMock,
    _resolve: MagicMock,
) -> None:
    """``logs`` forwards only ``n`` — the resolved container rides positionally.

    The registry spells logs' container arg ``--container`` (the standalone
    CLI's optional filter); forwarding it from parsed args used to hand the
    real handler ``container`` twice (TypeError: multiple values).
    """
    mock_shield = MagicMock()
    mock_shield.tail_log.return_value = [{"verdict": "allow"}]
    mock_mgr_cls.return_value.shield = mock_shield

    # ``container=None`` mirrors the pre-fix parser namespace; the kwarg
    # extraction must ignore the attribute whether or not argparse sets it.
    args = argparse.Namespace(
        cmd="shield",
        shield_cmd="logs",
        project_name="proj",
        task_id="1",
        container=None,
        n=5,
    )
    with patch("sys.stdout", new_callable=StringIO) as out:
        assert dispatch(args)

    mock_shield.tail_log.assert_called_once_with(5)
    assert "allow" in out.getvalue()


@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_preview_disengage_without_down_prints_error(mock_mgr_cls: MagicMock) -> None:
    """``preview --disengage`` without ``--down`` fails with a clean CLI error."""
    mock_shield = MagicMock()
    mock_shield.preview.side_effect = ValueError("--disengage requires --down")
    mock_mgr_cls.return_value.shield = mock_shield

    args = argparse.Namespace(cmd="shield", shield_cmd="preview", down=False, disengaged=True)
    with (
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc_info,
    ):
        dispatch(args)

    assert exc_info.value.code == 1
    assert "--disengage requires --down" in err.getvalue()


@patch("terok.cli.commands.shield._resolve_task", return_value=("proj-cli-1", MOCK_TASK_DIR_1))
@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_exec_error_surfaces_details(
    mock_mgr_cls: MagicMock,
    _resolve: MagicMock,
) -> None:
    """NFT execution errors surface the actual error details."""
    mock_shield = MagicMock()
    mock_shield.inspect.side_effect = ExecError(["nft", "list"], 1, "no such process")
    mock_mgr_cls.return_value.shield = mock_shield

    args = argparse.Namespace(cmd="shield", shield_cmd="status", project_name="proj", task_id="1")
    with (
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc_info,
    ):
        dispatch(args)

    assert exc_info.value.code == 1
    assert "shield operation failed" in err.getvalue()
    assert "task 1" in err.getvalue()


@patch("terok.cli.commands.shield._resolve_task", return_value=("proj-cli-1", MOCK_TASK_DIR_1))
@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_runtime_error_prints_message(
    mock_mgr_cls: MagicMock,
    _resolve: MagicMock,
) -> None:
    """Runtime errors from shield commands are surfaced cleanly.

    ``allow`` widens egress, so it reaches the shield through the manager
    (where the kill switch sits) — the error comes from there.
    """
    mock_mgr_cls.return_value.allow.side_effect = RuntimeError("No IPs allowed for proj-cli-1")

    args = argparse.Namespace(
        cmd="shield",
        shield_cmd="allow",
        project_name="proj",
        task_id="1",
        target="example.com",
    )
    with (
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc_info,
    ):
        dispatch(args)

    assert exc_info.value.code == 1
    assert "No IPs allowed" in err.getvalue()


@patch("terok.lib.integrations.sandbox.ShieldHooks.install")
def test_install_hooks_dispatch(mock_install: MagicMock) -> None:
    """``shield install-hooks`` delegates to ``ShieldHooks.install()``."""
    assert dispatch(argparse.Namespace(cmd="shield", shield_cmd="install-hooks"))
    mock_install.assert_called_once_with()


@patch("terok_shield.simple_clearance.run_simple_clearance")
@patch("terok.cli.commands.shield._resolve_task", return_value=("proj-cli-1", MOCK_TASK_DIR_1))
@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_simple_clearance(
    mock_mgr_cls: MagicMock,
    _resolve: MagicMock,
    mock_run: MagicMock,
) -> None:
    """``shield simple-clearance`` dispatches to the terminal fallback handler."""
    mock_shield = MagicMock()
    mock_shield.config.state_dir = MOCK_TASK_DIR_1 / "shield"
    mock_mgr_cls.return_value.shield = mock_shield
    mock_mgr_cls.return_value.disabled = False  # the kill switch refuses the verb outright

    args = argparse.Namespace(
        cmd="shield", shield_cmd="simple-clearance", project_name="proj", task_id="1"
    )
    assert dispatch(args)
    mock_run.assert_called_once_with(mock_shield.config.state_dir, "proj-cli-1")


@patch("terok_shield.watch.run_watch")
@patch("terok_shield._confine.confine_to_state")
@patch("terok.cli.commands.shield._resolve_task", return_value=("proj-cli-1", MOCK_TASK_DIR_1))
@patch("terok.cli.commands.shield.ShieldManager")
def test_dispatch_watch(
    mock_mgr_cls: MagicMock,
    _resolve: MagicMock,
    mock_confine: MagicMock,
    mock_run: MagicMock,
) -> None:
    """``shield watch`` confines the process first, then enters the watch loop."""
    mock_shield = MagicMock()
    mock_shield.config.state_dir = MOCK_TASK_DIR_1 / "shield"
    mock_mgr_cls.return_value.shield = mock_shield

    # Order is the security invariant: the Landlock floor must be in place
    # before the long-lived reader loop starts, so record both on one parent.
    sequence = MagicMock()
    sequence.attach_mock(mock_confine, "confine")
    sequence.attach_mock(mock_run, "run_watch")

    args = argparse.Namespace(cmd="shield", shield_cmd="watch", project_name="proj", task_id="1")
    assert dispatch(args)
    assert sequence.mock_calls == [
        call.confine(mock_shield.config.state_dir),
        call.run_watch(mock_shield.config.state_dir, "proj-cli-1"),
    ]


@patch("terok.lib.orchestration.tasks.load_task_meta", return_value=({"mode": None}, None))
@patch("terok.lib.core.projects.load_project")
def test_resolve_task_errors(
    mock_project: MagicMock,
    _meta: MagicMock,
) -> None:
    """Task resolution rejects tasks that have never been run."""
    mock_project.return_value = MagicMock()
    mock_project.return_value.name = "proj"
    with pytest.raises(ValueError, match="has never been run"):
        _resolve_task("proj", "1")


# ── _persist_desired_state ───────────────────────────────


class TestPersistDesiredState:
    """Verify that shield up/down dispatch persists the desired state file."""

    def test_up_persists(self, tmp_path: Path) -> None:
        """``shield up`` writes 'up' to the desired state file."""
        from terok.cli.commands.shield import _persist_desired_state

        _persist_desired_state("up", tmp_path, {})
        assert (tmp_path / "shield_desired_state").read_text().strip() == "up"

    def test_down_persists(self, tmp_path: Path) -> None:
        """``shield down`` writes 'down' to the desired state file."""
        from terok.cli.commands.shield import _persist_desired_state

        _persist_desired_state("down", tmp_path, {})
        assert (tmp_path / "shield_desired_state").read_text().strip() == "down"

    def test_disengaged_persists(self, tmp_path: Path) -> None:
        """``shield down --disengage`` writes 'disengaged' to the desired state file."""
        from terok.cli.commands.shield import _persist_desired_state

        _persist_desired_state("down", tmp_path, {"disengaged": True})
        assert (tmp_path / "shield_desired_state").read_text().strip() == "disengaged"

    def test_unrelated_command_noop(self, tmp_path: Path) -> None:
        """Non up/down commands do not create a state file."""
        from terok.cli.commands.shield import _persist_desired_state

        _persist_desired_state("rules", tmp_path, {})
        assert not (tmp_path / "shield_desired_state").exists()

    def test_oserror_swallowed(self, tmp_path: Path) -> None:
        """OSError during write is logged to stderr but does not raise."""
        from terok.cli.commands.shield import _persist_desired_state

        bad_dir = tmp_path / "no" / "such" / "dir"
        # Should not raise — the error is printed to stderr
        _persist_desired_state("up", bad_dir, {})

    @patch(
        "terok.lib.orchestration.task_runners.resolve_container_uuid",
        return_value="deadbeefcafe1234",
    )
    @patch("terok.cli.commands.shield._resolve_task")
    @patch("terok.cli.commands.shield.ShieldManager")
    def test_dispatch_up_persists_state(
        self,
        mock_mgr_cls: MagicMock,
        mock_resolve: MagicMock,
        _mock_uuid: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Full dispatch of ``shield up`` persists the desired state."""
        mock_resolve.return_value = ("proj-cli-1", tmp_path)
        mock_shield = MagicMock()
        mock_mgr_cls.return_value.shield = mock_shield

        args = argparse.Namespace(cmd="shield", shield_cmd="up", project_name="proj", task_id="1")
        assert dispatch(args)
        mock_shield.up.assert_called_once()
        assert (tmp_path / "shield_desired_state").read_text().strip() == "up"

    @patch(
        "terok.lib.orchestration.task_runners.resolve_container_uuid",
        return_value="deadbeefcafe1234",
    )
    @patch("terok.cli.commands.shield._resolve_task")
    @patch("terok.cli.commands.shield.ShieldManager")
    def test_dispatch_down_persists_state(
        self,
        mock_mgr_cls: MagicMock,
        mock_resolve: MagicMock,
        _mock_uuid: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Full dispatch of ``shield down`` persists the desired state."""
        mock_resolve.return_value = ("proj-cli-1", tmp_path)
        mock_shield = MagicMock()
        mock_mgr_cls.return_value.shield = mock_shield

        args = argparse.Namespace(
            cmd="shield", shield_cmd="down", project_name="proj", task_id="1", disengaged=False
        )
        assert dispatch(args)
        mock_shield.down.assert_called_once()
        assert (tmp_path / "shield_desired_state").read_text().strip() == "down"


# ── kill switch ──────────────────────────────────────────


def test_kill_switch_shield_routes_widening_calls_through_the_manager() -> None:
    """``allow``, ``bypass`` and ``override`` meet the manager's guard; the rest is the shield's."""
    manager = MagicMock()
    shield = _KillSwitchShield(manager)

    assert shield.allow is manager.allow
    assert shield.bypass is manager.bypass
    assert shield.override is manager.override
    assert shield.bypass_off is manager.shield.bypass_off
    assert shield.inspect is manager.shield.inspect
    assert shield.revoke_override is manager.shield.revoke_override


def _kill_switch_dispatch(raw: MagicMock, shield_cmd: str, **extra: object) -> object:
    """Dispatch *shield_cmd* on a task with the firewall kill switch thrown.

    The manager is real, built from a config with ``shield_disabled`` set;
    only the bare shield underneath is a double, so a call that slipped past
    the guard would show up on it.
    """
    args = argparse.Namespace(
        cmd="shield", shield_cmd=shield_cmd, project_name="proj", task_id="1", **extra
    )
    with (
        patch(
            "terok.cli.commands.shield._resolve_task",
            return_value=("proj-cli-1", MOCK_TASK_DIR_1),
        ),
        patch(
            "terok.cli.commands.shield.make_sandbox_config",
            return_value=SandboxConfig(shield_disabled=True),
        ),
        patch.object(ShieldManager, "shield", raw),
    ):
        return dispatch(args)


@pytest.mark.parametrize(
    ("shield_cmd", "extra", "message"),
    [
        pytest.param("allow", {"target": "h.test"}, "no ruleset to allow through", id="allow"),
        pytest.param(
            "bypass", {"duration": "5m", "off": False}, "no window to open", id="bypass-open"
        ),
        pytest.param(
            "override",
            {"target": "h.test", "expires": date(2099, 1, 1), "reason": "audit", "off": False},
            "no security tier to break glass",
            id="override-grant",
        ),
    ],
)
def test_kill_switch_refuses_the_verbs_that_widen_egress(
    shield_cmd: str, extra: dict[str, object], message: str
) -> None:
    """With the firewall switched off, a verb that would open something exits non-zero.

    Sandbox opens nothing while the switch is set; reaching past the manager
    to the bare shield would have widened egress on a container started
    before the switch was thrown.
    """
    raw = MagicMock()
    with (
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc_info,
    ):
        _kill_switch_dispatch(raw, shield_cmd, **extra)

    assert exc_info.value.code == 1
    assert message in err.getvalue()
    for call_name in ("allow", "bypass", "override"):
        getattr(raw, call_name).assert_not_called()


def test_kill_switch_refuses_simple_clearance_before_it_starts() -> None:
    """The terminal fallback allows through its own ``terok-shield allow`` child process.

    That child reads its own configuration and never sees terok's switch, so
    no wrapper around the shield can stop it: the verb must not start at all.
    """
    raw = MagicMock()
    with (
        patch("terok_shield.simple_clearance.run_simple_clearance") as session,
        patch("sys.stderr", new_callable=StringIO) as err,
        pytest.raises(SystemExit) as exc_info,
    ):
        _kill_switch_dispatch(raw, "simple-clearance")

    assert exc_info.value.code == 1
    assert "shield is disabled" in err.getvalue()
    assert "simple-clearance" in err.getvalue()
    session.assert_not_called()


@pytest.mark.parametrize(
    ("shield_cmd", "extra", "raw_call"),
    [
        pytest.param("bypass", {"duration": None, "off": True}, "bypass_off", id="close-window"),
        pytest.param(
            "override",
            {"target": "h.test", "expires": None, "reason": None, "off": True},
            "revoke_override",
            id="revoke-override",
        ),
    ],
)
def test_kill_switch_still_lets_a_door_close(
    shield_cmd: str, extra: dict[str, object], raw_call: str
) -> None:
    """Closing only narrows egress, so it runs whatever the switch says."""
    raw = MagicMock()
    with patch("sys.stdout", new_callable=StringIO):
        assert _kill_switch_dispatch(raw, shield_cmd, **extra)

    getattr(raw, raw_call).assert_called_once()
