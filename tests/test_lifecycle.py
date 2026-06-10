# tests/test_lifecycle.py
"""The headline promise: an embedded server is stopped and cleaned up when the
owning process exits.

The rest of the suite calls ``conn._cleanup()`` explicitly; these tests instead
exercise the real ``atexit`` and ``__del__`` paths that back the README's claim.
"""

import atexit
import gc
import json
import os
import subprocess
import sys
import time

import psutil

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


def test_double_cleanup_is_idempotent():
    conn = Valkey()
    conn._cleanup()
    # Second call short-circuits on `running is False` rather than erroring.
    conn._cleanup()
    assert conn.running is False


def test_cleanup_when_server_already_dead():
    conn = Valkey()
    pid = conn.pid
    proc = psutil.Process(pid)
    proc.kill()
    proc.wait(timeout=5)

    # The daemon is already gone; cleanup must swallow the failed SHUTDOWN and
    # the NoSuchProcess in _terminate, and still remove the temp tree.
    conn._cleanup()
    assert conn.running is False
    assert not os.path.exists(conn.dbdir)
