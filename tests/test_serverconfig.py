# tests/test_serverconfig.py
"""serverconfig overrides and security defaults must reach the running server.

The rest of the suite only checks rendered config text; here we assert the live
server reflects it via CONFIG GET and on-disk socket state.
"""

import os
import stat

from valkey_embedded import Valkey


def _config_value(conn, name):
    # decode_responses=True is set on the client, so CONFIG GET returns str.
    return conn.config_get(name)[name]


def test_serverconfig_maxmemory_applied():
    conn = Valkey(serverconfig={"maxmemory": "64mb"}, decode_responses=True)
    try:
        assert _config_value(conn, "maxmemory") == "67108864"
    finally:
        conn._cleanup()


def test_serverconfig_databases_applied():
    conn = Valkey(serverconfig={"databases": "32"}, decode_responses=True)
    try:
        assert _config_value(conn, "databases") == "32"
    finally:
        conn._cleanup()


def test_default_save_points_reach_server():
    # The curated default `save` list (a list-valued directive) must render as
    # multiple lines and be merged by the server into its snapshot schedule.
    conn = Valkey(decode_responses=True)
    try:
        assert _config_value(conn, "save") == "900 1 300 100 60 200 15 1000"
    finally:
        conn._cleanup()


def test_serverconfig_can_disable_save():
    # Disabling snapshots is done with a quoted empty string (omitting `save`
    # via None would instead fall back to Valkey's compiled-in default).
    conn = Valkey(serverconfig={"save": '""'}, decode_responses=True)
    try:
        assert _config_value(conn, "save") == ""
    finally:
        conn._cleanup()


def test_default_has_no_tcp_listener():
    # Security default: port 0 means unix-socket only, no TCP exposure.
    conn = Valkey(decode_responses=True)
    try:
        assert _config_value(conn, "port") == "0"
    finally:
        conn._cleanup()


def test_socket_is_owner_only():
    conn = Valkey()
    try:
        mode = stat.S_IMODE(os.stat(conn.socket_file).st_mode)
        assert mode == 0o700, "socket mode is {0:o}, expected 700".format(mode)
    finally:
        conn._cleanup()


def test_custom_unix_socket_path(tmp_path):
    sock = str(tmp_path / "custom.sock")
    conn = Valkey(unix_socket_path=sock)
    try:
        assert conn.socket_file == sock
        assert os.path.exists(sock)
        assert conn.ping() is True
    finally:
        conn._cleanup()
