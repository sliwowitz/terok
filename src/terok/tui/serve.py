# SPDX-FileCopyrightText: 2025 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Serve the Terok TUI as a web application via textual-serve."""

import sys


def main() -> None:
    """Launch the Terok TUI as a web application.

    Uses textual-serve to expose the TUI over HTTP/WebSocket so it can
    be accessed from a browser.  Accepts ``--host`` and ``--port`` to
    override the default listen address (localhost:8566).
    """
    try:
        from textual_serve.server import Server
    except ImportError:
        print(
            "terok-web requires the 'textual-serve' package.\n"
            "Install it with: pip install textual-serve",
            file=sys.stderr,
        )
        sys.exit(1)

    import argparse

    parser = argparse.ArgumentParser(
        prog="terok-web",
        description="Serve the Terok TUI as a web application",
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="Host to bind to (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8566,
        help="Port to listen on (default: 8566)",
    )
    args = parser.parse_args()

    server = Server("terok", host=args.host, port=args.port)
    server.serve()


if __name__ == "__main__":
    main()
