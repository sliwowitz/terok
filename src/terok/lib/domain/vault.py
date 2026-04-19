# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Vault access — the domain primitive every helper reaches through.

A single :func:`vault_db` context manager owns the open/close handshake
around :class:`terok_sandbox.CredentialDB`.  Without it, the try/finally
boilerplate multiplied across five call sites in an earlier iteration.
"""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def vault_db():
    """Open the shared vault :class:`CredentialDB` and close it on exit."""
    from terok_sandbox import CredentialDB

    from ..core.config import make_sandbox_config

    db = CredentialDB(make_sandbox_config().db_path)
    try:
        yield db
    finally:
        db.close()


__all__ = ["vault_db"]
