# setup.py
"""Build customization for valkey_embedded.

All package metadata lives in pyproject.toml (PEP 621). This file exists only to
(1) compile the embedded valkey-server at build time, and (2) declare a tiny
dummy C extension so the produced wheel is platform-specific (not py3-none-any).

The Valkey compile runs in a build_py subclass because setuptools runs build_py
(which copies package_data, including bin/valkey-server) BEFORE build_ext. Doing
the compile here guarantees the binary exists when package data is collected, so
the wheel is never assembled without it (critical for clean CI builds).
"""

import os
import sys

from setuptools import Extension, setup
from setuptools.command.build_py import build_py

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tools"))
import build_valkey  # noqa: E402


class BuildPyWithValkey(build_py):
    """Compile the embedded valkey-server BEFORE package data is collected."""

    def run(self) -> None:
        target_bin = os.path.join("src", "valkey_embedded", "bin")
        metadata = os.path.join("src", "valkey_embedded", "package_metadata.json")
        build_valkey.build(target_bin, metadata)
        super().run()


setup(
    ext_modules=[
        Extension("valkey_embedded._dummy", sources=["src/valkey_embedded/_dummy.c"]),
    ],
    cmdclass={"build_py": BuildPyWithValkey},
)
