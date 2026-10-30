# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``project.yml`` egress-policy writers and their in-place edits.

The writers' promise is that an edit changes the one entry it is about and
nothing else: comments, flow lists, indentation, long lines and the file's
mode stay as the operator left them.  The strongest tests here compare the
written file byte for byte against the original with only that entry added.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

import pytest

from terok.lib.core.config import egress_sets_dir
from terok.lib.core.projects import (
    add_project_shield_allow,
    add_project_shield_override,
    discover_projects,
    egress_set_users,
    enable_project_shield_set,
    load_project,
    set_project_shield_sets,
)
from tests.test_utils import project_env, write_project
from tests.testnet import EGRESS_DOMAIN, HTTPS_PORT, TEST_IP, TEST_UPSTREAM_URL

_NAME = "proj-writer"

_HEAD = f'project:\n  name: {_NAME}   # the slug\ngit:\n  upstream_url: "{TEST_UPSTREAM_URL}"\n'

_TRAILER = (
    "\n"
    "# The gate polls upstream every 5 minutes; uncomment to change it.\n"
    "# gatekeeping:\n"
    "#   upstream_polling:\n"
    "#     interval_minutes: 5\n"
)

_HAND_WRITTEN = (
    _HEAD
    + "\n"
    + "shield:\n"
    + "  # Hand-picked sets.\n"
    + "  sets: [python]   # more later\n"
    + "  allow:\n"
    + "    - ftp.mirror.example.org   # mirror\n"
    + "  override:\n"
    + "    - host: api.example.org\n"
    + "      reason: direct endpoint testing, kept on one line however long it gets to be\n"
    + "      expires: 2099-12-31\n"
    + _TRAILER
)
"""A project.yml the way an operator writes one: comments everywhere, a flow
list, ``    - `` sequences, a long line, and a commented-out block at the end."""

_FUTURE = date(2099, 6, 30)


@contextmanager
def _project(text: str) -> Iterator[Path]:
    """Lay out project ``_NAME`` with *text* as its project.yml; yield the file."""
    with project_env(text, project_name=_NAME) as env:
        yield env.config_root / _NAME / "project.yml"


def _write_user_set(name: str, text: str) -> Path:
    """Write a user egress set into the test-isolated egress-sets directory."""
    directory = egress_sets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ── shield.allow ────────────────────────────────────────────────────────


def test_allow_appends_one_line_and_leaves_every_other_byte() -> None:
    with _project(_HAND_WRITTEN) as path:
        edit = add_project_shield_allow(_NAME, "New.Example.org")
        assert edit.changed and edit.path == path
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN.replace(
            "    - ftp.mirror.example.org   # mirror\n",
            "    - ftp.mirror.example.org   # mirror\n    - new.example.org\n",
        )
        assert load_project(_NAME).shield_allow == ("ftp.mirror.example.org", "new.example.org")


def test_allow_creates_the_shield_section_above_trailing_comments() -> None:
    """A project without ``shield:`` gets one after its content, before the commented-out tail."""
    before = _HEAD + _TRAILER
    with _project(before) as path:
        add_project_shield_allow(_NAME, "new.example.org")
        assert path.read_text(encoding="utf-8") == (
            _HEAD + "\nshield:\n  allow:\n    - new.example.org\n" + _TRAILER
        )


def test_allow_keeps_the_files_own_sequence_indentation() -> None:
    """A file written with dashes flush under their key keeps that style."""
    before = _HEAD + "shield:\n  allow:\n  - a.example.org\n  - b.example.org\n"
    with _project(before) as path:
        add_project_shield_allow(_NAME, TEST_IP)
        assert path.read_text(encoding="utf-8") == before + f"  - {TEST_IP}\n"


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        pytest.param("FTP.mirror.example.org", "in shield.allow already", id="listed"),
        pytest.param(EGRESS_DOMAIN, "git upstream host", id="upstream"),
        pytest.param("pypi.org", "enabled egress set 'python'", id="enabled-set"),
    ],
)
def test_allow_writes_nothing_for_a_host_granted_already(host: str, reason: str) -> None:
    with _project(_HAND_WRITTEN) as path:
        edit = add_project_shield_allow(_NAME, host)
        assert not edit.changed and reason in edit.message
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN


def test_allow_sees_a_user_set_as_granting() -> None:
    _write_user_set("corp", "artifactory.corp.example\n")
    with _project(_HEAD + "shield:\n  sets: [corp]\n") as path:
        edit = add_project_shield_allow(_NAME, "artifactory.corp.example")
        assert not edit.changed and "'corp'" in edit.message
        assert path.read_text(encoding="utf-8") == _HEAD + "shield:\n  sets: [corp]\n"


@pytest.mark.parametrize("host", ["*.example.org", "localhost", f"pypi.org:{HTTPS_PORT}", ""])
def test_allow_refuses_what_is_not_one_host(host: str) -> None:
    with _project(_HAND_WRITTEN) as path:
        with pytest.raises(ValueError):
            add_project_shield_allow(_NAME, host)
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN


# ── shield.sets ─────────────────────────────────────────────────────────


def test_enable_appends_to_a_flow_list_in_place() -> None:
    with _project(_HAND_WRITTEN) as path:
        edit = enable_project_shield_set(_NAME, "go")
        assert edit.changed
        lines = path.read_text(encoding="utf-8").splitlines()
        sets_line = next(line for line in lines if line.lstrip().startswith("sets:"))
        assert sets_line.startswith("  sets: [python, go]") and sets_line.endswith("# more later")
        assert [line for line in lines if line != sets_line] == [
            line for line in _HAND_WRITTEN.splitlines() if not line.lstrip().startswith("sets:")
        ]
        assert load_project(_NAME).shield_sets == ("python", "go")


def test_enable_on_an_unset_selection_starts_the_list() -> None:
    with _project(_HEAD) as path:
        enable_project_shield_set(_NAME, "python")
        assert load_project(_NAME).shield_sets == ("python",)
        assert path.read_text(encoding="utf-8").startswith(_HEAD)


def test_enable_a_built_in_set_under_recommended_writes_nothing() -> None:
    before = _HEAD + "shield:\n  sets: [recommended]\n"
    with _project(before) as path:
        edit = enable_project_shield_set(_NAME, "python")
        assert not edit.changed and "recommended" in edit.message
        assert path.read_text(encoding="utf-8") == before


def test_enable_a_user_set_next_to_recommended_adds_it() -> None:
    """``recommended`` never covers a user set, so it joins the list."""
    _write_user_set("corp", "artifactory.corp.example\n")
    with _project(_HEAD + "shield:\n  sets: [recommended]\n"):
        assert enable_project_shield_set(_NAME, "corp").changed
        assert load_project(_NAME).shield_sets == ("recommended", "corp")


def test_enable_an_enabled_set_writes_nothing() -> None:
    with _project(_HAND_WRITTEN) as path:
        assert not enable_project_shield_set(_NAME, "python").changed
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN


def test_enable_a_missing_user_set_fails_naming_its_file() -> None:
    with _project(_HAND_WRITTEN) as path:
        with pytest.raises(SystemExit) as exc_info:
            enable_project_shield_set(_NAME, "corp")
        assert str(egress_sets_dir() / "corp.txt") in str(exc_info.value)
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN


def test_set_project_shield_sets_refills_the_list_in_place() -> None:
    """The picker's and ``--set``'s writer keeps the list's style and the file around it."""
    with _project(_HAND_WRITTEN) as path:
        set_project_shield_sets(_NAME, ("recommended",))
        before = _HAND_WRITTEN.splitlines()
        at = before.index("  sets: [python]   # more later")
        lines = path.read_text(encoding="utf-8").splitlines()
        # The end-of-line comment keeps its column while the list is shorter,
        # and moves right (one space after it) once the list outgrows it.
        assert re.fullmatch(r"  sets: \[recommended\] +# more later", lines[at])
        assert lines[:at] + lines[at + 1 :] == before[:at] + before[at + 1 :]
        assert load_project(_NAME).shield_sets == ("recommended",)


# ── shield.override ─────────────────────────────────────────────────────


def test_override_appends_an_entry_above_the_trailing_comments() -> None:
    with _project(_HAND_WRITTEN) as path:
        edit = add_project_shield_override(_NAME, "break.example.org", "vendor outage", _FUTURE)
        assert edit.changed and _FUTURE.isoformat() in edit.message
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN.replace(
            "      expires: 2099-12-31\n",
            "      expires: 2099-12-31\n"
            "    - host: break.example.org\n"
            "      reason: vendor outage\n"
            f"      expires: {_FUTURE.isoformat()}\n",
        )
        override = load_project(_NAME).shield_override[-1]
        assert (override.host, override.reason, override.expires) == (
            "break.example.org",
            "vendor outage",
            _FUTURE,
        )


def test_override_is_valid_through_its_date_so_today_is_accepted() -> None:
    with _project(_HEAD):
        add_project_shield_override(_NAME, "break.example.org", "vendor outage", date.today())
        assert load_project(_NAME).shield_override[0].expires == date.today()


@pytest.mark.parametrize(
    ("reason", "expires", "match"),
    [
        pytest.param("  ", _FUTURE, "reason", id="blank-reason"),
        pytest.param("vendor outage", date.today() - timedelta(days=1), "past", id="past-expiry"),
    ],
)
def test_override_requires_a_reason_and_a_current_expiry(
    reason: str, expires: date, match: str
) -> None:
    with _project(_HAND_WRITTEN) as path:
        with pytest.raises(ValueError, match=match):
            add_project_shield_override(_NAME, "break.example.org", reason, expires)
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN


def test_override_for_a_listed_host_is_updated_in_place() -> None:
    """An existing entry keeps its place and comments; the result names both changes."""
    with _project(_HAND_WRITTEN) as path:
        edit = add_project_shield_override(_NAME, "API.example.org", "re-test", _FUTURE)
        assert edit.changed and "re-test" in edit.message and _FUTURE.isoformat() in edit.message
        text = path.read_text(encoding="utf-8")
        assert text.count("host: api.example.org") == 1
        assert "      reason: re-test\n" in text
        assert f"      expires: {_FUTURE.isoformat()}\n" in text
        assert text.endswith(_TRAILER)


def test_override_identical_to_the_listed_one_writes_nothing() -> None:
    with _project(_HAND_WRITTEN) as path:
        edit = add_project_shield_override(
            _NAME,
            "api.example.org",
            "direct endpoint testing, kept on one line however long it gets to be",
            date(2099, 12, 31),
        )
        assert not edit.changed
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN


# ── The file itself ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_a_write_keeps_the_files_mode(mode: int) -> None:
    with _project(_HAND_WRITTEN) as path:
        path.chmod(mode)
        add_project_shield_allow(_NAME, "new.example.org")
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_a_write_through_a_symlink_updates_its_target() -> None:
    """A project.yml symlinked from a dotfiles checkout stays a symlink."""
    with _project(_HAND_WRITTEN) as path:
        target = path.parent.parent / "dotfiles-project.yml"
        path.rename(target)
        path.symlink_to(target)
        add_project_shield_allow(_NAME, "new.example.org")
        assert path.is_symlink()
        assert "new.example.org" in target.read_text(encoding="utf-8")


_A_LINE = "    - a.example.org\n"
_A_AND_B = "    - a.example.org\n    - b.example.org\n"


@pytest.mark.parametrize(
    "before",
    [
        pytest.param(
            _HEAD + "run:\n  podman_args:\n  - --pids-limit=500\nshield:\n  allow:\n" + _A_LINE,
            id="another-list-with-flush-dashes",
        ),
        pytest.param(
            _HEAD
            + "gate:\n  enabled: True\nimage:\n  family: ~\nrun:\n  cpus: null\n"
            + "shield:\n  allow:\n"
            + _A_LINE,
            id="spellings-a-dump-would-change",
        ),
        pytest.param(
            _HEAD
            + "image:\n    user_snippet_inline: |\n        RUN echo hi\n    family: deb\n"
            + "shield:\n  allow:\n"
            + _A_LINE,
            id="a-section-indented-its-own-way",
        ),
        pytest.param(
            "%YAML 1.2\n---\n" + _HEAD + "shield:\n  allow:\n" + _A_LINE + "...\n",
            id="directive-and-document-end",
        ),
        pytest.param(_HEAD + "shield:\n  allow:\n" + _A_LINE.rstrip("\n"), id="no-final-newline"),
    ],
)
def test_an_append_changes_one_line_and_no_other_byte(before: str) -> None:
    """Lines the edit does not touch are never re-emitted, however a dump would spell them."""
    with _project(before) as path:
        add_project_shield_allow(_NAME, "b.example.org")
        after = path.read_text(encoding="utf-8")
    if before.endswith(_A_LINE.rstrip("\n")):
        assert after == before + "\n    - b.example.org"
    else:
        assert after == before.replace(_A_LINE, _A_AND_B)


def test_a_new_section_leaves_the_last_keys_commented_out_body_with_it() -> None:
    """``run:`` holding only commented-out keys keeps them; the section goes below them.

    Ruamel files those lines among the document's closing comments, so an
    edit made through the loaded document would land between ``run:`` and
    its own commented-out body.
    """
    before = _HEAD + "run:\n  # gpus: all\n" + _TRAILER
    with _project(before) as path:
        add_project_shield_allow(_NAME, "b.example.org")
        after = path.read_text(encoding="utf-8")
    assert after == (
        _HEAD + "run:\n  # gpus: all\n" + "\nshield:\n  allow:\n    - b.example.org\n" + _TRAILER
    )


def test_a_new_section_keeps_the_files_trailing_blank_line() -> None:
    before = _HEAD + "run:\n  gpus: all\n\n"
    with _project(before) as path:
        enable_project_shield_set(_NAME, "python")
        after = path.read_text(encoding="utf-8")
    assert after == _HEAD + "run:\n  gpus: all\n\nshield:\n  sets:\n    - python\n\n"


def test_a_crlf_file_stays_crlf() -> None:
    """The file's own line ends survive, on the old lines and the new one."""
    before = (_HEAD + "shield:\n  allow:\n" + _A_LINE).replace("\n", "\r\n")
    with _project(before) as path:
        add_project_shield_allow(_NAME, "b.example.org")
        after = path.read_bytes().decode("utf-8")
    assert after == before + "    - b.example.org\r\n"


def test_a_new_section_in_a_crlf_file_is_crlf_too() -> None:
    before = (_HEAD + "gate:\n  enabled: True\n").replace("\n", "\r\n")
    with _project(before) as path:
        add_project_shield_allow(_NAME, "b.example.org")
        after = path.read_bytes().decode("utf-8")
    assert after == before + "\r\nshield:\r\n  allow:\r\n    - b.example.org\r\n"


def test_a_refilled_list_leaves_its_neighbours_spelled_as_they_were() -> None:
    """Only the ``sets`` line is the edit's; the ``allow`` line beside it keeps its spaces."""
    before = _HEAD + "shield:\n  allow: [ a.example.org ]\n  sets: [ python ]   # mine\n"
    with _project(before) as path:
        set_project_shield_sets(_NAME, ("go", "rust"))
        after = path.read_text(encoding="utf-8")
        assert load_project(_NAME).shield_sets == ("go", "rust")
    assert after.startswith(_HEAD + "shield:\n  allow: [ a.example.org ]\n")
    assert re.fullmatch(r"  sets: \[go, rust\] +# mine", after.splitlines()[-1])


def test_an_edit_that_cannot_be_placed_is_refused_naming_the_change() -> None:
    """When the lines around the change are not written as terok would write them, it stops.

    Rewriting them would reformat what the operator wrote, so nothing is
    written and the change is named for them to make by hand.
    """
    before = (
        _HEAD
        + "shield:\n  allow: [ a.example.org,\n           c.example.org ]\n"
        + "  sets: [ python ]\ngate:\n  enabled: true\n"
    )
    with _project(before) as path:
        with pytest.raises(SystemExit) as exc_info:
            add_project_shield_allow(_NAME, "b.example.org")
        assert path.read_text(encoding="utf-8") == before
    message = str(exc_info.value)
    assert "left as it was" in message
    assert "by hand" in message and "b.example.org" in message


def test_a_section_spelled_null_is_refused_naming_the_change() -> None:
    """``shield: null`` is the operator's line, and no block goes under it as written.

    Rewriting it is the operator's call, so the file is left as it was, and
    the refusal is the writer's own — never a parse error from the result.
    """
    before = _HEAD + "shield: null\n"
    with _project(before) as path:
        with pytest.raises(SystemExit) as exc_info:
            add_project_shield_allow(_NAME, "b.example.org")
        assert path.read_text(encoding="utf-8") == before
    message = str(exc_info.value)
    assert "left as it was" in message
    assert "by hand" in message and "b.example.org" in message


def test_a_write_protected_file_is_left_alone() -> None:
    """The rename needs only the directory; the file's own protection is still the operator's."""
    with _project(_HAND_WRITTEN) as path:
        path.chmod(0o444)
        if os.access(path, os.W_OK):  # root writes whatever the mode bits say
            pytest.skip("running as a user that can write a 0444 file")
        with pytest.raises(SystemExit) as exc_info:
            add_project_shield_allow(_NAME, "new.example.org")
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert "write-protected" in str(exc_info.value)
    assert "new.example.org" in str(exc_info.value)


def test_a_write_that_would_not_load_is_refused() -> None:
    """The result is validated like a load before it replaces the file."""
    before = _HEAD + "shield:\n  allow: [a.example.org]\n  bogus_key: 1\n"
    with _project(before) as path:
        with pytest.raises(SystemExit, match="bogus_key"):
            enable_project_shield_set(_NAME, "python")
        assert path.read_text(encoding="utf-8") == before


# ── Loading a project that names a user set ────────────────────────────


def test_a_project_naming_a_user_set_loads_with_its_hosts() -> None:
    _write_user_set("corp", "artifactory.corp.example\n")
    with _project(_HEAD + "shield:\n  sets: [recommended, corp]\n"):
        assert load_project(_NAME).shield_sets == ("recommended", "corp")


@pytest.mark.parametrize(
    ("set_file", "expected"),
    [
        pytest.param(None, "no user set file at", id="missing"),
        pytest.param("ok.example\n*.corp.example\n", "line 2", id="invalid"),
    ],
)
def test_a_project_naming_a_broken_user_set_fails_to_load(
    set_file: str | None, expected: str
) -> None:
    """Decision E: the load fails naming the set and its file, and the project lists as broken."""
    if set_file is not None:
        _write_user_set("corp", set_file)
    with _project(_HEAD + "shield:\n  sets: [corp]\n"):
        with pytest.raises(SystemExit) as exc_info:
            load_project(_NAME)
        message = str(exc_info.value)
        assert "'corp'" in message and expected in message
        assert str(egress_sets_dir() / "corp.txt") in message
        _valid, broken = discover_projects()
        assert [b.name for b in broken] == [_NAME]


# ── Who uses a set ──────────────────────────────────────────────────────


def test_egress_set_users_lists_explicit_mentions_even_of_broken_projects() -> None:
    """A project failing to load over a missing user set still counts as its user."""
    _write_user_set("corp", "artifactory.corp.example\n")
    with project_env(_HEAD + "shield:\n  sets: [corp, python]\n", project_name=_NAME) as env:
        write_project(env.config_root, "other", "shield:\n  sets: [recommended, corp, gone]\n")
        write_project(env.config_root, "plain", "shield:\n  sets: [recommended]\n")
        users = egress_set_users()
    assert users["corp"] == ("other", _NAME)
    assert users["gone"] == ("other",)
    assert users["recommended"] == ("other", "plain")
    assert "python" in users and "go" not in users


def test_set_project_shield_sets_with_the_same_sets_writes_nothing() -> None:
    """Refilling the list with what it already holds leaves the file untouched."""
    before = _HEAD + "shield:\n  sets: [python, go]   # mine\n"
    with _project(before) as path:
        set_project_shield_sets(_NAME, ("python", "go"))
        assert path.read_text(encoding="utf-8") == before


def test_override_takes_a_datetime_as_its_day() -> None:
    """A timestamp grants through its calendar day, like the date it falls on."""
    with _project(_HEAD):
        add_project_shield_override(
            _NAME, "break.example.org", "vendor outage", datetime.combine(_FUTURE, time(12))
        )
        assert load_project(_NAME).shield_override[0].expires == _FUTURE


def test_egress_set_users_skips_a_project_whose_file_does_not_parse() -> None:
    """A project.yml that is no YAML at all names no set — and stops nobody else's count."""
    with project_env(_HEAD + "shield:\n  sets: [python]\n", project_name=_NAME) as env:
        write_project(env.config_root, "garbled", "shield: [python\n")
        users = egress_set_users()
    assert users["python"] == (_NAME,)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        pytest.param(
            _HEAD + "shield:\n  sets: python\n", "shield.sets is not a list", id="sets-scalar"
        ),
        pytest.param(_HEAD + "shield: [python]\n", "shield is not a mapping", id="shield-list"),
        pytest.param("- just\n- a list\n", "expected a mapping", id="document-list"),
        pytest.param(_HEAD + "shield: [python\n", "Failed to read", id="no-yaml"),
    ],
)
def test_a_file_the_writer_cannot_read_as_a_shield_section_is_left_alone(
    text: str, match: str
) -> None:
    """Whatever the operator wrote that is not a shield mapping is theirs to fix, byte for byte."""
    with _project(text) as path:
        with pytest.raises(SystemExit, match=match):
            enable_project_shield_set(_NAME, "go")
        assert path.read_text(encoding="utf-8") == text


def test_a_file_of_comments_alone_gets_the_section_below_them() -> None:
    """ruamel keeps no comment of a document without content, so the section goes after them."""
    before = "# my project, configured later\n"
    with _project(before) as path:
        add_project_shield_allow(_NAME, "b.example.org")
        after = path.read_text(encoding="utf-8")
    assert after.startswith(before + "\n")
    assert "shield:" in after and "b.example.org" in after


def test_a_write_that_fails_part_way_leaves_the_file_and_no_temp_behind() -> None:
    """The temp file is renamed over the target or removed — never left beside it."""
    with _project(_HAND_WRITTEN) as path:
        with (
            mock.patch.object(Path, "replace", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            add_project_shield_allow(_NAME, "b.example.org")
        assert path.read_text(encoding="utf-8") == _HAND_WRITTEN
        assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
