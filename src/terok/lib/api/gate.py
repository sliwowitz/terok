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
"""

from terok.lib.domain.project import make_git_gate  # noqa: F401 — re-exported public API
from terok.lib.integrations.sandbox import (  # noqa: F401 — re-exported public API
    GateAuthNotConfigured,
    GateStalenessInfo,
)

__all__ = [
    "GateAuthNotConfigured",
    "GateStalenessInfo",
    "make_git_gate",
]
