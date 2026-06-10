# tests/test_pytest_plugin.py
"""The bundled pytest fixtures: session server, clean-per-test clients, factory.

Mirrors the developer experience of `pip install valkey-embedded` then using
the fixtures directly (here loaded via tests/conftest.py for the source run).
"""

import pytest
import valkey

_seen_pids = set()


def test_valkey_server_fixture_is_running(valkey_server):
    assert valkey_server.is_running()
    _seen_pids.add(valkey_server.pid)


def test_valkey_server_is_session_scoped(valkey_server):
    # The previous test recorded the server pid; same session -> same process.
    assert valkey_server.is_running()
    if _seen_pids:
        assert valkey_server.pid in _seen_pids


def test_valkey_client_fixture(valkey_client):
    valkey_client.set("k", "v")
    assert valkey_client.get("k") == b"v"


def test_valkey_client_starts_with_clean_keyspace(valkey_client):
    # The previous test set "k" on the shared session server; FLUSHALL on
    # fixture setup must have removed it.
    assert valkey_client.get("k") is None
    assert valkey_client.dbsize() == 0
    valkey_client.set("leaked", "1")


def test_valkey_client_cleans_between_tests(valkey_client):
    # If per-test FLUSHALL didn't run, "leaked" from the previous test remains.
    assert valkey_client.get("leaked") is None


def test_valkey_url_fixture_connects(valkey_url, valkey_server):
    assert valkey_url == valkey_server.connection_url
    client = valkey.Valkey.from_url(valkey_url)
    try:
        assert client.ping() is True
    finally:
        client.close()


def test_factory_creates_independent_servers(valkey_server, valkey_server_factory):
    private = valkey_server_factory()
    assert private.is_running()
    assert private.port != valkey_server.port
    # State on the private server does not touch the session server.
    private.client().set("only-here", "1")
    assert valkey_server.client().get("only-here") is None


@pytest.fixture
def _stopped_probe():
    # Set up BEFORE the factory in the test below, so this teardown runs AFTER
    # the factory's (fixture finalization is LIFO).
    holder = {}
    yield holder
    assert not holder["server"].is_running()


def test_factory_servers_are_stopped_at_teardown(_stopped_probe, valkey_server_factory):
    _stopped_probe["server"] = valkey_server_factory()
    assert _stopped_probe["server"].is_running()


def test_valkey_embedded_alias_is_gone(request):
    with pytest.raises(pytest.FixtureLookupError):
        request.getfixturevalue("valkey_embedded")
