# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""inotify-backed watcher for a task-metadata directory.

The TUI's task list is driven by host-side files: a task appears as a new
metadata file, its ``ready_at`` init marker and ``exit_code`` land as in-place
writes, and a delete unlinks the file.  Re-reading that directory on a 2-second
timer wakes the disk forever even when nothing changes; an inotify watch instead
stays idle until the kernel reports an actual change.

This module owns only the OS mechanism — open an inotify instance, watch one
directory, expose the readable fd, drain pending events, close.  Loop
registration, debouncing and the reconcile reaction live in
[`PollingMixin`][terok.tui.polling.PollingMixin], which keeps this class free of
asyncio/Textual coupling and testable against a real directory.

inotify is Linux-only.  That is the only platform terok's Podman runtime
targets, and the watched files are always on a local filesystem (never NFS), so
the watch is reliable here.  Construction failures degrade to ``False`` from
[`start`][terok.tui.task_watcher.TaskWatcher.start] so the caller can fall back
to polling rather than crash.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path

# inotify_init1 flag — hand back a non-blocking fd so a fully drained read
# raises EAGAIN (BlockingIOError) instead of stalling the event loop.
_IN_NONBLOCK = 0o4000

# Watch mask.  Membership moves (create / delete / rename in or out) plus
# CLOSE_WRITE, which fires when an in-place write to a metadata file finishes —
# that is how the ``ready_at`` and ``exit_code`` updates surface.  Atomic
# temp-file-then-rename writes surface as MOVED_TO.  We never inspect *which*
# file moved: any event on this directory means "re-read the task set".
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CLOSE_WRITE = 0x00000008
_WATCH_MASK = _IN_CREATE | _IN_DELETE | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CLOSE_WRITE

# One read drains every event queued since the last wake-up; the buffer only
# needs to clear the kernel queue, not hold a single event.
_READ_BUFFER_BYTES = 4096


def _load_libc() -> ctypes.CDLL:
    """Bind the three inotify syscalls from libc with errno tracking."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.inotify_init1.argtypes = [ctypes.c_int]
    libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
    return libc


class TaskWatcher:
    """Watch a single directory for changes via inotify.

    Args:
        path: Directory to watch — the project's task-metadata directory.
    """

    def __init__(self, path: Path) -> None:
        """Record the directory to watch; no syscalls until ``start``."""
        self._path = path
        self._libc = _load_libc()
        self._fd = -1
        self._wd = -1

    @property
    def fileno(self) -> int:
        """The inotify fd, ready for ``loop.add_reader``.  ``-1`` until started."""
        return self._fd

    def start(self) -> bool:
        """Open the inotify instance and arm the watch.

        Returns ``True`` once the directory is watched, ``False`` if inotify is
        unavailable or the directory can't be watched (e.g. it doesn't exist
        yet) — the caller falls back to polling on ``False``.
        """
        fd = self._libc.inotify_init1(_IN_NONBLOCK)
        if fd < 0:
            return False
        wd = self._libc.inotify_add_watch(fd, str(self._path).encode(), _WATCH_MASK)
        if wd < 0:
            os.close(fd)
            return False
        self._fd, self._wd = fd, wd
        return True

    def drain(self) -> bool:
        """Read and discard every queued event; report whether any were seen.

        The watch mask already restricts events to relevant ones, so the
        payload never needs parsing — clearing the queue and signalling "the
        directory changed" is enough to trigger a single reconcile.
        """
        seen = False
        while True:
            try:
                data = os.read(self._fd, _READ_BUFFER_BYTES)
            except BlockingIOError:
                break
            if not data:
                break
            seen = True
        return seen

    def stop(self) -> None:
        """Disarm the watch and close the fd.  Idempotent."""
        if self._fd < 0:
            return
        if self._wd >= 0:
            self._libc.inotify_rm_watch(self._fd, self._wd)
        os.close(self._fd)
        self._fd, self._wd = -1, -1


__all__ = ["TaskWatcher"]
