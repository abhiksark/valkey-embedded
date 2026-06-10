# tests/test_packaging.py
"""Distribution contract: the sdist must exclude the compiled binary and ship
the build script; the wheel must be platform-tagged and bundle the binary,
license, and metadata; an end user must be able to pip-install the wheel and run.

Tiers:
  * `packaging` (offline, fast): inspect any prebuilt wheel under dist/.
  * `slow` (network): build the sdist and pip-install the wheel in a clean venv.
Heavy tests skip cleanly when their inputs (a wheel, or network) are missing.
"""

import glob
import os
import subprocess
import sys
import tarfile
import venv
import zipfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WHEELS = sorted(glob.glob(os.path.join(_ROOT, "dist", "*.whl")))


# -- wheel inspection (offline) ------------------------------------------------


@pytest.mark.packaging
@pytest.mark.skipif(not _WHEELS, reason="no prebuilt wheel in dist/")
def test_wheel_is_platform_specific():
    name = os.path.basename(_WHEELS[-1])
    assert "py3-none-any" not in name, "wheel is not platform-specific: " + name


@pytest.mark.packaging
@pytest.mark.skipif(not _WHEELS, reason="no prebuilt wheel in dist/")
def test_wheel_bundles_binary_license_and_metadata():
    with zipfile.ZipFile(_WHEELS[-1]) as zf:
        names = zf.namelist()
    for expected in (
        "valkey_embedded/bin/valkey-server",
        "valkey_embedded/bin/VALKEY_COPYING.txt",
        "valkey_embedded/package_metadata.json",
        "valkey_embedded/py.typed",
    ):
        assert any(n.endswith(expected) for n in names), "wheel missing " + expected


# -- sdist build (network: build isolation installs the build backend) ---------


def _build_sdist(outdir):
    try:
        subprocess.check_call(
            [sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir), _ROOT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip("sdist build unavailable (no network/backend): {0}".format(exc))
    tarballs = glob.glob(os.path.join(str(outdir), "*.tar.gz"))
    assert tarballs, "build produced no sdist"
    return tarballs[0]


@pytest.mark.slow
def test_sdist_excludes_compiled_binary(tmp_path):
    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist) as tf:
        names = tf.getnames()
    assert not any(n.endswith("bin/valkey-server") for n in names), (
        "sdist must not ship the compiled binary"
    )
    assert not any(n.endswith("package_metadata.json") for n in names), (
        "sdist must not ship build-time metadata"
    )
    # Library-only sdist policy (see [tool.check-manifest] in pyproject.toml).
    for excluded in ("/tests/", "/examples/", "/docs/", "/.github/"):
        assert not any(excluded in n for n in names), "sdist must not ship " + excluded


@pytest.mark.slow
def test_sdist_includes_build_script_and_sources(tmp_path):
    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist) as tf:
        names = "\n".join(tf.getnames())
    for expected in (
        "tools/build_valkey.py",
        "src/valkey_embedded/client.py",
        "pyproject.toml",
        "LICENSE.txt",
    ):
        assert expected in names, "sdist missing " + expected


# -- end-user install path (network: installs runtime deps from PyPI) ----------


@pytest.mark.slow
@pytest.mark.skipif(not _WHEELS, reason="no prebuilt wheel in dist/")
def test_wheel_installs_and_runs_in_clean_venv(tmp_path):
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    py = env_dir / "bin" / "python"
    try:
        subprocess.check_call(
            [str(py), "-m", "pip", "install", "--quiet", _WHEELS[-1]],
            stdout=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip("wheel install unavailable (no network): {0}".format(exc))
    smoke = "import valkey_embedded; c = valkey_embedded.Valkey(); assert c.ping(); print('OK')"
    out = subprocess.check_output([str(py), "-c", smoke], text=True)
    assert "OK" in out
