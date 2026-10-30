# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""A task's clearance prompts: finding its hub, muting it, and saying what that means.

Every container runs its own clearance hub inside its supervisor, and the hub
keys everything by the container's 12-character short id: its socket
directory, the container named on every event, the set of containers it does
not prompt about.  terok knows a task by project and task id, and podman knows
it by container name — keys the hub has never seen, so a mute sent under
either is a silent no-op.  The short id comes from the shield's own record
(``ShieldManager.container_short_id``, which the OCI hook writes at the
container's first start), so addressing a task's hub costs no
``podman inspect``.

A mute silences the question, never the shield: the refusal already happened
in the kernel, later ones are refused the same way, and the audit log records
each of them — while audit is on.  With audit off, a muted task leaves no
trace of what it was refused, and saying so is part of muting it.

Whether a task has a hub at all was settled when its container last started,
by the ``services.clearance`` its project had then — ``off`` starts none —
and the container's sidecar records that mode.  The config can have moved on
since: a change reaches a running task only at its next start.  So nothing
here goes by the config.  A running hub is asked, whatever the project says
now; only when none answers does the container's own record decide what that
means
([`container_prompt_state`][terok.lib.domain.clearance.container_prompt_state]).

The mute itself lives in the hub's memory.  Every start of the container
builds a new hub in the project's mode — muted under ``mute``, asking under
``prompt`` — so a runtime mute or unmute lasts until the task next starts.
That memory is also the only record of it, which is why reading it back
([`container_prompts_muted`][terok.lib.domain.clearance.container_prompts_muted])
asks the hub rather than any file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..core.config import get_services_clearance, make_sandbox_config
from ..core.paths import runtime_dir
from ..core.projects import load_project
from ..orchestration.task_runners import task_shield_manager
from ..orchestration.tasks import container_name, load_task_meta

if TYPE_CHECKING:
    from terok.lib.integrations.sandbox import ClearanceMode, ShieldManager

_HUB_TIMEOUT_S = 5.0
"""How long one hub call may take before the operator is told it did not answer.

A one-shot call to a wedged hub would otherwise hold the caller's worker
forever; five seconds is ample for a same-host unix socket."""

_STATE_QUERY_TIMEOUT_S = 2.0
"""How long reading a hub's mutes back may take before the answer is "unknown".

Shorter than a mute's own wait: the reader is a status display — the task
pane, whose shield line waits on the same worker — and a hub too busy to
answer is better shown as unknown at once than holding the posture back."""

MuteSeverity = Literal["information", "warning", "error"]
"""How loudly a [`MuteResult`][terok.lib.domain.clearance.MuteResult] should be shown."""

PromptState = Literal["asking", "muted", "off", "unknown"]
"""Whether a running container's refusals reach the operator.

``asking`` and ``muted`` are its hub's answer; ``off`` means it started with
``services.clearance: off`` and runs no hub; ``unknown`` means a hub should
be there and did not answer."""


@dataclass(frozen=True)
class MuteResult:
    """What one mute or unmute request did, worded once for every surface."""

    muted: bool | None
    """The state the hub now holds; ``None`` when no hub was asked, or none answered."""

    message: str
    """One line for the operator, saying what happened and what it means."""

    severity: MuteSeverity = "information"
    """``warning`` when nothing changed or the change has a consequence the
    operator must hear (audit off); ``error`` when the hub could not be reached."""


_HUB_SOCKET_NAME = "hub.sock"
"""The file name every container's hub binds, inside its short-id directory."""


def clearance_hub_socket(short_id: str) -> Path:
    """Where the clearance hub of the container with *short_id* listens.

    The supervisor binds it at ``<terok runtime>/clearance/<short id>/hub.sock``
    — the short id because ``sun_path`` holds 108 bytes, which a 64-character
    container id would overrun.
    """
    return runtime_dir() / "clearance" / short_id / _HUB_SOCKET_NAME


def clearance_off_notice(project_name: str | None = None) -> str | None:
    """Why the live clearance screen would have nothing to show, or ``None`` when it might.

    The screen follows every running container's hub, across projects.  One
    hub listening is reason enough to open it, whatever the config says now —
    a task keeps the mode it started with until it next starts.  With none
    listening, only a task started from now on could prompt, and none will
    when ``services.clearance`` is ``off`` globally and, for the selected
    project (*project_name*), there too: then the screen would wait for
    nothing, and the notice says so — and where the refusals are instead.
    A project that cannot be loaded does not count either way.
    """
    if _any_hub_listening() or get_services_clearance() != "off":
        return None
    scope = "globally"
    if project_name is not None:
        try:
            if load_project(project_name).clearance != "off":
                return None
        except (SystemExit, Exception):  # noqa: BLE001 - a probe: no project means no override
            pass
        else:
            scope = f"globally and for {project_name}"
    return (
        f"Clearance is off {scope} (services.clearance: off), and no running task has a "
        "clearance hub: no refusal is put to anyone, so there is nothing to watch live.  "
        "Refusals stay enforced, and a task with shield audit on lists them in its harvest."
    )


def _any_hub_listening() -> bool:
    """Whether any container's clearance hub has its socket bound right now."""
    try:
        return any((runtime_dir() / "clearance").glob(f"*/{_HUB_SOCKET_NAME}"))
    except OSError:
        return False


def muted_notice(subject: str, *, audit_enabled: bool | None) -> MuteResult:
    """The result of a mute the hub took for *subject*, worded for its audit state.

    A mute leaves the audit log as the only record of a refusal; when that log
    is off (*audit_enabled* ``False``), nothing records them either, and the
    notice says so loudly.  ``None`` — unknown — reads as the shield's default,
    on.
    """
    if audit_enabled is False:
        return MuteResult(
            True,
            f"Muted {subject} — refusals are still enforced, but shield audit is off "
            "for it, so they are now neither prompted nor recorded",
            "warning",
        )
    return MuteResult(True, f"Muted {subject} — refusals are still enforced and logged")


def task_audit_enabled(project_name: str, task_id: str) -> bool | None:
    """Whether the task's shield records its refusals: the audit flag it was created with.

    The container keeps the flag from its creation, whatever ``shield.audit``
    says now, so this asks the task's own record rather than the config.  It
    reads the task's audit log on the way (through ``ShieldManager.harvest``),
    so keep it off a UI thread.  ``None`` when it cannot be read.
    """
    try:
        project = load_project(project_name)
    except (SystemExit, Exception):  # noqa: BLE001 - a probe: a failure reads as unknown
        return None
    return _audit_enabled(task_shield_manager(project.tasks_root / str(task_id)))


def set_task_prompts_muted(project_name: str, task_id: str, *, muted: bool) -> MuteResult:
    """Mute or unmute one task's clearance prompts, and say what that did.

    Asks the task's own hub, addressed by the short id the shield recorded,
    over a one-shot connection bounded by a timeout — whatever the project's
    ``services.clearance`` says now: the hub the container started with is
    the one that prompts, until the task next starts.  A task that has never
    started has no recorded id, and so no hub to ask — a warning that says
    so.  When no hub answers, the container's own record says what that
    means: one that started under ``off`` runs none, so there is nothing to
    mute — a warning naming why; any other gets an error naming the failure.
    A mute that lands is checked against the task's audit flag, because a
    muted task with audit off is refused things nobody hears about.

    Blocking (it drives its own event loop): call it from a worker thread,
    never from a running loop.  Propagates the ``SystemExit`` a project that
    cannot be loaded raises; every hub failure comes back as an ``error``
    result instead.
    """
    verb = "mute" if muted else "unmute"
    project = load_project(project_name)
    subject = f"task {project.name}/{task_id}"
    manager = task_shield_manager(project.tasks_root / str(task_id))
    short_id = manager.container_short_id
    if short_id is None:
        return MuteResult(
            None,
            f"Task {project.name}/{task_id} has not started yet: it has no hub to {verb}",
            "warning",
        )
    try:
        now = _send_mute(clearance_hub_socket(short_id), short_id, muted)
    except Exception as exc:  # noqa: BLE001 - transport errors and varlink refusals alike
        if _task_started_clearance(project.name, task_id) == "off":
            return MuteResult(
                None,
                f"Task {project.name}/{task_id} started with clearance off "
                f"(services.clearance: off): it raises no prompts, so there is nothing to "
                f"{verb} — refusals stay enforced",
                "warning",
            )
        reason = str(exc) or type(exc).__name__
        return MuteResult(
            None,
            f"Could not {verb} {subject}: its clearance hub did not answer ({reason})",
            "error",
        )
    if now != muted:
        state = "muted" if now else "unmuted"
        return MuteResult(now, f"The clearance hub of {subject} still reports it {state}", "error")
    if now:
        return muted_notice(subject, audit_enabled=_audit_enabled(manager))
    return MuteResult(False, f"Prompts are back on for {subject}")


def container_prompts_muted(short_id: str) -> bool | None:
    """Whether the clearance hub of the container with *short_id* has muted its prompts.

    ``True`` when it is muted, ``False`` when it asks; ``None`` whenever the
    hub cannot say — none is listening (the container is stopped, or its
    project runs clearance ``off``), it predates the ``Mutes()`` query, or it
    did not answer within a short timeout.  Unknown is never reported as
    asking: a status line that showed a muted task as audible would hide
    exactly what the indicator exists to show.

    One ``Mutes()`` round trip over a one-shot connection, addressed by the
    short id the shield recorded — the hub's only key.  Blocking (it drives
    its own event loop): call it from a worker thread, never from a running
    loop.  [`container_prompt_state`][terok.lib.domain.clearance.container_prompt_state]
    adds what ``None`` means for a container that runs no hub.
    """
    from terok.lib.integrations.clearance import container_mutes

    async def _call() -> frozenset[str]:
        """Bound the query so a wedged hub reads as unknown, quickly."""
        return await asyncio.wait_for(
            container_mutes(clearance_hub_socket(short_id)), timeout=_STATE_QUERY_TIMEOUT_S
        )

    try:
        mutes = asyncio.run(_call())
    except Exception:  # noqa: BLE001 - a probe: a failure reads as unknown
        return None
    return short_id in mutes


def container_prompt_state(container: str, short_id: str | None) -> PromptState:
    """Whether the running container *container* puts its refusals to the operator.

    Read from the container, never from the config, which reaches a running
    container only at its next start.  Its hub answers when one does —
    ``muted`` or ``asking``, by the short id the shield recorded (*short_id*;
    ``None`` when none is recorded).  When none answers, the mode the
    container started under decides: ``off`` started no hub, so ``off``;
    any other mode means a hub should be there and is not saying — the
    supervisor's clearance child died, or the hub predates the query — so
    ``unknown``, never ``asking``.

    *container* is the podman name, the key its sidecar is stored under.
    Blocking (the hub query drives its own event loop): call it from a
    worker thread.
    """
    if short_id is not None:
        muted = container_prompts_muted(short_id)
        if muted is not None:
            return "muted" if muted else "asking"
    if started_clearance_mode(container) == "off":
        return "off"
    return "unknown"


def started_clearance_mode(container: str) -> ClearanceMode | None:
    """The ``services.clearance`` mode the container *container* last started under.

    Its supervisor reads the mode from the container's sidecar at every
    start, and only a restart rewrites it — right before that start — so for
    a running container the sidecar holds the mode it runs with, whatever the
    config says now.  Read with sandbox's own rules: a sidecar from before the
    setting, or with a value it does not know, reads as ``prompt``.  ``None``
    when there is no sidecar (the container was never launched through
    terok's sandbox) or it cannot be read.  *container* is the podman name.
    """
    from terok.lib.integrations.sandbox import load_sidecar

    try:
        path = make_sandbox_config().sidecar_path(container)
        if not path.is_file():
            return None
        sidecar = load_sidecar(path)
    except (OSError, ValueError):
        return None
    return sidecar.clearance if sidecar is not None else None


def _task_started_clearance(project_name: str, task_id: str) -> ClearanceMode | None:
    """The clearance mode task *task_id*'s container last started under; ``None`` when unknown."""
    try:
        meta, _ = load_task_meta(project_name, task_id)
        mode = meta.get("mode")
        if not mode:
            return None
        return started_clearance_mode(container_name(project_name, mode, task_id))
    except (SystemExit, Exception):  # noqa: BLE001 - a probe: a failure reads as unknown
        return None


def _send_mute(socket: Path, short_id: str, muted: bool) -> bool:
    """One ``SetMute`` round trip to the hub at *socket*; the state it now holds.

    Raises whatever the one-shot raises — an unreachable socket, a refusal,
    or [`TimeoutError`][TimeoutError] when the hub sits on the call.
    """
    from terok.lib.integrations.clearance import set_container_mute

    async def _call() -> bool:
        """Bound the exchange so a wedged hub cannot hold the caller."""
        return await asyncio.wait_for(
            set_container_mute(socket, short_id, muted), timeout=_HUB_TIMEOUT_S
        )

    return asyncio.run(_call())


def _audit_enabled(manager: ShieldManager) -> bool | None:
    """The audit flag *manager*'s container was created with; ``None`` when unreadable."""
    try:
        return manager.harvest().audit_enabled
    except Exception:  # noqa: BLE001 - a probe: a failure reads as unknown
        return None
