# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Shield operations and CLI registry — public API surface.

Re-export catalog for the egress-firewall layer.  Sources:
[`terok.lib.integrations.sandbox`][terok.lib.integrations.sandbox] for
the high-level shield surface terok-sandbox owns
([`ShieldManager`][terok_sandbox.ShieldManager],
[`ShieldHooks`][terok_sandbox.ShieldHooks],
[`RecoveryStatus`][terok_sandbox.RecoveryStatus]), and
[`terok.lib.integrations.shield`][terok.lib.integrations.shield] for
the lower-level CLI registry (``COMMANDS``, ``ArgDef``, ``CommandDef``,
``ExecError``) that terok's ``terok shield`` bridge wires into its own
command tree.

shield's ``CommandDef`` is aliased to ``ShieldCommandDef`` so it doesn't
collide with sandbox's ``CommandDef`` (which already flows through
[`terok.lib.api`][terok.lib.api] for the CLI tree).

Frontends that act on a task's shield build the manager through
[`task_shield_manager`][terok.lib.orchestration.task_runners.shield.task_shield_manager],
never bare: the kill switch reaches sandbox only through terok's config.
The values the manager hands back travel through here as well —
``AllowResult``, ``OverrideRevocation`` and the ``SecurityTierRefusal`` an
allow of a security-tier host raises; ``HarvestReport`` / ``HarvestEntry``
with their ``TIER_*`` standings and ``REMEDY_*`` remedies; ``ShieldReading``
of a ``ShieldState``; ``RotationRisk``; and ``validate_target``, shield's
own grammar for a target.

Promotion comes from
[`egress_promotion`][terok.lib.domain.egress_promotion], the one engine both
frontends share:
[`review_egress`][terok.lib.domain.egress_promotion.review_egress] reads a
task's harvest into an ``EgressReview`` of ``EgressCandidate`` targets and
``SetSuggestion`` sets;
[`promote_egress`][terok.lib.domain.egress_promotion.promote_egress] applies
one promotion and returns a ``PromotionResult``, or raises
``PromotionRefused``; and
[`export_egress`][terok.lib.domain.egress_promotion.export_egress] writes the
JSON user groups send back.  All three block — call them from a worker
thread.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from terok.lib.domain.egress_promotion import (
        EXPORT_FORMAT as EXPORT_FORMAT,
        EXPORT_VERSION as EXPORT_VERSION,
        PROMOTION_DESTINATIONS as PROMOTION_DESTINATIONS,
        EgressCandidate as EgressCandidate,
        EgressReview as EgressReview,
        PromotionRefused as PromotionRefused,
        PromotionResult as PromotionResult,
        PromotionSeverity as PromotionSeverity,
        SetSuggestion as SetSuggestion,
        export_egress as export_egress,
        promote_egress as promote_egress,
        review_egress as review_egress,
        tier_heading as tier_heading,
    )
    from terok.lib.integrations.sandbox import (
        REMEDY_HOST_PORT as REMEDY_HOST_PORT,
        REMEDY_OVERRIDE as REMEDY_OVERRIDE,
        REMEDY_PROJECT as REMEDY_PROJECT,
        REMEDY_SET as REMEDY_SET,
        REMEDY_TASK as REMEDY_TASK,
        TIER_ALLOWLIST as TIER_ALLOWLIST,
        TIER_HARD_DENY as TIER_HARD_DENY,
        TIER_OPERATOR_DENY as TIER_OPERATOR_DENY,
        TIER_PRIVATE as TIER_PRIVATE,
        TIER_SECURITY as TIER_SECURITY,
        AllowResult as AllowResult,
        DnsTier as DnsTier,
        HarvestEntry as HarvestEntry,
        HarvestReport as HarvestReport,
        OverrideRevocation as OverrideRevocation,
        RecoveryStatus as RecoveryStatus,
        RotationRisk as RotationRisk,
        SandboxConfig as SandboxConfig,
        SecurityTierRefusal as SecurityTierRefusal,
        ShieldHooks as ShieldHooks,
        ShieldManager as ShieldManager,
        ShieldReading as ShieldReading,
        ShieldSetupError as ShieldSetupError,
        ShieldState as ShieldState,
        validate_target as validate_target,
    )
    from terok.lib.integrations.shield import (
        COMMANDS as SHIELD_COMMANDS,
        ArgDef as ArgDef,
        CommandDef as ShieldCommandDef,
        ExecError as ExecError,
        is_container_arg as shield_is_container_arg,
        needs_container as shield_needs_container,
        standalone_only as shield_standalone_only,
    )
    from terok.lib.orchestration.task_runners import (
        task_shield_manager as task_shield_manager,
    )

#: Public name -> defining module (PEP 562 lazy resolution).
_LAZY: dict[str, str] = {
    "AllowResult": "terok.lib.integrations.sandbox",
    "ArgDef": "terok.lib.integrations.shield",
    "DnsTier": "terok.lib.integrations.sandbox",
    "EXPORT_FORMAT": "terok.lib.domain.egress_promotion",
    "EXPORT_VERSION": "terok.lib.domain.egress_promotion",
    "EgressCandidate": "terok.lib.domain.egress_promotion",
    "EgressReview": "terok.lib.domain.egress_promotion",
    "ExecError": "terok.lib.integrations.shield",
    "HarvestEntry": "terok.lib.integrations.sandbox",
    "HarvestReport": "terok.lib.integrations.sandbox",
    "OverrideRevocation": "terok.lib.integrations.sandbox",
    "PROMOTION_DESTINATIONS": "terok.lib.domain.egress_promotion",
    "PromotionRefused": "terok.lib.domain.egress_promotion",
    "PromotionResult": "terok.lib.domain.egress_promotion",
    "PromotionSeverity": "terok.lib.domain.egress_promotion",
    "REMEDY_HOST_PORT": "terok.lib.integrations.sandbox",
    "REMEDY_OVERRIDE": "terok.lib.integrations.sandbox",
    "REMEDY_PROJECT": "terok.lib.integrations.sandbox",
    "REMEDY_SET": "terok.lib.integrations.sandbox",
    "REMEDY_TASK": "terok.lib.integrations.sandbox",
    "RecoveryStatus": "terok.lib.integrations.sandbox",
    "RotationRisk": "terok.lib.integrations.sandbox",
    "SHIELD_COMMANDS": "terok.lib.integrations.shield:COMMANDS",
    "SandboxConfig": "terok.lib.integrations.sandbox",
    "SecurityTierRefusal": "terok.lib.integrations.sandbox",
    "SetSuggestion": "terok.lib.domain.egress_promotion",
    "ShieldCommandDef": "terok.lib.integrations.shield:CommandDef",
    "ShieldHooks": "terok.lib.integrations.sandbox",
    "ShieldManager": "terok.lib.integrations.sandbox",
    "ShieldReading": "terok.lib.integrations.sandbox",
    "ShieldSetupError": "terok.lib.integrations.sandbox",
    "ShieldState": "terok.lib.integrations.sandbox",
    "TIER_ALLOWLIST": "terok.lib.integrations.sandbox",
    "TIER_HARD_DENY": "terok.lib.integrations.sandbox",
    "TIER_OPERATOR_DENY": "terok.lib.integrations.sandbox",
    "TIER_PRIVATE": "terok.lib.integrations.sandbox",
    "TIER_SECURITY": "terok.lib.integrations.sandbox",
    "export_egress": "terok.lib.domain.egress_promotion",
    "promote_egress": "terok.lib.domain.egress_promotion",
    "review_egress": "terok.lib.domain.egress_promotion",
    "shield_is_container_arg": "terok.lib.integrations.shield:is_container_arg",
    "shield_needs_container": "terok.lib.integrations.shield:needs_container",
    "shield_standalone_only": "terok.lib.integrations.shield:standalone_only",
    "task_shield_manager": "terok.lib.orchestration.task_runners",
    "tier_heading": "terok.lib.domain.egress_promotion",
    "validate_target": "terok.lib.integrations.sandbox",
}

__all__ = [
    "AllowResult",
    "ArgDef",
    "DnsTier",
    "EXPORT_FORMAT",
    "EXPORT_VERSION",
    "EgressCandidate",
    "EgressReview",
    "ExecError",
    "HarvestEntry",
    "HarvestReport",
    "OverrideRevocation",
    "PROMOTION_DESTINATIONS",
    "PromotionRefused",
    "PromotionResult",
    "PromotionSeverity",
    "REMEDY_HOST_PORT",
    "REMEDY_OVERRIDE",
    "REMEDY_PROJECT",
    "REMEDY_SET",
    "REMEDY_TASK",
    "RecoveryStatus",
    "RotationRisk",
    "SHIELD_COMMANDS",
    "SecurityTierRefusal",
    "SetSuggestion",
    "ShieldCommandDef",
    "ShieldManager",
    "ShieldReading",
    "ShieldSetupError",
    "ShieldState",
    "TIER_ALLOWLIST",
    "TIER_HARD_DENY",
    "TIER_OPERATOR_DENY",
    "TIER_PRIVATE",
    "TIER_SECURITY",
    "export_egress",
    "promote_egress",
    "review_egress",
    "shield_is_container_arg",
    "shield_needs_container",
    "shield_standalone_only",
    "task_shield_manager",
    "tier_heading",
    "validate_target",
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
