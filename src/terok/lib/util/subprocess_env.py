# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Environment-construction helpers for spawned ``terok`` child processes.

Centralises the ``PYTHONPATH`` shim that every ``subprocess.run`` /
``Popen`` of ``sys.executable`` must use, so adding a new spawn site
doesn't silently regress Franz Pöschel's Nix-wrapped-Python fix
(terok-ai/terok#717).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def child_process_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Build the environment for a spawned ``terok`` child process.

    Threads the parent's absolute non-cwd import paths through as ``PYTHONPATH`` so the
    child can import ``terok`` regardless of how the parent was
    launched.  Under Nix, ``sys.executable`` is a wrapper script that
    rewrites the env on startup — but spawning it directly via
    ``subprocess`` / ``create_subprocess_exec`` bypasses that wrapper,
    leaving the child unable to find the ``terok`` package.  This shim
    restores it (Franz Pöschel's fix in #717).

    *overrides* are applied on top of the parent env; ``PYTHONPATH``
    always wins so a stray ambient value can't shadow the parent's
    real import path.
    """
    cwd = Path.cwd()
    import_paths = (
        entry for entry in sys.path if os.path.isabs(entry) and Path(entry).resolve() != cwd
    )
    return {**os.environ, **(overrides or {}), "PYTHONPATH": os.pathsep.join(import_paths)}
