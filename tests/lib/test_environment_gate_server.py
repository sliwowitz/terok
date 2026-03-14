# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for environment.py gate-server integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from terok.lib.containers.environment import _security_mode_env_and_volumes
from terok.lib.core.projects import load_project
from test_utils import mock_git_config, project_env

_GATEKEEPING_YAML = """\
project:
  id: gk-proj
  security_class: gatekeeping
git:
  upstream_url: https://example.com/repo.git
  default_branch: main
"""

_ONLINE_YAML = """\
project:
  id: online-proj
  security_class: online
git:
  upstream_url: https://example.com/repo.git
  default_branch: main
"""


def gate_mounts(volumes: list[str]) -> list[str]:
    """Return any gate-related volume mounts from the generated volume list."""
    return [volume for volume in volumes if "git-gate" in volume or "gate" in volume.split(":")[0]]


@pytest.mark.parametrize(
    ("yaml_text", "project_id", "token", "env_key"),
    [
        pytest.param(_GATEKEEPING_YAML, "gk-proj", "deadbeef" * 4, "CODE_REPO", id="gatekeeping"),
        pytest.param(
            _ONLINE_YAML, "online-proj", "cafebabe" * 4, "CLONE_FROM", id="online-with-gate"
        ),
    ],
)
def test_gate_projects_use_http_urls_with_tokens(
    yaml_text: str,
    project_id: str,
    token: str,
    env_key: str,
) -> None:
    """Gate-backed project modes generate token-authenticated HTTP URLs."""
    with (
        mock_git_config(),
        project_env(yaml_text, project_id=project_id, with_gate=True) as ctx,
        patch("terok.lib.containers.environment.ensure_server_reachable"),
        patch("terok.lib.containers.environment.get_gate_server_port", return_value=9418),
        patch(
            "terok.lib.containers.environment.get_gate_base_path",
            return_value=ctx.state_dir / "gate",
        ),
        patch("terok.lib.security.gate_tokens.create_token", return_value=token),
    ):
        project = load_project(project_id)
        env, volumes = _security_mode_env_and_volumes(project, Path(ctx.base / "ssh"), "1")

    assert env[env_key] == f"http://{token}@host.containers.internal:9418/{project_id}.git"
    assert gate_mounts(volumes) == []

    if project.security_class == "gatekeeping":
        assert env["GIT_BRANCH"] == "main"
    else:
        assert env["CODE_REPO"] == "https://example.com/repo.git"


def test_gatekeeping_missing_gate_raises() -> None:
    """Gatekeeping mode requires a synced gate mirror before task startup."""
    with mock_git_config(), project_env(_GATEKEEPING_YAML, project_id="gk-proj", with_gate=False):
        project = load_project("gk-proj")
        with pytest.raises(SystemExit, match="gate-sync"):
            _security_mode_env_and_volumes(project, Path("/tmp/ssh"), "1")


def test_gatekeeping_server_not_running_raises() -> None:
    """Gatekeeping mode fails when the gate server cannot be reached."""
    with (
        mock_git_config(),
        project_env(_GATEKEEPING_YAML, project_id="gk-proj", with_gate=True),
        patch(
            "terok.lib.containers.environment.ensure_server_reachable",
            side_effect=SystemExit("Gate server unavailable"),
        ),
    ):
        project = load_project("gk-proj")
        with pytest.raises(SystemExit, match="Gate server"):
            _security_mode_env_and_volumes(project, Path("/tmp/ssh"), "1")


@pytest.mark.parametrize(
    "server_reachable",
    [pytest.param(True, id="server-up"), pytest.param(False, id="server-down")],
)
def test_online_gate_server_fallback(server_reachable: bool) -> None:
    """Online mode uses CLONE_FROM only when the gate server is reachable."""
    with (
        mock_git_config(),
        project_env(_ONLINE_YAML, project_id="online-proj", with_gate=True) as ctx,
        patch(
            "terok.lib.containers.environment.ensure_server_reachable",
            side_effect=None if server_reachable else SystemExit("server down"),
        ),
        patch("terok.lib.containers.environment.get_gate_server_port", return_value=9418),
        patch(
            "terok.lib.containers.environment.get_gate_base_path",
            return_value=ctx.state_dir / "gate",
        ),
        patch("terok.lib.security.gate_tokens.create_token", return_value="cafebabe" * 4),
    ):
        project = load_project("online-proj")
        env, volumes = _security_mode_env_and_volumes(project, Path(ctx.base / "ssh"), "1")

    if server_reachable:
        assert env["CLONE_FROM"] == (
            "http://cafebabecafebabecafebabecafebabe@host.containers.internal:9418/online-proj.git"
        )
    else:
        assert "CLONE_FROM" not in env
    assert env["CODE_REPO"] == "https://example.com/repo.git"
    assert gate_mounts(volumes) == []


def test_online_without_gate_has_no_clone_from() -> None:
    """Online mode without a gate mirror clones directly from upstream only."""
    with (
        mock_git_config(),
        project_env(_ONLINE_YAML, project_id="online-proj", with_gate=False) as ctx,
    ):
        project = load_project("online-proj")
        env, volumes = _security_mode_env_and_volumes(project, Path(ctx.base / "ssh"), "1")

    assert "CLONE_FROM" not in env
    assert env["CODE_REPO"] == "https://example.com/repo.git"
    assert gate_mounts(volumes) == []
