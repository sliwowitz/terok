# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the TUI review-lag polling handlers.

The compute path lives in [`terok.lib.domain.review_lag`][terok.lib.domain.review_lag]
(covered by ``test_review_lag``); here we exercise the TUI wiring — the
push-marker trigger's project guard and the state/notify handling — by
calling the unbound ``PollingMixin`` methods on a mock ``self``.
"""

from __future__ import annotations

from types import ModuleType
from unittest import mock

from tests.unit.tui.tui_test_helpers import build_textual_stubs, import_fresh


def _app() -> ModuleType:
    """Fresh-import the app module against Textual stubs."""
    _, _, app_mod = import_fresh(build_textual_stubs())
    return app_mod


class TestPollReviewLagGuard:
    """The marker-triggered recheck only runs for the current project."""

    def test_fires_for_the_current_project(self) -> None:
        """A marker fire for the selected project dispatches a recheck worker."""
        app = _app()
        me = mock.Mock(_polling_project_name="proj1", current_project_name="proj1")
        app.TerokTUI._poll_review_lag(me)
        me.run_worker.assert_called_once()
        assert me.run_worker.call_args.kwargs["group"] == "review_lag"

    def test_skips_when_project_changed(self) -> None:
        """A marker fire for a different project is ignored."""
        app = _app()
        me = mock.Mock(_polling_project_name="proj1", current_project_name="proj2")
        app.TerokTUI._poll_review_lag(me)
        me.run_worker.assert_not_called()

    def test_skips_when_polling_idle(self) -> None:
        """No dispatch when upstream polling is not running."""
        app = _app()
        me = mock.Mock(_polling_project_name=None, current_project_name="proj1")
        app.TerokTUI._poll_review_lag(me)
        me.run_worker.assert_not_called()


class TestOnReviewLagUpdated:
    """Level-triggered: new warnings toast; clearing is silent; stale is ignored."""

    def test_new_warnings_toast_and_refresh(self) -> None:
        """First-seen warnings toast at warning severity and refresh the panel."""
        app = _app()
        me = mock.Mock(current_project_name="proj1", _review_lag_lines=None)
        app.TerokTUI._on_review_lag_updated(me, "proj1", ["!42 feat/x +3"])
        assert me._review_lag_lines == ["!42 feat/x +3"]
        me.notify.assert_called_once()
        assert me.notify.call_args.kwargs.get("severity") == "warning"
        me._refresh_project_state.assert_called_once()

    def test_unchanged_warnings_do_not_re_toast(self) -> None:
        """Identical warnings neither re-toast nor re-refresh."""
        app = _app()
        me = mock.Mock(current_project_name="proj1", _review_lag_lines=["!42 feat/x +3"])
        app.TerokTUI._on_review_lag_updated(me, "proj1", ["!42 feat/x +3"])
        me.notify.assert_not_called()
        me._refresh_project_state.assert_not_called()

    def test_clearing_is_silent_but_refreshes(self) -> None:
        """Clearing the warnings is silent but still refreshes the panel."""
        app = _app()
        me = mock.Mock(current_project_name="proj1", _review_lag_lines=["!42 feat/x +3"])
        app.TerokTUI._on_review_lag_updated(me, "proj1", [])
        assert me._review_lag_lines == []
        me.notify.assert_not_called()
        me._refresh_project_state.assert_called_once()

    def test_stale_project_update_ignored(self) -> None:
        """An update for a no-longer-current project is dropped."""
        app = _app()
        me = mock.Mock(current_project_name="proj2", _review_lag_lines=None)
        app.TerokTUI._on_review_lag_updated(me, "proj1", ["!42 feat/x +3"])
        me.notify.assert_not_called()
        me._refresh_project_state.assert_not_called()
