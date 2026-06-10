# tests/test_connect.py
"""The SQLite-style connect() entry point and durable= (AOF) persistence.

connect() is sugar over Valkey() that reads like sqlite3.connect: open a
file-backed store in one call, opt into crash-safe durability, and release it
by leaving a `with` block.
"""

import atexit
import os
import time

import psutil
import pytest

import valkey_embedded
from valkey_embedded import connect


def _config_value(conn, name):
    """Return a single CONFIG GET value, decoded to str."""
    raw = conn.config_get(name)[name]
    return raw.decode() if isinstance(raw, bytes) else raw


def _wait_dead(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


def _hard_kill(conn):
    """SIGKILL the daemon behind ``conn`` — a crash, not a clean shutdown.

    Detaches the client's own cleanup so it cannot quietly save on the way out,
    so what survives is only what reached disk before the kill.
    """
    pid = conn.pid
    try:
        atexit.unregister(conn._cleanup)
    except Exception:
        pass
    conn.running = False
    psutil.Process(pid).kill()
    assert _wait_dead(pid), "daemon survived SIGKILL"


# -- basic plumbing ------------------------------------------------------


def test_connect_isolated_basic():
    conn = connect()
    try:
        assert conn.ping() is True
        conn.set("k", "v")
        assert conn.get("k") == b"v"
    finally:
        conn._cleanup()


def test_connect_kwargs_are_forwarded_to_client(tmp_path):
    db = str(tmp_path / "data.db")
    conn = connect(db, decode_responses=True)
    try:
        conn.set("k", "v")
        assert conn.get("k") == "v"  # str, not bytes -> decode_responses took
    finally:
        conn._cleanup()


def test_connect_persistent_roundtrip(tmp_path):
    db = str(tmp_path / "data.db")
    first = connect(db)
    first.set("greeting", "hello")
    first._cleanup()  # shutdown(save=True) writes the RDB

    second = connect(db)
    try:
        assert second.get("greeting") == b"hello"
    finally:
        second._cleanup()


# -- durable= (AOF) ------------------------------------------------------


def test_connect_durable_enables_aof(tmp_path):
    db = str(tmp_path / "data.db")
    conn = connect(db, durable=True)
    try:
        assert _config_value(conn, "appendonly") == "yes"
        assert _config_value(conn, "appendfsync") == "everysec"
        conn.set("k", "v")
        # Valkey 8 writes a multi-part AOF under <dir>/appendonlydir/.
        assert os.path.isdir(tmp_path / "appendonlydir")
    finally:
        conn._cleanup()


def test_connect_durable_always_fsyncs_every_write(tmp_path):
    db = str(tmp_path / "data.db")
    conn = connect(db, durable="always")
    try:
        assert _config_value(conn, "appendonly") == "yes"
        assert _config_value(conn, "appendfsync") == "always"
    finally:
        conn._cleanup()


def test_connect_durable_requires_a_path():
    with pytest.raises(ValueError, match="requires a path"):
        connect(durable=True)


def test_connect_rejects_unknown_durable_value(tmp_path):
    db = str(tmp_path / "data.db")
    with pytest.raises(ValueError, match="durable must be"):
        connect(db, durable="sometimes")


def test_explicit_serverconfig_overrides_durable_preset(tmp_path):
    db = str(tmp_path / "data.db")
    # durable=True implies everysec; an explicit override must win.
    conn = connect(db, durable=True, serverconfig={"appendfsync": "always"})
    try:
        assert _config_value(conn, "appendonly") == "yes"
        assert _config_value(conn, "appendfsync") == "always"
    finally:
        conn._cleanup()


def test_durable_data_survives_reconnect(tmp_path):
    db = str(tmp_path / "data.db")
    with connect(db, durable=True) as first:
        first.set("durable", "yes")
    # Leaving the block shut the server down (no explicit save call).
    with connect(db, durable=True) as second:
        assert second.get("durable") == b"yes"


def test_durable_always_survives_a_crash(tmp_path):
    """The headline promise: with fsync-every-write, a SIGKILL loses nothing."""
    db = str(tmp_path / "data.db")
    conn = connect(db, durable="always")
    conn.set("k", "survivor")
    _hard_kill(conn)  # no clean shutdown, no save

    reopened = connect(db, durable="always")
    try:
        assert reopened.get("k") == b"survivor"
    finally:
        reopened._cleanup()


def test_rdb_only_loses_recent_write_on_crash(tmp_path):
    """The flip side that justifies durable=: RDB snapshots can't survive a crash.

    A single write with no save point reached never hits disk, so a crash
    discards it. This is exactly what durable= exists to prevent.
    """
    db = str(tmp_path / "data.db")
    conn = connect(db)  # durable=False -> RDB snapshots only
    conn.set("k", "ephemeral")
    _hard_kill(conn)

    reopened = connect(db)
    try:
        assert reopened.get("k") is None
    finally:
        reopened._cleanup()


# -- context manager -----------------------------------------------------


def test_context_manager_releases_server(tmp_path):
    db = str(tmp_path / "data.db")
    with connect(db) as conn:
        assert conn.ping() is True
        pid = conn.pid
        assert pid and psutil.pid_exists(pid)
    # Exiting the `with` block stops the embedded server.
    assert _wait_dead(pid), "server still running after context exit"


def test_context_manager_returns_usable_client(tmp_path):
    db = str(tmp_path / "data.db")
    with connect(db) as conn:
        conn.set("k", "v")
        assert conn.get("k") == b"v"


def test_connect_is_exported():
    assert valkey_embedded.connect is connect
