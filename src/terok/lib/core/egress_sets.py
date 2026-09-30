# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Egress allowlist sets — named t40 content a project opts into.

Named bundles of hosts a task can be granted at MID granularity: coarse
enough that a user picks a handful of named sets instead of authoring host
lists, fine enough that a Python-only project doesn't drag in container
registries.  The selection (``shield.sets`` in ``project.yml``) feeds the
authored t40 project-allow tier (ordinary allows — a security-deny always
wins), alongside the project's git remote host and its custom
``shield.allow``.

Two origins share one flat namespace:

- **Built-in** sets ([`EGRESS_SETS`][terok.lib.core.egress_sets.EGRESS_SETS])
  ship with terok: well-known development endpoints, read-only.  The
  ``os-packages`` set alone resolves through terok-executor's
  ``package_repo_hosts`` — which distro repos a task needs is image
  knowledge, keyed on the project's detected package family.
- **User** sets are the operator's own: one ``<name>.txt`` per set in
  [`egress_sets_dir`][terok.lib.core.config.egress_sets_dir], one host per
  line, ``#`` comments.  The name comes from the file name alone and can
  never be a built-in set's, ``recommended`` or ``none`` — such a file is
  reported and ignored, never shadowing anything.

No set applies unless the project selects it (the TUI chooser or
``terok shield sets`` write ``shield.sets``).
[`RECOMMENDED_SET`][terok.lib.core.egress_sets.RECOMMENDED_SET] names every
*built-in* set, including sets a later release adds, and the new-project
wizard offers it.  It never reaches a user set: a set written for one
project would otherwise widen every recommended project at its next start,
and ``recommended`` would mean something different on every host.

A project naming a user set whose file is missing or invalid fails to load,
with an error naming the set and its file: the file is the operator's to
fix, and granting less than the project asks for would be a guess.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .config import egress_sets_dir

OS_PACKAGES_SET = "os-packages"
"""The one dynamic set: distro package repos, resolved by package family."""

OS_PACKAGES_SUMMARY = "distro repos, resolved by the image's package family"
"""What to show operators in place of a host list for the dynamic set."""

EGRESS_SETS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "git-hosting": (
            "github.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
            "raw.githubusercontent.com",
            "gist.github.com",
            "gitlab.com",
            "bitbucket.org",
            "codeberg.org",
        ),
        "python": (
            "pypi.org",
            "files.pythonhosted.org",
        ),
        "node": (
            "registry.npmjs.org",
            "registry.yarnpkg.com",
            "nodejs.org",
        ),
        "rust": (
            "crates.io",
            "static.crates.io",
            "index.crates.io",
            "static.rust-lang.org",
        ),
        "go": (
            "proxy.golang.org",
            "sum.golang.org",
            "index.golang.org",
        ),
        "containers": (
            "registry-1.docker.io",
            "auth.docker.io",
            "index.docker.io",
            "production.cloudflare.docker.com",
            "quay.io",
            "cdn.quay.io",
            "cdn01.quay.io",
            "cdn02.quay.io",
            "cdn03.quay.io",
            "ghcr.io",
            "pkg-containers.githubusercontent.com",
            "registry.fedoraproject.org",
        ),
        OS_PACKAGES_SET: (),  # resolved dynamically — see egress_set_hosts
    }
)
"""The built-in sets: name → static hosts (``os-packages`` is dynamic).

Read-only, so nothing can merge into it at runtime — user sets live beside
it, never in it, which is what keeps ``recommended`` built-in only.
"""

RECOMMENDED_SET = "recommended"
"""The meta-set naming every built-in set, including sets a later release adds.

It shares one flat namespace with the concrete set names: ``shield.sets``
lists ``recommended`` the same way it lists ``python``.  It never names a
user set.
"""

NO_SETS = "none"
"""How ``terok shield sets --set`` spells an explicit empty selection — so no set may take it."""

EgressSetOrigin = Literal["built-in", "user"]
"""Where a set comes from: shipped with terok, or the operator's own file."""

BUILT_IN_ORIGIN: EgressSetOrigin = "built-in"
"""Origin of the sets in [`EGRESS_SETS`][terok.lib.core.egress_sets.EGRESS_SETS]."""

USER_ORIGIN: EgressSetOrigin = "user"
"""Origin of a set read from the operator's egress-sets directory."""

USER_SET_SUFFIX = ".txt"
"""File suffix of a user set: ``<name>.txt``."""

_USER_SET_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_USER_SET_NAME_RULE = "lowercase letters, digits and '-', starting with a letter or digit"

_USER_SET_HEADER = (
    "# terok egress set {name!r}: one host per line (a domain name, an IP address\n"
    "# or a CIDR), '#' starts a comment.  A project grants it with shield.sets.\n"
)
"""The comment a newly created user-set file starts with."""


@dataclass(frozen=True, slots=True)
class EgressSet:
    """One named set as [`egress_set_catalog`][terok.lib.core.egress_sets.egress_set_catalog] lists it."""

    name: str
    origin: EgressSetOrigin
    hosts: tuple[str, ...] = ()
    """The hosts it grants — empty for the dynamic ``os-packages`` set
    (resolved per project image) and for a user set whose file does not parse."""
    path: Path | None = None
    """The user set's file; ``None`` for a built-in set."""
    error: str | None = None
    """Why a user set's file does not parse (with the line), or ``None`` when it does.

    A project naming such a set fails to load until the file is fixed."""

    @property
    def dynamic(self) -> bool:
        """Whether the hosts are resolved per project image rather than listed."""
        return self.origin == BUILT_IN_ORIGIN and self.name == OS_PACKAGES_SET


@dataclass(frozen=True, slots=True)
class IgnoredSetFile:
    """A ``.txt`` file in the egress-sets directory that defines no set, and why."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class EgressEdit:
    """What an egress-policy writer did to a file the operator owns — or why it left it alone."""

    path: Path
    """The file written, or the one that would have been."""
    changed: bool
    """``False`` when nothing needed writing (the host or set was already there)."""
    message: str
    """What happened, in the operator's words."""


# ── Hosts ───────────────────────────────────────────────────────────────


def normalize_egress_host(host: str) -> str:
    """Validate one host for a set, ``shield.allow`` or ``shield.override``; return it lower-cased.

    Shield's own target grammar decides — a domain name, an IP literal or a
    CIDR, with no port, ``%`` marker or ``+``/``-`` prefix — so terok never
    keeps a copy of the pattern that drifts.  Two narrower rules sit on
    top: no ``*.`` wildcard (a launch on a DNS tier without live dnsmasq
    cannot resolve one, and these lists name hosts, not patterns), and not
    ``localhost``, which shield reserves for ``+localhost:PORT`` grants.

    Raises:
        ValueError: Naming *host* and the rule it breaks.
    """
    from terok.lib.integrations.sandbox import validate_target

    candidate = host.strip().lower()
    if not candidate:
        raise ValueError("empty host")
    if candidate.startswith(("+", "-")):  # no DNS label starts so; a pasted policy line does
        raise ValueError(
            f"{host.strip()!r} looks like a shield policy line — list the host alone, "
            "without its +/- action"
        )
    if "*" in candidate:
        raise ValueError(f"{host.strip()!r} is a wildcard — list each host by name")
    if candidate == "localhost":
        raise ValueError(
            "'localhost' is not a host to allow — shield reserves it for "
            "host-service grants (+localhost:PORT)"
        )
    try:
        validate_target(candidate)
    except ValueError:
        raise ValueError(
            f"{host.strip()!r} is not a domain name, an IP address or a CIDR"
        ) from None
    return candidate


# ── User sets ───────────────────────────────────────────────────────────


def _user_set_name_problem(name: str) -> str | None:
    """Why *name* cannot name a user set, or ``None`` when it can."""
    if name in EGRESS_SETS:
        return f"{name!r} is a built-in set's name — a user set never shadows one"
    if name in (RECOMMENDED_SET, NO_SETS):
        return f"{name!r} is reserved"
    if not _USER_SET_NAME.fullmatch(name):
        return f"{name!r} is not a valid set name ({_USER_SET_NAME_RULE})"
    return None


def user_set_path(name: str) -> Path:
    """The file user set *name* lives in, whether or not it exists yet.

    Raises:
        ValueError: When *name* cannot name a user set — not a valid name,
            a built-in set's, or reserved.
    """
    if problem := _user_set_name_problem(name):
        raise ValueError(f"cannot use {name!r} for a user egress set: {problem}")
    return egress_sets_dir() / f"{name}{USER_SET_SUFFIX}"


def _parse_user_set(text: str) -> tuple[str, ...]:
    """The hosts of a user-set file's *text*, in file order, each once.

    Raises:
        ValueError: ``line N: …`` for the first line that is not a host.
    """
    hosts: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        try:
            hosts.append(normalize_egress_host(entry))
        except ValueError as exc:
            raise ValueError(f"line {number}: {exc}") from None
    return tuple(dict.fromkeys(hosts))


def _scan_user_set_dir() -> tuple[dict[str, Path], list[IgnoredSetFile]]:
    """The user-set files by name, and the ``.txt`` files that define no set.

    Dot-files and other suffixes (editor backups, a README) are not set
    files and pass silently; a ``.txt`` file with an unusable name is
    reported, so a misnamed set does not vanish without a word.
    """
    directory = egress_sets_dir()
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        return {}, [IgnoredSetFile(directory, f"cannot be read: {exc}")]
    found: dict[str, Path] = {}
    ignored: list[IgnoredSetFile] = []
    for path in entries:
        if path.name.startswith(".") or not path.name.endswith(USER_SET_SUFFIX):
            continue
        name = path.name.removesuffix(USER_SET_SUFFIX)
        if problem := _user_set_name_problem(name):
            ignored.append(IgnoredSetFile(path, problem))
        elif not path.is_file():
            ignored.append(IgnoredSetFile(path, "not a regular file"))
        else:
            found[name] = path
    return found, ignored


def user_egress_sets() -> tuple[EgressSet, ...]:
    """The operator's own sets, by name, read fresh from their files.

    A file that does not parse is still listed — with no hosts and its
    [`error`][terok.lib.core.egress_sets.EgressSet.error] — so a listing can
    show what needs fixing instead of hiding it.
    """
    sets: list[EgressSet] = []
    for name, path in _scan_user_set_dir()[0].items():
        try:
            sets.append(EgressSet(name, USER_ORIGIN, _parse_user_set(_read(path)), path))
        except (OSError, ValueError) as exc:
            sets.append(EgressSet(name, USER_ORIGIN, (), path, str(exc)))
    return tuple(sets)


def ignored_user_set_files() -> tuple[IgnoredSetFile, ...]:
    """The ``.txt`` files in the egress-sets directory that define no set, and why."""
    return tuple(_scan_user_set_dir()[1])


def egress_set_catalog() -> tuple[EgressSet, ...]:
    """Every set a project can name: the built-in sets in registry order, then the user sets by name."""
    built_in = tuple(EgressSet(name, BUILT_IN_ORIGIN, hosts) for name, hosts in EGRESS_SETS.items())
    return built_in + user_egress_sets()


def _read(path: Path) -> str:
    """A user-set file's text (``UnicodeDecodeError`` is a ``ValueError``: an invalid file)."""
    return path.read_text(encoding="utf-8")


def _load_user_set(name: str) -> tuple[tuple[str, ...], str | None]:
    """The hosts of user set *name*, or why there is no such usable set.

    ``(hosts, None)`` for a set that loads, ``((), problem)`` for one that
    does not — missing, unreadable, not a host list, or no name a user set
    can take.  *problem* names the set and its file.
    """
    unknown = f"Unknown shield.sets entry: {name!r} — not a built-in set, and"
    if problem := _user_set_name_problem(name):
        hint = "; write `sets: []` for no sets" if name == NO_SETS else ""
        return (), f"{unknown} {problem}{hint}."
    path = user_set_path(name)
    try:
        return _parse_user_set(_read(path)), None
    except FileNotFoundError:
        return (), f"{unknown} no user set file at {path}."
    except (OSError, ValueError) as exc:
        return (), f"Invalid user egress set {name!r} ({path}): {exc}"


def _user_set_hosts(name: str) -> tuple[str, ...]:
    """The hosts of user set *name*, failing loudly when there is no such usable set.

    Raises:
        SystemExit: Naming the set and its file — missing, unreadable, or
            not a host list — or saying why *name* cannot be a user set.
    """
    hosts, problem = _load_user_set(name)
    if problem:
        raise SystemExit(problem)
    return hosts


def add_host_to_user_set(name: str, host: str) -> EgressEdit:
    """Add *host* to user set *name*, creating the set's file when it is new.

    The file is only ever appended to — one line, after whatever the
    operator wrote — so its comments and order stay exactly as they were.
    Every project naming the set picks the host up at its tasks' next start
    ([`egress_set_users`][terok.lib.core.projects.egress_set_users] says
    which those are).

    Raises:
        ValueError: *name* cannot be a user set, or *host* is not one.
        SystemExit: The set's file exists but does not parse — it is the
            operator's to fix, and appending would only bury the problem —
            or it (or its directory) cannot be read or written; the message
            names the file and the host to add by hand.
    """
    path = user_set_path(name)
    host = normalize_egress_host(host)
    try:
        text = _read(path)
        hosts = _parse_user_set(text)
    except FileNotFoundError:
        return _create_user_set(name, path, host)
    except ValueError as exc:  # undecodable or not a host list
        raise SystemExit(
            f"Invalid user egress set {name!r} ({path}): {exc}.  Fix the file first."
        ) from None
    except OSError as exc:  # unreadable, or not a file at all
        raise _left_as_it_was(name, path, host, exc) from exc
    if host in hosts:
        return EgressEdit(path, False, f"{host} is already in the egress set {name!r}")
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(("" if not text or text.endswith("\n") else "\n") + f"{host}\n")
    except OSError as exc:
        raise _left_as_it_was(name, path, host, exc) from exc
    return EgressEdit(path, True, f"Added {host} to the egress set {name!r} ({path})")


def _create_user_set(name: str, path: Path, host: str) -> EgressEdit:
    """Write a new user-set file holding *host*; append instead if one appeared meanwhile."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _left_as_it_was(name, path, host, exc) from exc
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(_USER_SET_HEADER.format(name=name) + f"{host}\n")
    except FileExistsError:
        return add_host_to_user_set(name, host)
    except OSError as exc:
        raise _left_as_it_was(name, path, host, exc) from exc
    return EgressEdit(path, True, f"Created the egress set {name!r} at {path} with {host}")


def _left_as_it_was(name: str, path: Path, host: str, exc: OSError) -> SystemExit:
    """The refusal for a set file (or its directory) that cannot be read or written."""
    return SystemExit(
        f"{path} was left as it was: {exc}.  Make it writable, or add {host} to the "
        f"egress set {name!r} by hand."
    )


# ── Selection ───────────────────────────────────────────────────────────


def selected_egress_sets(names: tuple[str, ...] | None) -> tuple[str, ...]:
    """The concrete sets a ``shield.sets`` value grants; ``None`` (unset) grants none.

    [`RECOMMENDED_SET`][terok.lib.core.egress_sets.RECOMMENDED_SET] expands
    to every built-in set — never a user set; a set named twice counts
    once, at its first position.
    """
    chosen: list[str] = []
    for name in names or ():
        chosen += tuple(EGRESS_SETS) if name == RECOMMENDED_SET else (name,)
    return tuple(dict.fromkeys(chosen))


def describe_egress_sets(names: tuple[str, ...] | None) -> str:
    """Render a ``shield.sets`` value as authored — one wording, every surface."""
    return ", ".join(names or ()) or "none (no egress sets)"


def validate_egress_sets(names: Iterable[str] | None) -> None:
    """Reject a selection naming a set that does not exist or does not load.

    Called at project-load time, so a typo in ``shield.sets`` — or a user
    set whose file went missing or broke — fails the load loudly instead of
    silently granting less.  The error names each set (with its file for a
    user set) and spells out the sets available.

    Raises:
        SystemExit: With one line per bad name, then the available sets.
    """
    if names is None:
        return
    problems: list[str] = []
    for name in dict.fromkeys(names):
        if name in EGRESS_SETS or name == RECOMMENDED_SET:
            continue
        if problem := _load_user_set(name)[1]:
            problems.append(problem)
    if problems:
        available = [*EGRESS_SETS, RECOMMENDED_SET]
        available += [s.name for s in user_egress_sets() if s.error is None]
        raise SystemExit("\n".join([*problems, f"Available sets: {', '.join(available)}"]))


def egress_set_hosts(name: str, family: str | None) -> tuple[str, ...]:
    """The hosts one concrete set grants — built-in, dynamic, or the operator's own.

    *family* is the project image's package family (``deb``/``rpm``/None),
    consumed by the ``os-packages`` set through terok-executor's
    ``package_repo_hosts``; an unrecognized image gets the all-family union.
    A user set is read fresh from its file.

    Raises:
        SystemExit: For a user set whose file is missing or invalid.
    """
    from terok.lib.integrations.executor import package_repo_hosts

    if name == OS_PACKAGES_SET:
        return tuple(package_repo_hosts(family))
    if name in EGRESS_SETS:
        return EGRESS_SETS[name]
    return _user_set_hosts(name)


def resolve_egress_sets(names: tuple[str, ...] | None, family: str | None) -> tuple[str, ...]:
    """Resolve a set selection into its hosts (order-preserving, de-duplicated).

    *names* is the project's ``shield.sets``;
    [`selected_egress_sets`][terok.lib.core.egress_sets.selected_egress_sets]
    decides which sets it grants, so ``None`` and an empty tuple resolve to
    nothing and ``recommended`` resolves every built-in set.  Each set
    resolves through
    [`egress_set_hosts`][terok.lib.core.egress_sets.egress_set_hosts].
    """
    hosts: list[str] = []
    for name in selected_egress_sets(names):
        hosts += egress_set_hosts(name, family)
    return tuple(dict.fromkeys(hosts))


def granting_egress_set(host: str, names: tuple[str, ...] | None, family: str | None) -> str | None:
    """The first set in a ``shield.sets`` selection that grants *host* by name, or ``None``.

    An exact match on the normalized host — the question a writer asks
    before adding it elsewhere.  (On the ``dnsmasq-live`` DNS tier a listed
    domain also admits its subdomains; whether that covers a host is the
    caller's tier-aware question, not this one.)
    """
    for name in selected_egress_sets(names):
        if host in egress_set_hosts(name, family):
            return name
    return None
