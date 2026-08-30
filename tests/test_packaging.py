"""Build and distribution contracts for source archives and wheels.

The fast ``packaging`` tier validates every artifact already present under ``dist/``
and exercises the wheel checker with synthetic artifacts. The ``slow`` tier builds
from source and installs each local wheel into an external consumer environment.
"""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WHEELS = tuple(sorted((_ROOT / "dist").glob("*.whl")))
_CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_wheel", _ROOT / "tools" / "check_wheel.py"
)
check_wheel = importlib.util.module_from_spec(_CHECKER_SPEC)
_CHECKER_SPEC.loader.exec_module(check_wheel)

_RUNTIME_MEMBERS = {
    "valkey_embedded/__init__.py",
    "valkey_embedded/__main__.py",
    "valkey_embedded/client.py",
    "valkey_embedded/configuration.py",
    "valkey_embedded/debug.py",
    "valkey_embedded/patch.py",
    "valkey_embedded/py.typed",
    "valkey_embedded/pytest_plugin.py",
    "valkey_embedded/server.py",
}


def _wheel_parameters():
    if not _WHEELS:
        return [pytest.param(None, marks=pytest.mark.skip(reason="no wheel in dist/"))]
    return [pytest.param(wheel, id=wheel.name) for wheel in _WHEELS]


def _zip_member(name, payload, mode=0o644):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info, payload


def _record_payload(members, record_name):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for name in sorted(members):
        payload = members[name][1]
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + digest.decode("ascii"), len(payload)))
    writer.writerow((record_name, "", ""))
    return stream.getvalue().encode()


def _make_synthetic_wheel(
    directory,
    *,
    extras=None,
    modes=None,
    embedded_project_version="0.1.0",
    executable_path="bin/valkey-server",
    platform_tag="linux_x86_64",
    wheel_tags=None,
    omitted=(),
    tamper_member=None,
):
    directory.mkdir(parents=True, exist_ok=True)
    version = "0.1.0"
    server_version = "8.1.8"
    dist_info = "valkey_embedded-{0}.dist-info".format(version)
    members = {name: _zip_member(name, b"# runtime\n") for name in _RUNTIME_MEMBERS}
    generated = {
        "valkey_embedded_version": embedded_project_version,
        "valkey_server_version": server_version,
        "valkey_server_banner": "Valkey server v=8.1.8 build=synthetic",
        "valkey_executable": executable_path,
    }
    if wheel_tags is None:
        wheel_tags = ("cp39-cp39-" + platform_tag,)
    members.update(
        {
            "valkey_embedded/package_metadata.json": _zip_member(
                "valkey_embedded/package_metadata.json",
                json.dumps(generated).encode(),
            ),
            "valkey_embedded/bin/valkey-server": _zip_member(
                "valkey_embedded/bin/valkey-server", b"server", 0o755
            ),
            "valkey_embedded/bin/valkey-cli": _zip_member(
                "valkey_embedded/bin/valkey-cli", b"cli", 0o755
            ),
            "valkey_embedded/bin/VALKEY_COPYING.txt": _zip_member(
                "valkey_embedded/bin/VALKEY_COPYING.txt",
                "\n".join(
                    marker + "\n" + ("complete license text " * 30)
                    for marker in (
                        "----- COPYING (Valkey 8.1.8) -----",
                        "----- deps/lua/COPYRIGHT (Valkey 8.1.8) -----",
                        "----- deps/hdr_histogram/LICENSE.txt (Valkey 8.1.8) -----",
                        "----- deps/fpconv/LICENSE.txt (Valkey 8.1.8) -----",
                        (
                            "----- deps/linenoise/linenoise.c license header "
                            "(Valkey 8.1.8) -----"
                        ),
                    )
                ).encode(),
            ),
            "valkey_embedded/_dummy.cpython-39-x86_64-linux-gnu.so": _zip_member(
                "valkey_embedded/_dummy.cpython-39-x86_64-linux-gnu.so",
                b"extension",
                0o755,
            ),
            dist_info + "/METADATA": _zip_member(
                dist_info + "/METADATA",
                (
                    "Metadata-Version: 2.4\nName: valkey-embedded\nVersion: 0.1.0\n\n"
                ).encode(),
            ),
            dist_info + "/WHEEL": _zip_member(
                dist_info + "/WHEEL",
                (
                    "Wheel-Version: 1.0\n"
                    "Root-Is-Purelib: false\n"
                    + "".join("Tag: " + tag + "\n" for tag in wheel_tags)
                ).encode(),
            ),
            dist_info + "/entry_points.txt": _zip_member(
                dist_info + "/entry_points.txt",
                (
                    "[console_scripts]\n"
                    "valkey-embedded = valkey_embedded.__main__:main\n\n"
                    "[pytest11]\n"
                    "valkey_embedded = valkey_embedded.pytest_plugin\n"
                ).encode(),
            ),
            dist_info + "/top_level.txt": _zip_member(
                dist_info + "/top_level.txt", b"valkey_embedded\n"
            ),
            dist_info + "/licenses/LICENSE.txt": _zip_member(
                dist_info + "/licenses/LICENSE.txt",
                b"BSD 3-Clause License\n" + (b"complete project license text " * 40),
            ),
        }
    )
    for name, payload in (extras or {}).items():
        members[name] = _zip_member(name, payload)
    for name, mode in (modes or {}).items():
        members[name] = _zip_member(name, members[name][1], mode)
    for name in omitted:
        members.pop(name)

    record_name = dist_info + "/RECORD"
    record = _record_payload(members, record_name)
    if tamper_member is not None:
        info, payload = members[tamper_member]
        members[tamper_member] = (info, payload + b"tampered")
    members[record_name] = _zip_member(record_name, record)

    wheel = directory / ("valkey_embedded-0.1.0-cp39-cp39-{0}.whl".format(platform_tag))
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, payload in members.values():
            archive.writestr(info, payload)
    return wheel


def _embedded_versions(wheel):
    with zipfile.ZipFile(wheel) as archive:
        generated = json.loads(
            archive.read("valkey_embedded/package_metadata.json").decode()
        )
    return generated["valkey_embedded_version"], generated["valkey_server_version"]


def _run(command, *, cwd=None, env=None, timeout=300):
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    assert completed.returncode == 0, "command failed ({0}):\n{1}".format(
        " ".join(map(str, command)), completed.stdout
    )
    return completed.stdout


@pytest.mark.packaging
def test_wheel_validator_accepts_exact_contract(tmp_path):
    wheel = _make_synthetic_wheel(tmp_path)
    check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
def test_wheel_validator_accepts_compressed_platform_tags(tmp_path):
    wheel = _make_synthetic_wheel(
        tmp_path,
        platform_tag="manylinux_2_17_x86_64.manylinux2014_x86_64",
        wheel_tags=(
            "cp39-cp39-manylinux_2_17_x86_64",
            "cp39-cp39-manylinux2014_x86_64",
        ),
    )
    check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
def test_wheel_validator_rejects_obsolete_namespace(tmp_path):
    wheel = _make_synthetic_wheel(tmp_path, extras={"valkeylite/stale.py": b"stale"})
    with pytest.raises(check_wheel.WheelValidationError, match="top-level"):
        check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
@pytest.mark.parametrize(
    "member",
    (
        "tests/test_stale.py",
        "examples/stale.py",
        "valkey_embedded/__pycache__/stale.pyc",
        "valkey_embedded/_dummy.c",
    ),
)
def test_wheel_validator_rejects_non_runtime_members(tmp_path, member):
    wheel = _make_synthetic_wheel(tmp_path, extras={member: b"stale"})
    with pytest.raises(check_wheel.WheelValidationError):
        check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
def test_wheel_validator_requires_every_runtime_member(tmp_path):
    wheel = _make_synthetic_wheel(tmp_path, omitted=("valkey_embedded/py.typed",))
    with pytest.raises(check_wheel.WheelValidationError, match="missing runtime"):
        check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
def test_wheel_validator_requires_complete_license_notices(tmp_path):
    wheel = _make_synthetic_wheel(
        tmp_path,
        extras={
            "valkey_embedded/bin/VALKEY_COPYING.txt": (
                b"----- COPYING (Valkey 8.1.8) -----\ntruncated"
            )
        },
    )
    with pytest.raises(check_wheel.WheelValidationError, match="licenses"):
        check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
def test_wheel_validator_requires_executable_binaries(tmp_path):
    wheel = _make_synthetic_wheel(
        tmp_path, modes={"valkey_embedded/bin/valkey-server": 0o644}
    )
    with pytest.raises(check_wheel.WheelValidationError, match="mode 0755"):
        check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
def test_wheel_validator_requires_version_consistency_and_relative_paths(tmp_path):
    version_wheel = _make_synthetic_wheel(
        tmp_path / "version", embedded_project_version="9.9.9"
    )
    path_wheel = _make_synthetic_wheel(
        tmp_path / "path", executable_path="/home/builder/valkey-server"
    )
    with pytest.raises(check_wheel.WheelValidationError, match="package version"):
        check_wheel.validate_wheel(version_wheel)
    with pytest.raises(check_wheel.WheelValidationError, match="package-relative"):
        check_wheel.validate_wheel(path_wheel)


@pytest.mark.packaging
def test_wheel_validator_verifies_record_hashes(tmp_path):
    wheel = _make_synthetic_wheel(tmp_path, tamper_member="valkey_embedded/client.py")
    with pytest.raises(check_wheel.WheelValidationError, match="RECORD hash"):
        check_wheel.validate_wheel(wheel)


@pytest.mark.packaging
def test_wheel_validator_inspects_every_supplied_artifact(tmp_path):
    first = _make_synthetic_wheel(
        tmp_path / "first", extras={"valkeylite/old.py": b"old"}
    )
    second = _make_synthetic_wheel(tmp_path / "second", extras={"tests/old.py": b"old"})
    with pytest.raises(check_wheel.WheelValidationError) as caught:
        check_wheel.validate_wheels((first, second))
    assert str(first) in str(caught.value)
    assert str(second) in str(caught.value)


@pytest.mark.packaging
@pytest.mark.parametrize("wheel", _wheel_parameters())
def test_every_prebuilt_wheel_matches_exact_contract(wheel):
    check_wheel.validate_wheel(wheel)


@pytest.fixture(scope="module")
def built_sdist(tmp_path_factory):
    outdir = tmp_path_factory.mktemp("sdist")
    _run(
        (
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--outdir",
            outdir,
            _ROOT,
        )
    )
    tarballs = tuple(outdir.glob("*.tar.gz"))
    assert len(tarballs) == 1, "build must produce exactly one sdist"
    return tarballs[0]


@pytest.mark.slow
def test_sdist_excludes_generated_and_repository_only_files(built_sdist):
    with tarfile.open(built_sdist) as archive:
        names = archive.getnames()
    for generated in (
        "bin/valkey-server",
        "bin/valkey-cli",
        "bin/VALKEY_COPYING.txt",
        "package_metadata.json",
    ):
        assert not any(name.endswith(generated) for name in names), (
            "sdist must not ship " + generated
        )
    for excluded in ("/tests/", "/examples/", "/docs/", "/.github/"):
        assert not any(excluded in name for name in names), (
            "sdist must not ship " + excluded
        )


@pytest.mark.slow
def test_sdist_includes_build_script_and_sources(built_sdist):
    with tarfile.open(built_sdist) as archive:
        names = "\n".join(archive.getnames())
    for expected in (
        "tools/build_valkey.py",
        "tools/check_wheel.py",
        "src/valkey_embedded/client.py",
        "src/valkey_embedded/_dummy.c",
        "pyproject.toml",
        "LICENSE.txt",
    ):
        assert expected in names, "sdist missing " + expected


def _copy_build_inputs(destination):
    destination.mkdir()
    for filename in (
        "LICENSE.txt",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
        "setup.py",
    ):
        shutil.copy2(_ROOT / filename, destination / filename)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")
    shutil.copytree(_ROOT / "src", destination / "src", ignore=ignored)
    shutil.copytree(_ROOT / "tools", destination / "tools", ignore=ignored)


@pytest.mark.slow
@pytest.mark.packaging
def test_dirty_build_output_cannot_contaminate_wheel(tmp_path):
    project = tmp_path / "project"
    _copy_build_inputs(project)
    cache_tag = sys.implementation.cache_tag
    assert cache_tag is not None
    from sysconfig import get_platform

    stale_roots = (
        project / "build" / "lib",
        project / "build" / "lib.{0}-{1}".format(get_platform(), cache_tag),
    )
    for stale_root in stale_roots:
        stale = stale_root / "valkeylite" / "contaminated.py"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("stale\n")

    outdir = tmp_path / "wheelhouse"
    _run(
        (
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            outdir,
        ),
        cwd=project,
    )
    wheels = tuple(outdir.glob("*.whl"))
    assert len(wheels) == 1
    check_wheel.validate_wheel(wheels[0])


@pytest.mark.slow
@pytest.mark.parametrize("wheel", _wheel_parameters())
def test_wheel_installs_and_runs_in_clean_consumer(tmp_path, wheel):
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    bin_dir = env_dir / "bin"
    python = bin_dir / "python"
    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("PYTHONHOME", None)
    clean_env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    project_version, server_version = _embedded_versions(wheel)
    clean_env["EXPECTED_PROJECT_VERSION"] = project_version
    clean_env["EXPECTED_SERVER_VERSION"] = server_version

    _run(
        (python, "-m", "pip", "install", "--quiet", wheel, "pytest>=7.4"),
        env=clean_env,
    )
    _run((python, "-m", "pip", "check"), env=clean_env)

    smoke = """
import importlib.metadata
import os
import psutil
import subprocess
import valkey_embedded

assert valkey_embedded.__version__ == importlib.metadata.version("valkey-embedded")
assert valkey_embedded.__version__ == os.environ["EXPECTED_PROJECT_VERSION"]
client = valkey_embedded.Valkey()
pid = client.pid
try:
    assert client.ping()
    assert client.set("artifact", "clean")
    assert client.get("artifact") == b"clean"
finally:
    client.close()
assert not psutil.pid_exists(pid)
cli = os.path.join(os.path.dirname(valkey_embedded.__valkey_executable__), "valkey-cli")
assert os.environ["EXPECTED_SERVER_VERSION"] in subprocess.check_output(
    [cli, "--version"], text=True
)
"""
    _run((python, "-c", smoke), env=clean_env)
    cli_output = _run((bin_dir / "valkey-embedded", "--version"), env=clean_env)
    assert "valkey-embedded " + project_version in cli_output
    debug_output = _run((python, "-m", "valkey_embedded.debug"), env=clean_env)
    assert "valkey-server runnable: True" in debug_output

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "test_installed_plugin.py").write_text(
        "def test_installed_plugin(pytestconfig):\n"
        "    plugin = pytestconfig.pluginmanager.get_plugin('valkey_embedded')\n"
        "    assert plugin is not None\n"
        "    assert plugin.__name__ == 'valkey_embedded.pytest_plugin'\n"
    )
    plugin_output = _run((python, "-m", "pytest", "-q"), cwd=consumer, env=clean_env)
    assert "1 passed" in plugin_output
