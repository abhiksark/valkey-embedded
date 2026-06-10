# tests/test_build_valkey.py
"""Offline unit tests for the supply-chain build script (tools/build_valkey.py).

The checksum gate and the path-traversal-safe extraction are security controls,
so they are tested directly without downloading or compiling anything.
"""

import hashlib
import importlib.util
import io
import os
import sys
import tarfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "build_valkey", os.path.join(_ROOT, "tools", "build_valkey.py")
)
build_valkey = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_valkey)


def test_sha256_matches_hashlib(tmp_path):
    blob = b"valkey release bytes"
    f = tmp_path / "v.tar.gz"
    f.write_bytes(blob)
    assert build_valkey._sha256(f) == hashlib.sha256(blob).hexdigest()


def test_verify_passes_on_pinned_match(tmp_path, monkeypatch):
    f = tmp_path / "v.tar.gz"
    f.write_bytes(b"data")
    monkeypatch.setitem(build_valkey.KNOWN_SHA256, "9.9.9", build_valkey._sha256(f))
    build_valkey._verify(f, "9.9.9")  # must not raise


def test_verify_raises_on_checksum_mismatch(tmp_path, monkeypatch):
    f = tmp_path / "v.tar.gz"
    f.write_bytes(b"data")
    monkeypatch.setitem(build_valkey.KNOWN_SHA256, "9.9.9", "0" * 64)
    with pytest.raises(SystemExit):
        build_valkey._verify(f, "9.9.9")


def test_verify_refuses_unpinned_version(tmp_path, monkeypatch):
    f = tmp_path / "v.tar.gz"
    f.write_bytes(b"data")
    monkeypatch.delenv("VALKEY_ALLOW_UNPINNED", raising=False)
    with pytest.raises(SystemExit):
        build_valkey._verify(f, "0.0.0-unpinned")


def test_verify_allows_unpinned_with_env(tmp_path, monkeypatch):
    f = tmp_path / "v.tar.gz"
    f.write_bytes(b"data")
    monkeypatch.setenv("VALKEY_ALLOW_UNPINNED", "1")
    # The bypass is local-dev only; clear CI (set on hosted runners) to
    # exercise the local-dev path.
    monkeypatch.delenv("CI", raising=False)
    build_valkey._verify(f, "0.0.0-unpinned")  # bypass allowed for local dev


def test_verify_refuses_unpinned_bypass_in_ci(tmp_path, monkeypatch):
    f = tmp_path / "v.tar.gz"
    f.write_bytes(b"data")
    monkeypatch.setenv("VALKEY_ALLOW_UNPINNED", "1")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(SystemExit, match="refused when CI"):
        build_valkey._verify(f, "0.0.0-unpinned")


def test_default_version_is_pinned():
    version = build_valkey.VALKEY_VERSION
    assert version in build_valkey.KNOWN_SHA256, "default VALKEY_VERSION is unpinned"
    assert len(build_valkey.KNOWN_SHA256[version]) == 64


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="tarfile extraction filter='data' is only available on 3.12+",
)
def test_extract_blocks_path_traversal(tmp_path):
    malicious = tmp_path / "mal.tar"
    payload = b"pwned"
    with tarfile.open(malicious, "w") as tf:
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))

    into = tmp_path / "into"
    into.mkdir()
    with pytest.raises(Exception):
        build_valkey._extract(malicious, into, "x")
    assert not (tmp_path / "escape.txt").exists(), "path traversal escaped dest"
