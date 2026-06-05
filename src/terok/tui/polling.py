#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Polling mixin for the TerokTUI app.

Extracts upstream polling, container status polling, and auto-sync logic
from the main app module into a reusable mixin class.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App
    from textual.timer import Timer

    from terok.lib.api.gate import GateStalenessInfo

    from ..lib.api import TaskMeta
    from .task_watcher import TaskWatcher

    # At type-check time only, inherit from textual.App so all of its methods
    # (run_worker, set_interval, notify, …) resolve naturally on `self` with
    # the *real* signatures — no risk of MRO conflicts on TerokTUI. At
    # runtime the mixin still inherits from `object`.
    _MixinBase = App
else:
    _MixinBase = object

# Container-state cadence.  With an inotify watch on the task-metadata directory
# every membership / lifecycle change (create, delete, ``ready_at``,
# ``exit_code``) reconciles the instant the kernel reports it, so the periodic
# timer drops to a slow safety net that only catches what the watch can't see —
# a container dying with no host-side write, or a missed inotify event.  Without
# a watch (inotify unavailable) the timer is the only signal and stays hot.
_HOT_POLL_INTERVAL_S = 2
_BACKSTOP_POLL_INTERVAL_S = 15
# Coalesce a burst of writes (a metadata file rewritten field-by-field) into one
# reconcile rather than firing per event.
_WATCH_DEBOUNCE_S = 0.2


class PollingMixin(_MixinBase):
    """Mixin providing upstream and container status polling for the TUI app."""

    if TYPE_CHECKING:
        # State the host (TerokTUI) initialises — the mixin owns the polling
        # lifecycle but stores its bookkeeping on the host instance.
        current_task: "TaskMeta | None"
        _staleness_info: "GateStalenessInfo | None"
        _polling_timer: "Timer | None"
        _polling_project_id: str | None
        _last_notified_stale: bool
        _auto_sync_cooldown: dict[str, float]
        _container_status_timer: "Timer | None"
        _task_watcher: "TaskWatcher | None"
        _watch_debounce: "Timer | None"

        # TerokTUI helpers (not on textual.App).
        current_project_id: str | None

        def _log_debug(self, message: str) -> None: ...
        def _refresh_project_state(self) -> None: ...

    # ---------- Upstream polling ----------

    def _start_upstream_polling(self) -> None:
        """Start background polling for upstream changes.

        Only polls for gatekeeping projects with polling enabled and a gate initialized.
        """
        from ..lib.api import load_project

        self._stop_upstream_polling()  # Stop any existing timer
        self._staleness_info = None
        self._last_notified_stale = False

        if not self.current_project_id:
            return

        try:
            project = load_project(self.current_project_id)
        except SystemExit:
            return

        # Only poll for gatekeeping projects with polling enabled
        if project.security_class != "gatekeeping":
            return
        if not project.upstream_polling_enabled:
            return
        if not project.gate_path.exists():
            return

        interval_seconds = project.upstream_polling_interval_minutes * 60
        self._polling_project_id = self.current_project_id

        # Perform initial poll immediately (in background worker)
        self._poll_upstream()

        # Schedule recurring polls
        self._polling_timer = self.set_interval(
            interval_seconds, self._poll_upstream, name="upstream_polling"
        )

    def _stop_upstream_polling(self) -> None:
        """Stop the upstream polling timer."""
        if self._polling_timer is not None:
            self._polling_timer.stop()
            self._polling_timer = None
        self._polling_project_id = None

    def _start_container_status_polling(self) -> None:
        """Track container status: an inotify watch plus a safety-net timer.

        Seeds once, then arms an inotify watch on the project's task-metadata
        directory so disk-backed changes reconcile immediately.  The recurring
        timer drops to a slow backstop when the watch is live, and stays at the
        hot cadence when inotify is unavailable.
        """
        self._stop_container_status_polling()
        if not self.current_project_id:
            return
        # Seed the initial state before the first event/tick.
        self._poll_container_status()
        watching = self._start_task_watcher(self.current_project_id)
        interval_seconds = _BACKSTOP_POLL_INTERVAL_S if watching else _HOT_POLL_INTERVAL_S
        self._container_status_timer = self.set_interval(
            interval_seconds, self._poll_container_status, name="container_status_polling"
        )

    def _stop_container_status_polling(self) -> None:
        """Stop the container status timer and tear down the inotify watch."""
        if self._container_status_timer is not None:
            self._container_status_timer.stop()
            self._container_status_timer = None
        self._stop_task_watcher()

    def _start_task_watcher(self, project_id: str) -> bool:
        """Arm an inotify watch on *project_id*'s task-metadata directory.

        Returns ``True`` once the watch fd is registered on the event loop and
        a change there will drive a debounced reconcile; ``False`` (caller
        keeps the hot poll) if inotify is unavailable, the directory can't be
        watched yet, or there's no running loop to attach the fd to.
        """
        import asyncio

        from ..lib.api import tasks_meta_dir
        from .task_watcher import TaskWatcher

        try:
            watcher = TaskWatcher(tasks_meta_dir(project_id))
        except Exception as e:  # noqa: BLE001 — watch is best-effort; fall back to polling
            self._log_debug(f"task watcher init error: {e}")
            return False
        if not watcher.start():
            return False
        try:
            asyncio.get_running_loop().add_reader(watcher.fileno, self._on_task_dir_changed)
        except (RuntimeError, ValueError, OSError) as e:
            self._log_debug(f"task watcher attach error: {e}")
            watcher.stop()
            return False
        self._task_watcher = watcher
        return True

    def _stop_task_watcher(self) -> None:
        """Detach and close the inotify watch and any pending debounce."""
        if self._watch_debounce is not None:
            self._watch_debounce.stop()
            self._watch_debounce = None
        if self._task_watcher is None:
            return
        import asyncio

        try:
            asyncio.get_running_loop().remove_reader(self._task_watcher.fileno)
        except (RuntimeError, ValueError, OSError):
            pass
        self._task_watcher.stop()
        self._task_watcher = None

    def _on_task_dir_changed(self) -> None:
        """React to inotify activity: drain events, then debounce a reconcile."""
        if self._task_watcher is None or not self._task_watcher.drain():
            return
        # Restart the debounce window so a burst collapses into one reconcile.
        if self._watch_debounce is not None:
            self._watch_debounce.stop()
        self._watch_debounce = self.set_timer(_WATCH_DEBOUNCE_S, self._poll_container_status)

    def _poll_container_status(self) -> None:
        """Check container status for all visible tasks via a single batch query."""
        if not self.current_project_id:
            return
        self._queue_container_state_check(self.current_project_id)

    def _queue_container_state_check(self, project_id: str) -> None:
        """Queue a background batch check for all task container states."""
        self.run_worker(
            self._load_container_state_worker(project_id),
            name=f"container-state:{project_id}",
            group="container-state",
            exclusive=True,
        )

    async def _load_container_state_worker(self, project_id: str) -> tuple[str, list["TaskMeta"]]:
        """Batch-snapshot every task for a project with live container state.

        Returns fresh ``TaskMeta`` instances — the task set on disk plus each
        one's live container state — so the handler can both detect tasks
        created or deleted outside the TUI *and* refresh the lifecycle fields
        (init marker, work status, exit code) that drift on rows already shown.
        """
        import asyncio

        from ..lib.api import get_all_task_states, get_tasks

        def _snapshot() -> list["TaskMeta"]:
            tasks = get_tasks(project_id)
            states = get_all_task_states(project_id, tasks)
            for task in tasks:
                task.container_state = states.get(task.task_id)
            return tasks

        try:
            tasks = await asyncio.get_event_loop().run_in_executor(None, _snapshot)
            return (project_id, tasks)
        except (Exception, SystemExit) as e:  # noqa: BLE001 — background worker; must not crash TUI
            self._log_debug(f"container state batch check error: {e}")
            return (project_id, [])

    def _poll_upstream(self) -> None:
        """Check upstream for changes and update staleness info.

        Runs the actual comparison in a background worker to avoid blocking the UI.
        """
        project_id = self._polling_project_id
        if not project_id or project_id != self.current_project_id:
            # Project changed since timer was started, skip this poll
            return

        self._log_debug(f"polling upstream for {project_id}")
        # Run blocking git operation in background worker
        self.run_worker(
            self._poll_upstream_worker(project_id),
            name="poll_upstream",
            exclusive=True,  # Cancel any previous poll still running
        )

    async def _poll_upstream_worker(self, project_id: str) -> None:
        """Background worker to check upstream (runs in thread pool)."""
        import asyncio

        from ..lib.api import load_project, make_git_gate

        try:
            # Run blocking call in thread pool
            staleness = await asyncio.get_event_loop().run_in_executor(
                None, lambda: make_git_gate(load_project(project_id)).compare_vs_upstream()
            )

            # Validate project hasn't changed while we were polling
            if project_id != self.current_project_id:
                return

            self._on_staleness_updated(project_id, staleness)

        except (Exception, SystemExit) as e:  # noqa: BLE001 — background worker; must not crash TUI
            self._log_debug(f"upstream poll error: {e}")

    def _on_staleness_updated(self, project_id: str, staleness: "GateStalenessInfo") -> None:
        """Handle updated staleness info."""
        # Double-check project hasn't changed
        if project_id != self.current_project_id:
            return

        self._staleness_info = staleness

        # Only update notification state for valid (non-error) comparisons
        if staleness.error:
            # Don't change notification state on errors - preserve previous state
            pass
        elif staleness.is_stale and not self._last_notified_stale:
            behind_str = ""
            if staleness.commits_behind is not None:
                behind_str = f" ({staleness.commits_behind} commits behind)"
            self.notify(f"Gate is behind upstream on {staleness.branch}{behind_str}")
            self._last_notified_stale = True

            # Trigger auto-sync if enabled (with cooldown check)
            self._maybe_auto_sync(project_id)
        elif not staleness.is_stale:
            # Only reset when we have confirmed up-to-date status
            self._last_notified_stale = False

        # Refresh the project state display
        self._refresh_project_state()

    def _maybe_auto_sync(self, project_id: str) -> None:
        """Trigger auto-sync if enabled for this project.

        Runs sync in background worker to avoid blocking UI.
        Implements cooldown to prevent sync loops.
        """
        import time

        from ..lib.api import load_project

        if not project_id or project_id != self.current_project_id:
            return

        # Check cooldown (5 minute minimum between auto-syncs per project)
        now = time.time()
        cooldown_until = self._auto_sync_cooldown.get(project_id, 0)
        if now < cooldown_until:
            self._log_debug("auto-sync skipped: cooldown active")
            return

        try:
            project = load_project(project_id)
            if not project.auto_sync_enabled:
                return

            # Set cooldown before starting sync (5 minutes)
            self._auto_sync_cooldown[project_id] = now + 300

            self._log_debug(f"auto-syncing gate for {project_id}")
            self.notify("Auto-syncing gate from upstream...")

            # Run sync in background worker
            branches = project.auto_sync_branches or None
            self.run_worker(
                self._sync_worker(project_id, branches, is_auto=True),
                name="auto_sync",
                exclusive=True,
            )

        except (Exception, SystemExit) as e:  # noqa: BLE001 — background worker; must not crash TUI
            self._log_debug(f"auto-sync error: {e}")

    async def _sync_worker(
        self, project_id: str, branches: list[str] | None = None, is_auto: bool = False
    ) -> None:
        """Background worker to sync gate from upstream."""
        import asyncio

        from ..lib.api import load_project, make_git_gate

        try:
            # Run blocking sync in thread pool
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: make_git_gate(load_project(project_id)).sync_branches(branches)
            )

            # Validate project hasn't changed
            if project_id != self.current_project_id:
                return

            if result["success"]:
                label = "Auto-synced" if is_auto else "Synced"
                self.notify(f"{label} gate from upstream")

                # Re-check staleness after sync
                staleness = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: make_git_gate(load_project(project_id)).compare_vs_upstream()
                )

                if project_id == self.current_project_id:
                    self._staleness_info = staleness
                    # Only reset notification flag if we're actually up-to-date now
                    if not staleness.is_stale and not staleness.error:
                        self._last_notified_stale = False
                    self._refresh_project_state()
            else:
                label = "Auto-sync" if is_auto else "Sync"
                self.notify(f"{label} failed: {', '.join(result['errors'])}")

        except (Exception, SystemExit) as e:  # noqa: BLE001 — background worker; must not crash TUI
            label = "Auto-sync" if is_auto else "Sync"
            self.notify(f"{label} error: {e}")
