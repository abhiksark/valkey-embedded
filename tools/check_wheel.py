#!/usr/bin/env python3
"""Validate valkey-embedded wheels against the complete artifact contract.

The checker is intentionally independent of an installed valkey-embedded package so
release jobs can run it against wheels for other Python versions and platforms.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import List, Mapping, Optional, Sequence, Tuple, Union

_DISTRIBUTION_NAME = "valkey-embedded"
_WHEEL_DISTRIBUTION_NAME = "valkey_embedded"
_PACKAGE = "valkey_embedded"
_REQUIRED_PACKAGE_MEMBERS = {
    "valkey_embedded/__init__.py",
    "valkey_embedded/__main__.py",
    "valkey_embedded/client.py",
    "valkey_embedded/configuration.py",
    "valkey_embedded/debug.py",
    "valkey_embedded/package_metadata.json",
    "valkey_embedded/patch.py",
    "valkey_embedded/py.typed",
    "valkey_embedded/pytest_plugin.py",
    "valkey_embedded/server.py",
    "valkey_embedded/bin/VALKEY_COPYING.txt",
    "valkey_embedded/bin/valkey-cli",
    "valkey_embedded/bin/valkey-server",
}
_REQUIRED_DIST_INFO_BASENAMES = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/LICENSE.txt",
    "top_level.txt",
}
_GENERATED_METADATA_KEYS = {
    "valkey_embedded_version",
    "valkey_executable",
    "valkey_server_banner",
    "valkey_server_version",
}
_LICENSE_SOURCES = (
    "COPYING",
    "deps/lua/COPYRIGHT",
    "deps/hdr_histogram/LICENSE.txt",
    "deps/fpconv/LICENSE.txt",
)
_BUILD_HOST_PATH = re.compile(
    r"(?:file:///|(?:^|[\s\"'=])/(?!/)[^/\s]+/|"
    r"(?:^|[\s\"'=])[A-Za-z]:[\\/])"
)


class WheelValidationError(ValueError):
    """A wheel violates the valkey-embedded artifact contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WheelValidationError(message)


def _safe_member_name(name: str) -> None:
    path = PurePosixPath(name)
    _require(bool(name) and not name.endswith("/"), "directory/empty member: " + name)
    _require("\\" not in name, "member uses a backslash: " + name)
    _require(not path.is_absolute(), "absolute wheel member: " + name)
    _require(
        all(part not in ("", ".", "..") for part in path.parts),
        "unsafe wheel member: " + name,
    )
    _require(
        not re.match(r"^[A-Za-z]:", name),
        "drive-qualified wheel member: " + name,
    )


def _member_mode(info: zipfile.ZipInfo) -> int:
    mode = info.external_attr >> 16
    _require(stat.S_IFMT(mode) == stat.S_IFREG, "non-regular member: " + info.filename)
    return stat.S_IMODE(mode)


def _decode_member(archive: zipfile.ZipFile, name: str) -> str:
    try:
        return archive.read(name).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WheelValidationError(name + " is not UTF-8") from exc


def _single_header(message: Message, name: str) -> str:
    values = message.get_all(name, [])
    _require(len(values) == 1, "METADATA must contain one " + name + " header")
    return str(values[0])


def _escaped_component(value: str) -> str:
    return re.sub(r"[^\w\d.]+", "_", value, flags=re.ASCII)


def _validate_filename(path: Path, version: str) -> Tuple[str, str, str]:
    _require(path.name.endswith(".whl"), "not a wheel filename: " + path.name)
    components = path.name[:-4].rsplit("-", 3)
    _require(len(components) == 4, "invalid wheel filename: " + path.name)
    prefix, python_tag, abi_tag, platform_tag = components
    expected_prefix = "{0}-{1}".format(
        _WHEEL_DISTRIBUTION_NAME, _escaped_component(version)
    )
    _require(prefix == expected_prefix, "filename name/version does not match METADATA")
    _require(platform_tag != "any", "wheel must be platform-specific")
    return python_tag, abi_tag, platform_tag


def _validate_generated_metadata(
    archive: zipfile.ZipFile, metadata_version: str
) -> str:
    name = _PACKAGE + "/package_metadata.json"
    try:
        generated = json.loads(_decode_member(archive, name))
    except json.JSONDecodeError as exc:
        raise WheelValidationError(name + " is not valid JSON") from exc
    _require(isinstance(generated, dict), name + " must contain an object")
    _require(
        set(generated) == _GENERATED_METADATA_KEYS,
        name + " has an unexpected schema",
    )
    _require(
        all(isinstance(value, str) for value in generated.values()),
        name + " values must be strings",
    )
    _require(
        generated["valkey_embedded_version"] == metadata_version,
        "generated package version does not match METADATA",
    )
    _require(
        generated["valkey_executable"] == "bin/valkey-server",
        "generated executable path must be package-relative",
    )
    server_version = generated["valkey_server_version"]
    _require(bool(server_version), "generated Valkey version is empty")
    _require(
        "v=" + server_version in generated["valkey_server_banner"],
        "generated Valkey banner/version mismatch",
    )
    for value in generated.values():
        _require(
            _BUILD_HOST_PATH.search(value) is None,
            "generated metadata contains an absolute build-host path",
        )
    return server_version


def _validate_licenses(archive: zipfile.ZipFile, dist_info: str, version: str) -> None:
    bundle = _decode_member(archive, _PACKAGE + "/bin/VALKEY_COPYING.txt")
    markers = [
        "----- {0} (Valkey {1}) -----".format(source, version)
        for source in _LICENSE_SOURCES
    ]
    markers.append(
        "----- deps/linenoise/linenoise.c license header (Valkey {0}) -----".format(
            version
        )
    )
    positions = [bundle.find(marker) for marker in markers]
    _require(
        all(position >= 0 for position in positions) and positions == sorted(positions),
        "bundled licenses are missing or out of order",
    )
    for index, marker in enumerate(markers):
        start = positions[index] + len(marker)
        end = positions[index + 1] if index + 1 < len(markers) else len(bundle)
        _require(
            len(bundle[start:end].strip()) >= 500,
            "bundled license notice is incomplete: " + marker,
        )
    project_license = _decode_member(archive, dist_info + "/licenses/LICENSE.txt")
    _require(
        "BSD 3-Clause License" in project_license and len(project_license) >= 1000,
        "distribution license is incomplete",
    )


def _validate_entry_points(archive: zipfile.ZipFile, dist_info: str) -> None:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(_decode_member(archive, dist_info + "/entry_points.txt"))
    except configparser.Error as exc:
        raise WheelValidationError("invalid entry_points.txt") from exc
    actual = {section: dict(parser.items(section)) for section in parser.sections()}
    expected = {
        "console_scripts": {
            "valkey-embedded": "valkey_embedded.__main__:main",
        },
        "pytest11": {
            "valkey_embedded": "valkey_embedded.pytest_plugin",
        },
    }
    _require(actual == expected, "wheel entry points do not match the public contract")


def _validate_wheel_metadata(
    archive: zipfile.ZipFile,
    dist_info: str,
    filename_tags: Tuple[str, str, str],
) -> None:
    wheel = BytesParser(policy=policy.default).parsebytes(
        archive.read(dist_info + "/WHEEL")
    )
    _require(
        _single_header(wheel, "Root-Is-Purelib").lower() == "false",
        "wheel must declare Root-Is-Purelib: false",
    )
    tags = {str(value) for value in wheel.get_all("Tag", [])}
    python_tags, abi_tags, platform_tags = filename_tags
    expected_tags = {
        "{0}-{1}-{2}".format(python_tag, abi_tag, platform_tag)
        for python_tag in python_tags.split(".")
        for abi_tag in abi_tags.split(".")
        for platform_tag in platform_tags.split(".")
    }
    _require(tags == expected_tags, "WHEEL tags do not match the filename")


def _validate_record(
    archive: zipfile.ZipFile, infos: Mapping[str, zipfile.ZipInfo], record_name: str
) -> None:
    try:
        rows = list(csv.reader(io.StringIO(_decode_member(archive, record_name))))
    except csv.Error as exc:
        raise WheelValidationError("invalid RECORD CSV") from exc
    _require(all(len(row) == 3 for row in rows), "RECORD rows must have three fields")
    paths = [row[0] for row in rows]
    _require(len(paths) == len(set(paths)), "RECORD contains duplicate paths")
    for path in paths:
        _safe_member_name(path)
    _require(set(paths) == set(infos), "RECORD does not describe every wheel member")

    for member, digest, size in rows:
        if member == record_name:
            _require(not digest and not size, "RECORD must not hash itself")
            continue
        _require(digest.startswith("sha256="), member + " does not use a SHA-256 hash")
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(archive.read(member)).digest()
        ).rstrip(b"=")
        _require(
            digest[7:] == expected_digest.decode("ascii"),
            "RECORD hash mismatch for " + member,
        )
        _require(
            size == str(infos[member].file_size), "RECORD size mismatch for " + member
        )


def _validate_archive(path: Path, archive: zipfile.ZipFile) -> None:
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    _require(bool(names), "wheel is empty")
    _require(len(names) == len(set(names)), "wheel contains duplicate members")
    infos = {entry.filename: entry for entry in entries}
    for entry in entries:
        _safe_member_name(entry.filename)
        _require(not entry.flag_bits & 0x1, "encrypted wheel member: " + entry.filename)
        _member_mode(entry)

    top_levels = {PurePosixPath(name).parts[0] for name in names}
    dist_infos = sorted(name for name in top_levels if name.endswith(".dist-info"))
    _require(
        len(dist_infos) == 1, "wheel must contain exactly one .dist-info directory"
    )
    dist_info = dist_infos[0]
    _require(
        top_levels == {_PACKAGE, dist_info},
        "unexpected top-level wheel namespaces: " + ", ".join(sorted(top_levels)),
    )

    metadata_name = dist_info + "/METADATA"
    _require(metadata_name in infos, "wheel is missing METADATA")
    metadata = BytesParser(policy=policy.default).parsebytes(
        archive.read(metadata_name)
    )
    _require(not metadata.defects, "METADATA contains parse defects")
    project_name = _single_header(metadata, "Name")
    version = _single_header(metadata, "Version")
    _require(project_name == _DISTRIBUTION_NAME, "unexpected distribution name")
    _require(bool(version), "distribution version is empty")
    expected_dist_info = "{0}-{1}.dist-info".format(
        _WHEEL_DISTRIBUTION_NAME, _escaped_component(version)
    )
    _require(dist_info == expected_dist_info, ".dist-info name/version mismatch")
    filename_tags = _validate_filename(path, version)

    extensions = {
        name
        for name in names
        if name.startswith(_PACKAGE + "/_dummy.") and name.endswith(".so")
    }
    _require(len(extensions) == 1, "wheel must contain exactly one dummy extension")
    actual_package = {name for name in names if name.startswith(_PACKAGE + "/")}
    missing_package = _REQUIRED_PACKAGE_MEMBERS - actual_package
    unexpected_package = actual_package - _REQUIRED_PACKAGE_MEMBERS - extensions
    _require(
        not missing_package,
        "wheel missing runtime members: " + ", ".join(sorted(missing_package)),
    )
    _require(
        not unexpected_package,
        "wheel contains unexpected package members: "
        + ", ".join(sorted(unexpected_package)),
    )

    expected_dist_info_members = {
        dist_info + "/" + basename for basename in _REQUIRED_DIST_INFO_BASENAMES
    }
    actual_dist_info = {name for name in names if name.startswith(dist_info + "/")}
    _require(
        actual_dist_info == expected_dist_info_members,
        "wheel contains missing or unexpected .dist-info members",
    )

    for executable in (
        _PACKAGE + "/bin/valkey-server",
        _PACKAGE + "/bin/valkey-cli",
    ):
        _require(
            _member_mode(infos[executable]) == 0o755,
            executable + " must have mode 0755",
        )
        _require(infos[executable].file_size > 0, executable + " is empty")
    extension = next(iter(extensions))
    _require(
        _member_mode(infos[extension]) & stat.S_IXUSR != 0,
        extension + " is not executable",
    )

    server_version = _validate_generated_metadata(archive, version)
    _validate_licenses(archive, dist_info, server_version)
    _validate_entry_points(archive, dist_info)
    _validate_wheel_metadata(archive, dist_info, filename_tags)
    _require(
        _decode_member(archive, dist_info + "/top_level.txt") == _PACKAGE + "\n",
        "top_level.txt must name only valkey_embedded",
    )
    for header_value in metadata.values():
        _require(
            _BUILD_HOST_PATH.search(str(header_value)) is None,
            "METADATA contains an absolute build-host path",
        )
    _validate_record(archive, infos, dist_info + "/RECORD")


def validate_wheel(path: Union[os.PathLike[str], str]) -> None:
    """Validate one wheel, raising WheelValidationError on impurity."""
    wheel = Path(path)
    try:
        with zipfile.ZipFile(wheel) as archive:
            _validate_archive(wheel, archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WheelValidationError("cannot read wheel: {0}".format(exc)) from exc


def validate_wheels(paths: Sequence[Union[os.PathLike[str], str]]) -> None:
    """Validate every supplied wheel and report all failing artifacts together."""
    _require(bool(paths), "no wheels supplied")
    failures: List[str] = []
    for path in paths:
        try:
            validate_wheel(path)
        except WheelValidationError as exc:
            failures.append("{0}: {1}".format(path, exc))
    if failures:
        raise WheelValidationError(
            "wheel validation failed:\n  " + "\n  ".join(failures)
        )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate valkey-embedded wheel contents and metadata."
    )
    parser.add_argument("wheels", nargs="+", help="wheel paths to inspect")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the wheel validator CLI."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        validate_wheels(args.wheels)
    except WheelValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for wheel in args.wheels:
        print("validated " + wheel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
