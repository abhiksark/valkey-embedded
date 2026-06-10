# tests/test_debug.py
"""Tests for python -m valkey_embedded.debug."""

import subprocess
import sys

from valkey_embedded import debug


def test_debug_info_lists_key_facts():
    lines = debug.debug_info_list()
    blob = "\n".join(lines)
    assert "valkey_embedded version:" in blob
    assert "embedded valkey version:" in blob
    assert "valkey-server runnable: True" in blob


def test_debug_info_includes_environment_facts():
    blob = debug.debug_info()
    assert "python version:" in blob
    assert "platform:" in blob
    assert "module path:" in blob
    assert "valkey-server path:" in blob


def test_debug_module_runs_as_main():
    result = subprocess.run(
        [sys.executable, "-m", "valkey_embedded.debug"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "valkey-server" in result.stdout
