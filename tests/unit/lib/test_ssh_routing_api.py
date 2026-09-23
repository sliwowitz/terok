# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for the SSH key ↔ project routing API.

The vault DB and project discovery are stubbed so the assembly logic
(row axis, column axis with orphan scopes, infra exclusion) and the
mutating verbs can be checked without an encrypted store on disk.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest

from terok.lib.api import ssh_routing


class _FakeDB:
    """A minimal stand-in for ``CredentialDB``'s SSH-routing surface."""

    def __init__(self, keys, assignments):
        """Seed the fake with key rows and ``(scope, key_id)`` assignments."""
        self.keys = list(keys)
        self.assignments = list(assignments)
        self.calls: list[tuple] = []

    def list_all_ssh_keys(self):
        """Return the stored key rows."""
        return self.keys

    def list_ssh_key_assignments(self):
        """Return the stored assignment edges."""
        return self.assignments

    def list_ssh_key_defaults(self):
        """Return the default chosen independently for each scope."""
        return {"foo": 1, "bar": 2}

    def get_ssh_public_key(self, key_id):
        """Return a public line for a known key."""
        return "ssh-ed25519 AAAA example" if key_id == 1 else None

    def set_default_ssh_key(self, scope, key_id):
        """Record a default selection."""
        self.calls.append(("default", scope, key_id))

    def assign_ssh_key(self, scope, key_id):
        """Record a link."""
        self.calls.append(("assign", scope, key_id))

    def unassign_ssh_key(self, scope, key_id):
        """Record an unlink."""
        self.calls.append(("unassign", scope, key_id))

    def delete_ssh_key(self, key_id):
        """Record a delete."""
        self.calls.append(("delete", key_id))

    def set_ssh_key_comment(self, fingerprint, comment):
        """Record a comment edit and report success."""
        self.calls.append(("rename", fingerprint, comment))
        return True


def _key(key_id: int):
    """A throwaway key row carrying only the id the API reads."""
    return SimpleNamespace(id=key_id, key_type="ed25519", fingerprint="fp", comment="c")


@pytest.fixture()
def db(monkeypatch):
    """Install a fake vault DB and project list; hand the DB back for assertions."""
    fake = _FakeDB(
        keys=[_key(1), _key(2)],
        assignments=[("foo", 1), ("bar", 1), ("bar", 2), ("%host", 2)],
    )

    @contextmanager
    def _vault_db():
        yield fake

    monkeypatch.setattr(ssh_routing, "vault_db", _vault_db)
    monkeypatch.setattr(
        ssh_routing,
        "discover_projects",
        lambda: ([SimpleNamespace(name="foo"), SimpleNamespace(name="quux")], []),
    )
    return fake


class TestLoadKeyRouting:
    """Verify the routing snapshot assembled from the vault and project list."""

    def test_keys_are_the_row_axis(self, db):
        """Every stored key becomes a row, ids preserved."""
        routing = ssh_routing.load_key_routing()
        assert [k.id for k in routing.keys] == [1, 2]

    def test_columns_union_projects_and_orphan_scopes(self, db):
        """Columns = on-disk projects plus scopes that still hold a link."""
        routing = ssh_routing.load_key_routing()
        # foo, quux from disk; bar is an orphan scope kept because it has links.
        assert routing.projects == ("bar", "foo", "quux")

    def test_infra_scope_is_excluded_from_columns(self, db):
        """The ``%host`` infra scope never appears on the project axis."""
        routing = ssh_routing.load_key_routing()
        assert not any(p.startswith("%") for p in routing.projects)

    def test_links_carry_every_edge(self, db):
        """The full edge set — including the infra edge — is returned verbatim."""
        routing = ssh_routing.load_key_routing()
        assert routing.links == {("foo", 1), ("bar", 1), ("bar", 2), ("%host", 2)}

    def test_defaults_belong_to_scopes(self, db):
        assert ssh_routing.load_key_routing().defaults == {"foo": 1, "bar": 2}


class TestMutations:
    """Verify the verbs delegate to the matching DB calls."""

    def test_link_assigns(self, db):
        """link_key assigns the scope→key pair."""
        ssh_routing.link_key("quux", 2)
        assert ("assign", "quux", 2) in db.calls

    def test_unlink_unassigns(self, db):
        """unlink_key unassigns the scope→key pair."""
        ssh_routing.unlink_key("foo", 1)
        assert ("unassign", "foo", 1) in db.calls

    def test_delete_removes_key(self, db):
        """delete_key drops the whole key."""
        ssh_routing.delete_key(1)
        assert ("delete", 1) in db.calls

    def test_rename_sets_comment(self, db):
        """rename_key edits the comment by fingerprint and returns the result."""
        assert ssh_routing.rename_key("fp", "new comment") is True
        assert ("rename", "fp", "new comment") in db.calls

    def test_set_default_for_one_scope(self, db):
        ssh_routing.set_default_key("bar", 1)
        assert db.calls == [("default", "bar", 1)]

    def test_public_key_returns_only_public_line(self, db):
        assert ssh_routing.public_key(1) == "ssh-ed25519 AAAA example"

    def test_missing_public_key_reports_deleted_key(self, db):
        with pytest.raises(ValueError, match="no longer exists"):
            ssh_routing.public_key(99)


class TestMint:
    """Minting and naming use the same scope-bound manager as the CLI."""

    def test_default_requests_additive_generation(self, db):
        """A bare mint creates a named key, without an empty-comment sentinel."""
        manager = mock.Mock()
        manager.mint.return_value = {"key_id": 8}
        with mock.patch.object(ssh_routing, "SSHManager", return_value=manager) as factory:
            result = ssh_routing.mint_key("foo")
        factory.assert_called_once_with(scope="foo", db=db)
        manager.mint.assert_called_once_with(key_type="ed25519", comment=None)
        assert result == {"key_id": 8}

    def test_mint_preserves_explicit_comment(self, db):
        manager = mock.Mock()
        with mock.patch.object(ssh_routing, "SSHManager", return_value=manager):
            ssh_routing.mint_key("foo", key_type="rsa", comment="hi")
        manager.mint.assert_called_once_with(key_type="rsa", comment="hi")

    def test_suggestion_uses_canonical_scope_naming(self, db):
        manager = mock.Mock()
        manager.suggested_comment.return_value = "foo-2"
        with mock.patch.object(ssh_routing, "SSHManager", return_value=manager) as factory:
            assert ssh_routing.suggested_key_comment("foo") == "foo-2"
        factory.assert_called_once_with(scope="foo", db=db)
        manager.suggested_comment.assert_called_once_with()


class TestIsLastLink:
    """Verify the last-link predicate that gates destructive confirmation."""

    def test_true_when_sole_scope(self):
        """A key linked to exactly one scope reports that scope as last."""
        routing = ssh_routing.KeyRouting(keys=(), projects=(), links=frozenset({("foo", 1)}))
        assert ssh_routing.is_last_link(routing, "foo", 1) is True

    def test_false_when_shared(self):
        """A key shared across scopes has no last link from either side."""
        routing = ssh_routing.KeyRouting(
            keys=(), projects=(), links=frozenset({("foo", 1), ("bar", 1)})
        )
        assert ssh_routing.is_last_link(routing, "foo", 1) is False
