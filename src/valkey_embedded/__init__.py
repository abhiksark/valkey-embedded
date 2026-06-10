# src/valkey_embedded/__init__.py
"""valkey_embedded: an embedded, auto-managed Valkey server for Python.

Metadata dunders are set BEFORE importing client, because client reads
__valkey_executable__ at server-start time.
"""

from __future__ import annotations

import json
import os
from importlib import metadata as _metadata

_HERE = os.path.dirname(__file__)


def _load_package_metadata() -> dict[str, str]:
    """Load build-time metadata ({} when absent, e.g. fresh source checkout)."""
    path = os.path.join(_HERE, "package_metadata.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_PACKAGE_METADATA = _load_package_metadata()

try:
    __version__ = _metadata.version("valkey_embedded")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = _PACKAGE_METADATA.get("valkey_embedded_version", "0.0.0")

# Prefer a bundled binary if present; fall back to the build-time recorded
# path (stored package-relative so wheels are reproducible across build hosts).
_bundled = os.path.join(_HERE, "bin", "valkey-server")
if os.path.exists(_bundled):
    __valkey_executable__ = _bundled
else:
    _recorded = _PACKAGE_METADATA.get("valkey_executable", "")
    __valkey_executable__ = os.path.join(_HERE, _recorded) if _recorded else ""

# Plain version (e.g. "8.1.8"); the full server banner is available via
# `python -m valkey_embedded.debug`.
__valkey_server_version__ = _PACKAGE_METADATA.get("valkey_server_version", "")

from valkey_embedded.client import (  # noqa: E402  (must follow dunder setup)
    StrictValkey,
    Valkey,
    ValkeyEmbeddedError,
    ServerStartError,
    connect,
)
from valkey_embedded.server import ValkeyServer  # noqa: E402

__all__ = [
    "Valkey",
    "StrictValkey",
    "ValkeyServer",
    "connect",
    "ValkeyEmbeddedError",
    "ServerStartError",
    "__version__",
    "__valkey_executable__",
    "__valkey_server_version__",
]
