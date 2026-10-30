# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Clearance notifier + multi-socket subscriber + CLI registry — public API surface.

Re-export catalog for the operator-prompt layer.  Sources:
[`terok.lib.integrations.clearance`][terok.lib.integrations.clearance] for
the clearance wheel's surface, and
[`terok.lib.domain.clearance`][terok.lib.domain.clearance] for the
task-level operations built on it — addressing a task's hub by the short
container id the shield recorded, muting or unmuting its prompts with an
answer worded for the operator, reading back whether they are muted (or
were never asked, under the mode the container started with), and saying
when the live screen would have nothing to show.
``ClearanceMode`` — the ``services.clearance`` vocabulary (``prompt`` /
``mute`` / ``off``) — comes from sandbox, which owns the key.

The clearance ``COMMANDS`` registry is aliased to ``CLEARANCE_COMMANDS``
to match the call site in [`terok.cli`][terok.cli] (which wires several
sibling-wheel command trees into the top-level terok CLI under
distinct names).

Every container hosts its own hub + verdict server in the supervisor
process; there is no host-side ``HubService`` / ``NotifierService``
daemon.  Operator UIs multiplex across the per-container sockets via
[`MultiSocketSubscriber`][terok_clearance.MultiSocketSubscriber].
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terok.lib.domain.clearance import (
        MuteResult as MuteResult,
        MuteSeverity as MuteSeverity,
        PromptState as PromptState,
        clearance_hub_socket as clearance_hub_socket,
        clearance_off_notice as clearance_off_notice,
        container_prompt_state as container_prompt_state,
        container_prompts_muted as container_prompts_muted,
        muted_notice as muted_notice,
        set_task_prompts_muted as set_task_prompts_muted,
        started_clearance_mode as started_clearance_mode,
        task_audit_enabled as task_audit_enabled,
    )
    from terok.lib.integrations.clearance import (
        ALL_NOTIFY_CATEGORIES as ALL_NOTIFY_CATEGORIES,
        COMMANDS as CLEARANCE_COMMANDS,
        NOTIFY_BLOCKED as NOTIFY_BLOCKED,
        NOTIFY_VERDICT as NOTIFY_VERDICT,
        CallbackNotifier as CallbackNotifier,
        EventSubscriber as EventSubscriber,
        MultiSocketSubscriber as MultiSocketSubscriber,
        Notification as Notification,
        container_mutes as container_mutes,
        create_notifier as create_notifier,
        set_container_mute as set_container_mute,
    )
    from terok.lib.integrations.sandbox import ClearanceMode as ClearanceMode

#: Public name -> defining module (PEP 562 lazy resolution).
_LAZY: dict[str, str] = {
    "ALL_NOTIFY_CATEGORIES": "terok.lib.integrations.clearance",
    "CLEARANCE_COMMANDS": "terok.lib.integrations.clearance:COMMANDS",
    "CallbackNotifier": "terok.lib.integrations.clearance",
    "ClearanceMode": "terok.lib.integrations.sandbox",
    "EventSubscriber": "terok.lib.integrations.clearance",
    "MultiSocketSubscriber": "terok.lib.integrations.clearance",
    "MuteResult": "terok.lib.domain.clearance",
    "MuteSeverity": "terok.lib.domain.clearance",
    "NOTIFY_BLOCKED": "terok.lib.integrations.clearance",
    "NOTIFY_VERDICT": "terok.lib.integrations.clearance",
    "Notification": "terok.lib.integrations.clearance",
    "PromptState": "terok.lib.domain.clearance",
    "clearance_hub_socket": "terok.lib.domain.clearance",
    "clearance_off_notice": "terok.lib.domain.clearance",
    "container_mutes": "terok.lib.integrations.clearance",
    "container_prompt_state": "terok.lib.domain.clearance",
    "container_prompts_muted": "terok.lib.domain.clearance",
    "create_notifier": "terok.lib.integrations.clearance",
    "muted_notice": "terok.lib.domain.clearance",
    "set_container_mute": "terok.lib.integrations.clearance",
    "set_task_prompts_muted": "terok.lib.domain.clearance",
    "started_clearance_mode": "terok.lib.domain.clearance",
    "task_audit_enabled": "terok.lib.domain.clearance",
}

__all__ = [
    "CLEARANCE_COMMANDS",
    "CallbackNotifier",
    "ClearanceMode",
    "MultiSocketSubscriber",
    "MuteResult",
    "MuteSeverity",
    "Notification",
    "PromptState",
    "clearance_hub_socket",
    "clearance_off_notice",
    "container_mutes",
    "container_prompt_state",
    "container_prompts_muted",
    "muted_notice",
    "set_container_mute",
    "set_task_prompts_muted",
    "started_clearance_mode",
    "task_audit_enabled",
]


def __getattr__(name: str) -> object:
    """Resolve a re-exported name to its source module on first access (PEP 562)."""
    try:
        target = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module_path, _, source_name = target.partition(":")
    value = getattr(importlib.import_module(module_path), source_name or name)
    globals()[name] = value  # cache so subsequent lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    """Expose the lazy names to ``dir()`` / autocompletion."""
    return sorted({*globals(), *_LAZY})
