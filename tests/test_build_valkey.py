# tests/test_build_valkey.py
"""Offline unit tests for the supply-chain build script (tools/build_valkey.py).

The checksum gate and the path-traversal-safe extraction are security controls,
so they are tested directly without downloading or compiling anything.
"""

import hashlib
import importlib.util
import io
import json
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


def _cached_build(tmp_path, version="8.1.8"):
    target = tmp_path / "bin"
    target.mkdir()
    for name in ("valkey-server", "valkey-cli"):
        binary = target / name
        binary.write_bytes(b"binary")
        binary.chmod(0o755)
    (target / "VALKEY_COPYING.txt").write_text(
        "\n".join(
            marker + "\n" + ("complete license text " * 30)
            for marker in build_valkey._license_markers(version)
        )
    )
    metadata = tmp_path / "package_metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "valkey_embedded_version": "old-project-version",
                "valkey_server_version": version,
                "valkey_server_banner": "old banner",
                "valkey_executable": "bin/valkey-server",
            }
        )
    )
    return target, metadata


def test_cached_build_requires_complete_executable_artifacts(tmp_path, monkeypatch):
    target, metadata = _cached_build(tmp_path)
    monkeypatch.setattr(
        build_valkey,
        "_server_version",
        lambda binary: "Valkey server v=8.1.8 build=test",
    )
    monkeypatch.setattr(
        build_valkey.subprocess,
        "check_output",
        lambda command, text: "valkey-cli 8.1.8",
    )

    assert build_valkey._is_current(target, str(metadata), "8.1.8") is True

    (target / "valkey-cli").chmod(0o644)
    assert build_valkey._is_current(target, str(metadata), "8.1.8") is False
    (target / "valkey-cli").chmod(0o755)
    (target / "valkey-cli").unlink()
    assert build_valkey._is_current(target, str(metadata), "8.1.8") is False
    (target / "valkey-cli").symlink_to(target / "valkey-server")
    assert build_valkey._is_current(target, str(metadata), "8.1.8") is False


def test_cached_build_requires_complete_license_bundle(tmp_path, monkeypatch):
    target, metadata = _cached_build(tmp_path)
    monkeypatch.setattr(
        build_valkey,
        "_server_version",
        lambda binary: "Valkey server v=8.1.8 build=test",
    )
    monkeypatch.setattr(
        build_valkey.subprocess,
        "check_output",
        lambda command, text: "valkey-cli 8.1.8",
    )
    (target / "VALKEY_COPYING.txt").write_text("incomplete")

    assert build_valkey._is_current(target, str(metadata), "8.1.8") is False


def test_reused_binary_refreshes_project_metadata(tmp_path, monkeypatch):
    target, metadata = _cached_build(tmp_path)
    monkeypatch.setattr(
        build_valkey,
        "_server_version",
        lambda binary: "Valkey server v=8.1.8 build=current",
    )
    monkeypatch.setattr(
        build_valkey.subprocess,
        "check_output",
        lambda command, text: "valkey-cli 8.1.8",
    )
    monkeypatch.setattr(build_valkey, "_project_version", lambda: "0.2.0")

    build_valkey.build(str(target), str(metadata), version="8.1.8")

    refreshed = json.loads(metadata.read_text())
    assert refreshed == {
        "valkey_embedded_version": "0.2.0",
        "valkey_server_version": "8.1.8",
        "valkey_server_banner": "Valkey server v=8.1.8 build=current",
        "valkey_executable": "bin/valkey-server",
    }


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
