# src/valkey_embedded/pytest_plugin.py
"""Pytest fixtures backed by a real embedded Valkey server.

Installed packages register this automatically via the ``pytest11`` entry
point, so once ``valkey-embedded`` is installed the fixtures below are
available with no conftest wiring.

Fixtures:
    valkey_server         -> session-scoped started ValkeyServer (one per
                             session / xdist worker; CONFIG, Lua-script, and
                             ACL state persists across tests)
    valkey_client         -> function-scoped client to that server; FLUSHALL
                             runs before each test so keys never leak between
                             tests
    valkey_url            -> session-scoped ``valkey://host:port`` URL
    valkey_server_factory -> function-scoped factory for tests that need a
                             private, pristine server (all created servers are
                             stopped at teardown)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

import pytest

from valkey_embedded.server import ValkeyServer

if TYPE_CHECKING:
    from collections.abc import Iterator

    import valkey


@pytest.fixture(scope="session")
def valkey_server() -> Iterator[ValkeyServer]:
    """One started ValkeyServer shared by the whole session (per xdist worker)."""
    with ValkeyServer() as server:
        yield server


@pytest.fixture
def valkey_client(valkey_server: ValkeyServer) -> Iterator[valkey.Valkey]:
    """A client to the session server with a clean keyspace (FLUSHALL on setup).

    Only keys are reset between tests; server-level state (CONFIG SET, loaded
    Lua scripts, ACLs) persists for the session. Use ``valkey_server_factory``
    when a test needs a pristine server process.
    """
    client = valkey_server.client()
    try:
        client.flushall()
        yield client
    finally:
        # valkey-py's close() is untyped; harmless to call.
        client.close()  # type: ignore[no-untyped-call]


@pytest.fixture(scope="session")
def valkey_url(valkey_server: ValkeyServer) -> str:
    """The ``valkey://host:port`` connection URL of the session server."""
    return valkey_server.connection_url


@pytest.fixture
def valkey_server_factory() -> Iterator[Callable[..., ValkeyServer]]:
    """Create private ValkeyServers: ``server = valkey_server_factory(**config)``.

    Each call starts a fresh, isolated server; all servers created through the
    factory are stopped when the test finishes.
    """
    servers: list[ValkeyServer] = []

    def factory(**kwargs: Any) -> ValkeyServer:
        """Start and register a private ValkeyServer(**kwargs)."""
        server = ValkeyServer(**kwargs)
        server.start()
        servers.append(server)
        return server

    try:
        yield factory
    finally:
        for server in servers:
            server.stop()
