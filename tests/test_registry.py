# tests/test_registry.py
"""Cross-process sharing via the .settings registry, plus its failure modes.

test_client.py covers same-process sharing; the registry exists so that
*separate processes* attach to one server, which is what we verify here.
"""

import json
import subprocess
import sys
import types

import psutil

from valkey_embedded import Valkey
from valkey_embedded.client import ValkeyMixin


def _dead_pid():
    # A reaped child's pid is reliably dead (vs. guessing a high number).
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_cross_process_attach_via_registry(tmp_path):
    dbfile = str(tmp_path / "shared.db")
    owner = Valkey(dbfile)
    try:
        child = (
            "import valkey_embedded;"
            "c = valkey_embedded.Valkey({0!r});"
            "assert c.ping();"
            "c.set('from_child', '1')".format(dbfile)
        )
        # Child attaches to the owner's server (same dbfile), writes a key, and
        # exits as a NON-owner (owner still connected -> server stays up).
        subprocess.check_call([sys.executable, "-c", child])

        assert psutil.pid_exists(owner.pid), "server died when child exited"
        assert owner.get("from_child") == b"1", "child's write not visible to owner"
    finally:
        owner._cleanup()


def test_load_registry_rejects_dead_pid(tmp_path):
    reg = tmp_path / "x.settings"
    pidfile = tmp_path / "x.pid"
    pidfile.write_text(str(_dead_pid()))
    reg.write_text(
        json.dumps(
            {
                "pidfile": str(pidfile),
                "unixsocket": str(tmp_path / "x.sock"),
                "dbdir": str(tmp_path),
                "dbfilename": "x.db",
            }
        )
    )
    obj = types.SimpleNamespace(settingregistryfile=str(reg))
    assert ValkeyMixin._load_setting_registry(obj) is False


def test_load_registry_rejects_corrupt_json(tmp_path):
    reg = tmp_path / "x.settings"
    reg.write_text("{ not valid json")
    obj = types.SimpleNamespace(settingregistryfile=str(reg))
    assert ValkeyMixin._load_setting_registry(obj) is False


def test_load_registry_rejects_missing_file(tmp_path):
    obj = types.SimpleNamespace(settingregistryfile=str(tmp_path / "nope.settings"))
    assert ValkeyMixin._load_setting_registry(obj) is False


def test_pid_zero_when_pidfile_missing(tmp_path):
    obj = types.SimpleNamespace(pidfile=str(tmp_path / "nope.pid"))
    assert ValkeyMixin.pid.fget(obj) == 0


def test_pid_zero_when_pidfile_garbage(tmp_path):
    pidfile = tmp_path / "p"
    pidfile.write_text("not-an-int")
    obj = types.SimpleNamespace(pidfile=str(pidfile))
    assert ValkeyMixin.pid.fget(obj) == 0


def test_valkey_log_empty_when_missing(tmp_path):
    obj = types.SimpleNamespace(logfile=str(tmp_path / "nope.log"))
    assert ValkeyMixin.valkey_log.fget(obj) == ""


def test_connection_count_zero_after_shutdown():
    conn = Valkey()
    conn._cleanup()
    # Server is gone; client_list() raises ConnectionError -> counted as 0.
    assert conn._connection_count() == 0
