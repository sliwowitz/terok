# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Serve the Terok TUI as a web application via textual-serve."""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import stat
import sys
from base64 import b64decode
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiohttp import web

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 8566
_AUTH_USER = "terok"
"""Basic-auth username.  Constant so users only memorise the password."""
_AUTH_REALM = "terok-tui"


def _valid_port(value: str) -> int:
    """Validate that *value* is a valid TCP port number (1–65535)."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port value: {value!r} (must be an integer)")
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(
            f"invalid port value: {value!r} (must be between 1 and 65535)"
        )
    return port


def _password_path() -> Path:
    """Return the ephemeral file path that stores the current session password.

    ``$XDG_RUNTIME_DIR`` is a tmpfs cleared on reboot, so the password
    naturally rotates each session without any on-disk state leaking.
    Falls back to ``/tmp/terok-$UID`` when XDG_RUNTIME_DIR is unset.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        root = Path(base) / "terok"
    else:
        # Fallback when XDG_RUNTIME_DIR is unset: keep the password in a
        # per-UID dir under /tmp so it is still isolated from other users.
        root = Path(f"/tmp/terok-{os.getuid()}")  # nosec B108  # noqa: S108
    root.mkdir(parents=True, exist_ok=True)
    return root / "serve.password"


def _load_or_mint_password() -> str:
    """Read the current password from the runtime dir, or mint a fresh one.

    The file is created with mode 0600.  Existing files are refused if
    they are world- or group-readable, to avoid another local user
    sneaking a readable copy into the runtime dir between sessions.
    """
    path = _password_path()
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise SystemExit(
                f"Refusing to read {path}: mode is {oct(mode)}, expected 0600. "
                f"Delete it or fix the permissions."
            )
        value = path.read_text().strip()
        if value:
            return value
    value = secrets.token_urlsafe(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)
    return value


def _basic_auth_middleware(expected: str) -> Callable[..., Awaitable[web.StreamResponse]]:
    """Build an aiohttp middleware that enforces Basic auth for every request.

    The username is fixed (:data:`_AUTH_USER`); the password is compared
    in constant time against *expected*.  On missing or wrong creds, a
    401 with ``WWW-Authenticate: Basic`` is returned so browsers prompt
    once per origin and cache the credentials for the tab lifetime.
    """
    from aiohttp import web

    challenge = {"WWW-Authenticate": f'Basic realm="{_AUTH_REALM}"'}
    expected_token = f"{_AUTH_USER}:{expected}".encode()

    @web.middleware
    async def mw(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Pass through when creds match; otherwise respond with a 401 challenge."""
        header = request.headers.get("Authorization", "")
        scheme, _, payload = header.partition(" ")
        if scheme.lower() == "basic":
            try:
                candidate = b64decode(payload.encode(), validate=True)
            except ValueError:
                candidate = b""
            if hmac.compare_digest(candidate, expected_token):
                return await handler(request)
        return web.Response(status=401, headers=challenge, text="Unauthorized")

    return mw


def _build_server(command: str, host: str, port: int, public_url: str | None, password: str):
    """Construct a ``textual_serve`` Server with basic-auth middleware injected.

    Wraps ``Server._make_app`` on the instance so it returns the parent
    app with our auth middleware appended.  Using instance-level shadowing
    (instead of subclassing) keeps the indirection to one line and leaves
    the ``Server(...)`` call shape unchanged — it breaks only if textual-
    serve renames ``_make_app`` (asserted at import time).
    """
    from textual_serve.server import Server

    mw = _basic_auth_middleware(password)
    server = Server(command, host=host, port=port, public_url=public_url)
    original_make_app = server._make_app

    async def _make_app_with_auth():
        """Return the vanilla textual-serve app with our middleware appended."""
        app = await original_make_app()
        app.middlewares.append(mw)
        return app

    server._make_app = _make_app_with_auth
    return server


def main() -> None:
    """Launch the Terok TUI as a web application.

    Uses textual-serve to expose the TUI over HTTP/WebSocket so it can
    be accessed from a browser.  Accepts ``--host`` and ``--port`` to
    override the default listen address.  A random per-session password
    gates the listener so other local users cannot reach it — the
    password is printed to the launching terminal and also persisted
    (0600) under ``$XDG_RUNTIME_DIR/terok/serve.password``.
    """
    try:
        from textual_serve.server import Server
    except ModuleNotFoundError as exc:
        if exc.name in ("textual_serve", "textual_serve.server"):
            print(
                "terok-web requires the 'textual-serve' package.\n"
                "Install it with: pip install textual-serve",
                file=sys.stderr,
            )
            sys.exit(1)
        raise

    if not hasattr(Server, "_make_app"):
        print(
            "Unsupported textual-serve version: Server._make_app is missing.  "
            "terok pins the upstream seam used to inject basic-auth middleware.",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog="terok-web",
        description="Serve the Terok TUI as a web application",
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"Host to bind to (default: {_DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=_valid_port,
        default=_DEFAULT_PORT,
        help=f"Port to listen on (default: {_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help="Public URL for browser-facing links and WebSocket connections "
        "(e.g. http://myhost:8566). Required when serving to LAN or "
        "behind a reverse proxy. If omitted, derived from --host and --port.",
    )
    args = parser.parse_args()

    password = _load_or_mint_password()
    server = _build_server("terok-tui", args.host, args.port, args.public_url, password)

    display_host = args.host if args.public_url is None else args.public_url
    print(
        f"terok-web: serving at http://{display_host}:{args.port}/ "
        f"(user '{_AUTH_USER}', password in {_password_path()})",
        file=sys.stderr,
    )
    print(f"terok-web: password = {password}", file=sys.stderr)
    server.serve()


if __name__ == "__main__":
    main()
