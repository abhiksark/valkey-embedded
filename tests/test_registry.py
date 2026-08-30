"""Versioned cross-process registry identity, recovery, and locking tests."""

import concurrent.futures
import fcntl
import shutil
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from dataclasses import replace

import psutil
import pytest
import valkey

from valkey_embedded import ServerStartError, Valkey
from valkey_embedded.client import (
    _MAX_REGISTRY_BYTES,
    _REGISTRY_VERSION,
    ValkeyMixin,
    _ManagedDaemonIdentity,
    _RegistryHolder,
    _RegistryIdentityMismatch,
    _RegistryNotReady,
    _parse_registry_record,
    _read_private_registry,
    _safe_remove,
    _safe_rmdir,
)


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    return process.pid


def _wait_dead(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


def _stop_verified_daemon(identity):
    """Best-effort test cleanup that never signals an unverified process."""
    pid = int(identity["pid"])
    if not psutil.pid_exists(pid):
        return
    expected = os.path.realpath(__import__("valkey_embedded").__valkey_executable__)
    process = psutil.Process(pid)
    if (
        process.create_time() != float(identity["process_create_time"])
        or os.path.realpath(process.exe()) != expected
    ):
        return
    probe = valkey.Valkey(
        unix_socket_path=identity["unixsocket"],
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        decode_responses=True,
    )
    try:
        info = probe.info("server")
        if int(info["process_id"]) != pid or str(info["run_id"]) != identity["run_id"]:
            return
        try:
            probe.shutdown(nosave=True)
        except valkey.exceptions.ValkeyError:
            pass
    finally:
        probe.close()
    if not _wait_dead(pid) and psutil.pid_exists(pid):
        process = psutil.Process(pid)
        if (
            process.create_time() == float(identity["process_create_time"])
            and os.path.realpath(process.exe()) == expected
        ):
            process.terminate()
            process.wait(timeout=5)


def _legacy_registry(dbfile, pid, unixsocket):
    dbdir = os.path.dirname(dbfile)
    pidfile = os.path.join(dbdir, "valkey.pid")
    with open(pidfile, "w") as fh:
        fh.write(str(pid))
    registry = dbfile + ".settings"
    with open(registry, "w") as fh:
        json.dump(
            {
                "pidfile": pidfile,
                "unixsocket": unixsocket,
                "dbdir": dbdir,
                "dbfilename": os.path.basename(dbfile),
            },
            fh,
        )
    return registry


def _versioned_stale_registry(dbfile, pid, unixsocket, process_create_time=None):
    dbdir = os.path.dirname(dbfile)
    pidfile = os.path.join(dbdir, "valkey.pid")
    with open(pidfile, "w") as fh:
        fh.write(str(pid))
    registry = dbfile + ".settings"
    data = {
        "version": _REGISTRY_VERSION,
        "database": os.path.realpath(dbfile),
        "pid": pid,
        "process_create_time": (
            psutil.Process(pid).create_time()
            if process_create_time is None
            else process_create_time
        ),
        "run_id": "stale-run-id",
        "pidfile": pidfile,
        "unixsocket": unixsocket,
        "socket_dir": None,
        "socket_owned": True,
        "dbdir": dbdir,
        "dbfilename": os.path.basename(dbfile),
        "configfile": os.path.join(dbdir, "valkey.conf"),
        "logfile": os.path.join(dbdir, "valkey.log"),
        "holders": [
            {
                "token": "stale-holder",
                "pid": os.getpid(),
                "process_create_time": psutil.Process().create_time(),
            }
        ],
    }
    with open(registry, "w") as fh:
        json.dump(data, fh)
    os.chmod(registry, 0o600)
    return registry


class _ClosingUnixListener:
    """Accept and close connections so the path is live but not Valkey."""

    def __init__(self, path):
        self.path = path
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(path)
        self._listener.listen()
        self._listener.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connection.close()

    def close(self):
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5)
        if os.path.exists(self.path):
            os.remove(self.path)


def test_stale_registry_with_live_non_valkey_pid_recovers(tmp_path):
    dbfile = str(tmp_path / "shared.db")
    missing_socket = str(tmp_path / "missing.sock")
    registry = _versioned_stale_registry(
        dbfile,
        os.getpid(),
        missing_socket,
    )
    sentinel = tmp_path / "caller-owned.txt"
    sentinel.write_text("keep")

    conn = Valkey(dbfile)
    pid = conn.pid
    try:
        assert conn.ping() is True
        assert pid != os.getpid()
        assert psutil.pid_exists(os.getpid())
        data = json.loads((tmp_path / "shared.db.settings").read_text())
        assert data["version"] == _REGISTRY_VERSION
        assert data["pid"] == pid
        assert data["run_id"] == conn.info("server")["run_id"]
        assert sentinel.read_text() == "keep"
    finally:
        conn.close()

    assert _wait_dead(pid)
    assert not os.path.exists(registry)
    assert sentinel.read_text() == "keep"


def test_stale_registry_with_dead_pid_recovers(tmp_path):
    dead_pid = _dead_pid()
    dbfile = str(tmp_path / "dead.db")
    _versioned_stale_registry(
        dbfile,
        dead_pid,
        str(tmp_path / "missing.socket"),
        process_create_time=1.0,
    )

    conn = Valkey(dbfile)
    pid = conn.pid
    try:
        assert conn.ping() is True
        assert pid != dead_pid
    finally:
        conn.close()
    assert _wait_dead(pid)


def test_stale_pidfile_naming_live_bundled_process_is_not_signalled(tmp_path):
    owner = Valkey()
    owner_pid = owner.pid
    dbfile = str(tmp_path / "foreign.db")
    _versioned_stale_registry(
        dbfile,
        owner_pid,
        str(tmp_path / "missing.socket"),
    )
    try:
        with pytest.raises(ServerStartError, match="live bundled process"):
            Valkey(dbfile)
        assert owner.ping() is True
        assert owner.pid == owner_pid
    finally:
        owner.close()
    assert _wait_dead(owner_pid)


def test_legacy_stale_registry_is_replaced_not_left_blocking(tmp_path):
    dbfile = str(tmp_path / "legacy.db")
    registry = _legacy_registry(
        dbfile,
        os.getpid(),
        str(tmp_path / "missing.sock"),
    )

    conn = Valkey(dbfile)
    try:
        assert conn.ping() is True
        data = json.loads((tmp_path / "legacy.db.settings").read_text())
        assert data["version"] == _REGISTRY_VERSION
        assert data["pid"] == conn.pid
    finally:
        conn.close()
    assert not os.path.exists(registry)


def test_registry_records_exact_live_identity_and_private_mode(tmp_path):
    dbfile = str(tmp_path / "identity.db")
    registry = tmp_path / "identity.db.settings"
    owner = Valkey(dbfile)
    second = None
    try:
        raw = registry.read_text()
        data = json.loads(raw)
        record = _parse_registry_record(data)
        info = owner.info("server")
        process = psutil.Process(owner.pid)

        assert set(data) == {
            "version",
            "database",
            "pid",
            "process_create_time",
            "run_id",
            "pidfile",
            "unixsocket",
            "socket_dir",
            "socket_owned",
            "dbdir",
            "dbfilename",
            "configfile",
            "logfile",
            "holders",
        }
        assert record.identity.pid == owner.pid == info["process_id"]
        assert record.identity.create_time == process.create_time()
        assert record.identity.run_id == info["run_id"]
        assert os.path.realpath(record.configfile) == os.path.realpath(
            info["config_file"]
        )
        assert info["tcp_port"] == 0
        assert record.database == os.path.realpath(dbfile)
        assert len(record.holders) == 1
        assert record.holders[0].pid == os.getpid()
        assert record.holders[0].create_time == psutil.Process().create_time()
        assert stat.S_IMODE(registry.stat().st_mode) == 0o600
        assert (
            stat.S_IMODE((tmp_path / "identity.db.settings.lock").stat().st_mode)
            == 0o600
        )
        assert not list(tmp_path.glob(".identity.db.settings.*"))

        second = Valkey(dbfile)
        assert second.pid == owner.pid
        assert second._daemon_identity == owner._daemon_identity
        attached_record = _parse_registry_record(json.loads(registry.read_text()))
        assert len(attached_record.holders) == 2
        assert len({holder.token for holder in attached_record.holders}) == 2
        owner.set("shared", "yes")
        assert second.get("shared") == b"yes"
    finally:
        if second is not None:
            second.close()
        owner.close()


def test_registry_rejects_unmanaged_path_relationships(tmp_path):
    dbfile = str(tmp_path / "paths.db")
    owner = Valkey(dbfile)
    try:
        record = owner._registry_record
        assert record is not None
        invalid = [
            replace(record, unixsocket="relative.socket"),
            replace(record, unixsocket=str(tmp_path / "other.socket")),
            replace(record, socket_dir="relative-directory"),
            replace(
                record,
                socket_dir="/tmp/not-managed",
                unixsocket="/tmp/not-managed/valkey.socket",
            ),
            replace(record, socket_owned=False, socket_dir="/tmp/vkey-sock-fake"),
        ]
        assert all(not owner._registry_paths_match(item) for item in invalid)
    finally:
        owner.close()


def test_non_private_registry_is_never_trusted(tmp_path):
    dbfile = str(tmp_path / "permissions.db")
    registry = tmp_path / "permissions.db.settings"
    owner = Valkey(dbfile)
    owner_pid = owner.pid
    original = registry.read_text()
    registry.chmod(0o644)
    try:
        with pytest.raises(ServerStartError, match="live bundled process"):
            Valkey(dbfile)
        assert owner.pid == owner_pid
        assert owner.ping() is True
    finally:
        registry.write_text(original)
        registry.chmod(0o600)
        owner.close()
    assert _wait_dead(owner_pid)


def test_registry_never_serializes_authentication_secret(tmp_path):
    dbfile = str(tmp_path / "authenticated.db")
    secret = "registry-must-not-contain-this-password"
    config = {"requirepass": secret}
    owner = Valkey(dbfile, serverconfig=config, password=secret)
    attached = None
    try:
        registry_path = tmp_path / "authenticated.db.settings"
        registry_text = registry_path.read_text()
        assert secret not in registry_text
        with pytest.raises(ServerStartError, match="authenticated Valkey"):
            Valkey(dbfile, password="wrong-password")
        assert registry_path.read_text() == registry_text
        assert owner.ping() is True

        attached = Valkey(dbfile, serverconfig=config, password=secret)
        assert attached.pid == owner.pid
        assert attached.ping() is True
    finally:
        if attached is not None:
            attached.close()
        owner.close()


@pytest.mark.parametrize(
    "mutation",
    ["pid", "create-time", "run-id", "database", "config-file"],
)
def test_tampered_registry_never_adopts_or_signals_live_owner(tmp_path, mutation):
    dbfile = str(tmp_path / (mutation + ".db"))
    registry = tmp_path / (mutation + ".db.settings")
    owner = Valkey(dbfile)
    owner_pid = owner.pid
    try:
        data = json.loads(registry.read_text())
        original_data = dict(data)
        if mutation == "pid":
            data["pid"] = os.getpid()
            data["process_create_time"] = psutil.Process().create_time()
        elif mutation == "create-time":
            data["process_create_time"] += 1000.0
        elif mutation == "run-id":
            data["run_id"] = "wrong-run-id"
        elif mutation == "database":
            data["database"] = str(tmp_path / "other.db")
        else:
            data["configfile"] = str(tmp_path / "other.conf")
        registry.write_text(json.dumps(data))

        with pytest.raises(ServerStartError, match="live Valkey endpoint"):
            Valkey(dbfile)

        assert owner.ping() is True
        assert owner.pid == owner_pid
        assert psutil.pid_exists(owner_pid)
        assert registry.exists()
    finally:
        registry.write_text(json.dumps(original_data))
        registry.chmod(0o600)
        owner.close()
    assert _wait_dead(owner_pid)


def test_startup_endpoint_mismatch_never_adopts_partial_identity(tmp_path, monkeypatch):
    pidfile = tmp_path / "valkey.pid"
    pidfile.write_text("123")
    short_dir = tempfile.mkdtemp(prefix="vkey-registry-test-")
    socket_file = os.path.join(short_dir, "valkey.socket")
    mixin = object.__new__(ValkeyMixin)
    mixin.pidfile = str(pidfile)
    mixin.socket_file = socket_file
    mixin.configfile = str(tmp_path / "valkey.conf")
    mixin._probe_kwargs = {}
    mixin._daemon_identity = None

    process = types.SimpleNamespace(create_time=lambda: 456.0)
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(mixin, "_matching_managed_process", lambda identity: process)
    monkeypatch.setattr(
        mixin,
        "_probe_server_info",
        lambda path: {
            "process_id": 123,
            "run_id": "foreign-run",
            "config_file": str(tmp_path / "foreign.conf"),
        },
    )

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(socket_file)
            with pytest.raises(
                _RegistryIdentityMismatch, match="unexpected config file"
            ):
                mixin._current_managed_identity()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)

    assert mixin._daemon_identity is None


def test_startup_identity_treats_transient_pid_and_socket_state_as_not_ready(
    tmp_path, monkeypatch
):
    pid = 123
    create_time = 456.0
    process = types.SimpleNamespace(create_time=lambda: create_time)
    mixin = object.__new__(ValkeyMixin)
    mixin._read_pidfile_value = lambda path=None: pid
    mixin._matching_managed_process = lambda identity: process
    mixin._probe_kwargs = {}
    mixin.configfile = str(tmp_path / "valkey.conf")
    mixin.socket_file = str(tmp_path / "missing.socket")
    mixin._preexisting_pidfile_identity = (pid, create_time)
    monkeypatch.setattr(psutil, "Process", lambda candidate: process)

    with pytest.raises(_RegistryNotReady, match="replace stale pidfile"):
        mixin._current_managed_identity()

    mixin._preexisting_pidfile_identity = None
    with pytest.raises(_RegistryNotReady, match="socket is not ready"):
        mixin._current_managed_identity()

    regular_file = tmp_path / "not-a-socket"
    regular_file.write_text("regular")
    mixin.socket_file = str(regular_file)
    with pytest.raises(_RegistryNotReady, match="not a socket"):
        mixin._current_managed_identity()


def test_startup_identity_rejects_unreadable_pid_process(tmp_path, monkeypatch):
    mixin = object.__new__(ValkeyMixin)
    mixin._read_pidfile_value = lambda path=None: 123
    mixin.socket_file = str(tmp_path / "missing.socket")

    def denied(pid):
        raise psutil.AccessDenied(pid=pid)

    monkeypatch.setattr(psutil, "Process", denied)
    with pytest.raises(_RegistryIdentityMismatch, match="creation identity"):
        mixin._current_managed_identity()


def test_pid_creation_mismatch_is_never_signalled():
    conn = Valkey()
    identity = conn._daemon_identity
    assert identity is not None
    mismatched = type(identity)(
        identity.pid,
        identity.create_time + 1000.0,
        identity.run_id,
    )
    try:
        conn._daemon_identity = mismatched
        conn._terminate(identity.pid)
        assert psutil.pid_exists(identity.pid)

        conn._daemon_identity = identity
        assert conn.ping() is True
    finally:
        conn._daemon_identity = identity
        conn.close()
    assert _wait_dead(identity.pid)


def test_client_terminate_escalates_only_a_reverified_identity():
    identity = _ManagedDaemonIdentity(123, 456.0, "run")
    calls = []

    class Process:
        waits = 0

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        def wait(self, timeout):
            del timeout
            self.waits += 1
            if self.waits == 1:
                raise psutil.TimeoutExpired(5, pid=identity.pid)
            raise psutil.NoSuchProcess(identity.pid)

    process = Process()
    obj = types.SimpleNamespace(
        _daemon_identity=identity,
        _matching_managed_process=lambda candidate: process,
    )
    ValkeyMixin._terminate(obj, identity.pid, grace_period=0)
    assert calls == ["terminate", "kill"]

    obj._matching_managed_process = lambda candidate: None
    ValkeyMixin._terminate(obj, identity.pid, grace_period=0)
    assert calls == ["terminate", "kill"]


def test_live_non_valkey_unix_listener_is_preserved_during_recovery():
    short_dir = tempfile.mkdtemp(prefix="vkey-registry-test-")
    dbfile = os.path.join(short_dir, "listener.db")
    expected_socket = os.path.join(short_dir, "valkey.socket")
    listener = _ClosingUnixListener(expected_socket)
    registry = _versioned_stale_registry(dbfile, os.getpid(), expected_socket)
    conn = None
    try:
        conn = Valkey(dbfile)
        assert conn.ping() is True
        assert conn.socket_file != expected_socket
        assert conn._socket_dir is not None
        assert os.path.exists(expected_socket)
        with open(registry) as fh:
            data = json.load(fh)
        assert data["unixsocket"] == conn.socket_file
        assert data["socket_dir"] == conn._socket_dir
    finally:
        if conn is not None:
            conn.close()
        listener.close()
        shutil.rmtree(short_dir, ignore_errors=True)


def test_stale_caller_provided_socket_is_never_removed(tmp_path):
    dbfile = str(tmp_path / "caller.db")
    caller_socket = tmp_path / "caller.socket"
    caller_socket.write_text("caller-owned")

    with pytest.raises(ServerStartError, match="caller-provided Unix socket"):
        Valkey(dbfile, unix_socket_path=str(caller_socket))

    assert caller_socket.read_text() == "caller-owned"
    assert not (tmp_path / "caller.db.settings").exists()


def test_cross_process_attach_uses_same_verified_registry_identity(tmp_path):
    dbfile = str(tmp_path / "shared.db")
    owner = Valkey(dbfile)
    try:
        child_source = (
            "import json, valkey_embedded;"
            "c = valkey_embedded.Valkey({0!r});"
            "assert c.ping();"
            "c.set('from_child', '1');"
            "print(json.dumps({{'pid': c.pid, 'run_id': c.info('server')['run_id']}}));"
            "c.close()"
        ).format(dbfile)
        output = subprocess.check_output(
            [sys.executable, "-c", child_source],
            text=True,
            timeout=30,
        )
        child_info = json.loads(output.strip().splitlines()[-1])

        assert child_info["pid"] == owner.pid
        assert child_info["run_id"] == owner._daemon_identity.run_id
        assert psutil.pid_exists(owner.pid)
        assert owner.get("from_child") == b"1"
    finally:
        owner.close()


def test_unknown_holder_process_is_preserved(monkeypatch):
    holder = _RegistryHolder("holder", os.getpid(), psutil.Process().create_time())

    def denied(pid):
        raise psutil.AccessDenied(pid=pid)

    monkeypatch.setattr(psutil, "Process", denied)
    obj = object.__new__(ValkeyMixin)
    assert obj._holder_is_live(holder) is True


def test_dead_process_holder_is_pruned_on_next_attach(tmp_path):
    dbfile = str(tmp_path / "abandoned.db")
    registry = tmp_path / "abandoned.db.settings"
    child_source = """
import json
import os
import sys
import valkey_embedded

client = valkey_embedded.Valkey(sys.argv[1])
record = json.load(open(sys.argv[1] + ".settings"))
print(json.dumps(record), flush=True)
os._exit(0)
"""
    child = subprocess.run(
        [sys.executable, "-c", child_source, dbfile],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    abandoned = json.loads(child.stdout.strip().splitlines()[-1])
    assert psutil.pid_exists(abandoned["pid"])
    assert not psutil.pid_exists(abandoned["holders"][0]["pid"])

    conn = None
    try:
        conn = Valkey(dbfile)
        record = _parse_registry_record(json.loads(registry.read_text()))
        assert conn.pid == abandoned["pid"]
        assert len(record.holders) == 1
        assert record.holders[0].pid == os.getpid()
    finally:
        if conn is not None:
            conn.close()
        _stop_verified_daemon(abandoned)

    assert _wait_dead(abandoned["pid"])
    assert not registry.exists()


@pytest.mark.parametrize("worker_count", [2, 5])
def test_concurrent_stale_recovery_publishes_one_daemon(tmp_path, worker_count):
    dbfile = str(tmp_path / "race.db")
    registry = _legacy_registry(
        dbfile,
        os.getpid(),
        str(tmp_path / "missing.sock"),
    )
    release = tmp_path / "release"
    child_source = """
import json
import os
import sys
import time
import valkey_embedded

client = valkey_embedded.Valkey(sys.argv[1])
print(json.dumps({"pid": client.pid, "run_id": client.info("server")["run_id"]}), flush=True)
deadline = time.monotonic() + 30
while not os.path.exists(sys.argv[2]):
    if time.monotonic() >= deadline:
        raise RuntimeError("release file was not created")
    time.sleep(0.05)
client.close()
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_source, dbfile, str(release)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(worker_count)
    ]
    infos = []
    daemon_pid = 0
    daemon_create_time = 0.0
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:
            futures = [
                executor.submit(process.stdout.readline) for process in processes
            ]
            for future in futures:
                line = future.result(timeout=30)
                assert line, "child exited before reporting registry identity"
                infos.append(json.loads(line))

        daemon_pids = {info["pid"] for info in infos}
        run_ids = {info["run_id"] for info in infos}
        assert len(daemon_pids) == 1
        assert len(run_ids) == 1
        daemon_pid = daemon_pids.pop()
        with open(registry) as fh:
            data = json.load(fh)
        daemon_create_time = float(data["process_create_time"])
        assert data["pid"] == daemon_pid
        assert data["run_id"] in run_ids
        assert len(data["holders"]) == worker_count
        assert len({holder["token"] for holder in data["holders"]}) == worker_count

        release.touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, "stdout={0}\nstderr={1}".format(
                stdout, stderr
            )
        assert _wait_dead(daemon_pid)
        assert not os.path.exists(registry)
    finally:
        release.touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        if daemon_pid and daemon_create_time and psutil.pid_exists(daemon_pid):
            process = psutil.Process(daemon_pid)
            expected = os.path.realpath(
                __import__("valkey_embedded").__valkey_executable__
            )
            try:
                if (
                    process.create_time() == daemon_create_time
                    and os.path.realpath(process.exe()) == expected
                ):
                    process.kill()
                    process.wait(timeout=5)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"version": True},
        {"version": 1},
        {"version": _REGISTRY_VERSION, "pid": 0},
    ],
)
def test_registry_parser_rejects_malformed_or_legacy_schema(raw):
    with pytest.raises(ValueError):
        _parse_registry_record(raw)


def test_registry_parser_rejects_invalid_managed_holders(tmp_path):
    registry = _versioned_stale_registry(
        str(tmp_path / "schema.db"),
        os.getpid(),
        str(tmp_path / "missing.socket"),
    )
    with open(registry) as fh:
        valid = json.load(fh)
    invalid_holders = [
        None,
        [],
        [None],
        [{"token": "", "pid": os.getpid(), "process_create_time": 1.0}],
        [{"token": "x", "pid": True, "process_create_time": 1.0}],
        [{"token": "x", "pid": os.getpid(), "process_create_time": False}],
        [
            {
                "token": "x",
                "pid": os.getpid(),
                "process_create_time": 1.0,
                "unexpected": True,
            }
        ],
        valid["holders"] * 2,
    ]
    for holders in invalid_holders:
        candidate = dict(valid)
        candidate["holders"] = holders
        with pytest.raises(ValueError):
            _parse_registry_record(candidate)

    invalid_fields = [
        ("pid", True),
        ("process_create_time", False),
        ("socket_dir", ""),
        ("socket_owned", 1),
        ("database", ""),
    ]
    for field, value in invalid_fields:
        candidate = dict(valid)
        candidate[field] = value
        with pytest.raises(ValueError):
            _parse_registry_record(candidate)

    unexpected = dict(valid)
    unexpected["password"] = "must-not-be-accepted"
    with pytest.raises(ValueError, match="unexpected fields"):
        _parse_registry_record(unexpected)


def test_private_registry_reader_bounds_type_and_size(tmp_path):
    directory = tmp_path / "registry-directory"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        _read_private_registry(str(directory))

    oversized = tmp_path / "oversized.settings"
    oversized.write_bytes(b"x" * (_MAX_REGISTRY_BYTES + 1))
    oversized.chmod(0o600)
    with pytest.raises(ValueError, match="size limit"):
        _read_private_registry(str(oversized))


def test_best_effort_path_cleanup_preserves_unexpected_contents(tmp_path):
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    sentinel = nonempty / "sentinel"
    sentinel.write_text("keep")

    _safe_remove(None)
    _safe_rmdir(None)
    _safe_rmdir(str(nonempty))

    assert sentinel.read_text() == "keep"
    shutil.rmtree(nonempty)


def test_remove_files_preserves_unverified_isolated_socket(tmp_path):
    dbdir = tmp_path / "isolated"
    dbdir.mkdir()
    pidfile = dbdir / "valkey.pid"
    configfile = dbdir / "valkey.conf"
    socket_file = dbdir / "valkey.socket"
    for path in (pidfile, configfile, socket_file):
        path.write_text("preserve only the socket")
    obj = types.SimpleNamespace(
        settingregistryfile=None,
        dbdir=str(dbdir),
        pidfile=str(pidfile),
        configfile=str(configfile),
        socket_file=str(socket_file),
        _socket_dir=None,
    )

    ValkeyMixin._remove_files(obj, preserve_socket=True)

    assert not pidfile.exists()
    assert not configfile.exists()
    assert socket_file.read_text() == "preserve only the socket"
    shutil.rmtree(dbdir)


def test_load_registry_rejects_corrupt_json_and_removes_it(tmp_path):
    registry = tmp_path / "x.settings"
    registry.write_text("{ not valid json")
    obj = types.SimpleNamespace(settingregistryfile=str(registry))

    assert ValkeyMixin._load_setting_registry(obj) is False
    assert not registry.exists()


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


def test_unverified_holder_state_never_authorizes_shutdown(tmp_path):
    dbfile = str(tmp_path / "holder-state.db")
    registry = tmp_path / "holder-state.db.settings"
    owner = Valkey(dbfile)
    pid = owner.pid
    raw = registry.read_text()
    holder = owner._registry_holder
    assert holder is not None
    try:
        owner._registry_holder = replace(holder, pid=-1)
        with owner._locked_setting_registry():
            assert owner._release_registry_holder() is False
        assert psutil.pid_exists(pid)

        owner._registry_holder = holder
        tampered = json.loads(raw)
        tampered["run_id"] = "foreign-run"
        registry.write_text(json.dumps(tampered))
        registry.chmod(0o600)
        with owner._locked_setting_registry():
            assert owner._release_registry_holder() is False
        assert psutil.pid_exists(pid)

        registry.unlink()
        with owner._locked_setting_registry():
            assert owner._release_registry_holder() is False
        assert psutil.pid_exists(pid)
    finally:
        owner._registry_holder = holder
        registry.write_text(raw)
        registry.chmod(0o600)
        owner.close()
    assert _wait_dead(pid)


def test_registry_lock_symlink_is_rejected_without_touching_target(tmp_path):
    dbfile = str(tmp_path / "symlink.db")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("preserve")
    os.symlink(sentinel, dbfile + ".settings.lock")

    with pytest.raises(ServerStartError, match="securely open"):
        Valkey(dbfile)

    assert sentinel.read_text() == "preserve"
    assert not os.path.exists(dbfile + ".settings")


def test_registry_lock_hardlink_is_rejected_without_touching_target(tmp_path):
    dbfile = str(tmp_path / "hardlink.db")
    sentinel = tmp_path / "hardlink-sentinel"
    sentinel.write_text("preserve")
    sentinel.chmod(0o644)
    os.link(sentinel, dbfile + ".settings.lock")

    with pytest.raises(ServerStartError, match="private regular file"):
        Valkey(dbfile)

    assert sentinel.read_text() == "preserve"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o644
    assert not os.path.exists(dbfile + ".settings")


def test_registry_thread_lock_wait_is_bounded(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "timeout.db")
    owner = Valkey(dbfile)
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def hold_lock():
        try:
            with owner._locked_setting_registry():
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("timed out waiting to release registry lock")
        except BaseException as exc:  # noqa: BLE001 - propagate thread failure
            errors.append(exc)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(timeout=5)
    monkeypatch.setattr(Valkey, "start_timeout", 0.1)
    try:
        with pytest.raises(ServerStartError, match="thread lock"):
            Valkey(dbfile)
    finally:
        release.set()
        thread.join(timeout=5)
        owner.close()

    assert not thread.is_alive()
    assert errors == []


def test_registry_process_lock_wait_is_bounded(tmp_path, monkeypatch):
    dbfile = str(tmp_path / "process-timeout.db")
    lockfile = dbfile + ".settings.lock"
    release = tmp_path / "release-process-lock"
    owner = Valkey(dbfile)
    child_source = """
import fcntl
import os
import sys
import time

with open(sys.argv[1], "r+") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    print("locked", flush=True)
    deadline = time.monotonic() + 10
    while not os.path.exists(sys.argv[2]):
        if time.monotonic() >= deadline:
            raise RuntimeError("release file was not created")
        time.sleep(0.05)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_source, lockfile, str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        monkeypatch.setattr(Valkey, "start_timeout", 0.2)
        with pytest.raises(ServerStartError, match="process lock"):
            Valkey(dbfile)
    finally:
        release.touch()
        stdout, stderr = child.communicate(timeout=5)
        owner.close()
    assert child.returncode == 0, "stdout={0}\nstderr={1}".format(stdout, stderr)


def test_registry_lock_is_private_regular_file(tmp_path):
    dbfile = str(tmp_path / "locked.db")
    conn = Valkey(dbfile)
    lockfile = tmp_path / "locked.db.settings.lock"
    try:
        assert stat.S_ISREG(lockfile.stat().st_mode)
        assert stat.S_IMODE(lockfile.stat().st_mode) == 0o600
        fd = os.open(lockfile, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        conn.close()
