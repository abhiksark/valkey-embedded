# tests/test_server.py
"""ValkeyServer: explicit lifecycle control with a TCP endpoint.

The other half of the API from Valkey() -- here the server listens on TCP so
any Redis-compatible client can connect via host/port, and the caller controls
start/stop/terminate explicitly.
"""

import os
import socket
import threading

import psutil
import pytest
import valkey

from valkey_embedded import ValkeyServer
from valkey_embedded.server import (
    ServerStartError,
    _find_free_port,
    _StartupIdentityMismatch,
)


def _port_is_listening(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


# -- start / stop --------------------------------------------------------


def test_context_manager_starts_and_stops():
    with ValkeyServer() as server:
        assert server.is_running()
        assert server.port and server.port > 0
        pid = server.pid
        assert psutil.pid_exists(pid)
    # Exiting the block stops the server.
    assert not psutil.pid_exists(pid)
    assert not server.is_running()


def test_explicit_start_stop():
    server = ValkeyServer()
    assert not server.is_running()
    server.start()
    try:
        assert server.is_running()
        assert _port_is_listening(server.host, server.port)
    finally:
        server.stop()
    assert not server.is_running()


def test_start_is_idempotent_while_running():
    server = ValkeyServer()
    server.start()
    try:
        pid = server.pid
        port = server.port

        server.start()

        assert server.pid == pid
        assert server.port == port
    finally:
        server.stop()


def test_stop_before_start_is_idempotent():
    server = ValkeyServer()
    workdir = server.data_dir

    server.stop()
    server.stop()

    assert server.port is None
    assert not os.path.exists(workdir)


def test_wait_until_ready_times_out_after_private_connection_errors(monkeypatch):
    server = ValkeyServer(port=6380)
    server.port = 6380
    moments = iter([0.0, 0.0, 1.0])

    class RunningProcess:
        pid = os.getpid()

        @staticmethod
        def poll():
            return None

    class UnavailableClient:
        @staticmethod
        def ping():
            raise valkey.exceptions.ConnectionError()

        @staticmethod
        def close():
            return None

    server._process = RunningProcess()
    with open(server.pidfile, "w") as fh:
        fh.write(str(RunningProcess.pid))

    monkeypatch.setattr(
        "valkey_embedded.server.time.monotonic", lambda: next(moments, 1.0)
    )
    monkeypatch.setattr("valkey_embedded.server.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        server, "_private_client", lambda **_kwargs: UnavailableClient()
    )

    try:
        with pytest.raises(
            ServerStartError, match="failed to start and prove daemon identity"
        ):
            server._wait_until_ready(timeout=0.5)
    finally:
        server.stop()


def test_tcp_client_can_connect_with_byo_client():
    with ValkeyServer() as server:
        # A plain valkey-py client (not ours) connects over host/port.
        client = valkey.Valkey(**server.connection_kwargs)
        try:
            client.set("k", "v")
            assert client.get("k") == b"v"
        finally:
            client.close()


def test_builtin_client_helper():
    with ValkeyServer() as server:
        client = server.client()
        try:
            assert client.ping() is True
        finally:
            client.close()


# -- ports ---------------------------------------------------------------


def test_specific_port_is_honored():
    port = _find_free_port()
    with ValkeyServer(port=port) as server:
        assert server.port == port
        assert _port_is_listening("127.0.0.1", port)


def test_auto_port_is_assigned_when_none():
    with ValkeyServer() as server:
        assert isinstance(server.port, int) and server.port > 0


def test_requested_port_collision_rejects_foreign_valkey():
    first = ValkeyServer()
    second = None
    first_client = None
    try:
        first.start()
        first_pid = first.pid
        first_client = first.client()
        first_client.set("owner", "first")

        second = ValkeyServer(port=first.port)
        failed_workdir = second.data_dir
        with pytest.raises(ServerStartError, match="Address already in use"):
            second.start(timeout=2.0)

        assert first.is_running()
        assert first.pid == first_pid
        assert first_client.get("owner") == b"first"
        assert second.port is None
        assert not second.is_running()
        with pytest.raises(RuntimeError, match="call start"):
            second.client()
        assert not os.path.exists(failed_workdir)
        assert not second._atexit_registered
    finally:
        if second is not None:
            second.stop(timeout=0)
        if first_client is not None:
            first_client.close()
        first.stop(timeout=0)


def test_plain_tcp_pong_listener_cannot_satisfy_readiness():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    stopping = threading.Event()

    def serve_pong() -> None:
        while not stopping.is_set():
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                try:
                    connection.recv(4096)
                    connection.sendall(b"+PONG\r\n")
                except OSError:
                    pass

    thread = threading.Thread(target=serve_pong, daemon=True)
    thread.start()
    server = ValkeyServer(port=port)
    failed_workdir = server.data_dir
    try:
        with pytest.raises(ServerStartError, match="Address already in use"):
            server.start(timeout=2.0)
        assert server.port is None
        assert not server.is_running()
        assert not os.path.exists(failed_workdir)
    finally:
        server.stop(timeout=0)
        stopping.set()
        listener.close()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_auto_port_collision_fails_without_adopting_foreign_server(monkeypatch):
    first = ValkeyServer()
    second = None
    try:
        first.start()
        monkeypatch.setattr(
            "valkey_embedded.server._find_free_port", lambda _host: first.port
        )
        second = ValkeyServer()
        failed_workdir = second.data_dir

        with pytest.raises(ServerStartError, match="Address already in use"):
            second.start(timeout=2.0)

        assert first.is_running()
        assert second.port is None
        assert not os.path.exists(failed_workdir)
    finally:
        if second is not None:
            second.stop(timeout=0)
        first.stop(timeout=0)


def test_identity_mismatch_rolls_back_owned_process_and_files(monkeypatch):
    server = ValkeyServer()
    failed_workdir = server.data_dir
    observed_pid = []

    def reject_tcp_identity(identity, _timeout):
        observed_pid.append(identity.pid)
        raise _StartupIdentityMismatch("simulated foreign TCP endpoint")

    monkeypatch.setattr(server, "_verify_tcp_identity", reject_tcp_identity)

    try:
        with pytest.raises(ServerStartError, match="identity mismatch"):
            server.start(timeout=2.0)

        assert len(observed_pid) == 1
        assert not psutil.pid_exists(observed_pid[0])
        assert not os.path.exists(failed_workdir)
        assert server.port is None
        assert server.pid == 0
        assert not server._atexit_registered
    finally:
        server.stop(timeout=0)


def test_successful_start_proves_private_and_tcp_identity():
    with ValkeyServer() as server:
        identity = server._identity
        assert identity is not None
        assert identity.pid == server.pid
        assert server.is_running()

        private_client = valkey.Valkey(unix_socket_path=server.socket_file)
        tcp_client = server.client()
        try:
            private_info = private_client.info("server")
            tcp_info = tcp_client.info("server")
        finally:
            private_client.close()
            tcp_client.close()

        assert private_info["process_id"] == identity.pid
        assert tcp_info["process_id"] == identity.pid
        assert private_info["run_id"] == identity.run_id
        assert tcp_info["run_id"] == identity.run_id
        assert private_info["tcp_port"] == server.port


def test_multiple_servers_are_independent():
    with ValkeyServer() as a, ValkeyServer() as b:
        assert a.port != b.port
        ca, cb = a.client(), b.client()
        try:
            ca.set("who", "a")
            cb.set("who", "b")
            assert ca.get("who") == b"a"
            assert cb.get("who") == b"b"
        finally:
            ca.close()
            cb.close()


# -- connection metadata -------------------------------------------------


def test_connection_kwargs_and_url():
    with ValkeyServer(host="127.0.0.1") as server:
        assert server.connection_kwargs == {"host": "127.0.0.1", "port": server.port}
        assert server.connection_url == "valkey://127.0.0.1:{0}".format(server.port)


def test_client_before_start_raises():
    server = ValkeyServer()
    # Misuse (forgot start()) is a RuntimeError with a remedy, not a
    # ServerStartError -- nothing failed to start.
    with pytest.raises(RuntimeError, match="call start"):
        server.client()


# -- config / persistence / terminate -----------------------------------


@pytest.mark.parametrize(
    "managed_key",
    [
        "bind",
        "daemonize",
        "dbdir",
        "dbfilename",
        "dir",
        "include",
        "logfile",
        "pidfile",
        "port",
        "unixsocket",
        "unixsocketperm",
    ],
)
def test_managed_runtime_config_overrides_are_rejected(managed_key):
    with pytest.raises(ValueError, match="managed lifecycle setting"):
        ValkeyServer(config={managed_key: "caller-value"})


def test_managed_runtime_config_override_check_is_case_insensitive():
    with pytest.raises(ValueError, match="PORT"):
        ValkeyServer(config={"PORT": "6380"})


def test_config_overrides_reach_server():
    with ValkeyServer(config={"maxmemory": "64mb"}) as server:
        client = server.client()
        try:
            # redis-py decodes CONFIG GET values to str regardless of bytes mode.
            assert client.config_get("maxmemory")["maxmemory"] == "67108864"
        finally:
            client.close()


def test_persist_keeps_data_dir_and_data(tmp_path):
    data_dir = str(tmp_path / "store")
    server = ValkeyServer(data_dir=data_dir, persist=True)
    server.start()
    server.client().set("kept", "yes")
    port = server.port
    server.stop()
    assert os.path.isdir(data_dir), "persisted data dir was removed"

    # Reopen the same data dir on the same port; data survived the RDB save.
    again = ValkeyServer(data_dir=data_dir, port=port, persist=True)
    again.start()
    try:
        assert again.client().get("kept") == b"yes"
    finally:
        again.stop()


def test_temp_data_dir_removed_on_stop():
    server = ValkeyServer()
    server.start()
    workdir = server.data_dir
    assert os.path.isdir(workdir)
    server.stop()
    assert not os.path.isdir(workdir), "temp data dir leaked after stop"


def test_terminate_kills_immediately():
    server = ValkeyServer()
    server.start()
    pid = server.pid
    server.terminate()
    assert not psutil.pid_exists(pid)
    assert not server.is_running()


def test_stop_cleans_up_when_graceful_shutdown_raises(monkeypatch):
    server = ValkeyServer()
    server.start()
    pid = server.pid
    workdir = server.data_dir
    shutdown_calls = []

    class FailingClient:
        @staticmethod
        def info(_section):
            assert server._identity is not None
            return {
                "process_id": server._identity.pid,
                "run_id": server._identity.run_id,
            }

        def shutdown(self, **kwargs):
            shutdown_calls.append(kwargs)
            raise RuntimeError("simulated shutdown failure")

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(server, "_private_client", lambda **_kwargs: FailingClient())

    try:
        server.stop(timeout=0)
        assert shutdown_calls == [{"nosave": True}]
        assert not psutil.pid_exists(pid)
        assert not os.path.exists(workdir)
        assert not server.is_running()
    finally:
        if psutil.pid_exists(pid):
            server.terminate()
