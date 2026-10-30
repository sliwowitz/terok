# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""What a task reached for — and moving what it needed into policy, one target at a time.

A full-page screen over the promotion engine's review of one task's harvest.
The review merges what the shield recorded per target and classifies each by
the tier standing in its way; the screen groups the targets the way the
decision is made:

- **suggested egress sets** — sets the project does not enable that would
  cover what nothing allows yet, best first;
- what nothing allows yet, by what the shield did about it: **refused by the
  policy**, **let through by the bypass window**, or **accepted only because
  the shield is down** — which the UP posture would have refused;
- what **your own runtime deny** holds; what the **security tier** or a
  **private range** refuses — break-glass only; the **hard-deny floor** —
  never;
- anything the shield classified under a tier this terok does not know, under
  a heading of its own: a row is never dropped.

Each row offers only the destinations its tier allows.  ``enter`` opens the
promotion form; ``t`` / ``p`` / ``s`` / ``o`` open it on this task / the
project / an egress set / a break-glass override, and a closed destination
says why instead.  The review is read in a worker — the audit log is read
whole — and each promotion runs in the form's own; the result is announced,
with when it takes effect, and the review reloads on the same target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual import on, screen
from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from terok.lib.api import USER_ORIGIN, egress_set_catalog
from terok.lib.api.shield import (
    PROMOTION_DESTINATIONS,
    REMEDY_HOST_PORT,
    REMEDY_OVERRIDE,
    REMEDY_PROJECT,
    REMEDY_SET,
    REMEDY_TASK,
    TIER_ALLOWLIST,
    TIER_HARD_DENY,
    TIER_OPERATOR_DENY,
    TIER_PRIVATE,
    TIER_SECURITY,
    review_egress,
    tier_heading,
)

from .screens import _footer_binding, _modal_binding
from .shield_promote_screen import (
    DESTINATION_LABELS,
    ShieldEnableSetScreen,
    ShieldPromoteScreen,
    destination_hint,
)

if TYPE_CHECKING:
    from terok.lib.api.shield import EgressCandidate, EgressReview, PromotionResult, SetSuggestion

_REVIEW_GROUP = "shield-harvest-review"

# What nothing allows yet (``t40``) splits by what the shield did about it —
# three different stories behind the same set of remedies.
_REFUSED = "refused"
_WINDOW = "window"
_DOWN = "down"
_PASSED_ACTIONS = frozenset({"bypass", "down"})
"""The actions that let a target through rather than refusing it."""

_GROUP_HEADINGS: dict[str, str] = {
    _REFUSED: "Refused by the policy — promote it to this task, the project, or a set",
    _WINDOW: "Let through by the bypass window — promote it to keep it once the window closes",
    _DOWN: (
        "Accepted only because the shield is down — UP would refuse it; promote it before "
        "raising the shield"
    ),
}
"""Headings for the three ``t40`` groups; every other tier takes the engine's own."""

_GROUP_ORDER = (
    _REFUSED,
    _WINDOW,
    _DOWN,
    TIER_OPERATOR_DENY,
    TIER_SECURITY,
    TIER_PRIVATE,
    TIER_HARD_DENY,
)
"""Promotable outright first, break-glass next, the floor last; unknown tiers after."""

_STANDINGS: dict[str, str] = {
    _REFUSED: "refused by the policy",
    _WINDOW: "let through by the bypass window",
    _DOWN: "accepted only because the shield is down",
    TIER_OPERATOR_DENY: "held by your own runtime deny",
    TIER_SECURITY: "refused by the security tier",
    TIER_PRIVATE: "in a private network range",
    TIER_HARD_DENY: "in the hard-deny floor",
}
"""The detail pane's one-phrase standing per group."""

_SUGGESTED_HEADING = "Suggested egress sets — enable one for everything it covers"

_SHORTCUTS: dict[str, str] = dict(zip(PROMOTION_DESTINATIONS, "tpso", strict=True))
"""The key that opens the form on each destination, as the bindings below spell it."""

_POSTURE_NOTES: dict[str, str] = {
    "UP": "The shield is up: what it refuses from now on lands here, ready to promote.",
    "DOWN": (
        "The shield is down: it accepts what the allowlist would refuse, and each such "
        "destination lands here — as accepted only because the shield is down — to promote "
        "before you raise it."
    ),
    "DISENGAGED": (
        "The shield is disengaged: it enforces nothing, and each destination the allowlist "
        "would refuse lands here as accepted only because the shield is down."
    ),
    "QUARANTINE": "The task is quarantined: everything it reaches for is dropped and lands here.",
}
"""What the current posture records, for the empty state: the review alone cannot know it."""

_NOT_RUNNING = "The task is not running: this is what it reached for while it ran."
_LOADING = "Reading what the task reached for…"
_TARGET_WIDTH_CAP = 44
"""Wider targets push their own row out rather than every row's columns."""

_RESULT_TIMEOUT_S = 15.0
"""How long a promotion's result stays up: several lines, each worth reading."""


@dataclass(frozen=True)
class _Loaded:
    """What the review worker read: the review and the operator's set names, or why not."""

    review: EgressReview | None = None
    user_sets: tuple[str, ...] = ()
    error: str = ""


def _read(project: str, task_id: str) -> _Loaded:
    """Read the task's review and the operator's own set names (runs in a thread).

    ``SystemExit`` — a project that does not load, a task that is gone — is
    caught with everything else: it is an answer for the screen, and a
    worker thread must not die on it.
    """
    try:
        review = review_egress(project, task_id)
    except SystemExit as exc:
        return _Loaded(error=str(exc) or "the project or the task could not be loaded")
    except Exception as exc:  # noqa: BLE001 - shown to the operator and never raised
        return _Loaded(error=str(exc) or type(exc).__name__)
    try:
        user_sets = tuple(
            egress_set.name
            for egress_set in egress_set_catalog()
            if egress_set.origin == USER_ORIGIN and egress_set.error is None
        )
    except Exception:  # noqa: BLE001 - suggestions only: the form takes any name
        user_sets = ()
    return _Loaded(review=review, user_sets=user_sets)


def _group(candidate: EgressCandidate) -> str:
    """The group a candidate is listed under: its tier, or for ``t40`` what the shield did."""
    if candidate.tier != TIER_ALLOWLIST:
        return candidate.tier
    actions = set(candidate.actions)
    if not actions or actions - _PASSED_ACTIONS:
        return _REFUSED
    return _WINDOW if "bypass" in actions else _DOWN


def _heading(group: str) -> str:
    """The heading a group is listed under.

    The three ``t40`` groups take the TUI's own; every other tier takes the
    wording the CLI uses.
    """
    return _GROUP_HEADINGS.get(group) or tier_heading(group)


def _when(timestamp: str) -> str:
    """An audit timestamp in local time, to the minute; the raw text when it does not parse."""
    if not timestamp:
        return "-"
    try:
        return datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return timestamp


def _status(candidate: EgressCandidate) -> str:
    """The row's last column: what already grants it, what would, or what is left."""
    if candidate.covered_by:
        return f"already granted by {candidate.covered_by}"
    if candidate.suggested_set:
        return f"in the suggested set {candidate.suggested_set}"
    if not candidate.destinations:
        if REMEDY_HOST_PORT in candidate.remedies:
            return "a per-port localhost grant only"
        return "never promoted"
    if candidate.destinations == (REMEDY_OVERRIDE,):
        return "break-glass only"
    return ""


def _candidate_row(candidate: EgressCandidate, target_width: int) -> Text:
    """One row: target, tries, ports, last seen, status — in aligned columns."""
    ports = ",".join(str(port) for port in candidate.ports) or "-"
    row = Text(
        f"  {candidate.target:<{target_width}}  {candidate.count:>5}×  {ports:<12}  "
        f"{_when(candidate.last_seen):<16}  "
    )
    if status := _status(candidate):
        row.append(status, style=Style(italic=True))
    return row


def _suggestion_row(suggestion: SetSuggestion) -> Text:
    """One suggested set: what it is, and what it covers."""
    return Text(f"  enable {suggestion.summary}")


def _heading_option(text: str) -> Option:
    """A group heading: an option the cursor skips."""
    return Option(Text(text, style=Style(bold=True)), disabled=True)


class _HarvestList(OptionList):
    """The harvest's rows, where ``enter`` promotes the highlighted one — and the footer says so.

    ``OptionList`` binds ``enter`` itself, hidden from the footer, and the
    focused list's binding shadows any the screen declares for the key; so
    the list re-declares it, shown, under the name it has here.
    """

    BINDINGS = [_footer_binding("enter", "select", "Promote…")]


class ShieldHarvestScreen(screen.Screen[None]):
    """One task's harvest, grouped for promotion, with the keys that promote it.

    *posture* and *bypass_closes_at* are the task's shield as the TUI last
    read it, and *running* whether its container runs: the review reads
    files only, so those come from the caller, for the header, for the empty
    state — what the current posture records — and for the form, which says
    whether a grant lands now or at the next start.
    """

    BINDINGS = [
        _modal_binding("escape", "close", "Back"),
        _footer_binding("q", "close", "Back"),
        _footer_binding("t", f"promote_to({REMEDY_TASK!r})", "This task"),
        _footer_binding("p", f"promote_to({REMEDY_PROJECT!r})", "Project"),
        _footer_binding("s", f"promote_to({REMEDY_SET!r})", "Egress set"),
        _footer_binding("o", f"promote_to({REMEDY_OVERRIDE!r})", "Break glass"),
        _footer_binding("r", "reload", "Reload"),
    ]

    CSS = """
    ShieldHarvestScreen {
        layout: vertical;
        background: $background;
    }
    #harvest-header {
        height: auto;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    #harvest-note {
        height: auto;
        color: $warning;
        padding: 0 1;
    }
    #harvest-list {
        height: 1fr;
        border: round $primary;
        border-title-align: right;
        background: $surface;
    }
    #harvest-detail {
        height: auto;
        max-height: 50%;
        border: round $primary-darken-2;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def __init__(
        self,
        project: str,
        task_id: str,
        *,
        posture: str | None = None,
        running: bool = False,
        bypass_closes_at: datetime | None = None,
    ) -> None:
        """Bind the screen to task *task_id* of *project*; the review loads once it mounts."""
        super().__init__()
        self._project = project
        self._task_id = task_id
        self._posture = posture
        self._task_running = running
        self._bypass_closes_at = bypass_closes_at
        self._review: EgressReview | None = None
        self._user_sets: tuple[str, ...] = ()
        self._candidate_rows: dict[str, EgressCandidate] = {}
        self._suggestion_rows: dict[str, SetSuggestion] = {}
        self._keep: str | None = None

    # ── Layout and loading ───────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """Header, the grouped list, the highlighted row's detail, and the keys."""
        yield Static(self._header(), id="harvest-header")
        note = Static("", id="harvest-note")
        note.display = False
        yield note
        listing = _HarvestList(id="harvest-list")
        listing.border_title = "target · tries · ports · last seen · status"
        listing.display = False
        yield listing
        yield Static(Text(_LOADING), id="harvest-detail")
        yield Footer()

    def on_mount(self) -> None:
        """Start reading the review."""
        self._load()

    def _load(self, keep: str | None = None) -> None:
        """Read the review in a worker; *keep* is the target to highlight once it is back."""
        self._keep = keep
        self.run_worker(
            lambda: _read(self._project, self._task_id),
            name=f"{_REVIEW_GROUP}:{self._project}:{self._task_id}",
            group=_REVIEW_GROUP,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Show the review the worker read, or why there is none."""
        if event.worker.group != _REVIEW_GROUP:
            return
        if event.state == WorkerState.SUCCESS:
            loaded = event.worker.result
            self._show(loaded if isinstance(loaded, _Loaded) else _Loaded(error="no review"))
        elif event.state == WorkerState.ERROR:
            self._show(_Loaded(error=str(event.worker.error)))

    def _show(self, loaded: _Loaded) -> None:
        """Lay the review out — the grouped list, or an empty state that says what it means."""
        try:
            listing = self.query_one("#harvest-list", OptionList)
            detail = self.query_one("#harvest-detail", Static)
            note = self.query_one("#harvest-note", Static)
        except NoMatches:  # pragma: no cover - closed meanwhile
            return
        self._review = loaded.review
        self._user_sets = loaded.user_sets
        self.query_one("#harvest-header", Static).update(self._header())
        review = loaded.review
        if review is None:
            listing.display = False
            note.display = False
            detail.update(Text(f"Could not read the harvest: {loaded.error}", style="bold red"))
            self.notify(
                f"Could not read the harvest of task {self._project}/{self._task_id}: "
                f"{loaded.error}",
                severity="error",
                markup=False,
            )
            return
        note.update(Text(review.attribution_note or ""))
        note.display = review.attribution_note is not None
        options = self._options(review)
        if not options:
            listing.display = False
            detail.update(Text(self._empty_text(review)))
            return
        listing.set_options(options)
        listing.display = True
        listing.highlighted = self._initial_highlight(listing)
        listing.focus()
        self._describe(listing)

    def _options(self, review: EgressReview) -> list[Option | None]:
        """The list's options: suggestions, then each group under its heading."""
        self._candidate_rows = {}
        self._suggestion_rows = {}
        options: list[Option | None] = []
        if review.suggestions:
            options.append(_heading_option(_SUGGESTED_HEADING))
            for index, suggestion in enumerate(review.suggestions):
                option_id = f"suggestion-{index}"
                self._suggestion_rows[option_id] = suggestion
                options.append(Option(_suggestion_row(suggestion), id=option_id))
        groups: dict[str, list[EgressCandidate]] = {}
        for candidate in review.candidates:
            groups.setdefault(_group(candidate), []).append(candidate)
        order = [group for group in _GROUP_ORDER if group in groups]
        order += [group for group in groups if group not in _GROUP_ORDER]
        width = min(max((len(c.target) for c in review.candidates), default=0), _TARGET_WIDTH_CAP)
        for group in order:
            if options:
                options.append(None)
            options.append(_heading_option(_heading(group)))
            for candidate in groups[group]:
                option_id = f"candidate-{len(self._candidate_rows)}"
                self._candidate_rows[option_id] = candidate
                options.append(Option(_candidate_row(candidate, width), id=option_id))
        return options

    def _initial_highlight(self, listing: OptionList) -> int | None:
        """The row to highlight: the kept target's, else the first one there is."""
        selectable = [
            index
            for index in range(listing.option_count)
            if not listing.get_option_at_index(index).disabled
        ]
        if self._keep is not None:
            wanted = self._keep.lower()
            for index in selectable:
                option_id = listing.get_option_at_index(index).id
                candidate = self._candidate_rows.get(option_id or "")
                if candidate is not None and candidate.target.lower() == wanted:
                    return index
        return selectable[0] if selectable else None

    def _header(self) -> Text:
        """Whose harvest this is, the shield's posture, and what the review says of itself."""
        parts = [f"What {self._project}/{self._task_id} reached for"]
        if not self._task_running:
            parts.append("not running")
        elif self._posture:
            posture = f"shield {self._posture.lower()}"
            closes = self._bypass_closes_at
            if self._posture == "UP" and closes is not None and closes > datetime.now():
                posture += f", bypass window open until {closes:%H:%M:%S}"
            parts.append(posture)
        review = self._review
        if review is not None:
            parts.append(f"DNS tier {review.dns_tier or 'not recorded'}")
            parts.append("audit on" if review.audit_enabled else "audit OFF")
        return Text(" " + " · ".join(parts))

    def _empty_text(self, review: EgressReview) -> str:
        """Why there is nothing to review — and, with audit on, what the posture records now."""
        reason = review.empty_reason or ""
        if not review.audit_enabled:
            return reason
        note = (
            _NOT_RUNNING if not self._task_running else _POSTURE_NOTES.get(self._posture or "", "")
        )
        return f"{reason}\n\n{note}" if note else reason

    # ── The highlighted row ──────────────────────────────────────────

    @on(OptionList.OptionHighlighted, "#harvest-list")
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Follow the cursor with the detail pane."""
        self._describe(event.option_list)

    def _describe(self, listing: OptionList) -> None:
        """Show what the highlighted row is, and where it can — and cannot — go."""
        option = listing.highlighted_option
        option_id = option.id if option is not None else None
        detail = self.query_one("#harvest-detail", Static)
        if option_id in self._suggestion_rows:
            detail.update(self._suggestion_detail(self._suggestion_rows[option_id]))
        elif option_id in self._candidate_rows:
            detail.update(self._candidate_detail(self._candidate_rows[option_id]))
        else:
            detail.update(Text(""))

    def _candidate_detail(self, candidate: EgressCandidate) -> Text:
        """The target's standing, its evidence, and every destination open or closed."""
        group = _group(candidate)
        standing = _STANDINGS.get(group, f"classified {candidate.tier} by the shield")
        tries = "1 try" if candidate.count == 1 else f"{candidate.count} tries"
        ports = ",".join(str(port) for port in candidate.ports) or "-"
        text = Text()
        text.append(candidate.target, style=Style(bold=True))
        text.append(f" — {standing} ({candidate.tier})\n")
        text.append(
            f"{tries} ({', '.join(candidate.actions)}) on port(s) {ports}; first seen "
            f"{_when(candidate.first_seen)}, last seen {_when(candidate.last_seen)}\n"
        )
        if candidate.addresses:
            text.append(f"Addresses: {', '.join(candidate.addresses)}\n")
        if candidate.covered_by:
            text.append(
                f"Already granted by {candidate.covered_by}: granted after the task started, it "
                "applies from its next start — or, on a DNS tier that resolves names once, the "
                "name moved to an address the launch did not pin.\n",
                style=Style(italic=True),
            )
        for destination in PROMOTION_DESTINATIONS:
            key = _SHORTCUTS[destination]
            label = DESTINATION_LABELS[destination].split(" — ")[0]
            closed = candidate.refusal(destination)
            if closed is None:
                hint = destination_hint(
                    destination,
                    candidate,
                    project=self._project,
                    task_id=self._task_id,
                    running=self._task_running,
                )
                text.append(f"\n{key}  {label}: ", style=Style(bold=True))
                text.append(hint)
            else:
                text.append(f"\n{key}  {label}: closed — {closed}", style=Style(dim=True))
        return text

    def _suggestion_detail(self, suggestion: SetSuggestion) -> Text:
        """What enabling the suggested set covers, and what else comes with it."""
        noun = "host" if suggestion.hosts == 1 else "hosts"
        text = Text()
        text.append(f"Enable {suggestion.name}", style=Style(bold=True))
        text.append(f" — {suggestion.origin}, {suggestion.hosts} {noun}\n")
        text.append(
            f"It covers {len(suggestion.covers)} of the {suggestion.of} targets nothing allows "
            f"yet: {', '.join(suggestion.covers)}.\n"
        )
        text.append(
            f"\nenter / s: enable it for {self._project} — every task gets the whole set from its "
            "next start."
        )
        return text

    # ── Promoting ────────────────────────────────────────────────────

    def _highlighted_id(self) -> str | None:
        """The highlighted option's id; ``None`` when the list shows nothing."""
        try:
            listing = self.query_one("#harvest-list", OptionList)
        except NoMatches:  # pragma: no cover - closed meanwhile
            return None
        if not listing.display:
            return None
        option = listing.highlighted_option
        return option.id if option is not None else None

    @on(OptionList.OptionSelected, "#harvest-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter on a row opens its promotion."""
        self._open(event.option.id, None)

    def action_promote_to(self, destination: str) -> None:
        """Open the promotion form on *destination* — or say why it is closed for this row."""
        self._open(self._highlighted_id(), destination)

    def _open(self, option_id: str | None, destination: str | None) -> None:
        """Open the modal the row takes: the form for a target, a confirm for a suggestion."""
        if option_id in self._suggestion_rows:
            if destination not in (None, REMEDY_SET):
                self.notify(
                    "A suggested set is enabled, not promoted elsewhere: press enter or s.",
                    severity="warning",
                )
                return
            self.app.push_screen(
                ShieldEnableSetScreen(
                    self._suggestion_rows[option_id],
                    project=self._project,
                    task_id=self._task_id,
                    running=self._task_running,
                ),
                self._on_promoted,
            )
            return
        candidate = self._candidate_rows.get(option_id or "")
        if candidate is None:
            self.notify("Nothing to promote here: highlight a target.", severity="warning")
            return
        if not candidate.destinations:
            self.notify(candidate.refusal(REMEDY_TASK) or "", severity="warning", markup=False)
            return
        if destination is not None and (closed := candidate.refusal(destination)) is not None:
            self.notify(closed, severity="warning", markup=False)
            return
        self.app.push_screen(
            ShieldPromoteScreen(
                candidate,
                project=self._project,
                task_id=self._task_id,
                running=self._task_running,
                set_names=self._set_names_for(candidate),
                destination=destination,
            ),
            self._on_promoted,
        )

    def _set_names_for(self, candidate: EgressCandidate) -> tuple[str, ...]:
        """The set names the form suggests: sets covering the target first, then the operator's."""
        covering = [
            suggestion.name
            for suggestion in (self._review.suggestions if self._review else ())
            if candidate.target in suggestion.covers
        ]
        return tuple(dict.fromkeys((*covering, *self._user_sets)))

    def _on_promoted(self, result: PromotionResult | None) -> None:
        """Announce what the promotion did, then read the review again on the same target."""
        if result is None:
            return
        self.notify(
            result.message or _title(result),
            title=_title(result),
            severity=result.severity,
            timeout=_RESULT_TIMEOUT_S,
            markup=False,
        )
        self._load(keep=result.target or self._highlighted_target())

    def _highlighted_target(self) -> str | None:
        """The highlighted candidate's target, to keep across a reload."""
        candidate = self._candidate_rows.get(self._highlighted_id() or "")
        return candidate.target if candidate is not None else None

    def action_reload(self) -> None:
        """Read the review again, keeping the highlighted target."""
        self._load(keep=self._highlighted_target())

    def action_close(self) -> None:
        """Back to where the screen was opened from."""
        self.dismiss(None)


def _title(result: PromotionResult) -> str:
    """The result notice's title: what went where, and when it takes effect.

    Worded as a direction, not a claim that something changed: a target the
    project grants already is no new write, and the engine's own notes —
    the notice's body — say so.
    """
    if result.failed:
        return f"{result.target or result.set_name}: nothing granted"
    if result.target is None:
        return f"Egress set {result.set_name} enabled — {_timing(result)}"
    where = {
        REMEDY_TASK: "this task",
        REMEDY_PROJECT: "the project",
        REMEDY_SET: f"the egress set {result.set_name}",
        REMEDY_OVERRIDE: "a break-glass override",
    }.get(result.destination, result.destination)
    return f"{result.target} → {where} — {_timing(result)}"


def _timing(result: PromotionResult) -> str:
    """When the promotion takes effect, in a few words."""
    if result.live and result.next_start:
        return "live on this task now; applies to every task at its next start"
    if result.live:
        return "live on this task now"
    return "applies at next start"
