# src/valkey_embedded/debug.py
"""Environment diagnostics for bug reports: python -m valkey_embedded.debug."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from typing import List

import valkey_embedded


def debug_info_list() -> List[str]:
    """Collect environment diagnostics, one human-readable line per item."""
    exe = valkey_embedded.__valkey_executable__
    runnable = bool(exe) and os.path.exists(exe)
    lines = [
        "valkey_embedded version: {0}".format(valkey_embedded.__version__),
        "module path: {0}".format(os.path.dirname(valkey_embedded.__file__)),
        "embedded valkey version: {0}".format(
            valkey_embedded.__valkey_server_version__
        ),
        "valkey-server path: {0}".format(exe),
        "python version: {0}".format(sys.version.replace("\n", " ")),
        "platform: {0}".format(platform.platform()),
        "valkey-server runnable: {0}".format(runnable),
    ]
    if runnable:
        try:
            out = subprocess.check_output([exe, "--version"], text=True).strip()
            lines.append("valkey-server --version: {0}".format(out))
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            lines.append("valkey-server --version failed: {0}".format(exc))
    return lines


def debug_info() -> str:
    """The diagnostics joined into one report string (for bug reports)."""
    return os.linesep.join(debug_info_list())


def print_debug_info() -> None:
    """Print the diagnostics report to stdout."""
    print(debug_info())


if __name__ == "__main__":
    print_debug_info()
