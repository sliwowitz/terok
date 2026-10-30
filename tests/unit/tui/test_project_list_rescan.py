# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""A project-list re-scan selects the project again, through the real Textual ``ListView``.

[`TerokTUI.refresh_projects`][terok.tui.app.TerokTUI.refresh_projects]
awaits the list rebuild, then selects the project.  While the rebuild is
still in flight the list holds the old rows, and a selection lands on one
of them and is dropped — so a project repaired in the editor would never
be selected again, and its pollers would never restart.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult

from terok.lib.api import BrokenProject
from terok.tui.widgets.project_list import ProjectList


class _ListHost(App):
    """An app holding just the project list, recording every project selection."""

    def __init__(self) -> None:
        super().__init__()
        self.selected: list[tuple[str, bool]] = []

    def compose(self) -> ComposeResult:
        yield ProjectList(id="project-list")

    def on_project_list_project_selected(self, message: ProjectList.ProjectSelected) -> None:
        self.selected.append((message.project_name, message.is_broken))


def _project(name: str, tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(name=name, security_class="online", root=tmp_path / name)


@pytest.mark.asyncio
@pytest.mark.parametrize("global_error", [False, True], ids=["project-config", "global-config"])
async def test_repaired_project_is_selected_after_the_rescan(
    tmp_path: Path, global_error: bool
) -> None:
    """Broken before, healthy after: the selection after the rebuild reaches the new row."""
    app = _ListHost()
    async with app.run_test() as pilot:
        project_list = app.query_one(ProjectList)
        broken = BrokenProject(
            name="beta",
            config_path=tmp_path / "beta.yml",
            error="bad",
            global_config_paths=(tmp_path / "config.yml",) if global_error else (),
        )
        await project_list.set_projects([_project("alpha", tmp_path)], [broken])
        project_list.select_project("beta")
        await pilot.pause()
        assert app.selected[-1] == ("beta", True)

        healthy = [_project("alpha", tmp_path), _project("beta", tmp_path)]
        await project_list.set_projects(healthy, [])
        project_list.select_project("beta")
        await pilot.pause()

        assert app.selected[-1] == ("beta", False)
        assert project_list.highlighted_child is not None
        assert project_list.highlighted_child.project_name == "beta"
