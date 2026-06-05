# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""The inotify task watcher detects changes in a metadata directory.

These run against real inotify (terok targets Linux), so they assert the actual
kernel behaviour the TUI relies on: a create, delete, rename or completed write
in the watched directory becomes a readable event, and a quiet directory yields
nothing.  Event delivery for a local filesystem is synchronous to the inotify
queue, so a ``drain`` immediately after the filesystem op already sees it.
"""

from __future__ import annotations

from pathlib import Path

from terok.tui.task_watcher import TaskWatcher


def _started(path: Path) -> TaskWatcher:
    """Return a watcher armed on *path* (skips if inotify is unavailable)."""
    watcher = TaskWatcher(path)
    assert watcher.start(), "inotify watch failed to arm"
    return watcher


class TestLifecycle:
    """Arming, fd exposure, and teardown."""

    def test_start_exposes_a_real_fd(self, tmp_path: Path) -> None:
        watcher = _started(tmp_path)
        try:
            assert watcher.fileno >= 0
        finally:
            watcher.stop()

    def test_start_on_missing_dir_returns_false(self, tmp_path: Path) -> None:
        watcher = TaskWatcher(tmp_path / "does-not-exist")
        assert watcher.start() is False
        assert watcher.fileno == -1

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        watcher = _started(tmp_path)
        watcher.stop()
        watcher.stop()  # second call must not raise
        assert watcher.fileno == -1


class TestDetectsChanges:
    """Every membership / lifecycle move surfaces as a drained event."""

    def test_file_create_is_seen(self, tmp_path: Path) -> None:
        watcher = _started(tmp_path)
        try:
            (tmp_path / "1.json").write_text("{}", encoding="utf-8")
            assert watcher.drain() is True
        finally:
            watcher.stop()

    def test_in_place_write_is_seen(self, tmp_path: Path) -> None:
        """A completed write (the ``ready_at`` update) fires CLOSE_WRITE."""
        meta = tmp_path / "1.json"
        meta.write_text("{}", encoding="utf-8")
        watcher = _started(tmp_path)
        try:
            assert watcher.drain() is False  # the pre-watch write isn't ours to see
            meta.write_text('{"ready_at": "now"}', encoding="utf-8")
            assert watcher.drain() is True
        finally:
            watcher.stop()

    def test_atomic_rename_is_seen(self, tmp_path: Path) -> None:
        """Temp-file-then-rename writes surface as MOVED_TO."""
        watcher = _started(tmp_path)
        try:
            tmp = tmp_path / ".1.json.tmp"
            tmp.write_text('{"ready_at": "now"}', encoding="utf-8")
            watcher.drain()  # clear the temp-file create/close events
            tmp.replace(tmp_path / "1.json")
            assert watcher.drain() is True
        finally:
            watcher.stop()

    def test_delete_is_seen(self, tmp_path: Path) -> None:
        meta = tmp_path / "1.json"
        meta.write_text("{}", encoding="utf-8")
        watcher = _started(tmp_path)
        try:
            watcher.drain()
            meta.unlink()
            assert watcher.drain() is True
        finally:
            watcher.stop()

    def test_quiet_directory_drains_empty(self, tmp_path: Path) -> None:
        watcher = _started(tmp_path)
        try:
            assert watcher.drain() is False
        finally:
            watcher.stop()
