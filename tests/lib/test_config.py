# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from terok.lib.core import config as cfg


@pytest.fixture(autouse=True)
def reset_experimental() -> None:
    """Reset the module-global experimental flag around each test."""
    cfg.set_experimental(False)
    yield
    cfg.set_experimental(False)


def write_config(tmp_path: Path, content: str) -> Path:
    """Write a temporary config file and return its path."""
    path = tmp_path / "config.yml"
    path.write_text(content, encoding="utf-8")
    return path


def test_global_config_search_paths_respects_env_override(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg_path))

    assert cfg.global_config_search_paths() == [cfg_path.expanduser().resolve()]


def test_global_config_path_prefers_xdg(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "terok" / "config.yml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("ui:\n  base_port: 7000\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert cfg.global_config_path() == config_file.resolve()


def test_state_root_respects_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEROK_STATE_DIR", str(tmp_path))
    assert cfg.state_root() == tmp_path.resolve()


@pytest.mark.parametrize(
    ("config_text", "resolver", "expected_name"),
    [
        pytest.param("paths:\n  state_root: {path}\n", cfg.state_root, "state", id="state-root"),
        pytest.param(
            "paths:\n  user_projects_root: {path}\n",
            cfg.user_projects_root,
            "projects",
            id="user-projects-root",
        ),
        pytest.param(
            "ui:\n  base_port: 8123\nenvs:\n  base_dir: {path}\n",
            cfg.get_envs_base_dir,
            "envs",
            id="envs-root",
        ),
    ],
)
def test_configured_paths_are_resolved_from_global_config(
    monkeypatch,
    tmp_path: Path,
    config_text: str,
    resolver,
    expected_name: str,
) -> None:
    expected_path = tmp_path / expected_name
    cfg_path = write_config(tmp_path, config_text.format(path=expected_path))
    monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg_path))

    assert resolver() == expected_path.resolve()


def test_ui_base_port_is_read_from_global_config(monkeypatch, tmp_path: Path) -> None:
    cfg_path = write_config(tmp_path, "ui:\n  base_port: 8123\n")
    monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg_path))

    assert cfg.get_ui_base_port() == 8123


@pytest.mark.parametrize(
    ("config_text", "expected"),
    [
        pytest.param("tui:\n  default_tmux: true\n", True, id="true"),
        pytest.param("", False, id="default-false"),
        pytest.param("tui:\n  default_tmux: false\n", False, id="explicit-false"),
    ],
)
def test_tui_default_tmux(monkeypatch, tmp_path: Path, config_text: str, expected: bool) -> None:
    cfg_path = write_config(tmp_path, config_text)
    monkeypatch.setenv("TEROK_CONFIG_FILE", str(cfg_path))

    assert cfg.get_tui_default_tmux() is expected


def test_experimental_flag_roundtrip() -> None:
    assert not cfg.is_experimental()

    cfg.set_experimental(True)
    assert cfg.is_experimental()

    cfg.set_experimental(False)
    assert not cfg.is_experimental()


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        pytest.param({}, False, id="default-false"),
        pytest.param({"bypass_firewall_no_protection": True}, True, id="enabled"),
        pytest.param({"bypass_firewall_no_protection": False}, False, id="explicit-false"),
    ],
)
def test_get_shield_bypass_firewall_no_protection(section: dict[str, bool], expected: bool) -> None:
    with patch.object(cfg, "get_global_section", return_value=section) as mock_section:
        assert cfg.get_shield_bypass_firewall_no_protection() is expected

    mock_section.assert_called_once_with("shield")
