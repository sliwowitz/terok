# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Tests for web port allocation and validation."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from terok.lib.orchestration.ports import assign_web_port, is_port_free


class TestIsPortFree:
    """is_port_free() probes localhost bindability."""

    def test_unbound_port(self) -> None:
        """An unbound port reports as free."""
        # Bind to 0, get an OS-assigned port, close it — it should be free.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        assert is_port_free(port)

    def test_occupied_port(self) -> None:
        """A currently bound port reports as not free."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            assert not is_port_free(port)


class TestAssignWebPort:
    """assign_web_port() scans for free ports."""

    def test_skips_occupied(self) -> None:
        """Occupied base port is skipped, next free port returned."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            occupied = s.getsockname()[1]

            with (
                patch("terok.lib.orchestration.ports.get_ui_base_port", return_value=occupied),
                patch("terok.lib.orchestration.ports._collect_all_web_ports", return_value=set()),
            ):
                assigned = assign_web_port()
                assert assigned > occupied

    def test_exhaustion_raises(self) -> None:
        """Raises SystemExit when all 200 ports are exhausted."""
        with (
            patch("terok.lib.orchestration.ports.get_ui_base_port", return_value=50000),
            patch("terok.lib.orchestration.ports._collect_all_web_ports", return_value=set()),
            patch("terok.lib.orchestration.ports.is_port_free", return_value=False),
            pytest.raises(SystemExit, match="No free web ports"),
        ):
            assign_web_port()
