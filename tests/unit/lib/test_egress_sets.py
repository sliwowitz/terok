# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the egress-set registry, the operator's own sets, and their resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from terok.lib.core import egress_sets as egress_sets_mod
from terok.lib.core.config import egress_sets_dir
from terok.lib.core.egress_sets import (
    BUILT_IN_ORIGIN,
    EGRESS_SETS,
    OS_PACKAGES_SET,
    RECOMMENDED_SET,
    USER_ORIGIN,
    add_host_to_user_set,
    describe_egress_sets,
    egress_set_catalog,
    egress_set_hosts,
    granting_egress_set,
    ignored_user_set_files,
    normalize_egress_host,
    resolve_egress_sets,
    selected_egress_sets,
    user_egress_sets,
    user_set_path,
    validate_egress_sets,
)
from tests.testnet import HTTPS_PORT, TEST_IP, TEST_NET_CIDR

_CORP_HOSTS = ("artifactory.corp.example", "pypi-mirror.corp.example")


def _write_set(name: str, text: str) -> Path:
    """Write a user-set file into the (test-isolated) egress-sets directory."""
    directory = egress_sets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def corp_set() -> Path:
    """A valid user set named ``corp`` holding ``_CORP_HOSTS``."""
    return _write_set("corp.txt", "".join(f"{host}\n" for host in _CORP_HOSTS))


def test_registry_shape() -> None:
    """Every static set carries hosts; only ``os-packages`` is dynamic (empty here)."""
    for name, hosts in EGRESS_SETS.items():
        assert name == name.lower()
        if name == OS_PACKAGES_SET:
            assert hosts == ()
        else:
            assert hosts, f"static set {name} has no hosts"
            assert len(hosts) == len(set(hosts)), f"duplicate hosts in {name}"


def test_recommended_shares_the_flat_namespace_without_shadowing_a_set() -> None:
    """Users write ``recommended`` like any set name, so no concrete set may take it."""
    assert RECOMMENDED_SET not in EGRESS_SETS


@pytest.mark.parametrize("names", [None, ()], ids=["unset", "empty"])
def test_selection_without_names_grants_nothing(names: tuple[str, ...] | None) -> None:
    """Unset and an explicit empty list both grant no curated set."""
    assert selected_egress_sets(names) == ()


def test_recommended_expands_to_every_set_in_registry_order() -> None:
    assert selected_egress_sets((RECOMMENDED_SET,)) == tuple(EGRESS_SETS)


def test_registry_is_read_only() -> None:
    """Nothing merges into the built-in registry at runtime — user sets live beside it."""
    with pytest.raises(TypeError):
        EGRESS_SETS["new-set"] = ("new.example.org",)  # type: ignore[index]


def test_recommended_never_includes_user_sets(corp_set: Path) -> None:
    """``recommended`` is the built-in sets only: a user set is granted by name alone."""
    assert selected_egress_sets((RECOMMENDED_SET,)) == tuple(EGRESS_SETS)
    assert not set(_CORP_HOSTS) & set(resolve_egress_sets((RECOMMENDED_SET,), "deb"))


def test_selection_drops_repeats_keeping_first_position() -> None:
    """``[recommended, python]`` names python twice; it is granted once."""
    assert selected_egress_sets((RECOMMENDED_SET, "python")) == tuple(EGRESS_SETS)
    others = tuple(n for n in EGRESS_SETS if n != "python")
    assert selected_egress_sets(("python", RECOMMENDED_SET)) == ("python", *others)


def test_explicit_selection_is_granted_as_listed() -> None:
    assert selected_egress_sets(("go", "python")) == ("go", "python")


def test_describe_renders_the_authored_value() -> None:
    """``recommended`` stays as written; unset and empty share one wording."""
    assert describe_egress_sets((RECOMMENDED_SET,)) == RECOMMENDED_SET
    assert describe_egress_sets(("python", "go")) == "python, go"
    assert describe_egress_sets(None) == describe_egress_sets(())
    assert describe_egress_sets(None).startswith("none")


def test_validate_accepts_known_recommended_and_none() -> None:
    validate_egress_sets(None)
    validate_egress_sets(())
    validate_egress_sets(tuple(EGRESS_SETS))
    validate_egress_sets((RECOMMENDED_SET, "python"))


def test_validate_rejects_unknown_with_available_names() -> None:
    """A typo fails loudly and spells out every accepted name, ``recommended`` included."""
    with pytest.raises(SystemExit, match="Available sets") as exc_info:
        validate_egress_sets(("pythn",))
    message = str(exc_info.value)
    assert RECOMMENDED_SET in message
    assert all(name in message for name in EGRESS_SETS)


def test_resolve_none_grants_no_hosts() -> None:
    """An unset ``shield.sets`` resolves to no curated hosts at all."""
    assert resolve_egress_sets(None, "deb") == ()


def test_resolve_empty_grants_no_hosts() -> None:
    assert resolve_egress_sets((), "rpm") == ()


def test_resolve_recommended_is_the_union_of_every_set() -> None:
    """``recommended`` resolves exactly what enumerating the whole registry resolves."""
    from terok.lib.integrations.executor import package_repo_hosts

    hosts = resolve_egress_sets((RECOMMENDED_SET,), "deb")
    assert hosts == resolve_egress_sets(tuple(EGRESS_SETS), "deb")
    for name, static_hosts in EGRESS_SETS.items():
        if name != OS_PACKAGES_SET:
            assert set(static_hosts) <= set(hosts)
    assert set(package_repo_hosts("deb")) <= set(hosts)


def test_resolve_subset_is_exactly_its_hosts() -> None:
    name = next(n for n in EGRESS_SETS if n != OS_PACKAGES_SET)
    assert resolve_egress_sets((name,), None) == EGRESS_SETS[name]


def test_resolve_os_packages_follows_family() -> None:
    """The dynamic set delegates to executor's family-keyed repo data."""
    from terok.lib.integrations.executor import package_repo_hosts

    assert resolve_egress_sets((OS_PACKAGES_SET,), "rpm") == package_repo_hosts("rpm")
    # Unrecognized image → the all-family union.
    assert resolve_egress_sets((OS_PACKAGES_SET,), None) == package_repo_hosts(None)


# ── The operator's own sets ─────────────────────────────────────────────


def test_egress_sets_dir_is_in_the_users_terok_config() -> None:
    """User sets sit beside the user's projects: ``$XDG_CONFIG_HOME/terok/egress-sets``."""
    assert egress_sets_dir() == Path(os.environ["XDG_CONFIG_HOME"]) / "terok" / "egress-sets"


def test_user_set_reads_one_host_per_line_with_comments() -> None:
    """Comments and blank lines drop out; names lower-case, repeats count once, IPs and CIDRs stay."""
    _write_set(
        "corp.txt",
        "# corporate mirrors\n"
        "\n"
        "Artifactory.Corp.Example   # build cache\n"
        f"{TEST_IP}\n"
        f"  {TEST_NET_CIDR}  \n"
        "artifactory.corp.example\n",
    )
    (corp,) = user_egress_sets()
    assert (corp.name, corp.origin, corp.error) == ("corp", USER_ORIGIN, None)
    assert corp.hosts == ("artifactory.corp.example", TEST_IP, TEST_NET_CIDR)
    assert corp.path == egress_sets_dir() / "corp.txt"


def test_catalog_lists_built_in_sets_then_user_sets_by_name() -> None:
    """Built-in sets in registry order first, then the user's own, alphabetically."""
    _write_set("zeta.txt", "zeta.example\n")
    _write_set("alpha.txt", "alpha.example\n")
    catalog = egress_set_catalog()
    assert [s.name for s in catalog] == [*EGRESS_SETS, "alpha", "zeta"]
    built_in = catalog[: len(EGRESS_SETS)]
    assert all(s.origin == BUILT_IN_ORIGIN and s.path is None for s in built_in)
    assert [s.name for s in built_in if s.dynamic] == [OS_PACKAGES_SET]
    assert all(s.origin == USER_ORIGIN and s.path is not None for s in catalog[len(EGRESS_SETS) :])


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("*.corp.example", id="wildcard"),
        pytest.param(f"pypi.org:{HTTPS_PORT}", id="port"),
        pytest.param("localhost", id="localhost"),
        pytest.param("+pypi.org", id="policy-allow-prefix"),
        pytest.param("-telemetry.example", id="policy-deny-prefix"),
        pytest.param("-pypi.org %reason=x", id="policy-line"),
        pytest.param("pypi.org files.pythonhosted.org", id="two-hosts"),
    ],
)
def test_a_line_that_is_not_one_host_makes_the_set_invalid(line: str) -> None:
    """The listing keeps the set, with no hosts and an error naming the line."""
    _write_set("corp.txt", f"ok.example\n{line}\n")
    (corp,) = user_egress_sets()
    assert corp.hosts == ()
    assert corp.error is not None and corp.error.startswith("line 2:")


def test_unusable_file_names_are_ignored_and_reported() -> None:
    """A built-in's name, a reserved word or a bad slug defines no set — and says why."""
    for name in ("python.txt", "recommended.txt", "none.txt", "Corp_Mirrors.txt"):
        _write_set(name, "x.example\n")
    _write_set(".hidden.txt", "x.example\n")  # dot-files and other suffixes pass silently
    _write_set("README.md", "notes\n")
    assert user_egress_sets() == ()
    reasons = {ignored.path.name: ignored.reason for ignored in ignored_user_set_files()}
    assert set(reasons) == {"python.txt", "recommended.txt", "none.txt", "Corp_Mirrors.txt"}
    assert "built-in" in reasons["python.txt"]
    assert "reserved" in reasons["none.txt"]
    assert "not a valid set name" in reasons["Corp_Mirrors.txt"]


def test_a_set_path_that_is_no_regular_file_is_reported() -> None:
    """A directory named like a set defines none, and says so instead of vanishing."""
    (egress_sets_dir() / "corp.txt").mkdir(parents=True)
    assert user_egress_sets() == ()
    (ignored,) = ignored_user_set_files()
    assert (ignored.path.name, ignored.reason) == ("corp.txt", "not a regular file")


def test_a_sets_directory_that_cannot_be_read_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sets, and one line saying why — a permission problem never reads as "you have none"."""
    egress_sets_dir().mkdir(parents=True)

    def unreadable(_self: Path) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "iterdir", unreadable)
    assert user_egress_sets() == ()
    (ignored,) = ignored_user_set_files()
    assert ignored.path == egress_sets_dir()
    assert ignored.reason.startswith("cannot be read: ")


def test_a_user_file_never_shadows_a_built_in_set() -> None:
    """``python.txt`` is ignored: ``python`` keeps granting the built-in hosts."""
    _write_set("python.txt", "evil.example\n")
    assert egress_set_hosts("python", None) == EGRESS_SETS["python"]


def test_validate_accepts_a_user_set(corp_set: Path) -> None:
    validate_egress_sets((RECOMMENDED_SET, "corp"))


def test_validate_names_a_missing_user_set_and_its_file() -> None:
    """Decision E: a project naming a user set with no file fails, naming set and path."""
    with pytest.raises(SystemExit) as exc_info:
        validate_egress_sets(("python", "corp"))
    message = str(exc_info.value)
    assert "'corp'" in message
    assert str(egress_sets_dir() / "corp.txt") in message
    assert "Available sets" in message


def test_validate_names_an_invalid_user_set_its_file_and_line() -> None:
    path = _write_set("corp.txt", "ok.example\n*.corp.example\n")
    with pytest.raises(SystemExit) as exc_info:
        validate_egress_sets(("corp",))
    message = str(exc_info.value)
    assert "'corp'" in message and str(path) in message and "line 2" in message


def test_validate_reports_every_bad_set_one_line_each_before_the_available_sets() -> None:
    path = _write_set("corp.txt", "*.corp.example\n")
    with pytest.raises(SystemExit) as exc_info:
        validate_egress_sets(("corp", "ghost"))
    corp_line, ghost_line, available_line = str(exc_info.value).splitlines()
    assert "'corp'" in corp_line and str(path) in corp_line
    assert "'ghost'" in ghost_line and str(egress_sets_dir() / "ghost.txt") in ghost_line
    assert available_line.startswith("Available sets: ")


def test_validate_lists_only_loadable_user_sets_as_available(corp_set: Path) -> None:
    """The available names include the user's valid sets, never a broken one."""
    _write_set("broken.txt", "*.x.example\n")
    with pytest.raises(SystemExit) as exc_info:
        validate_egress_sets(("pythn",))
    available = str(exc_info.value).rsplit("Available sets: ", 1)[1]
    assert "corp" in available.split(", ")
    assert "broken" not in available.split(", ")


def test_validate_points_none_at_the_empty_list() -> None:
    """``none`` is the CLI's spelling of no sets — in project.yml that is ``[]``."""
    with pytest.raises(SystemExit, match=r"sets: \[\]"):
        validate_egress_sets(("none",))


def test_resolve_includes_a_named_user_set(corp_set: Path) -> None:
    hosts = resolve_egress_sets(("python", "corp"), None)
    assert hosts == (*EGRESS_SETS["python"], *_CORP_HOSTS)


def test_resolve_fails_loudly_when_a_user_set_vanished(corp_set: Path) -> None:
    """A launch after the file went away refuses rather than granting less."""
    corp_set.unlink()
    with pytest.raises(SystemExit, match="corp"):
        resolve_egress_sets(("corp",), None)


def test_granting_egress_set_names_the_first_set_listing_the_host(corp_set: Path) -> None:
    assert granting_egress_set(_CORP_HOSTS[1], ("python", "corp"), None) == "corp"
    assert granting_egress_set("pypi.org", (RECOMMENDED_SET, "corp"), None) == "python"
    assert granting_egress_set("other.example", ("python", "corp"), None) is None


# ── Host grammar ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param(" PyPI.org ", "pypi.org", id="domain-lowercased"),
        pytest.param(TEST_IP, TEST_IP, id="ip"),
        pytest.param(TEST_NET_CIDR, TEST_NET_CIDR, id="cidr"),
    ],
)
def test_normalize_egress_host_accepts_shields_grammar(given: str, expected: str) -> None:
    assert normalize_egress_host(given) == expected


@pytest.mark.parametrize(
    "host",
    [
        "",
        "*.corp.example",
        "localhost",
        f"pypi.org:{HTTPS_PORT}",
        "+pypi.org",
        "-pypi.org",
        "two words.example",
    ],
)
def test_normalize_egress_host_rejects_what_is_not_one_host(host: str) -> None:
    with pytest.raises(ValueError):
        normalize_egress_host(host)


@pytest.mark.parametrize("name", ["python", "recommended", "none", "Corp", "-corp", "a_b"])
def test_user_set_path_refuses_names_a_user_set_cannot_take(name: str) -> None:
    with pytest.raises(ValueError, match="user egress set"):
        user_set_path(name)


# ── Adding to a user set ────────────────────────────────────────────────


def test_add_host_creates_a_new_set_file() -> None:
    """A new set gets its directory, a short header comment, and the host."""
    edit = add_host_to_user_set("corp", "Artifactory.Corp.Example")
    assert edit.changed and edit.path == egress_sets_dir() / "corp.txt"
    assert "Created" in edit.message
    text = edit.path.read_text(encoding="utf-8")
    assert text.startswith("#") and text.endswith("artifactory.corp.example\n")
    (corp,) = user_egress_sets()
    assert corp.hosts == ("artifactory.corp.example",)


def test_add_host_appends_and_leaves_the_rest_of_the_file_alone() -> None:
    """Only one line is appended: comments, order and spacing stay as written."""
    original = "# mirrors we trust\nzeta.example   # last one wins\n\nalpha.example"
    path = _write_set("corp.txt", original)  # no trailing newline
    edit = add_host_to_user_set("corp", "beta.example")
    assert edit.changed
    assert path.read_text(encoding="utf-8") == f"{original}\nbeta.example\n"


def test_add_host_already_in_the_set_writes_nothing(corp_set: Path) -> None:
    before = corp_set.read_text(encoding="utf-8")
    edit = add_host_to_user_set("corp", _CORP_HOSTS[0].upper())
    assert not edit.changed and "already" in edit.message
    assert corp_set.read_text(encoding="utf-8") == before


def test_add_host_appends_to_a_set_file_that_appears_during_the_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A set file written between the lookup and the create is appended to, never overwritten."""
    original = "# written by a second terok\nalpha.example\n"
    path = _write_set("corp.txt", original)
    real_read = egress_sets_mod._read
    missed: list[Path] = []

    def _read_missing_once(target: Path) -> str:
        """Report the file missing on the first look, as it was before the other writer."""
        if not missed:
            missed.append(target)
            raise FileNotFoundError(target)
        return real_read(target)

    monkeypatch.setattr(egress_sets_mod, "_read", _read_missing_once)

    edit = add_host_to_user_set("corp", "beta.example")

    assert missed == [path]
    assert edit.changed and "Added" in edit.message
    assert path.read_text(encoding="utf-8") == f"{original}beta.example\n"


def test_add_host_refuses_a_set_file_that_does_not_parse() -> None:
    """The broken file is the operator's to fix; appending would bury the problem."""
    path = _write_set("corp.txt", "*.corp.example\n")
    with pytest.raises(SystemExit, match="line 1"):
        add_host_to_user_set("corp", "ok.example")
    assert path.read_text(encoding="utf-8") == "*.corp.example\n"


def test_add_host_leaves_a_write_protected_set_file_alone() -> None:
    """A set file the operator write-protected is refused cleanly, naming what to add by hand."""
    path = _write_set("corp.txt", "alpha.example\n")
    path.chmod(0o444)
    if os.access(path, os.W_OK):  # root writes whatever the mode bits say
        pytest.skip("running as a user that can write a 0444 file")
    with pytest.raises(SystemExit) as exc_info:
        add_host_to_user_set("corp", "beta.example")
    message = str(exc_info.value)
    assert f"{path} was left as it was" in message
    assert "beta.example" in message and "by hand" in message
    assert path.read_text(encoding="utf-8") == "alpha.example\n"


def test_add_host_refuses_a_set_path_that_is_not_a_file() -> None:
    """A directory standing where the set file belongs is the operator's to clear."""
    path = egress_sets_dir() / "corp.txt"
    path.mkdir(parents=True)
    with pytest.raises(SystemExit) as exc_info:
        add_host_to_user_set("corp", "beta.example")
    message = str(exc_info.value)
    assert f"{path} was left as it was" in message
    assert "beta.example" in message and "by hand" in message
    assert path.is_dir()


def test_add_host_refuses_when_the_set_directory_cannot_be_made() -> None:
    """A new set whose directory the operator's permissions forbid is not a raw traceback."""
    config_home = egress_sets_dir().parent
    config_home.mkdir(parents=True, exist_ok=True)
    config_home.chmod(0o555)
    try:
        if os.access(config_home, os.W_OK):  # root writes whatever the mode bits say
            pytest.skip("running as a user that can write into a 0555 directory")
        with pytest.raises(SystemExit) as exc_info:
            add_host_to_user_set("corp", "beta.example")
    finally:
        config_home.chmod(0o755)
    message = str(exc_info.value)
    assert f"{egress_sets_dir() / 'corp.txt'} was left as it was" in message
    assert "beta.example" in message and "by hand" in message
    assert not egress_sets_dir().exists()


def test_add_host_refuses_to_create_a_set_in_a_write_protected_directory() -> None:
    """A set directory the operator write-protected is refused cleanly, naming the host."""
    directory = egress_sets_dir()
    directory.mkdir(parents=True)
    directory.chmod(0o555)
    try:
        if os.access(directory, os.W_OK):  # root writes whatever the mode bits say
            pytest.skip("running as a user that can write into a 0555 directory")
        with pytest.raises(SystemExit) as exc_info:
            add_host_to_user_set("corp", "beta.example")
    finally:
        directory.chmod(0o755)
    message = str(exc_info.value)
    assert f"{directory / 'corp.txt'} was left as it was" in message
    assert "beta.example" in message and "by hand" in message
    assert not (directory / "corp.txt").exists()


@pytest.mark.parametrize(
    ("name", "host"),
    [
        pytest.param("python", "ok.example", id="built-in-name"),
        pytest.param("corp", "*.corp.example", id="wildcard-host"),
    ],
)
def test_add_host_refuses_bad_input(name: str, host: str) -> None:
    with pytest.raises(ValueError):
        add_host_to_user_set(name, host)
    assert not (egress_sets_dir() / f"{name}.txt").exists()
