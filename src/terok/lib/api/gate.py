# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Gate operations and types — public API surface.

Re-export catalog for the per-container git gate.  Sources:
[`terok.lib.integrations.sandbox`][terok.lib.integrations.sandbox] for
the mirror staleness / auth types (terok-sandbox owns the gate
infrastructure — the gate runs inside each container's supervisor), and
[`terok.lib.domain.project`][terok.lib.domain.project] for
``make_git_gate`` (terok's per-project gate factory).

Deliberately absent: raw token minting.  The task meta's ``gate_token``
is the single source of truth for a task's gate token; the only mint
point is the task-scoped accessor inside
[`terok.lib.orchestration.environment`][terok.lib.orchestration.environment],
so no caller can create a token value that bypasses the store.

Every name resolves lazily (PEP 562) so importing this barrel doesn't
pull the sandbox integration or the domain layer until a symbol is used.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terok.lib.domain.project import (
        describe_pending_op as describe_pending_op,
        make_git_gate as make_git_gate,
        summarize_gate_sync as summarize_gate_sync,
    )
    from terok.lib.integrations.sandbox import (
        AppliedOp as AppliedOp,
        ApplyPendingResult as ApplyPendingResult,
        BackupRef as BackupRef,
        GateAuthNotConfigured as GateAuthNotConfigured,
        GateStalenessInfo as GateStalenessInfo,
        GateSyncResult as GateSyncResult,
        GitGate as GitGate,
        PendingOp as PendingOp,
    )

#: Public name -> defining module (PEP 562 lazy resolution).
_LAZY: dict[str, str] = {
    "AppliedOp": "terok.lib.integrations.sandbox",
    "ApplyPendingResult": "terok.lib.integrations.sandbox",
    "BackupRef": "terok.lib.integrations.sandbox",
    "GateAuthNotConfigured": "terok.lib.integrations.sandbox",
    "GateStalenessInfo": "terok.lib.integrations.sandbox",
    "GateSyncResult": "terok.lib.integrations.sandbox",
    "GitGate": "terok.lib.integrations.sandbox",
    "PendingOp": "terok.lib.integrations.sandbox",
    "describe_pending_op": "terok.lib.domain.project",
    "make_git_gate": "terok.lib.domain.project",
    "summarize_gate_sync": "terok.lib.domain.project",
}

__all__ = [
    "AppliedOp",
    "ApplyPendingResult",
    "BackupRef",
    "GateAuthNotConfigured",
    "GateStalenessInfo",
    "GateSyncResult",
    "GitGate",
    "PendingOp",
    "describe_pending_op",
    "make_git_gate",
    "summarize_gate_sync",
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
