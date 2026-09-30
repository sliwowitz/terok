# SPDX-FileCopyrightText: 2026 Jiri Vyskocil
# SPDX-License-Identifier: Apache-2.0

"""Shared test constants: IP addresses, ports, and URLs."""

LOCALHOST = "127.0.0.1"
"""Loopback address used for bind/connect in tests."""

CONTAINER_HOSTNAME = "host.containers.internal"
"""Hostname Podman exposes for reaching services on the host."""

TEST_IP = "198.51.100.42"
"""RFC 5737 TEST-NET-2 address for mock returns."""

TEST_IP_RFC5737 = "203.0.113.42"
"""RFC 5737 TEST-NET-3 address for firewall allow/deny tests."""

ALLOWED_TARGET_IPS = ["1.1.1.1", "1.0.0.1"]
"""Cloudflare anycast pair used for real egress integration tests."""

ALLOWED_TARGET_DOMAIN = "one.one.one.one"
"""Cloudflare DNS hostname that resolves to ``ALLOWED_TARGET_IPS``."""

ALLOWED_TARGET_HTTP = "http://1.1.1.1/"
"""HTTP target used for real egress reachability checks in integration tests."""

SLIRP_GATEWAY = "10.0.2.2"
"""Default slirp4netns gateway address."""

PASTA_HOST_LOOPBACK_MAP = "169.254.1.2"
"""pasta --map-host-loopback address: container traffic to this link-local
address is translated by pasta to 127.0.0.1 on the host."""

HOST_ALIAS_PASTA = f"{CONTAINER_HOSTNAME}:{PASTA_HOST_LOOPBACK_MAP}"
"""Podman --add-host value for pasta mode (via --map-host-loopback)."""

HOST_ALIAS_LOOPBACK = HOST_ALIAS_PASTA
"""Alias for backwards compatibility — points to pasta host alias."""

HOST_ALIAS_SLIRP = f"{CONTAINER_HOSTNAME}:{SLIRP_GATEWAY}"
"""Podman --add-host value for slirp4netns mode."""

GATE_PORT = 9418
"""Default gate server port."""

FAKE_PEER_PORT = 12345
"""Arbitrary port for fake client_address tuples."""

LOCALHOST_PEER = (LOCALHOST, FAKE_PEER_PORT)
"""Fake peer address for HTTP handler tests."""

TEST_EGRESS_URL = "http://example.com"
"""URL used in egress filtering tests."""

EGRESS_DOMAIN = "example.com"
"""Domain name used in egress filtering tests."""

TEST_UPSTREAM_URL = "https://example.com/source.git"
"""Upstream URL used in project-derivation integration tests."""

EXAMPLE_UPSTREAM_URL = "https://example.com/demo.git"
"""Upstream URL used in task-lifecycle integration tests."""


def localhost_url(port: int) -> str:
    """Build an ``http://127.0.0.1:{port}/`` URL."""
    return f"http://{LOCALHOST}:{port}/"


#: Hostname the container uses to reach its per-container gate via the
#: localhost socat bridge — must match ``environment._gate_url`` (literal
#: ``localhost``, not the numeric loopback, since git resolves the name).
GATE_HOST = "localhost"


def gate_repo_url(project_name: str, token: str, *, port: int = GATE_PORT) -> str:
    """Build the authenticated in-container gate repository URL.

    The container always reaches the per-container gate through the fixed
    localhost socat bridge (``TCP-LISTEN:9418``) in both socket and TCP
    modes, so the URL points at ``localhost``.
    """
    return f"http://{token}@{GATE_HOST}:{port}/{project_name}.git"


VAULT_LOOPBACK_PORT = 9419
"""In-container port of the vault's loopback bridge, which provider handles point at."""

VAULT_LOOPBACK_URL = f"http://{GATE_HOST}:{VAULT_LOOPBACK_PORT}"
"""Base URL a vault-routed provider handle carries inside a task container."""

TEST_NET_CIDR = "198.51.100.0/24"
"""RFC 5737 TEST-NET-2 range — the CIDR form of an allowlist entry."""

HTTPS_PORT = 443
"""The HTTPS port — where most harvested egress goes."""

ALT_HTTPS_PORT = 8443
"""A second TLS port, so a merged harvest entry has more than one port to join."""

PRIVATE_LAN_IP = "192.168.10.20"
"""An RFC 1918 address standing in for a LAN host the shield refuses as private."""

CLOUD_METADATA_IP = "169.254.169.254"
"""The cloud metadata address — inside the link-local hard-deny floor."""
