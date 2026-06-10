# tools/build_valkey.py
"""Download a pinned Valkey release, verify its checksum, compile it, and emit
the server binary plus package_metadata.json.

Usable two ways:
  * standalone for local dev:  python tools/build_valkey.py
  * from setup.py at wheel build time (see setup.py)

Checksum policy: the SHA-256 of each release tarball must be pinned in
KNOWN_SHA256. If a version is unpinned, the build fails and prints the computed
hash so it can be recorded (set VALKEY_ALLOW_UNPINNED=1 to bypass for local dev
only). This closes the supply-chain hole of compiling an unverified download.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict

VALKEY_VERSION = os.environ.get("VALKEY_VERSION", "8.1.8")

# sha256(valkey-<version>.tar.gz). Populate per the README "Pinning" procedure.
KNOWN_SHA256: Dict[str, str] = {
    "8.1.8": "0edc455ba7524f0cfa4f73fdc70b91dec6941e893a09bcbdd012470d08043cec",
}

# NOTE: GitHub's auto-generated archive tarballs are not guaranteed byte-stable
# over time. The pinned SHA-256 protects each download; if upstream ever
# regenerates the archive the build fails loudly rather than silently compiling
# different bytes. Follow-up: prefer an uploaded release asset if one exists.
DOWNLOAD_URL = "https://github.com/valkey-io/valkey/archive/refs/tags/{version}.tar.gz"


def _preflight() -> None:
    """Fail fast, before any download, on platforms that cannot build Valkey."""
    if os.name != "posix":
        raise SystemExit(
            "ERROR: valkey-embedded requires a POSIX platform; Windows is "
            "unsupported (WSL works). Prebuilt wheels cover Linux x86_64 and "
            "macOS 14+ arm64."
        )
    if shutil.which("make") is None or not any(
        shutil.which(cc) for cc in ("cc", "gcc", "clang")
    ):
        raise SystemExit(
            "ERROR: building valkey-embedded's bundled Valkey server needs "
            "`make` and a C compiler (cc/gcc/clang). Install them (e.g. "
            "`apt install make gcc`, or the Xcode command line tools) and retry."
        )


def _download(version: str, dest: Path) -> None:
    url = DOWNLOAD_URL.format(version=version)
    print("Downloading {0}".format(url))
    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, version: str) -> None:
    actual = _sha256(path)
    expected = KNOWN_SHA256.get(version, "")
    if not expected:
        msg = (
            "No pinned SHA-256 for Valkey {0}.\n"
            "Computed: {1}\n"
            "Record it in KNOWN_SHA256, then rebuild.".format(version, actual)
        )
        if os.environ.get("VALKEY_ALLOW_UNPINNED") == "1":
            # The bypass is for local development only; in CI it would
            # silently neuter the supply-chain control, so refuse it there.
            if os.environ.get("CI"):
                raise SystemExit(
                    "ERROR: VALKEY_ALLOW_UNPINNED is refused when CI is set. " + msg
                )
            print("WARNING (unpinned, allowed by env): " + msg)
            return
        raise SystemExit("ERROR: " + msg)
    if actual != expected:
        raise SystemExit(
            "ERROR: checksum mismatch for Valkey {0}\n"
            "  expected {1}\n  actual   {2}".format(version, expected, actual)
        )
    print("Checksum OK for Valkey {0}".format(version))


def _validate_members(tf: tarfile.TarFile, into: Path) -> None:
    """Reject absolute paths and traversal outside the extraction dir."""
    base = into.resolve()
    for member in tf.getmembers():
        target = (base / member.name).resolve()
        if not str(target).startswith(str(base) + os.sep):
            raise SystemExit(
                "ERROR: tarball member escapes extraction dir: " + member.name
            )


def _extract(tarball: Path, into: Path, version: str) -> Path:
    with tarfile.open(tarball) as tf:
        # filter="data" guards against path traversal (CVE-2007-4559). It was
        # backported to 3.9.17/3.10.12/3.11.4, so attempt it everywhere and
        # validate members manually only on older point releases.
        try:
            tf.extractall(into, filter="data")
        except TypeError:
            _validate_members(tf, into)
            tf.extractall(into)  # noqa: S202 - members validated above
    # Archive extracts to valkey-<version>/
    return into / "valkey-{0}".format(version)


def _compile(src_dir: Path) -> None:
    jobs = str(os.cpu_count() or 2)
    # MALLOC=libc avoids the jemalloc build dependency (as redislite does).
    subprocess.check_call(
        ["make", "-j", jobs, "MALLOC=libc", "BUILD_TLS=no"], cwd=str(src_dir)
    )


def _server_version(binary: Path) -> str:
    out = subprocess.check_output([str(binary), "--version"], text=True)
    return out.strip()


def _project_version() -> str:
    """Single-source the package version from pyproject.toml.

    A line scan rather than tomllib so it works on Python 3.9/3.10 at sdist
    build time with no extra build dependency.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        for line in pyproject.read_text().splitlines():
            if line.startswith("version"):
                return line.split('"')[1]
    except (OSError, IndexError):
        pass
    return "0.0.0"


def _is_current(target: Path, metadata_path: str, version: str) -> bool:
    """True if the built binary for this Valkey version is already in place."""
    if os.environ.get("VALKEY_FORCE_REBUILD") == "1":
        return False
    server = target / "valkey-server"
    if not server.exists():
        return False
    try:
        with open(metadata_path) as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        return False
    return existing.get("valkey_server_version") == version


def build(
    target_bin_dir: str,
    metadata_path: str,
    version: str = VALKEY_VERSION,
) -> None:
    """Build Valkey and place valkey-server/valkey-cli in target_bin_dir."""
    target = Path(target_bin_dir)
    if _is_current(target, metadata_path, version):
        print("Reusing existing valkey-server {0} (cached build)".format(version))
        return
    _preflight()
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        workdir = Path(work)
        tarball = workdir / "valkey-{0}.tar.gz".format(version)
        _download(version, tarball)
        _verify(tarball, version)
        src_dir = _extract(tarball, workdir, version)
        _compile(src_dir)
        for name in ("valkey-server", "valkey-cli"):
            built = src_dir / "src" / name
            shutil.copy2(built, target / name)
            os.chmod(target / name, 0o755)
        # Bundle Valkey's license plus the licenses of third-party code
        # statically linked into the binary; binary-redistribution clauses
        # require reproducing those notices. jemalloc is excluded because the
        # build uses MALLOC=libc.
        license_sources = (
            "COPYING",
            "deps/lua/COPYRIGHT",
            "deps/hdr_histogram/LICENSE.txt",
            "deps/fpconv/LICENSE.txt",
        )
        with open(target / "VALKEY_COPYING.txt", "w") as bundle:
            for rel in license_sources:
                lic = src_dir / rel
                if not lic.exists():
                    print("NOTE: license file missing from tarball: " + rel)
                    continue
                bundle.write("----- {0} (Valkey {1}) -----\n".format(rel, version))
                bundle.write(lic.read_text())
                bundle.write("\n")
            # linenoise (used by the bundled valkey-cli) ships its BSD-2
            # license only as the leading comment of its source file.
            linenoise = src_dir / "deps" / "linenoise" / "linenoise.c"
            if linenoise.exists():
                header = linenoise.read_text().split("*/", 1)[0] + "*/"
                bundle.write(
                    "----- deps/linenoise/linenoise.c license header "
                    "(Valkey {0}) -----\n".format(version)
                )
                bundle.write(header)
                bundle.write("\n")
        server = target / "valkey-server"
        version_line = _server_version(server)

    metadata = {
        "valkey_embedded_version": _project_version(),
        "valkey_server_version": version,
        "valkey_server_banner": version_line,
        # Package-relative: resolved against the package dir at import time
        # (an absolute path would bake the build host's tree into the wheel).
        "valkey_executable": "bin/valkey-server",
    }
    with open(metadata_path, "w") as fh:
        json.dump(metadata, fh, indent=2)
    print("Built {0} -> {1}".format(version_line, server))


if __name__ == "__main__":
    here = Path(__file__).resolve().parent.parent
    build(
        str(here / "src" / "valkey_embedded" / "bin"),
        str(here / "src" / "valkey_embedded" / "package_metadata.json"),
    )
