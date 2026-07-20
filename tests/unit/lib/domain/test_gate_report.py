# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the gate sync report formatters (CLI/TUI summary lines)."""

from __future__ import annotations

from terok.lib.domain.project import describe_pending_op, summarize_gate_sync


def _result(**overrides: object) -> dict:
    """A complete, quiet GateSyncResult, selectively overridden."""
    result: dict = {
        "path": "/tmp/terok-testing/gate/p.git",
        "upstream_url": "https://example.com/r.git",
        "created": False,
        "migrated": False,
        "success": True,
        "errors": [],
        "notes": [],
        "applied": [],
        "pending": [],
        "gate_only_branches": [],
        "cache_refreshed": False,
        "cache_error": None,
    }
    result.update(overrides)
    return result


def _op(**overrides: object) -> dict:
    op: dict = {
        "branch": "feat/x",
        "kind": "delete",
        "reason": "upstream_delete",
        "gate_sha": "a" * 40,
        "upstream_sha": None,
        "old_snapshot_sha": "a" * 40,
        "lossless": True,
        "gate_only_commits": 0,
    }
    op.update(overrides)
    return op


class TestDescribePendingOp:
    """Each pending op renders as one decision-ready line."""

    def test_lossless_delete(self) -> None:
        line = describe_pending_op(_op())
        assert line == "delete feat/x (deleted upstream; no gate-local commits)"

    def test_lossy_force_counts_commits(self) -> None:
        line = describe_pending_op(
            _op(
                kind="force_update",
                reason="upstream_rewrite",
                upstream_sha="b" * 40,
                lossless=False,
                gate_only_commits=3,
            )
        )
        assert "force-update feat/x" in line
        assert "upstream rewrote history" in line
        assert "would discard 3 gate-local commit(s)" in line

    def test_unknown_provenance_is_honest_about_uncertainty(self) -> None:
        line = describe_pending_op(
            _op(
                reason="unknown_provenance",
                old_snapshot_sha=None,
                lossless=False,
                gate_only_commits=None,
            )
        )
        assert "pre-dates sync tracking" in line
        assert "cannot be ruled out" in line


class TestSummarizeGateSync:
    """The abridged summary shows what happened without scrolling forever."""

    def test_quiet_sync_is_one_line(self) -> None:
        assert summarize_gate_sync(_result()) == ["gate is up to date with upstream"]

    def test_applied_grouped_by_kind_with_shas(self) -> None:
        lines = summarize_gate_sync(
            _result(
                applied=[
                    {"branch": "new", "kind": "create", "old_sha": None, "new_sha": "c" * 40},
                    {
                        "branch": "master",
                        "kind": "fast_forward",
                        "old_sha": "a" * 40,
                        "new_sha": "b" * 40,
                    },
                ]
            )
        )
        assert "created:" in lines and "fast-forwarded:" in lines
        assert f"  new -> {'c' * 12}" in lines
        assert f"  master -> {'b' * 12}" in lines

    def test_gate_only_and_pending_and_notes(self) -> None:
        lines = summarize_gate_sync(
            _result(
                gate_only_branches=["feat/wip"],
                pending=[_op()],
                notes=["upstream moved existing tag(s) not updated: v1"],
            )
        )
        text = "\n".join(lines)
        assert "kept (gate-only, not on upstream): 1" in text
        assert "pending destructive change(s)" in text
        assert "delete feat/x" in text
        assert "note: upstream moved existing tag(s)" in text

    def test_long_categories_are_capped(self) -> None:
        many = [f"branch-{i}" for i in range(25)]
        lines = summarize_gate_sync(_result(gate_only_branches=many))
        assert "  … and 15 more" in lines
        assert sum(1 for line in lines if line.startswith("  branch-")) == 10

    def test_migration_is_announced(self) -> None:
        lines = summarize_gate_sync(_result(migrated=True))
        assert lines[0].startswith("Gate migrated")
