# tests/test_lifecycle.py
"""The headline promise: an embedded server is stopped and cleaned up when its
owner closes it or the owning process exits.

These tests exercise the public ``close()`` boundary and the ``atexit`` and
``__del__`` fallbacks that back the README's claim.
"""

import atexit
import gc
import json
import os
import subprocess
import sys
import threading
import time

import psutil
import pytest
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

from valkey_embedded import Valkey


def _wait_dead(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return False


def test_atexit_stops_server_and_removes_workdir():
    # A child interpreter creates an isolated server and exits normally; the
    # atexit hook must shut the daemon down and remove the private temp dir.
    child = (
        "import json, valkey_embedded;"
        "c = valkey_embedded.Valkey();"
        "print(json.dumps({'pid': c.pid, 'dbdir': c.dbdir}))"
    )
    out = subprocess.check_output([sys.executable, "-c", child], text=True)
    info = json.loads(out.strip().splitlines()[-1])

    assert not psutil.pid_exists(info["pid"]), "daemon survived process exit"
    assert not os.path.exists(info["dbdir"]), "workdir survived process exit"


def test_del_triggers_cleanup():
    conn = Valkey()
    pid = conn.pid
    dbdir = conn.dbdir
    assert psutil.pid_exists(pid)

    # The atexit hook registers a bound method, which pins the instance for the
    # life of the process; drop that reference so the object can actually be
    # finalized and the __del__ -> _cleanup path runs now rather than at exit.
    atexit.unregister(conn._cleanup)
    del conn
    gc.collect()

    assert _wait_dead(pid), "__del__ did not stop the daemon"
    assert not os.path.exists(dbdir), "__del__ did not remove the workdir"


def test_close_stops_isolated_server_and_removes_workdir(monkeypatch):
    conn = Valkey()
    pid = conn.pid
    dbdir = conn.dbdir
    disconnect = conn.connection_pool.disconnect
    disconnect_calls = 0

    def tracked_disconnect():
        nonlocal disconnect_calls
        disconnect_calls += 1
        disconnect()

    monkeypatch.setattr(conn.connection_pool, "disconnect", tracked_disconnect)
    conn.close()

    assert _wait_dead(pid), "close() did not stop the isolated daemon"
    assert not os.path.exists(dbdir), "close() did not remove the workdir"
    assert conn.running is False
    assert disconnect_calls == 1


def test_shared_close_keeps_server_until_last_client(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "shared.db")
    registry = dbfile + ".settings"
    first = Valkey(dbfile)
    second = Valkey(dbfile)
    pid = first.pid
    first_disconnect = first.connection_pool.disconnect
    second_disconnect = second.connection_pool.disconnect
    disconnect_calls = {"first": 0, "second": 0}

    def track_first_disconnect():
        disconnect_calls["first"] += 1
        first_disconnect()

    def track_second_disconnect():
        disconnect_calls["second"] += 1
        second_disconnect()

    monkeypatch.setattr(first.connection_pool, "disconnect", track_first_disconnect)
    monkeypatch.setattr(second.connection_pool, "disconnect", track_second_disconnect)

    first.set("survives", "yes")
    first.close()

    assert second.ping() is True
    assert second.get("survives") == b"yes"
    assert psutil.pid_exists(pid)
    assert os.path.exists(registry)
    assert disconnect_calls["first"] == 1

    second.close()

    assert _wait_dead(pid), "last shared close() did not stop the daemon"
    assert os.path.exists(dbfile), "last shared close() did not preserve the RDB"
    assert not os.path.exists(registry)
    assert disconnect_calls["second"] == 1


def test_shared_close_retries_after_ownership_check_failure(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "retry.db")
    registry = dbfile + ".settings"
    conn = Valkey(dbfile)
    pid = conn.pid
    connection_count = conn._connection_count
    unregister = atexit.unregister
    unregister_calls = []
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValkeyTimeoutError("simulated CLIENT LIST timeout")
        return connection_count()

    def tracked_unregister(callback):
        unregister_calls.append(callback)
        return unregister(callback)

    monkeypatch.setattr(conn, "_connection_count", fail_once)
    monkeypatch.setattr(atexit, "unregister", tracked_unregister)

    try:
        with pytest.raises(ValkeyTimeoutError, match="CLIENT LIST timeout"):
            conn.close()

        assert conn.running is True
        assert psutil.pid_exists(pid)
        assert os.path.exists(registry)
        assert unregister_calls == []

        conn.close()

        assert attempts == 2
        assert conn.running is False
        assert _wait_dead(pid), "retry did not stop the shared daemon"
        assert not os.path.exists(registry)
        assert len(unregister_calls) == 1
    finally:
        if conn.running:
            monkeypatch.setattr(conn, "_connection_count", connection_count)
            conn.close()


def test_close_is_idempotent():
    conn = Valkey()
    pid = conn.pid
    dbdir = conn.dbdir

    conn.close()
    conn.close()

    assert _wait_dead(pid)
    assert not os.path.exists(dbdir)
    assert conn.running is False


def test_concurrent_close_runs_lifecycle_once(monkeypatch):
    conn = Valkey()
    pid = conn.pid
    dbdir = conn.dbdir
    shutdown = conn.shutdown
    shutdown_entered = threading.Event()
    second_close_attempted = threading.Event()
    duplicate_shutdown = threading.Event()
    release_shutdown = threading.Event()
    calls_lock = threading.Lock()
    shutdown_calls = 0
    errors = []

    def blocked_shutdown(*args, **kwargs):
        nonlocal shutdown_calls
        with calls_lock:
            shutdown_calls += 1
            if shutdown_calls > 1:
                duplicate_shutdown.set()
        shutdown_entered.set()
        if not release_shutdown.wait(timeout=5):
            raise AssertionError("timed out waiting to release shutdown")
        return shutdown(*args, **kwargs)

    def close_client(attempted=None):
        if attempted is not None:
            attempted.set()
        try:
            conn.close()
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures
            errors.append(exc)

    monkeypatch.setattr(conn, "shutdown", blocked_shutdown)
    first = threading.Thread(target=close_client)
    first.start()
    assert shutdown_entered.wait(timeout=5)

    second = threading.Thread(target=close_client, args=(second_close_attempted,))
    second.start()
    assert second_close_attempted.wait(timeout=5)
    try:
        assert not duplicate_shutdown.wait(timeout=0.2)
    finally:
        release_shutdown.set()

    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert shutdown_calls == 1
    assert _wait_dead(pid)
    assert not os.path.exists(dbdir)
    assert conn.running is False


def test_close_when_server_already_dead():
    conn = Valkey()
    pid = conn.pid
    proc = psutil.Process(pid)
    proc.kill()
    proc.wait(timeout=5)

    # The daemon is already gone; close must swallow the failed SHUTDOWN and
    # the NoSuchProcess in _terminate, and still remove the temp tree.
    conn.close()
    assert conn.running is False
    assert not os.path.exists(conn.dbdir)


def test_close_inside_context_is_idempotent():
    with Valkey() as conn:
        pid = conn.pid
        dbdir = conn.dbdir
        conn.close()
        assert conn.running is False

    assert _wait_dead(pid)
    assert not os.path.exists(dbdir)


def test_repeated_create_close_cycles_leave_no_servers_or_workdirs():
    resources = []
    for _ in range(3):
        conn = Valkey()
        resources.append((conn.pid, conn.dbdir))
        conn.close()

    for pid, dbdir in resources:
        assert _wait_dead(pid)
        assert not os.path.exists(dbdir)
