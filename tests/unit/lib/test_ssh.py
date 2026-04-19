# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for SSH project initialization helpers (DB-backed)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestRegisterSshKey:
    """Tests for ``register_ssh_key`` in the domain facade."""

    def test_assigns_key_to_scope(self) -> None:
        """register_ssh_key delegates to ``CredentialDB.assign_ssh_key``."""
        from terok.lib.domain.facade import register_ssh_key

        db = MagicMock()
        with patch("terok.lib.domain.facade._open_vault_db", return_value=db):
            register_ssh_key("myproj", 7)
        db.assign_ssh_key.assert_called_once_with("myproj", 7)
        db.close.assert_called_once()

    def test_propagates_errors_from_db(self) -> None:
        """Errors from the DB layer propagate (no silent swallowing)."""
        import pytest

        from terok.lib.domain.facade import register_ssh_key

        db = MagicMock()
        db.assign_ssh_key.side_effect = RuntimeError("disk full")
        with (
            patch("terok.lib.domain.facade._open_vault_db", return_value=db),
            pytest.raises(RuntimeError, match="disk full"),
        ):
            register_ssh_key("proj", 1)
        db.close.assert_called_once()
