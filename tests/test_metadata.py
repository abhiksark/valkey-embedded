# tests/test_metadata.py
"""Package metadata dunders are populated from the build artifact."""

import os

import valkey_embedded


def test_version_is_a_string():
    assert isinstance(valkey_embedded.__version__, str)
    assert valkey_embedded.__version__ != ""


def test_executable_points_at_a_real_binary():
    exe = valkey_embedded.__valkey_executable__
    assert exe
    assert os.path.exists(exe)


def test_server_version_recorded():
    # Plain dotted version (e.g. "8.1.8"), not the raw server banner.
    version = valkey_embedded.__valkey_server_version__
    assert version
    assert all(part.isdigit() for part in version.split("."))


def test_public_classes_exported():
    assert hasattr(valkey_embedded, "Valkey")
    assert hasattr(valkey_embedded, "StrictValkey")
    # Upstream alias: both names resolve to one class.
    assert valkey_embedded.StrictValkey is valkey_embedded.Valkey
