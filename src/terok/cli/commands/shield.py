# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Shield egress firewall management commands.

Uses the ``terok_shield`` command registry to build subcommands.
Commands that need a container take positional ``project_name task_id``
(same convention as ``terok task …``), which are resolved to a
container name + task directory for the registry handler.

Four subcommands are terok's own, registered by hand beside the registry's:
``install-hooks``; ``sets``, the egress-set chooser; ``promote``, which
reviews a task's harvest and moves one target into policy; and ``export``,
which writes a project's harvests as the JSON a user group gets sent.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from terok.lib.api import shield as _shield_api
from terok.lib.api.shield import (
    SHIELD_COMMANDS as COMMANDS,
    ArgDef,
    ExecError,
    ShieldCommandDef as CommandDef,
    ShieldManager,
    shield_is_container_arg,
    shield_needs_container,
    shield_standalone_only,
)

from ...lib.core.config import make_sandbox_config
from ...lib.orchestration.tasks import resolve_task_id

if TYPE_CHECKING:
    from terok.lib.api.shield import EgressCandidate, EgressReview, PromotionResult

_EXIT_USAGE = 2
"""Exit status for a flag combination the command does not take — argparse's own."""


def _add_arg(parser: argparse.ArgumentParser, arg: ArgDef) -> None:
    """Register an `ArgDef` with an argparse parser."""
    kwargs: dict = {}
    if arg.help:
        kwargs["help"] = arg.help
    for field in ("type", "default", "action", "dest", "nargs"):
        val = getattr(arg, field)
        if val is not None:
            kwargs[field] = val
    parser.add_argument(arg.name, **kwargs)


def _resolve_task(project_name: str, task_id: str) -> tuple[str, Path]:
    """Resolve project+task to (container_name, task_dir).

    Returns:
        Tuple of (container_name, task_dir) for constructing a Shield.

    Raises:
        ValueError: If the task has never been run (no container exists).
    """
    from ...lib.core.projects import load_project
    from ...lib.orchestration.tasks import container_name, load_task_meta

    project = load_project(project_name)
    meta, _ = load_task_meta(project.name, task_id)
    mode = meta.get("mode")
    if mode is None:
        raise ValueError(
            f"Task {task_id} in project {project_name!r} has never been run — no container exists"
        )
    cname = container_name(project.name, mode, task_id)
    task_dir = project.tasks_root / str(task_id)
    return cname, task_dir


class _KillSwitchShield:
    """A task's shield as shield's registry handlers see it, with sandbox's kill switch in the way.

    The handlers take a bare ``Shield``, but the kill switch lives one layer
    up, in the ``ShieldManager`` terok builds from its own config: sandbox
    reads it from no file.  Handed the bare shield, ``terok shield allow``,
    ``bypass --for`` and ``override`` would widen egress on a container
    started before the switch was thrown — exactly what the switch forbids.
    So those three calls go through the manager, which refuses them with
    its own message while the switch is set; everything else — reads,
    closes, revokes, posture changes — reaches the shield as before, the
    way every terok surface treats them.  A verb that grants without calling
    the shield it is handed (``simple-clearance``) never gets this far while
    the switch is set: ``dispatch`` refuses it first.
    """

    _WIDENING = frozenset({"allow", "bypass", "override"})
    """Shield calls that open something, and so must pass the kill switch."""

    def __init__(self, manager: ShieldManager) -> None:
        """Wrap the task's *manager*, built with terok's config."""
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        """Send the widening calls through the manager, everything else to the shield."""
        if name in self._WIDENING:
            return getattr(self._manager, name)
        return getattr(self._manager.shield, name)


_GRANTS_OUT_OF_PROCESS = frozenset({"simple-clearance"})
"""Registry verbs that can widen egress without calling the shield they are handed.

``simple-clearance`` turns each operator Allow into a ``terok-shield allow``
child process, which reads its own configuration and never sees terok's kill
switch — so ``_KillSwitchShield`` cannot stand in its way, and the verb is
refused outright while the switch is set."""


def _refuse_while_disabled(cmd_name: str, manager: ShieldManager) -> None:
    """Refuse a verb that would grant past the kill switch, before it starts.

    Raises:
        RuntimeError: *cmd_name* is in ``_GRANTS_OUT_OF_PROCESS`` and the
            switch is set; the caller turns it into an exit status.
    """
    if cmd_name in _GRANTS_OUT_OF_PROCESS and manager.disabled:
        raise RuntimeError(
            f"shield is disabled — {cmd_name} would allow through a terok-shield process "
            "the kill switch does not reach, so it does not run while "
            "shield.disable_firewall_no_protection is set"
        )


def _extract_handler_kwargs(args: argparse.Namespace, cmd_def: CommandDef) -> dict:
    """Extract keyword arguments for a registry handler from parsed args.

    Skips the container-reference args, any spelling the registry uses
    ([`is_container_arg`][terok_shield.commands.is_container_arg] names
    them all): the CLI resolves the container from ``project_name`` +
    ``task_id``, and ``--container-id`` from the container's UUID — see
    [`resolve_container_uuid`][terok.lib.orchestration.task_runners.shield.resolve_container_uuid].
    """
    kwargs: dict = {}
    for arg in cmd_def.args:
        if shield_is_container_arg(arg):
            continue
        key = arg.dest or arg.name.lstrip("-").replace("-", "_")
        if hasattr(args, key):
            kwargs[key] = getattr(args, key)
    return kwargs


_DESIRED_STATE_FILENAME = "shield_desired_state"


def _persist_desired_state(cmd_name: str, task_dir: Path, kwargs: dict) -> None:
    """Write desired shield state after a successful ``up`` or ``down`` command.

    Persists the operator's intent so ``on_task_restart: retain`` can
    restore the correct state after a container stop/start cycle.
    Best-effort: OSError is logged but swallowed so the shield command
    itself stays successful.
    """
    if cmd_name == "up":
        value = "up"
    elif cmd_name == "down":
        value = "disengaged" if kwargs.get("disengaged") else "down"
    else:
        return
    try:
        (task_dir / _DESIRED_STATE_FILENAME).write_text(f"{value}\n")
    except OSError as exc:
        print(
            f"Warning: could not persist {_DESIRED_STATE_FILENAME} to {task_dir}: {exc}",
            file=sys.stderr,
        )


def _resolved_commands() -> tuple[CommandDef, ...]:
    """Materialise the (now lazy) terok-shield command registry.

    terok-shield ships lazy [`CommandDef`][terok_util.cli_types.CommandDef]s
    (``source`` set) whose ``args`` / ``handler`` / ``extras`` (which
    ``needs_container`` / ``standalone_only`` read) populate only on
    ``resolve()``.  Both register and dispatch read those fields, so resolve
    up front — this runs only for an actual ``terok shield`` invocation (or
    the full ``--help`` surface), never for another verb.
    """
    return tuple(cmd.resolve() for cmd in COMMANDS)


def _print_set_registry() -> None:
    """List every set a project can name, by origin, then the files that define none.

    Built-in sets come with their hosts and what ``recommended`` expands to
    (built-in sets only).  The operator's own sets come with their file and
    the projects naming them — the projects an edit to the file widens at
    their next task start; a file that does not parse shows its error, and
    a ``.txt`` file that names no set says why it is ignored.
    """
    from terok.lib.api import (
        OS_PACKAGES_SUMMARY,
        RECOMMENDED_SET,
        USER_ORIGIN,
        egress_set_catalog,
        egress_set_users,
        egress_sets_dir,
        ignored_user_set_files,
        selected_egress_sets,
    )

    catalog = egress_set_catalog()
    print("Built-in egress sets (project.yml shield.sets; unset = none):")
    for egress_set in (s for s in catalog if s.origin != USER_ORIGIN):
        hosts = OS_PACKAGES_SUMMARY if egress_set.dynamic else ", ".join(egress_set.hosts)
        print(f"  {egress_set.name}: {hosts}")
    built_in = ", ".join(selected_egress_sets((RECOMMENDED_SET,)))
    print(f"  {RECOMMENDED_SET}: {built_in} (built-in sets only, never your own)")

    directory = egress_sets_dir()
    user_sets = [s for s in catalog if s.origin == USER_ORIGIN]
    print(f"\nYour egress sets ({directory}/<name>.txt, one host per line):")
    if not user_sets:
        print("  none yet")
    users = egress_set_users() if user_sets else {}
    for egress_set in user_sets:
        if egress_set.error is not None:
            print(f"  {egress_set.name}: INVALID — {egress_set.error}")
        else:
            print(f"  {egress_set.name}: {', '.join(egress_set.hosts) or '(empty)'}")
        print(f"      file: {egress_set.path}")
        print(f"      used by: {', '.join(users.get(egress_set.name, ())) or 'no project'}")
    for ignored in ignored_user_set_files():
        print(f"  ignored {ignored.path}: {ignored.reason}")


def _print_project_sets(project_name: str) -> None:
    """Show a project's ``shield.sets`` as authored and the concrete sets it grants."""
    from terok.lib.api import describe_egress_sets, load_project, selected_egress_sets

    sets = load_project(project_name).shield_sets
    print(f"shield.sets for {project_name}: {describe_egress_sets(sets)}")
    print(f"Active egress sets: {describe_egress_sets(selected_egress_sets(sets))}")


def _parse_set_selection(selection: str) -> tuple[str, ...]:
    """Map a ``--set`` value onto ``shield.sets``: 'none' → empty, else the listed names."""
    if selection.strip().lower() == "none":
        return ()
    return tuple(s.strip() for s in selection.split(",") if s.strip())


def _write_project_sets(project_name: str, selection: str) -> None:
    """Replace a project's ``shield.sets`` and report the new selection."""
    from terok.lib.api import describe_egress_sets, set_project_shield_sets

    chosen = _parse_set_selection(selection)
    path = set_project_shield_sets(project_name, chosen)
    print(f"shield.sets for {project_name}: {describe_egress_sets(chosen)}\nWritten to {path}")
    print("Tasks pick the new selection up at their next start or restart.")


def _handle_sets(project_name: str | None, selection: str | None) -> None:
    """List the egress sets; show or replace a project's selection.

    Without a project: every set by origin — built-in sets with their
    hosts and what ``recommended`` expands to, the operator's own with
    their files and the projects naming them.  With a project: its
    ``shield.sets`` and the sets it grants (none when unset).  With
    ``--set``: replace the selection (``none`` → explicit empty list; user
    sets by name) and remind that running containers pick it up on restart.
    """
    if selection is not None and project_name is None:
        print("Error: --set requires a project name", file=sys.stderr)
        sys.exit(1)
    if selection is not None and project_name is not None:
        _write_project_sets(project_name, selection)
    elif project_name is not None:
        _print_project_sets(project_name)
    else:
        _print_set_registry()


# ── promote ────────────────────────────────────────────────────────────


def _promotion_destination(text: str) -> tuple[str, str | None]:
    """Parse ``--to``: ``task``, ``project``, ``override`` or ``set:NAME``."""
    from terok.lib.api.shield import PROMOTION_DESTINATIONS, REMEDY_SET

    kind, sep, name = text.partition(":")
    if kind == REMEDY_SET and sep and name.strip():
        return REMEDY_SET, name.strip()
    if not sep and kind in PROMOTION_DESTINATIONS and kind != REMEDY_SET:
        return kind, None
    spelled = ", ".join(f"{d}:NAME" if d == REMEDY_SET else d for d in PROMOTION_DESTINATIONS)
    raise argparse.ArgumentTypeError(f"expected one of {spelled}, not {text!r}")


def _iso_date(text: str) -> date:
    """Parse ``--expires``: an ISO calendar date, the day a break-glass stays valid through."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date as YYYY-MM-DD, not {text!r}") from None


def _complete_destinations(
    prefix: str, parsed_args: argparse.Namespace, **kwargs: object
) -> list[str]:  # pragma: no cover
    """Return the ``--to`` values matching *prefix*: the destinations, a ``set:`` per egress set."""
    try:
        from terok.lib.api import egress_set_catalog
        from terok.lib.api.shield import PROMOTION_DESTINATIONS, REMEDY_SET

        options = [d for d in PROMOTION_DESTINATIONS if d != REMEDY_SET]
        options += [f"{REMEDY_SET}:{egress_set.name}" for egress_set in egress_set_catalog()]
    except Exception:
        return []
    return [option for option in options if option.startswith(prefix)]


def _complete_targets(
    prefix: str, parsed_args: argparse.Namespace, **kwargs: object
) -> list[str]:  # pragma: no cover
    """Return the targets in the task's harvest matching *prefix*, for ``--target``."""
    project_name = getattr(parsed_args, "project_name", None)
    task_id = getattr(parsed_args, "task_id", None)
    if not project_name or not task_id:
        return []
    try:
        from terok.lib.api.shield import review_egress

        review = review_egress(project_name, resolve_task_id(project_name, task_id))
    except (Exception, SystemExit):
        return []
    return [c.target for c in review.candidates if c.target.startswith(prefix)]


def _usage_error(message: str) -> None:
    """Refuse a flag combination the way argparse would: on stderr, with exit status 2."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(_EXIT_USAGE)


def _check_promote_flags(
    destination: str | None, target: str | None, reason: str | None, expires: date | None
) -> None:
    """Hold ``--target``/``--reason``/``--expires`` to what ``--to`` takes — argparse cannot.

    An override needs both a reason and an expiry; no other destination
    takes either.  Every destination but a set needs a target: a set
    without one is enabled for everything it covers in the harvest.
    """
    from terok.lib.api.shield import REMEDY_OVERRIDE, REMEDY_SET

    if destination == REMEDY_OVERRIDE:
        missing = [
            flag
            for flag, value in (("--reason", (reason or "").strip()), ("--expires", expires))
            if not value
        ]
        if missing:
            _usage_error(
                f"--to override needs {' and '.join(missing)}: a break-glass says why it "
                "exists and when it closes"
            )
    elif reason is not None or expires is not None:
        _usage_error("--reason and --expires go with --to override only")
    if destination is not None and target is None and destination != REMEDY_SET:
        _usage_error(f"--to {destination} needs --target HOST, one of the targets the review lists")


def _handle_promote(args: argparse.Namespace) -> None:
    """Review a task's harvest, or promote one target out of it.

    Without ``--to``: list what the task reached for, grouped by the tier
    standing in its way, suggested egress sets first, each with the
    commands that would promote it (``--target`` narrows the listing to
    one target).  With ``--to``: apply that one promotion and say what took
    effect now and what waits for a next start.
    """
    from terok.lib.api.shield import promote_egress, review_egress

    project_name = args.project_name
    task_id = resolve_task_id(project_name, args.task_id)
    destination, set_name = args.promote_to or (None, None)
    _check_promote_flags(destination, args.target, args.promote_reason, args.promote_expires)
    if destination is None:
        _show_review(review_egress(project_name, task_id), args.target, as_json=args.promote_json)
        return
    try:
        result = promote_egress(
            project_name,
            task_id,
            args.target,
            destination,
            set_name=set_name,
            reason=args.promote_reason,
            expires=args.promote_expires,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(
            f"Error: a file could not be written ({exc}) — anything written before it stays; "
            "re-run the review (promote without --to) to see where things stand.",
            file=sys.stderr,
        )
        sys.exit(1)
    _show_result(result, as_json=args.promote_json)


def _show_review(review: EgressReview, target: str | None, *, as_json: bool) -> None:
    """Print the review — the whole of it, or the one *target* — as text or JSON."""
    candidates = review.candidates if target is None else (_candidate_for(review, target),)
    if as_json:
        data = review.as_dict()
        data["candidates"] = [candidate.as_dict() for candidate in candidates]
        print(json.dumps(data, indent=2))
        return
    _print_review_text(review, candidates, with_suggestions=target is None)


def _candidate_for(review: EgressReview, target: str) -> EgressCandidate:
    """The candidate for *target* — exit 1 when it is not among what the task reached for."""
    found = review.candidate(target)
    if found is None:
        print(
            f"Error: {target} is not among what task {review.project}/{review.task} reached for",
            file=sys.stderr,
        )
        sys.exit(1)
    return found


def _print_review_text(
    review: EgressReview, candidates: tuple[EgressCandidate, ...], *, with_suggestions: bool
) -> None:
    """Print the review as text: each candidate under its tier, with the commands promoting it.

    The header names the DNS tier and the audit state the harvest was
    recorded under — what it could hold at all; an empty review stops at why.
    """
    tier = review.dns_tier or "not recorded"
    audit = "on" if review.audit_enabled else "OFF"
    print(
        f"What task {review.project}/{review.task} reached for "
        f"(DNS tier: {tier}; shield audit {audit})"
    )
    if review.empty_reason:
        print(review.empty_reason)
        return
    if review.attribution_note:
        print(f"\n{review.attribution_note}")
    if with_suggestions and review.suggestions:
        _print_suggestions(review)
    _print_by_tier(review, candidates)


def _print_suggestions(review: EgressReview) -> None:
    """Print the egress sets that would cover what nothing allows yet, each with its command."""
    print("\nSuggested egress sets — enable one for everything it covers:")
    for suggestion in review.suggestions:
        print(f"  {suggestion.summary}")
        print(f"    {_promote_command(review, None, f'--to set:{suggestion.name}')}")


def _print_by_tier(review: EgressReview, candidates: tuple[EgressCandidate, ...]) -> None:
    """Print *candidates* grouped under the heading of the tier standing in their way."""
    from terok.lib.api.shield import tier_heading

    groups: dict[str, list[EgressCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.tier, []).append(candidate)
    for tier, members in groups.items():
        print(f"\n{tier_heading(tier)}:")
        for candidate in members:
            _print_candidate(review, candidate)


def _print_candidate(review: EgressReview, candidate: EgressCandidate) -> None:
    """Print one candidate: its line, any grant it already has, and the commands promoting it.

    With no destination open, why a promotion is refused stands in for the commands.
    """
    from terok.lib.api.shield import REMEDY_TASK

    print(f"  {_describe_candidate(candidate)}")
    if candidate.covered_by:
        print(
            f"    already granted by {candidate.covered_by}: granted since the task "
            "started, it applies from the next start — or, on a DNS tier that "
            "resolves names once, the name moved to an address the launch did not pin"
        )
    for destination in candidate.destinations:
        flags = _destination_flags(candidate, destination)
        print(f"    {_promote_command(review, candidate.target, flags)}")
    if not candidate.destinations:
        print(f"    {candidate.refusal(REMEDY_TASK)}")


def _describe_candidate(candidate: EgressCandidate) -> str:
    """One line for a candidate: target, tries, what the shield did, ports, when last seen."""
    tries = "1 try" if candidate.count == 1 else f"{candidate.count} tries"
    ports = ",".join(str(port) for port in candidate.ports) or "-"
    return (
        f"{candidate.target}  ({tries}: {', '.join(candidate.actions)}; ports {ports}; "
        f"last seen {candidate.last_seen})"
    )


def _destination_flags(candidate: EgressCandidate, destination: str) -> str:
    """The ``--to`` flags promoting *candidate* to *destination*, placeholders where it takes input."""
    from terok.lib.api.shield import REMEDY_OVERRIDE, REMEDY_SET

    if destination == REMEDY_SET:
        if candidate.suggested_set:
            return f"--to set:{candidate.suggested_set}"
        return "--to set:NAME   # NAME: one of your own sets, created when new"
    if destination == REMEDY_OVERRIDE:
        return "--to override --reason 'WHY' --expires YYYY-MM-DD"
    return f"--to {destination}"


def _promote_command(review: EgressReview, target: str | None, flags: str) -> str:
    """The ``terok shield promote`` command line for this task, *target* and ``--to`` *flags*."""
    words = ["terok", "shield", "promote", review.project, review.task]
    if target is not None:
        words += ["--target", target]
    return f"{shlex.join(words)} {flags}"


def _show_result(result: PromotionResult, *, as_json: bool) -> None:
    """Print what a promotion did — notes on stdout, warnings on stderr — and fail if nothing took."""
    if as_json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        for line in result.notes:
            print(line)
        for line in result.warnings:
            print(f"Warning: {line}", file=sys.stderr)
    if result.failed:
        sys.exit(1)


# ── export ─────────────────────────────────────────────────────────────


def _handle_export(args: argparse.Namespace) -> None:
    """Write the project's aggregated harvest as JSON — to ``--output``, or stdout."""
    from terok.lib.api.shield import export_egress

    project_name = args.project_name
    task_ids = None
    if args.export_task is not None:
        task_ids = (resolve_task_id(project_name, args.export_task),)
    document = export_egress(project_name, task_ids, scrub=args.scrub)
    text = json.dumps(document, indent=2) + "\n"
    if args.export_output is None:
        sys.stdout.write(text)
        return
    try:
        Path(args.export_output).write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"Error: cannot write {args.export_output}: {exc}", file=sys.stderr)
        sys.exit(1)
    scrubbed = " (scrubbed)" if args.scrub else ""
    print(
        f"Wrote {len(document['entries'])} targets from {document['task_count']} tasks"
        f"{scrubbed} to {args.export_output}"
    )


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``shield`` subcommand group from the registry."""
    p = subparsers.add_parser("shield", help="Manage egress firewall (terok-shield)")
    sub = p.add_subparsers(dest="shield_cmd", required=True)

    for cmd in _resolved_commands():
        if shield_standalone_only(cmd):
            continue

        sp = sub.add_parser(cmd.name, help=cmd.help)

        # Commands that need a container get positional project_name + task_id,
        # matching the ``terok task …`` convention.  Commands with an
        # *optional* container arg (like ``status``) get nargs="?" so they
        # work both with and without a task target.  Completers attach
        # either way so tab-complete works in both forms.
        from ._completers import add_project_name, add_task_id

        if shield_needs_container(cmd):
            add_project_name(sp, help="Project name")
            add_task_id(sp, help="Task ID")
        elif any(shield_is_container_arg(a) for a in cmd.args):
            add_project_name(sp, nargs="?", help="Project name")
            add_task_id(sp, nargs="?", help="Task ID")

        # ``--container-id`` is the per-container hub socket routing
        # key; terok always knows the UUID at the call site (it
        # invokes ``ShieldManager.up`` / ``down`` directly from the
        # task runner), so we don't surface it as a CLI flag — the
        # dispatch function injects it from a ``podman inspect`` lookup.
        for arg in cmd.args:
            if shield_is_container_arg(arg):
                continue
            _add_arg(sp, arg)

    # Manually register install-hooks (standalone_only in registry, needs
    # subprocess passthrough).  Named explicitly so it doesn't shadow the
    # top-level ``terok setup`` which installs *all* host services — this
    # one touches only the shield OCI hooks.
    sub.add_parser("install-hooks", help="Install global OCI hooks for shield")

    # Manually registered: the egress-set chooser is a terok concept
    # (authored t40 content), not a terok-shield registry command.
    sets_p = sub.add_parser("sets", help="List egress sets; show or set a project's choice")
    from ._completers import add_project_name

    add_project_name(sets_p, nargs="?", help="Project whose selection to show or change")
    sets_p.add_argument(
        "--set",
        dest="sets_selection",
        metavar="SET[,SET…]",
        help=(
            "Replace the project's shield.sets with this comma-separated selection "
            "of built-in and your own sets ('recommended' grants every built-in "
            "set, 'none' grants none; requires a project name)"
        ),
    )
    _register_promote(sub)
    _register_export(sub)


def _register_promote(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``promote`` — terok's own: promotion writes terok's policy, not shield's."""
    from ._completers import add_project_name, add_task_id, set_completer

    promote_p = sub.add_parser(
        "promote",
        help=(
            "Review what a task reached for and promote it: to the task, the project, "
            "an egress set, or a break-glass override"
        ),
    )
    add_project_name(promote_p, help="Project name")
    add_task_id(promote_p, help="Task ID")
    target = promote_p.add_argument(
        "--target",
        metavar="HOST",
        help="The target to promote (or, without --to, to show) — one the review lists",
    )
    set_completer(target, _complete_targets)
    to = promote_p.add_argument(
        "--to",
        dest="promote_to",
        type=_promotion_destination,
        metavar="DESTINATION",
        help=(
            "Where to promote it: task, project, set:NAME (a built-in set naming it, or "
            "one of your own — created when new; without --target, enable the set for "
            "everything it covers) or override.  Without --to, list the candidates"
        ),
    )
    set_completer(to, _complete_destinations)
    promote_p.add_argument(
        "--reason",
        dest="promote_reason",
        metavar="TEXT",
        help="Why the break-glass override exists (required with --to override, only there)",
    )
    promote_p.add_argument(
        "--expires",
        dest="promote_expires",
        type=_iso_date,
        metavar="YYYY-MM-DD",
        help=(
            "The last day the override is valid — the kernel closes it at the end of that "
            "day (required with --to override, only there)"
        ),
    )
    promote_p.add_argument(
        "--json",
        dest="promote_json",
        action="store_true",
        help="Print the review, or the promotion's result, as JSON",
    )


def _register_export(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``export`` — the aggregated harvest a user group gets sent."""
    from ._completers import add_project_name

    export_p = sub.add_parser(
        "export",
        help="Write what a project's tasks reached for as JSON, to send back to a user group",
    )
    add_project_name(export_p, help="Project name")
    export_p.add_argument(
        "--task", dest="export_task", metavar="ID", help="Export this task only (default: all)"
    )
    export_p.add_argument(
        "--scrub",
        action="store_true",
        help=(
            "Leave out what identifies your network: addresses, internal names, the "
            "project and task ids, your own set names"
        ),
    )
    export_p.add_argument(
        "--output",
        dest="export_output",
        metavar="FILE",
        help="Write to FILE instead of stdout",
    )


def dispatch(args: argparse.Namespace) -> bool:
    """Handle shield commands.  Returns True if handled."""
    if args.cmd != "shield":
        return False

    cmd_name = args.shield_cmd

    # install-hooks is standalone_only and needs subprocess passthrough
    # (no registry handler).  Single layout: descriptors, scripts, and
    # ballast all land in the canonical terok-owned dir under
    # ``paths.root``.
    if cmd_name == "install-hooks":
        # Module-attribute access so the test ``@patch("...ShieldHooks.install")``
        # intercepts the call.
        _shield_api.ShieldHooks.install()
        return True

    if cmd_name == "sets":
        _handle_sets(getattr(args, "project_name", None), getattr(args, "sets_selection", None))
        return True

    if cmd_name == "promote":
        _handle_promote(args)
        return True

    if cmd_name == "export":
        _handle_export(args)
        return True

    cmd_lookup = {cmd.name: cmd for cmd in _resolved_commands() if not shield_standalone_only(cmd)}
    cmd_def = cmd_lookup.get(cmd_name)
    if cmd_def is None or cmd_def.handler is None:
        return False

    project_name = getattr(args, "project_name", None)
    task_id = getattr(args, "task_id", None)
    if (project_name is None) != (task_id is None):
        print("Error: provide both <project_name> and <task_id>, or neither", file=sys.stderr)
        sys.exit(1)
    has_task = project_name is not None and task_id is not None

    try:
        # mypy narrows the inner pair via the explicit check; ``has_task``
        # is kept around for the except branch's error wording below.
        if project_name is not None and task_id is not None:
            task_id = resolve_task_id(project_name, task_id)
            cname, task_dir = _resolve_task(project_name, task_id)
            manager = ShieldManager(task_dir, make_sandbox_config())
            _refuse_while_disabled(cmd_name, manager)
            task_shield = _KillSwitchShield(manager)
            kwargs = _extract_handler_kwargs(args, cmd_def)
            if cmd_name in {"up", "down"}:
                # ``container_id`` is the per-container hub socket
                # routing key — resolved from the live container at
                # dispatch time so the operator never needs to think
                # about UUIDs.
                from terok.lib.orchestration.task_runners import resolve_container_uuid

                kwargs["container_id"] = resolve_container_uuid(cname)
            if shield_needs_container(cmd_def):
                cmd_def.handler(task_shield, cname, **kwargs)
                _persist_desired_state(cmd_name, task_dir, kwargs)
            else:
                # Optional container arg (e.g. ``status <project> <task>``)
                kwargs["container"] = cname
                cmd_def.handler(task_shield, **kwargs)
        else:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                shield = ShieldManager(Path(tmp), make_sandbox_config()).shield
                kwargs = _extract_handler_kwargs(args, cmd_def)
                cmd_def.handler(shield, **kwargs)
    except ExecError as exc:
        print(
            f"Error: shield operation failed for task {task_id}: {exc}"
            if has_task
            else f"Error: shield operation failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    return True
