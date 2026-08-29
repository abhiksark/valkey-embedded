# tests/test_socket_fallback.py
"""Deep working directories must not break the unix socket (sun_path limit).

AF_UNIX socket paths are capped at 104 bytes on macOS (108 on Linux). pytest's
tmp_path on macOS CI runners alone is ~100 characters, so a socket placed in
the data directory cannot bind. The library relocates the socket to a short
private dir in that case; these tests force the overflow on every platform.
"""

import os

from valkey_embedded import Valkey, ValkeyServer
from valkey_embedded.client import _SUN_PATH_LIMIT, _socket_path_for


def _deep_dir(tmp_path):
    deep = tmp_path
    for _ in range(12):
        deep = deep / "deepdirname"
    deep.mkdir(parents=True, exist_ok=True)
    assert len(str(deep)) > _SUN_PATH_LIMIT
    return deep


def test_socket_path_for_keeps_short_paths_in_place(tmp_path):
    path, owned = _socket_path_for("/tmp", "s.sock")
    assert path == "/tmp/s.sock"
    assert owned is None


def test_socket_path_for_relocates_long_paths(tmp_path):
    deep = _deep_dir(tmp_path)
    path, owned = _socket_path_for(str(deep), "valkey.socket")
    try:
        assert owned is not None
        assert path.startswith(owned)
        assert len(os.fsencode(path)) < _SUN_PATH_LIMIT
    finally:
        if owned:
            os.rmdir(owned)


def test_valkey_works_from_a_deep_dbdir(tmp_path):
    deep = _deep_dir(tmp_path)
    conn = Valkey(str(deep / "data.db"))
    socket_dir = conn._socket_dir
    try:
        assert conn.ping() is True
        assert socket_dir is not None, "expected the short-socket fallback"
        assert not conn.socket_file.startswith(str(deep))
    finally:
        conn._cleanup()
    assert not os.path.exists(socket_dir), "fallback socket dir leaked"
    assert (deep / "data.db").parent.exists(), "data dir must be kept"


def test_valkey_server_works_from_a_deep_data_dir(tmp_path):
    deep = _deep_dir(tmp_path)
    with ValkeyServer(data_dir=str(deep)) as server:
        socket_dir = server._socket_dir
        assert socket_dir is not None, "expected the short-socket fallback"
        assert server.client().ping() is True
    assert not os.path.exists(socket_dir), "fallback socket dir leaked"
    assert deep.exists(), "user-supplied data dir must be kept"
