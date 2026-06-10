# tests/test_patch.py
"""Tests for patching valkey.Valkey / valkey.StrictValkey."""

import valkey

import valkey_embedded
from valkey_embedded import patch


def test_patch_and_unpatch_restore_originals():
    original = valkey.Valkey
    original_sv = valkey.StrictValkey
    patch.patch_valkey()
    try:
        assert valkey.Valkey is valkey_embedded.Valkey
        assert valkey.StrictValkey is valkey_embedded.StrictValkey
    finally:
        patch.unpatch_valkey()
    assert valkey.Valkey is original
    assert valkey.StrictValkey is original_sv


def test_patch_is_idempotent():
    patch.patch_valkey()
    patch.patch_valkey()  # second call is a no-op, not a double-wrap
    try:
        assert valkey.Valkey is valkey_embedded.Valkey
    finally:
        patch.unpatch_valkey()


def test_patched_client_starts_embedded_server():
    patch.patch_valkey()
    try:
        conn = valkey.Valkey()
        try:
            assert conn.ping() is True
        finally:
            conn._cleanup()
    finally:
        patch.unpatch_valkey()


def test_per_class_patch_and_unpatch():
    original = valkey.Valkey
    patch.patch_valkey_Valkey()
    try:
        assert valkey.Valkey is valkey_embedded.Valkey
    finally:
        patch.unpatch_valkey_Valkey()
    assert valkey.Valkey is original


def test_unpatch_when_never_patched_is_safe():
    original = valkey.Valkey
    # No patch applied; unpatch must be a harmless no-op, not raise.
    patch.unpatch_valkey()
    assert valkey.Valkey is original


def test_patched_strictvalkey_starts_embedded_server():
    patch.patch_valkey()
    try:
        conn = valkey.StrictValkey()
        try:
            assert conn.ping() is True
        finally:
            conn._cleanup()
    finally:
        patch.unpatch_valkey()


def test_patch_with_dbfile_sets_persistent_location(tmp_path):
    dbfile = str(tmp_path / "patched.db")
    patch.patch_valkey(dbfile=dbfile)
    try:
        assert valkey_embedded.Valkey.settingregistryfile == dbfile + ".settings"
    finally:
        patch.unpatch_valkey()
        # Reset class-level state mutated by the dbfile patch (symmetric cleanup
        # so nothing leaks into later test modules).
        valkey_embedded.Valkey.settingregistryfile = None
        valkey_embedded.Valkey.dbdir = None
        valkey_embedded.Valkey.dbfilename = "valkey.db"
