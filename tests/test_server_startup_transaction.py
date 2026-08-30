"""Transactional ``ValkeyServer.start`` failure and retry guarantees."""

import atexit
import os
import shutil
import stat
import subprocess

import psutil
import pytest

import valkey_embedded
from valkey_embedded import ServerStartError, ValkeyServer
from valkey_embedded.server import _StartupNotReady


def _kill_owned_pid(pid):
    if not pid or not psutil.pid_exists(pid):
        return
    process = psutil.Process(pid)
    process.kill()
    process.wait(timeout=5)


def test_invalid_config_reports_parse_error_and_rolls_back_owned_state():
    server = ValkeyServer(config={"loglevel": "bogus-level"})
    workdir = server.data_dir
    try:
        with pytest.raises(ServerStartError) as exc_info:
            server.start(timeout=2)

        message = str(exc_info.value)
        assert "bogus-level" in message
        assert "startup output" in message
        assert server.port is None
        assert server.pid == 0
        assert not server.is_running()
        assert server._process is None
        assert not server._atexit_registered
        assert not os.path.exists(workdir)

        server.stop(timeout=0)
        server.terminate()
    finally:
        server.stop(timeout=0)


def test_unexpected_identity_probe_error_is_wrapped_and_rolled_back(monkeypatch):
    server = ValkeyServer()
    workdir = server.data_dir
    observed_pid = []

    def fail_tcp_probe(identity, _timeout):
        observed_pid.append(identity.pid)
        raise RuntimeError("simulated probe close failure")

    monkeypatch.setattr(server, "_verify_tcp_identity", fail_tcp_probe)
    try:
        with pytest.raises(ServerStartError, match="simulated probe close failure"):
            server.start(timeout=2)

        assert len(observed_pid) == 1
        assert not psutil.pid_exists(observed_pid[0])
        assert server.port is None
        assert server._process is None
        assert not server._atexit_registered
        assert not os.path.exists(workdir)
    finally:
        for pid in observed_pid:
            _kill_owned_pid(pid)
        server.stop(timeout=0)


def test_config_write_error_is_wrapped_without_leaking_state(monkeypatch):
    server = ValkeyServer()
    workdir = server.data_dir
    monkeypatch.setattr(
        server,
        "_write_config",
        lambda: (_ for _ in ()).throw(OSError("simulated config write failure")),
    )

    with pytest.raises(ServerStartError, match="simulated config write failure"):
        server.start(timeout=1)

    assert server.port is None
    assert server._process is None
    assert not os.path.exists(workdir)


def test_atexit_registration_error_terminates_launched_process(monkeypatch):
    server = ValkeyServer()
    workdir = server.data_dir
    launched_pids = []
    original_launch = server._launch_process

    def tracked_launch():
        original_launch()
        assert server._process is not None
        launched_pids.append(server._process.pid)

    def fail_register(_callback):
        raise RuntimeError("simulated atexit registration failure")

    monkeypatch.setattr(server, "_launch_process", tracked_launch)
    monkeypatch.setattr(atexit, "register", fail_register)
    try:
        with pytest.raises(
            ServerStartError, match="simulated atexit registration failure"
        ):
            server.start(timeout=2)

        assert len(launched_pids) == 1
        assert not psutil.pid_exists(launched_pids[0])
        assert server.port is None
        assert server._process is None
        assert not server._atexit_registered
        assert not os.path.exists(workdir)
    finally:
        for pid in launched_pids:
            _kill_owned_pid(pid)
        server.stop(timeout=0)


def test_failed_start_restores_preexisting_user_runtime_files(tmp_path):
    data_dir = tmp_path / "user-data"
    data_dir.mkdir()
    config_path = data_dir / "valkey.conf"
    log_path = data_dir / "valkey.log"
    rdb_path = data_dir / "valkey.db"
    aof_path = data_dir / "appendonly.aof"
    config_content = b"caller-owned config\n"
    log_content = b"caller-owned log\n"
    config_path.write_bytes(config_content)
    log_path.write_bytes(log_content)
    rdb_path.write_bytes(b"rdb sentinel")
    aof_path.write_bytes(b"aof sentinel")
    os.chmod(config_path, 0o640)
    os.chmod(log_path, 0o640)

    server = ValkeyServer(
        data_dir=str(data_dir),
        config={"loglevel": "bogus-level"},
    )
    with pytest.raises(ServerStartError):
        server.start(timeout=2)

    assert data_dir.is_dir()
    assert config_path.read_bytes() == config_content
    assert log_path.read_bytes() == log_content
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o640
    assert rdb_path.read_bytes() == b"rdb sentinel"
    assert aof_path.read_bytes() == b"aof sentinel"
    assert not os.path.exists(server.pidfile)
    assert not os.path.exists(server.socket_file)
    assert server.port is None


def test_failed_start_retains_persistent_owned_directory():
    server = ValkeyServer(
        persist=True,
        config={"loglevel": "bogus-level"},
    )
    workdir = server.data_dir
    try:
        with pytest.raises(ServerStartError):
            server.start(timeout=2)

        assert os.path.isdir(workdir)
        assert os.listdir(workdir) == []
        assert server.port is None
    finally:
        server.stop(timeout=0)
        shutil.rmtree(workdir, ignore_errors=True)


def test_failed_start_removes_fallback_socket_directory(tmp_path):
    deep_dir = tmp_path / ("deep-" + ("x" * 90))
    server = ValkeyServer(
        data_dir=str(deep_dir),
        config={"loglevel": "bogus-level"},
    )
    socket_dir = server._socket_dir
    assert socket_dir is not None

    with pytest.raises(ServerStartError):
        server.start(timeout=2)

    assert deep_dir.is_dir()
    assert not os.path.exists(socket_dir)
    assert server.port is None


def test_non_executable_binary_is_actionable_and_transactional(monkeypatch, tmp_path):
    executable = tmp_path / "valkey-server"
    executable.write_text("not executable")
    os.chmod(executable, 0o600)
    monkeypatch.setattr(valkey_embedded, "__valkey_executable__", str(executable))
    server = ValkeyServer()
    workdir = server.data_dir

    with pytest.raises(ServerStartError, match="Permission denied"):
        server.start(timeout=1)

    assert server.port is None
    assert server._process is None
    assert not os.path.exists(workdir)


@pytest.mark.parametrize("failure_mode", ["create", "close"])
def test_probe_client_creation_or_close_error_is_transactional(
    monkeypatch, failure_mode
):
    server = ValkeyServer()
    workdir = server.data_dir
    launched_pids = []
    original_launch = server._launch_process
    original_private_client = server._private_client

    def tracked_launch():
        original_launch()
        assert server._process is not None
        launched_pids.append(server._process.pid)

    def private_client(**kwargs):
        if failure_mode == "create":
            raise RuntimeError("simulated probe creation failure")
        client = original_private_client(**kwargs)

        class CloseFailureClient:
            def ping(self):
                return client.ping()

            def info(self, section):
                return client.info(section)

            def close(self):
                client.close()
                raise RuntimeError("simulated probe close failure")

        return CloseFailureClient()

    monkeypatch.setattr(server, "_launch_process", tracked_launch)
    monkeypatch.setattr(server, "_private_client", private_client)
    try:
        with pytest.raises(ServerStartError, match="simulated probe"):
            server.start(timeout=2)

        assert len(launched_pids) == 1
        assert not psutil.pid_exists(launched_pids[0])
        assert not os.path.exists(workdir)
        assert server.port is None
        assert not server._atexit_registered
    finally:
        for pid in launched_pids:
            _kill_owned_pid(pid)
        server.stop(timeout=0)


def test_readiness_timeout_is_bounded_and_transactional(monkeypatch):
    server = ValkeyServer()
    workdir = server.data_dir
    launched_pids = []
    original_launch = server._launch_process

    def tracked_launch():
        original_launch()
        assert server._process is not None
        launched_pids.append(server._process.pid)

    def not_ready(_timeout):
        raise _StartupNotReady("simulated probe remains unavailable")

    monkeypatch.setattr(server, "_launch_process", tracked_launch)
    monkeypatch.setattr(server, "_probe_private_identity", not_ready)
    try:
        with pytest.raises(
            ServerStartError, match="simulated probe remains unavailable"
        ):
            server.start(timeout=0.2)

        assert len(launched_pids) == 1
        assert not psutil.pid_exists(launched_pids[0])
        assert not os.path.exists(workdir)
        assert server.port is None
    finally:
        for pid in launched_pids:
            _kill_owned_pid(pid)
        server.stop(timeout=0)


def test_atexit_unregister_error_does_not_mask_startup_failure(monkeypatch):
    server = ValkeyServer()

    def fail_probe(_identity, _timeout):
        raise RuntimeError("primary startup failure")

    def fail_unregister(_callback):
        raise RuntimeError("secondary unregister failure")

    monkeypatch.setattr(server, "_verify_tcp_identity", fail_probe)
    monkeypatch.setattr(atexit, "unregister", fail_unregister)

    with pytest.raises(ServerStartError, match="primary startup failure"):
        server.start(timeout=2)

    assert server.port is None
    assert server._process is None
    assert not server._atexit_registered


def test_retry_succeeds_after_complete_launch_rollback(monkeypatch):
    server = ValkeyServer()
    original_popen = subprocess.Popen
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("first launch fails")
        return original_popen(*args, **kwargs)

    monkeypatch.setattr("valkey_embedded.server.subprocess.Popen", fail_once)
    try:
        with pytest.raises(ServerStartError, match="first launch fails"):
            server.start(timeout=1)
        assert server.port is None

        server.start(timeout=2)
        assert server.is_running()
        assert server.client().ping() is True
    finally:
        server.stop(timeout=0)


def test_repeated_failed_starts_do_not_grow_fds_or_leave_runtime_state():
    server = ValkeyServer(config={"loglevel": "bogus-level"})
    process = psutil.Process()
    before_fds = process.num_fds()

    for _ in range(25):
        with pytest.raises(ServerStartError):
            server.start(timeout=1)
        assert server.port is None
        assert server._process is None
        assert not server._atexit_registered
        assert not os.path.exists(server.data_dir)

    after_fds = process.num_fds()
    assert after_fds <= before_fds + 1
    server.stop(timeout=0)
