# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Global credentials failures must not masquerade as broken project files."""

import traceback
from pathlib import Path

import pytest

from terok.lib.core.projects import (
    discover_projects,
    list_projects,
    load_project,
    validate_project_yaml,
)
from tests.test_utils import write_project


@pytest.fixture
def invalid_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give two valid projects a global configuration using the removed key."""
    config_path = tmp_path / "config.yml"
    config_path.write_text("credentials:\n  use_keyring: true\n", encoding="utf-8")
    monkeypatch.setenv("TEROK_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("TEROK_CONFIG_DIR", str(tmp_path))
    for name in ("first", "second"):
        write_project(tmp_path / "projects", name, f"project:\n  name: {name}\n")
    return config_path


def test_global_credentials_error_is_not_a_project_error(invalid_global_config: Path) -> None:
    """CLI callers get the global source and migration hint, not a project-file accusation."""
    with pytest.raises(SystemExit) as exc_info:
        load_project("first")
    message = str(exc_info.value)
    assert str(invalid_global_config) in message
    assert "credentials.use_desktop_keyring" in message
    assert "project.yml" not in message


def test_discovery_marks_projects_as_globally_blocked(invalid_global_config: Path) -> None:
    """The TUI receives structured global provenance for every affected project."""
    valid, broken = discover_projects()
    assert valid == []
    assert [project.name for project in broken] == ["first", "second"]
    for project in broken:
        assert project.global_config_paths == (invalid_global_config,)
        assert str(invalid_global_config) in project.error
        assert "project.yml" not in project.error


def test_project_editor_reports_global_failure(invalid_global_config: Path) -> None:
    """A valid project draft is blocked by the global error, not labeled invalid."""
    error = validate_project_yaml("first", "project:\n  name: first\n")
    assert error is not None
    assert str(invalid_global_config) in error
    assert "project.yml" not in error


def test_cli_listing_calls_global_failures_blocked(
    invalid_global_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Discovery's CLI wrapper keeps the global/project distinction too."""
    assert list_projects() == []
    output = capsys.readouterr().err
    assert "skipping blocked project 'first'" in output
    assert str(invalid_global_config) in output
    assert "broken project" not in output


def test_fixed_global_credentials_config_recovers_without_project_edits(
    invalid_global_config: Path,
) -> None:
    """Fresh credential-policy reads let a rescan recover after the global file is repaired."""
    assert discover_projects()[0] == []
    invalid_global_config.write_text(
        "credentials:\n  use_desktop_keyring: true\n", encoding="utf-8"
    )
    valid, broken = discover_projects()
    assert [project.name for project in valid] == ["first", "second"]
    assert broken == []


def test_genuinely_invalid_project_still_points_to_its_file(invalid_global_config: Path) -> None:
    """An independent project syntax error remains repairable as a project error."""
    project_path = invalid_global_config.parent / "projects" / "first" / "project.yml"
    project_path.write_text("project: [\n", encoding="utf-8")
    _, broken = discover_projects()
    first, second = broken
    assert first.global_config_paths == ()
    assert first.config_path == project_path
    assert str(project_path) in first.error
    assert second.global_config_paths == (invalid_global_config,)


@pytest.mark.parametrize(
    "configuration",
    [
        "credentials:\n  use_keyring: sensitive-test-value\n",
        "credentials:\n  sensitive-test-value: true\n",
        "credentials:\n  passphrase: sensitive-test-value\n",
        "credentials:\n  passphrase_command: [sensitive-test-value\n",
    ],
    ids=["removed-key-value", "unknown-key", "removed-plaintext", "malformed-yaml"],
)
def test_global_failure_does_not_disclose_credentials(
    invalid_global_config: Path, configuration: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither project errors nor earlier global-config warnings quote credential contents."""
    invalid_global_config.write_text(configuration, encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_project("first")
    captured = capsys.readouterr()
    diagnostics = "".join(traceback.format_exception(exc_info.value)) + captured.out + captured.err
    assert "sensitive-test-value" not in diagnostics
    assert str(invalid_global_config) in str(exc_info.value)
    assert "Invalid project.yml" not in str(exc_info.value)
