"""Monkeypatch valkey-py client names with symmetric embedded versions."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Type

import valkey

from valkey_embedded.client import StrictValkey, Valkey

logger = logging.getLogger(__name__)

_PATCH_NAMES = ("Valkey", "StrictValkey")
_originals: Dict[str, Optional[Type[Any]]] = {
    "Valkey": None,
    "StrictValkey": None,
}
_patched: Dict[str, bool] = {"Valkey": False, "StrictValkey": False}
_patch_targets: Dict[str, Optional[Type[Any]]] = {
    "Valkey": None,
    "StrictValkey": None,
}
_patch_lock = threading.RLock()


@dataclass(frozen=True)
class _ClassPathState:
    """Exact process-global path attributes for one embedded class object."""

    dbdir: Optional[str]
    dbfilename: str
    settingregistryfile: Optional[str]


@dataclass(frozen=True)
class _ClassConfiguration:
    """Original class state and the normalized active database path."""

    original: _ClassPathState
    dbfile: str


@dataclass(frozen=True)
class _GlobalPatchState:
    """All mutable state needed to roll one public patch operation back."""

    originals: Dict[str, Optional[Type[Any]]]
    patched: Dict[str, bool]
    targets: Dict[str, Optional[Type[Any]]]
    configurations: Dict[Type[Any], _ClassConfiguration]
    upstream: Dict[str, Type[Any]]
    class_paths: Dict[Type[Any], _ClassPathState]


_configured_classes: Dict[Type[Any], _ClassConfiguration] = {}


def _embedded_target(name: str) -> Type[Any]:
    """Return the embedded class installed for one upstream name."""
    if name == "Valkey":
        return Valkey
    if name == "StrictValkey":
        return StrictValkey
    raise ValueError("unsupported valkey patch target: {0}".format(name))


def _capture_class_path_state(cls: Type[Any]) -> _ClassPathState:
    """Capture exact path attributes from an embedded client class."""
    return _ClassPathState(
        dbdir=getattr(cls, "dbdir"),
        dbfilename=getattr(cls, "dbfilename"),
        settingregistryfile=getattr(cls, "settingregistryfile"),
    )


def _restore_class_path_state(cls: Type[Any], state: _ClassPathState) -> None:
    """Restore exact path attributes on an embedded client class."""
    setattr(cls, "dbdir", state.dbdir)
    setattr(cls, "dbfilename", state.dbfilename)
    setattr(cls, "settingregistryfile", state.settingregistryfile)


def _capture_global_state() -> _GlobalPatchState:
    """Snapshot all patch state before one public mutation attempt."""
    classes = {Valkey, StrictValkey}
    classes.update(_configured_classes)
    classes.update(target for target in _patch_targets.values() if target is not None)
    return _GlobalPatchState(
        originals=dict(_originals),
        patched=dict(_patched),
        targets=dict(_patch_targets),
        configurations=dict(_configured_classes),
        upstream={name: getattr(valkey, name) for name in _PATCH_NAMES},
        class_paths={cls: _capture_class_path_state(cls) for cls in classes},
    )


def _restore_global_state(state: _GlobalPatchState) -> None:
    """Restore a transaction snapshot after a partial patch failure."""
    for cls, path_state in state.class_paths.items():
        _restore_class_path_state(cls, path_state)
    for name, upstream_class in state.upstream.items():
        setattr(valkey, name, upstream_class)

    _originals.clear()
    _originals.update(state.originals)
    _patched.clear()
    _patched.update(state.patched)
    _patch_targets.clear()
    _patch_targets.update(state.targets)
    _configured_classes.clear()
    _configured_classes.update(state.configurations)


def _run_transaction(operation: Callable[[], None]) -> None:
    """Run one process-global mutation and restore every field on failure."""
    state = _capture_global_state()
    try:
        operation()
    except BaseException:
        _restore_global_state(state)
        raise


def _normalize_dbfile(dbfile: Optional[str]) -> Optional[str]:
    """Return the canonical database path, preserving no-path semantics."""
    return os.path.abspath(dbfile) if dbfile else None


def _apply_dbfile(cls: Type[Any], dbfile: str) -> None:
    """Point a client class at one canonical persistent database file."""
    dbdir = os.path.dirname(dbfile)
    dbfilename = os.path.basename(dbfile)
    setattr(cls, "dbdir", dbdir)
    setattr(cls, "dbfilename", dbfilename)
    setattr(cls, "settingregistryfile", os.path.join(dbdir, dbfilename + ".settings"))


def _configure_target(cls: Type[Any], dbfile: str) -> None:
    """Configure one actual class object or reject an active path conflict."""
    configured = _configured_classes.get(cls)
    if configured is not None:
        if configured.dbfile != dbfile:
            raise ValueError(
                "embedded client class is already patched for {0!r}; "
                "unpatch all aliases before selecting {1!r}".format(
                    configured.dbfile, dbfile
                )
            )
        return

    original = _capture_class_path_state(cls)
    _apply_dbfile(cls, dbfile)
    _configured_classes[cls] = _ClassConfiguration(
        original=original,
        dbfile=dbfile,
    )


def _install_upstream(name: str, cls: Type[Any]) -> None:
    """Install a class under one valkey-py module name."""
    setattr(valkey, name, cls)


def _patch_name(name: str, dbfile: Optional[str]) -> None:
    """Apply one name mutation inside an outer transaction and lock."""
    target = _patch_targets[name] if _patched[name] else _embedded_target(name)
    assert target is not None
    normalized_dbfile = _normalize_dbfile(dbfile)
    if normalized_dbfile is not None:
        _configure_target(target, normalized_dbfile)

    if _patched[name]:
        logger.info("valkey.%s already patched", name)
        return

    _originals[name] = getattr(valkey, name)
    _patch_targets[name] = target
    _install_upstream(name, target)
    _patched[name] = True


def _unpatch_name(name: str) -> None:
    """Restore one upstream name and any class state no alias still needs."""
    if not _patched[name]:
        return

    target = _patch_targets[name]
    original = _originals[name]
    if original is not None:
        _install_upstream(name, original)
    _originals[name] = None
    _patch_targets[name] = None
    _patched[name] = False

    if target is None:
        return
    target_still_active = any(
        _patched[other_name] and _patch_targets[other_name] is target
        for other_name in _PATCH_NAMES
    )
    if target_still_active:
        return
    configured = _configured_classes.pop(target, None)
    if configured is not None:
        _restore_class_path_state(target, configured.original)


def patch_valkey_Valkey(dbfile: Optional[str] = None) -> None:
    """Replace ``valkey.Valkey`` with the embedded client.

    Args:
        dbfile: Optional RDB path. An active alias configured for a different
            path causes a clear conflict instead of changing shared class state.

    Raises:
        ValueError: The shared embedded class already uses another active path.
    """
    with _patch_lock:
        _run_transaction(lambda: _patch_name("Valkey", dbfile))


def unpatch_valkey_Valkey() -> None:
    """Restore ``valkey.Valkey`` and dependent class state when safe."""
    with _patch_lock:
        _run_transaction(lambda: _unpatch_name("Valkey"))


def patch_valkey_StrictValkey(dbfile: Optional[str] = None) -> None:
    """Replace ``valkey.StrictValkey`` with the embedded client.

    Args:
        dbfile: Optional RDB path. An active alias configured for a different
            path causes a clear conflict instead of changing shared class state.

    Raises:
        ValueError: The shared embedded class already uses another active path.
    """
    with _patch_lock:
        _run_transaction(lambda: _patch_name("StrictValkey", dbfile))


def unpatch_valkey_StrictValkey() -> None:
    """Restore ``valkey.StrictValkey`` and dependent class state when safe."""
    with _patch_lock:
        _run_transaction(lambda: _unpatch_name("StrictValkey"))


def _patch_all(dbfile: Optional[str]) -> None:
    """Patch both names inside one already-established transaction."""
    _patch_name("Valkey", dbfile)
    _patch_name("StrictValkey", dbfile)


def _unpatch_all() -> None:
    """Unpatch both names inside one already-established transaction."""
    _unpatch_name("Valkey")
    _unpatch_name("StrictValkey")


def patch_valkey(dbfile: Optional[str] = None) -> None:
    """Patch both upstream names as one all-or-nothing operation.

    Args:
        dbfile: Optional RDB path shared by both aliases.

    Raises:
        ValueError: The shared embedded class already uses another active path.
    """
    with _patch_lock:
        _run_transaction(lambda: _patch_all(dbfile))


def unpatch_valkey() -> None:
    """Restore both upstream names and shared class state atomically."""
    with _patch_lock:
        _run_transaction(_unpatch_all)
