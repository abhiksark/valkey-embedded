# tests/test_negative.py
"""Failure paths and the StrictValkey alias as a live client."""

import glob
import os
import tempfile

import pytest

import valkey_embedded
from valkey_embedded import StrictValkey, Valkey
from valkey_embedded.client import ServerStartError

_TEMPDIR_GLOB = os.path.join(tempfile.gettempdir(), "valkey_embedded-*")


def test_missing_binary_raises_and_does_not_leak(monkeypatch):
    # _start_server reads the module-level executable path at call time.
    monkeypatch.setattr(
        valkey_embedded, "__valkey_executable__", "/no/such/valkey-server"
    )
    before = set(glob.glob(_TEMPDIR_GLOB))
    with pytest.raises(ServerStartError):
        Valkey()
    leaked = set(glob.glob(_TEMPDIR_GLOB)) - before
    assert leaked == set(), "failed start leaked temp dirs: {0}".format(leaked)


def test_strictvalkey_is_a_usable_client():
    conn = StrictValkey()
    try:
        assert conn.ping() is True
        conn.set("k", "v")
        assert conn.get("k") == b"v"
    finally:
        conn._cleanup()
