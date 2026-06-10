# tests/test_client.py
"""Embedded-server lifecycle tests (run against the real built binary)."""

import os

import pytest

from valkey_embedded import Valkey
from valkey_embedded.client import ServerStartError


def test_starts_and_responds_to_ping():
    conn = Valkey()
    try:
        assert conn.ping() is True
        # Unix socket exists while the server runs.
        assert os.path.exists(conn.socket_file)
    finally:
        conn._cleanup()


def test_cleanup_removes_isolated_workdir():
    conn = Valkey()
    workdir = conn.dbdir
    assert os.path.isdir(workdir)
    conn._cleanup()
    assert not os.path.exists(workdir)


def test_set_and_get_roundtrip():
    conn = Valkey()
    try:
        conn.set("key", "value")
        assert conn.get("key") == b"value"
    finally:
        conn._cleanup()


def test_log_property_reports_ready():
    conn = Valkey()
    try:
        # Valkey logs this line once the server accepts connections.
        assert "Ready to accept connections" in conn.valkey_log
    finally:
        conn._cleanup()


def test_persistence_across_restarts(tmp_path):
    dbfile = str(tmp_path / "persist.db")
    first = Valkey(dbfile)
    first.set("survives", "yes")
    first.save()  # force RDB write before shutdown
    first._cleanup()

    second = Valkey(dbfile)
    try:
        assert second.get("survives") == b"yes"
    finally:
        second._cleanup()


def test_shared_servers_attach_to_same_process(tmp_path):
    dbfile = str(tmp_path / "shared.db")
    first = Valkey(dbfile)
    second = Valkey(dbfile)
    try:
        # Same db path -> second attaches to first's server.
        assert first.pid == second.pid
        first.set("k", "v")
        assert second.get("k") == b"v"
    finally:
        second._cleanup()
        first._cleanup()


def test_isolated_servers_are_independent():
    first = Valkey()
    second = Valkey()
    try:
        assert first.pid != second.pid
        assert first.socket_file != second.socket_file
        first.set("only", "first")
        assert second.get("only") is None
    finally:
        first._cleanup()
        second._cleanup()


def test_multiple_independent_servers_in_one_process():
    servers = [Valkey() for _ in range(3)]
    try:
        for index, conn in enumerate(servers):
            conn.set("id", str(index))
        for index, conn in enumerate(servers):
            assert conn.get("id") == str(index).encode()
        pids = {conn.pid for conn in servers}
        assert len(pids) == 3
    finally:
        for conn in servers:
            conn._cleanup()


def test_last_client_shuts_down_shared_server(tmp_path):
    import psutil

    dbfile = str(tmp_path / "lifecycle.db")
    first = Valkey(dbfile)
    second = Valkey(dbfile)
    pid = first.pid
    # Closing the non-owner leaves the server running for the other client.
    second._cleanup()
    assert first.ping() is True
    # Closing the last client stops the server.
    first._cleanup()
    assert not psutil.pid_exists(pid)


def test_failed_start_cleans_up_tempdir(monkeypatch):
    import glob

    before = set(glob.glob("/tmp/valkey_embedded-*"))
    monkeypatch.setattr(Valkey, "start_timeout", 2)
    with pytest.raises(ServerStartError):
        Valkey(serverconfig={"loglevel": "bogus-level"})
    leaked = set(glob.glob("/tmp/valkey_embedded-*")) - before
    assert leaked == set(), "leaked temp dirs: {0}".format(leaked)
