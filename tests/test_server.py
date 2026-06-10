# tests/test_server.py
"""ValkeyServer: explicit lifecycle control with a TCP endpoint.

The other half of the API from Valkey() -- here the server listens on TCP so
any Redis-compatible client can connect via host/port, and the caller controls
start/stop/terminate explicitly.
"""

import os
import socket

import psutil
import pytest
import valkey

from valkey_embedded import ValkeyServer
from valkey_embedded.server import _find_free_port


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
