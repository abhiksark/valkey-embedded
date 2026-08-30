"""Deterministic identity and rollback branches for ``ValkeyServer``."""

import os
import socket
from types import SimpleNamespace

import psutil
import pytest
import valkey

import valkey_embedded
from valkey_embedded import ValkeyServer
from valkey_embedded.server import (
    ServerStartError,
    _DaemonIdentity,
    _StartupIdentityMismatch,
    _StartupNotReady,
)


class _FakeClient:
    def __init__(self, info=None, ping=True, error=None):
        self._info = info or {}
        self._ping = ping
        self._error = error
        self.closed = False

    def ping(self):
        if self._error is not None:
            raise self._error
        return self._ping

    def info(self, _section):
        return self._info

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


def _probe_server(tmp_path, monkeypatch):
    server = ValkeyServer(data_dir=str(tmp_path), port=6380)
    server.port = 6380
    server._process = _FakeProcess()
    server._process_create_time = 12.5
    with open(server.pidfile, "w") as fh:
        fh.write(str(server._process.pid))
    monkeypatch.setattr(
        server,
        "_matching_process",
        lambda _pid, _create_time: SimpleNamespace(),
    )
    return server


def _private_info(server):
    assert server._process is not None
    return {
        "process_id": server._process.pid,
        "tcp_port": server.port,
        "run_id": "run-private",
        "config_file": server.configfile,
    }


def test_start_rejects_negative_timeout():
    server = ValkeyServer()
    workdir = server.data_dir
    try:
        with pytest.raises(ValueError, match="non-negative"):
            server.start(timeout=-0.1)
    finally:
        server.stop()
    assert not os.path.exists(workdir)


def test_start_rejects_live_pidfile_without_signalling_it(tmp_path):
    server = ValkeyServer(data_dir=str(tmp_path), port=6380)
    with open(server.pidfile, "w") as fh:
        fh.write(str(os.getpid()))

    with pytest.raises(ServerStartError, match="identifies live process"):
        server.start(timeout=0.1)

    assert os.path.exists(server.pidfile)
    assert server.port is None
    assert psutil.pid_exists(os.getpid())


def test_prepare_removes_stale_unix_socket(tmp_path):
    server = ValkeyServer(data_dir=str(tmp_path))
    with open(server.socket_file, "w") as fh:
        fh.write("stale")

    server._prepare_runtime_paths()

    assert not os.path.exists(server.socket_file)


def test_prepare_rejects_live_unix_socket(tmp_path):
    server = ValkeyServer(data_dir=str(tmp_path))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(server.socket_file)
    listener.listen()
    try:
        with pytest.raises(ServerStartError, match="accepting connections"):
            server._prepare_runtime_paths()
    finally:
        listener.close()
        if os.path.exists(server.socket_file):
            os.remove(server.socket_file)


def test_missing_binary_rolls_back_server_tempdir(monkeypatch):
    server = ValkeyServer()
    workdir = server.data_dir
    monkeypatch.setattr(
        valkey_embedded, "__valkey_executable__", "/missing/valkey-server"
    )

    with pytest.raises(ServerStartError, match="bundled valkey-server not found"):
        server.start()

    assert server.port is None
    assert not os.path.exists(workdir)
    assert not server._atexit_registered


def test_popen_error_becomes_start_error_and_rolls_back(monkeypatch):
    server = ValkeyServer()
    workdir = server.data_dir

    def fail_popen(*_args, **_kwargs):
        raise OSError("simulated exec failure")

    monkeypatch.setattr("valkey_embedded.server.subprocess.Popen", fail_popen)

    with pytest.raises(ServerStartError, match="simulated exec failure"):
        server.start()

    assert server.port is None
    assert not os.path.exists(workdir)


def test_immediate_process_exit_cleans_new_user_runtime_files(monkeypatch, tmp_path):
    old_log = tmp_path / "valkey.log"
    old_log.write_text("pre-existing log")
    process = _FakeProcess(returncode=19)
    server = ValkeyServer(data_dir=str(tmp_path), port=6380)

    monkeypatch.setattr(
        "valkey_embedded.server.subprocess.Popen", lambda *_args, **_kwargs: process
    )

    def no_process(_pid):
        raise psutil.NoSuchProcess(_pid)

    monkeypatch.setattr("valkey_embedded.server.psutil.Process", no_process)

    with pytest.raises(ServerStartError, match="exited with status 19"):
        server.start()

    assert old_log.read_text() == "pre-existing log"
    assert not os.path.exists(server.configfile)
    assert server.port is None
    assert process.wait_calls == [0]


def test_wait_without_launched_process_is_actionable(tmp_path):
    server = ValkeyServer(data_dir=str(tmp_path), port=6380)
    server.port = 6380

    with pytest.raises(ServerStartError, match="process was not launched"):
        server._wait_until_ready(timeout=0)


def test_wait_rejects_process_that_disappears_after_endpoint_checks(
    monkeypatch, tmp_path
):
    server = _probe_server(tmp_path, monkeypatch)
    identity = _DaemonIdentity(4242, 12.5, "run-private")
    monkeypatch.setattr(server, "_probe_private_identity", lambda _timeout: identity)
    monkeypatch.setattr(server, "_verify_tcp_identity", lambda *_args: None)
    monkeypatch.setattr(server, "_matching_process", lambda *_args: None)

    with pytest.raises(ServerStartError, match="changed or exited"):
        server._wait_until_ready(timeout=0.1)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("pidfile", "pidfile PID"),
        ("missing-info", "incomplete server identity"),
        ("process-id", "private endpoint PID"),
        ("port", "reports TCP port"),
        ("run-id", "empty run ID"),
        ("config-file", "unexpected config file"),
        ("creation", "creation identity"),
    ],
)
def test_private_identity_rejects_mismatched_components(
    case, message, monkeypatch, tmp_path
):
    server = _probe_server(tmp_path, monkeypatch)
    info = _private_info(server)
    if case == "pidfile":
        with open(server.pidfile, "w") as fh:
            fh.write("9999")
    elif case == "missing-info":
        info.pop("run_id")
    elif case == "process-id":
        info["process_id"] = 9999
    elif case == "port":
        info["tcp_port"] = 9999
    elif case == "run-id":
        info["run_id"] = ""
    elif case == "config-file":
        info["config_file"] = str(tmp_path / "foreign.conf")
    elif case == "creation":
        monkeypatch.setattr(server, "_matching_process", lambda *_args: None)
    client = _FakeClient(info=info)
    monkeypatch.setattr(server, "_private_client", lambda **_kwargs: client)

    with pytest.raises(_StartupIdentityMismatch, match=message):
        server._probe_private_identity(timeout=0.1)

    if case != "pidfile":
        assert client.closed


def test_private_identity_requires_process_and_ping(monkeypatch, tmp_path):
    unlaunched = ValkeyServer(data_dir=str(tmp_path), port=6380)
    with pytest.raises(_StartupNotReady, match="direct child"):
        unlaunched._probe_private_identity(timeout=0.1)
    unlaunched.stop()

    server = _probe_server(tmp_path, monkeypatch)
    client = _FakeClient(info=_private_info(server), ping=False)
    monkeypatch.setattr(server, "_private_client", lambda **_kwargs: client)
    with pytest.raises(_StartupNotReady, match="did not answer PING"):
        server._probe_private_identity(timeout=0.1)
    assert client.closed


def test_private_identity_recovers_creation_time(monkeypatch, tmp_path):
    server = _probe_server(tmp_path, monkeypatch)
    server._process_create_time = None
    client = _FakeClient(info=_private_info(server))
    monkeypatch.setattr(server, "_private_client", lambda **_kwargs: client)
    monkeypatch.setattr(
        "valkey_embedded.server.psutil.Process",
        lambda _pid: SimpleNamespace(create_time=lambda: 42.0),
    )

    identity = server._probe_private_identity(timeout=0.1)

    assert identity.create_time == 42.0
    assert server._process_create_time == 42.0


def test_private_identity_handles_process_exit_while_reading_creation_time(
    monkeypatch, tmp_path
):
    server = _probe_server(tmp_path, monkeypatch)
    server._process_create_time = None
    monkeypatch.setattr(
        server,
        "_private_client",
        lambda **_kwargs: _FakeClient(info=_private_info(server)),
    )

    def no_process(_pid):
        raise psutil.NoSuchProcess(_pid)

    monkeypatch.setattr("valkey_embedded.server.psutil.Process", no_process)

    with pytest.raises(_StartupNotReady, match="launched process exited"):
        server._probe_private_identity(timeout=0.1)


@pytest.mark.parametrize(
    ("case", "expected_exception", "message"),
    [
        ("ping", _StartupNotReady, "did not answer PING"),
        ("connection", _StartupNotReady, "TCP endpoint is not ready"),
        ("missing-info", _StartupIdentityMismatch, "incomplete server identity"),
        ("pid", _StartupIdentityMismatch, "belongs to PID/run ID"),
        ("run-id", _StartupIdentityMismatch, "belongs to PID/run ID"),
    ],
)
def test_tcp_identity_rejects_unready_or_foreign_endpoint(
    case, expected_exception, message, monkeypatch, tmp_path
):
    server = ValkeyServer(data_dir=str(tmp_path), port=6380)
    server.port = 6380
    identity = _DaemonIdentity(4242, 12.5, "expected-run")
    info = {"process_id": identity.pid, "run_id": identity.run_id}
    ping = True
    error = None
    if case == "ping":
        ping = False
    elif case == "connection":
        error = valkey.exceptions.ConnectionError("not ready")
    elif case == "missing-info":
        info.pop("run_id")
    elif case == "pid":
        info["process_id"] = 9999
    elif case == "run-id":
        info["run_id"] = "foreign-run"
    client = _FakeClient(info=info, ping=ping, error=error)
    monkeypatch.setattr(server, "_tcp_client", lambda **_kwargs: client)

    with pytest.raises(expected_exception, match=message):
        server._verify_tcp_identity(identity, timeout=0.1)

    assert client.closed


def test_log_tail_is_bounded_and_redacts_configured_secrets(tmp_path):
    secret = "do-not-print-this"
    server = ValkeyServer(
        data_dir=str(tmp_path),
        config={"maxmemory": "1mb", "requirepass": ["", secret]},
    )
    with open(server.logfile, "w") as fh:
        fh.write("x" * 5000 + "\npassword=" + secret)

    tail = server._redacted_log_tail()

    assert secret not in tail
    assert "password=<redacted>" in tail
    assert len(tail.encode()) <= 4096


def test_stop_skips_shutdown_for_mismatched_private_endpoint(monkeypatch):
    server = ValkeyServer()
    server.start()
    pid = server.pid
    shutdown_calls = []
    fake = _FakeClient(
        info={"process_id": pid, "run_id": "foreign-run"},
    )
    fake.shutdown = lambda **kwargs: shutdown_calls.append(kwargs)
    monkeypatch.setattr(server, "_private_client", lambda **_kwargs: fake)

    try:
        server.stop(timeout=0)
        assert shutdown_calls == []
        assert not psutil.pid_exists(pid)
    finally:
        if psutil.pid_exists(pid):
            psutil.Process(pid).kill()


def test_dead_identity_returns_zero_and_client_refuses_foreign_endpoint():
    server = ValkeyServer()
    server.start()
    identity = server._identity
    assert identity is not None
    process = psutil.Process(identity.pid)
    try:
        process.kill()
        process.wait(timeout=5)
        assert server.pid == 0
        assert not server.is_running()
        with pytest.raises(RuntimeError, match="not running"):
            server.client()
    finally:
        server.stop(timeout=0)


def test_rollback_recovers_direct_child_creation_identity(monkeypatch):
    server = ValkeyServer()
    workdir = server.data_dir
    process = _FakeProcess()
    server._process = process
    server._process_create_time = None
    terminate_calls = []
    monkeypatch.setattr(
        "valkey_embedded.server.psutil.Process",
        lambda _pid: SimpleNamespace(create_time=lambda: 42.0),
    )
    monkeypatch.setattr(
        server,
        "_terminate_process",
        lambda pid, create_time, timeout: terminate_calls.append(
            (pid, create_time, timeout)
        ),
    )

    server._rollback_failed_start(None)

    assert terminate_calls == [(process.pid, 42.0, 0)]
    assert process.wait_calls == [0]
    assert not os.path.exists(workdir)


def test_creation_time_mismatch_is_never_signalled(monkeypatch):
    server = ValkeyServer()
    process = _FakeProcess()
    server._process = process
    signal_calls = []
    candidate = SimpleNamespace(
        create_time=lambda: 99.0,
        is_running=lambda: True,
        status=lambda: "running",
        terminate=lambda: signal_calls.append("terminate"),
        kill=lambda: signal_calls.append("kill"),
    )
    monkeypatch.setattr("valkey_embedded.server.psutil.Process", lambda _pid: candidate)

    try:
        assert server._matching_process(process.pid, 42.0) is None
        server._terminate_process(process.pid, 42.0, timeout=0)
        assert signal_calls == []
    finally:
        server._process = None
        server.stop()


def test_terminate_process_escalates_only_the_matching_identity(monkeypatch):
    server = ValkeyServer()
    process = _FakeProcess()
    server._process = process
    calls = []

    class StubbornProcess:
        @staticmethod
        def terminate():
            calls.append("terminate")

        @staticmethod
        def kill():
            calls.append("kill")

        @staticmethod
        def wait(timeout):
            calls.append(("wait", timeout))
            raise psutil.TimeoutExpired(timeout)

    stubborn = StubbornProcess()
    monkeypatch.setattr(server, "_matching_process", lambda *_args: stubborn)

    try:
        server._terminate_process(process.pid, 42.0, timeout=0)
        assert calls == ["terminate", ("wait", 5), "kill", ("wait", 5)]
    finally:
        server._process = None
        server.stop()


def test_terminate_process_never_accepts_pid_zero():
    server = ValkeyServer()
    try:
        server._terminate_process(0, 0.0, timeout=0)
    finally:
        server.stop()
