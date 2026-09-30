# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""``terok shield promote`` and ``terok shield export`` — terok's own verbs beside shield's.

The engine is tested on its own; these tests pin what the command line adds:
how ``--to`` and ``--expires`` parse, which flag combinations it refuses
(argparse cannot express them), what the review listing and the ready-to-run
commands look like, how a result or a refusal reaches the operator, and where
an export is written.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from terok.cli.commands.shield import dispatch, register
from terok.lib.api.shield import (
    REMEDY_OVERRIDE,
    REMEDY_PROJECT,
    REMEDY_SET,
    REMEDY_TASK,
    TIER_ALLOWLIST,
    TIER_HARD_DENY,
    TIER_SECURITY,
    AllowResult,
    DnsTier,
    EgressCandidate,
    EgressReview,
    HarvestEntry,
    HarvestReport,
    PromotionRefused,
    PromotionResult,
    SetSuggestion,
    tier_heading,
)
from terok.lib.core.projects import load_project
from tests.test_utils import project_env
from tests.testfs import MOCK_BASE
from tests.testnet import CLOUD_METADATA_IP, HTTPS_PORT, TEST_IP, TEST_UPSTREAM_URL

_API = "terok.lib.api.shield"
_PROJECT = "proj"
_TASK = "t1"
"""The task id as typed on the command line."""
_RESOLVED = f"{_TASK}-resolved"
"""What ``resolve_task_id`` makes of it — distinct, so forwarding the typed id cannot pass."""
_UNTIL = date.today() + timedelta(days=7)
_SEEN = "2026-10-02T11:00:00+00:00"


@pytest.fixture()
def parser() -> argparse.ArgumentParser:
    """An argument parser with the shield subcommands registered."""
    root = argparse.ArgumentParser()
    register(root.add_subparsers(dest="cmd"))
    return root


@pytest.fixture(autouse=True)
def _stub_resolve_task_id() -> Iterator[MagicMock]:
    """Resolve ``_TASK`` to ``_RESOLVED`` — these tests use no task store.

    The resolved id differs from the typed one, so every engine call below
    pins that the command hands on what the resolver returned.
    """
    with patch(
        "terok.cli.commands.shield.resolve_task_id",
        side_effect=lambda _p, tid: _RESOLVED if tid == _TASK else tid,
    ) as stub:
        yield stub


def _run(parser: argparse.ArgumentParser, *argv: str) -> tuple[str, str, int | None]:
    """Dispatch ``terok shield *argv``; return stdout, stderr and the exit status (None: no exit)."""
    args = parser.parse_args(["shield", *argv])
    code: int | None = None
    with (
        patch("sys.stdout", new_callable=StringIO) as out,
        patch("sys.stderr", new_callable=StringIO) as err,
    ):
        try:
            assert dispatch(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return out.getvalue(), err.getvalue(), code


def _candidate(
    target: str, tier: str, remedies: tuple[str, ...], **extra: object
) -> EgressCandidate:
    """A review candidate seen three times on the HTTPS port."""
    fields: dict[str, object] = {
        "target": target,
        "tier": tier,
        "remedies": remedies,
        "actions": ("blocked",),
        "count": 3,
        "first_seen": _SEEN,
        "last_seen": _SEEN,
        "ports": (HTTPS_PORT,),
        "addresses": (),
        **extra,
    }
    return EgressCandidate(**fields)  # type: ignore[arg-type]


_REVIEW = EgressReview(
    project=_PROJECT,
    task=_TASK,
    candidates=(
        _candidate(
            "pypi.org",
            TIER_ALLOWLIST,
            (REMEDY_TASK, REMEDY_PROJECT, REMEDY_SET),
            suggested_set="python",
        ),
        _candidate(
            "granted.example.org",
            TIER_ALLOWLIST,
            (REMEDY_TASK, REMEDY_PROJECT, REMEDY_SET),
            covered_by="shield.allow",
        ),
        _candidate("api.vendor.example", TIER_SECURITY, (REMEDY_OVERRIDE,)),
        _candidate(CLOUD_METADATA_IP, TIER_HARD_DENY, ()),
    ),
    suggestions=(SetSuggestion("python", "built-in", ("pypi.org",), 2, 2),),
    audit_enabled=True,
    dns_tier="dnsmasq-live",
    domain_attribution=True,
    suffix_match=True,
)


# ── Parsing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("to", "expected"),
    [
        pytest.param("task", (REMEDY_TASK, None), id="task"),
        pytest.param("project", (REMEDY_PROJECT, None), id="project"),
        pytest.param("override", (REMEDY_OVERRIDE, None), id="override"),
        pytest.param("set:python", (REMEDY_SET, "python"), id="set"),
    ],
)
def test_to_parses_each_destination(
    parser: argparse.ArgumentParser, to: str, expected: tuple[str, str | None]
) -> None:
    args = parser.parse_args(["shield", "promote", _PROJECT, _TASK, "--to", to])
    assert args.promote_to == expected


@pytest.mark.parametrize("to", ["set", "set:", "bogus", "task:x", "host_port"])
def test_to_refuses_what_is_no_destination(parser: argparse.ArgumentParser, to: str) -> None:
    with patch("sys.stderr", new_callable=StringIO), pytest.raises(SystemExit):
        parser.parse_args(["shield", "promote", _PROJECT, _TASK, "--to", to])


def test_expires_takes_a_calendar_date_only(parser: argparse.ArgumentParser) -> None:
    args = parser.parse_args(
        ["shield", "promote", _PROJECT, _TASK, "--expires", _UNTIL.isoformat()]
    )
    assert args.promote_expires == _UNTIL
    with patch("sys.stderr", new_callable=StringIO), pytest.raises(SystemExit):
        parser.parse_args(["shield", "promote", _PROJECT, _TASK, "--expires", "+30d"])


def test_export_parses_its_flags(parser: argparse.ArgumentParser) -> None:
    output = str(MOCK_BASE / "out.json")
    args = parser.parse_args(
        ["shield", "export", _PROJECT, "--task", _TASK, "--scrub", "--output", output]
    )
    assert (args.export_task, args.scrub, args.export_output) == (_TASK, True, output)


# ── Flag rules argparse cannot express ─────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "words"),
    [
        pytest.param(["--target", "h", "--to", "override"], "--reason and --expires", id="bare"),
        pytest.param(
            ["--target", "h", "--to", "override", "--expires", _UNTIL.isoformat()],
            "needs --reason",
            id="no-reason",
        ),
        pytest.param(
            ["--target", "h", "--to", "override", "--reason", " ", "--expires", _UNTIL.isoformat()],
            "needs --reason",
            id="blank-reason",
        ),
        pytest.param(["--target", "h", "--to", "task", "--reason", "x"], "override only", id="rsn"),
        pytest.param(["--expires", _UNTIL.isoformat()], "override only", id="expires-alone"),
        pytest.param(["--to", "project"], "needs --target", id="no-target"),
    ],
)
def test_flags_that_do_not_fit_the_destination_are_a_usage_error(
    parser: argparse.ArgumentParser, argv: list[str], words: str
) -> None:
    """Exit 2, like argparse — and nothing is reviewed or promoted."""
    with (
        patch(f"{_API}.promote_egress") as promote,
        patch(f"{_API}.review_egress") as review,
    ):
        _out, err, code = _run(parser, "promote", _PROJECT, _TASK, *argv)
    assert code == 2
    assert words in err
    promote.assert_not_called()
    review.assert_not_called()


# ── The review listing ─────────────────────────────────────────────────


def test_the_listing_groups_by_tier_with_ready_to_run_commands(
    parser: argparse.ArgumentParser,
) -> None:
    with patch(f"{_API}.review_egress", return_value=_REVIEW) as review:
        out, _err, code = _run(parser, "promote", _PROJECT, _TASK)
    review.assert_called_once_with(_PROJECT, _RESOLVED)
    assert code is None
    command = f"terok shield promote {_PROJECT} {_TASK}"
    assert out.index("Suggested egress sets") < out.index(tier_heading(TIER_ALLOWLIST))
    assert f"{command} --to set:python" in out
    for flags in ("--to task", "--to project", "--to set:python"):
        assert f"{command} --target pypi.org {flags}" in out
    assert "--to set:NAME" in out  # granted.example.org has no suggested set
    assert "already granted by shield.allow" in out
    assert tier_heading(TIER_SECURITY) in out
    assert (
        f"{command} --target api.vendor.example --to override --reason 'WHY' "
        "--expires YYYY-MM-DD" in out
    )
    assert "--target api.vendor.example --to task" not in out
    assert "nothing reaches it" in out  # the floor offers no command, only why


def test_the_listing_narrows_to_one_target(parser: argparse.ArgumentParser) -> None:
    with patch(f"{_API}.review_egress", return_value=_REVIEW):
        out, _err, _code = _run(parser, "promote", _PROJECT, _TASK, "--target", "PyPI.org")
    assert "pypi.org" in out
    assert "api.vendor.example" not in out
    assert "Suggested egress sets" not in out


def test_the_listing_of_an_unknown_target_fails(parser: argparse.ArgumentParser) -> None:
    with patch(f"{_API}.review_egress", return_value=_REVIEW):
        _out, err, code = _run(parser, "promote", _PROJECT, _TASK, "--target", "nope.example")
    assert code == 1
    assert "not among what task" in err


def test_the_listing_as_json(parser: argparse.ArgumentParser) -> None:
    with patch(f"{_API}.review_egress", return_value=_REVIEW):
        out, _err, _code = _run(parser, "promote", _PROJECT, _TASK, "--json")
    data = json.loads(out)
    assert [c["target"] for c in data["candidates"]][:1] == ["pypi.org"]
    assert data["suggestions"][0]["set"] == "python"


def test_an_empty_listing_says_why(parser: argparse.ArgumentParser) -> None:
    empty = EgressReview(_PROJECT, _TASK, (), (), False, "dnsmasq-live", True)
    with patch(f"{_API}.review_egress", return_value=empty):
        out, _err, _code = _run(parser, "promote", _PROJECT, _TASK)
    assert "audit was off" in out


# ── Promoting ──────────────────────────────────────────────────────────


def test_a_promotion_reaches_the_engine_and_reports(parser: argparse.ArgumentParser) -> None:
    result = PromotionResult(
        "api.vendor.example",
        REMEDY_OVERRIDE,
        notes=("Added a break-glass override",),
        warnings=("Not opened on the running task now",),
    )
    with patch(f"{_API}.promote_egress", return_value=result) as promote:
        out, err, code = _run(
            parser,
            "promote",
            _PROJECT,
            _TASK,
            "--target",
            "api.vendor.example",
            "--to",
            "override",
            "--reason",
            "vendor outage",
            "--expires",
            _UNTIL.isoformat(),
        )
    promote.assert_called_once_with(
        _PROJECT,
        _RESOLVED,
        "api.vendor.example",
        REMEDY_OVERRIDE,
        set_name=None,
        reason="vendor outage",
        expires=_UNTIL,
    )
    assert code is None
    assert "Added a break-glass override" in out
    assert "Warning: Not opened" in err


def test_a_set_promotion_without_a_target_enables_the_set(parser: argparse.ArgumentParser) -> None:
    result = PromotionResult(None, REMEDY_SET, set_name="python", notes=("Enabled",))
    with patch(f"{_API}.promote_egress", return_value=result) as promote:
        _run(parser, "promote", _PROJECT, _TASK, "--to", "set:python")
    promote.assert_called_once_with(
        _PROJECT, _RESOLVED, None, REMEDY_SET, set_name="python", reason=None, expires=None
    )


def test_a_refused_promotion_exits_1_with_the_reason(parser: argparse.ArgumentParser) -> None:
    with patch(f"{_API}.promote_egress", side_effect=PromotionRefused("break-glass only")):
        _out, err, code = _run(parser, "promote", _PROJECT, _TASK, "--target", "h", "--to", "task")
    assert code == 1
    assert "Error: break-glass only" in err


def test_a_promotion_that_cannot_write_exits_1_and_says_earlier_writes_stand(
    parser: argparse.ArgumentParser,
) -> None:
    with patch(f"{_API}.promote_egress", side_effect=PermissionError("project.yml is read-only")):
        _out, err, code = _run(parser, "promote", _PROJECT, _TASK, "--target", "h", "--to", "task")
    assert code == 1
    assert "project.yml is read-only" in err
    assert "anything written before it stays" in err
    assert "Traceback" not in err


def test_a_promotion_that_granted_nothing_exits_1(parser: argparse.ArgumentParser) -> None:
    failed = PromotionResult("h", REMEDY_TASK, warnings=("took none",), failed=True)
    with patch(f"{_API}.promote_egress", return_value=failed):
        _out, err, code = _run(parser, "promote", _PROJECT, _TASK, "--target", "h", "--to", "task")
    assert code == 1
    assert "took none" in err


def test_a_promotion_result_as_json(parser: argparse.ArgumentParser) -> None:
    result = PromotionResult("h", REMEDY_TASK, notes=("Allowed",), live=True)
    with patch(f"{_API}.promote_egress", return_value=result):
        out, _err, _code = _run(
            parser, "promote", _PROJECT, _TASK, "--target", "h", "--to", "task", "--json"
        )
    assert json.loads(out) | {"notes": None} == result.as_dict() | {"notes": None}


def test_the_command_line_drives_the_real_engine(parser: argparse.ArgumentParser) -> None:
    """End to end below the shield: the CLI's promotion lands in the project's file."""
    entry = HarvestEntry("blocked", "svc.example.org", 1, _SEEN, _SEEN, (HTTPS_PORT,), ())
    manager = MagicMock()
    manager.harvest.return_value = HarvestReport(entries=(entry,))
    manager.dns_tier = DnsTier.DNSMASQ_LIVE
    manager.allow.return_value = AllowResult(target="svc.example.org", ips=(TEST_IP,))
    engine = "terok.lib.domain.egress_promotion"
    yml = f'project:\n  name: {_PROJECT}\ngit:\n  upstream_url: "{TEST_UPSTREAM_URL}"\n'
    with (
        project_env(yml, project_name=_PROJECT),
        patch(f"{engine}.task_shield_manager", return_value=manager),
        patch(f"{engine}.load_task_meta", return_value=({"mode": "cli"}, MOCK_BASE / "meta")),
        patch(f"{engine}.get_task_container_state", return_value="running"),
    ):
        out, _err, code = _run(
            parser, "promote", _PROJECT, _TASK, "--target", "svc.example.org", "--to", "project"
        )
        assert load_project(_PROJECT).shield_allow == ("svc.example.org",)
    assert code is None
    assert "Added svc.example.org to shield.allow" in out


# ── Export ─────────────────────────────────────────────────────────────

_DOCUMENT = {"format": "terok-egress-export", "task_count": 1, "entries": [{"target": "a"}]}


def test_export_writes_json_to_stdout(parser: argparse.ArgumentParser) -> None:
    with patch(f"{_API}.export_egress", return_value=_DOCUMENT) as export:
        out, _err, code = _run(parser, "export", _PROJECT)
    export.assert_called_once_with(_PROJECT, None, scrub=False)
    assert code is None
    assert json.loads(out) == _DOCUMENT


def test_export_to_a_file_for_one_task_scrubbed(
    parser: argparse.ArgumentParser, tmp_path: Path, _stub_resolve_task_id: MagicMock
) -> None:
    target = tmp_path / "harvest.json"
    with patch(f"{_API}.export_egress", return_value=_DOCUMENT) as export:
        out, _err, code = _run(
            parser, "export", _PROJECT, "--task", _TASK, "--scrub", "--output", str(target)
        )
    _stub_resolve_task_id.assert_called_with(_PROJECT, _TASK)
    export.assert_called_once_with(_PROJECT, (_RESOLVED,), scrub=True)
    assert code is None
    assert json.loads(target.read_text(encoding="utf-8")) == _DOCUMENT
    assert "Wrote 1 targets from 1 tasks (scrubbed)" in out


def test_export_to_an_unwritable_file_fails(
    parser: argparse.ArgumentParser, tmp_path: Path
) -> None:
    with patch(f"{_API}.export_egress", return_value=_DOCUMENT):
        _out, err, code = _run(
            parser, "export", _PROJECT, "--output", str(tmp_path / "missing" / "out.json")
        )
    assert code == 1
    assert "cannot write" in err
