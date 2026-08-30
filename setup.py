# setup.py
"""Build customization for valkey_embedded.

All package metadata lives in pyproject.toml (PEP 621). This file exists only to
(1) compile the embedded valkey-server at build time, (2) declare a tiny dummy C
extension so the wheel is platform-specific (not py3-none-any), and (3) empty the
owned build-lib directory so stale packages cannot contaminate the artifact.

The Valkey compile runs in a build_py subclass because setuptools runs build_py
(which copies package_data, including bin/valkey-server) BEFORE build_ext. Doing
the compile here guarantees the binary exists when package data is collected, so
the wheel is never assembled without it (critical for clean CI builds).
"""

import os
import shutil
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_py import build_py

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tools"))
import build_valkey  # noqa: E402


class BuildPyWithValkey(build_py):
    """Compile Valkey and copy packages into an empty owned build directory."""

    def _remove_stale_build_lib(self) -> None:
        """Remove only this command's generated build-lib directory."""
        build = self.get_finalized_command("build")
        raw_build_lib = Path(self.build_lib).absolute()
        build_base = Path(build.build_base).resolve()
        source_dir = (Path(__file__).resolve().parent / "src").resolve()
        if raw_build_lib.is_symlink():
            raise RuntimeError("refusing to clean a symlinked build-lib directory")
        build_lib = raw_build_lib.resolve()
        try:
            relative = build_lib.relative_to(build_base)
        except ValueError as exc:
            raise RuntimeError(
                "build-lib must be inside the configured build base"
            ) from exc
        if (
            not relative.parts
            or build_lib == source_dir
            or source_dir in build_lib.parents
        ):
            raise RuntimeError("refusing to clean a source directory as build output")
        if build_lib.exists():
            shutil.rmtree(build_lib)

    def run(self) -> None:
        target_bin = os.path.join("src", "valkey_embedded", "bin")
        metadata = os.path.join("src", "valkey_embedded", "package_metadata.json")
        build_valkey.build(target_bin, metadata)
        # setuptools otherwise reuses build/lib.*, allowing removed or renamed
        # packages from a previous invocation to leak into a new wheel.
        self._remove_stale_build_lib()
        super().run()


setup(
    ext_modules=[
        Extension("valkey_embedded._dummy", sources=["src/valkey_embedded/_dummy.c"]),
    ],
    cmdclass={"build_py": BuildPyWithValkey},
)
