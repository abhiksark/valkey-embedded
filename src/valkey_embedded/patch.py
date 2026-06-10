# src/valkey_embedded/patch.py
"""Monkeypatch valkey.Valkey / valkey.StrictValkey with embedded versions."""

from __future__ import annotations

import logging
import os
from typing import Optional

import valkey

from valkey_embedded.client import StrictValkey, Valkey

logger = logging.getLogger(__name__)

_originals: dict[str, type | None] = {"Valkey": None, "StrictValkey": None}
_patched: dict[str, bool] = {"Valkey": False, "StrictValkey": False}


def _apply_dbfile(cls: type, dbfile: str) -> None:
    """Point a client class at a persistent db file (class-level, pre-init).

    Resolves to an absolute path (matching client.py) so dbdir is never a
    relative, cwd-dependent string.
    """
    # Setting ValkeyMixin class attributes through a bare `type` parameter is
    # invisible to mypy; the attributes are declared on ValkeyMixin itself.
    dbfile = os.path.abspath(dbfile)
    cls.dbdir = os.path.dirname(dbfile)  # type: ignore[attr-defined]
    cls.dbfilename = os.path.basename(dbfile)  # type: ignore[attr-defined]
    cls.settingregistryfile = os.path.join(  # type: ignore[attr-defined]
        cls.dbdir,  # type: ignore[attr-defined]
        cls.dbfilename + ".settings",  # type: ignore[attr-defined]
    )


def patch_valkey_Valkey(dbfile: Optional[str] = None) -> None:
    """Replace ``valkey.Valkey`` with the embedded client (idempotent).

    Args:
        dbfile: Optional RDB path; patched instances then share one
            persistent server instead of each getting a private one.
    """
    if _patched["Valkey"]:
        logger.info("valkey.Valkey already patched")
        return
    if dbfile:
        _apply_dbfile(Valkey, dbfile)
    _originals["Valkey"] = valkey.Valkey
    # Swapping a module-level class is the point of this module; mypy
    # rightly flags it, so each swap carries a targeted ignore.
    valkey.Valkey = Valkey  # type: ignore[misc, assignment]
    _patched["Valkey"] = True


def unpatch_valkey_Valkey() -> None:
    """Restore the original ``valkey.Valkey`` (no-op if not patched)."""
    if _originals["Valkey"] is not None:
        valkey.Valkey = _originals["Valkey"]  # type: ignore[misc, assignment]
        _originals["Valkey"] = None
    _patched["Valkey"] = False


def patch_valkey_StrictValkey(dbfile: Optional[str] = None) -> None:
    """Replace ``valkey.StrictValkey`` with the embedded client (idempotent)."""
    if _patched["StrictValkey"]:
        logger.info("valkey.StrictValkey already patched")
        return
    if dbfile:
        _apply_dbfile(StrictValkey, dbfile)
    _originals["StrictValkey"] = valkey.StrictValkey
    valkey.StrictValkey = StrictValkey  # type: ignore[assignment]
    _patched["StrictValkey"] = True


def unpatch_valkey_StrictValkey() -> None:
    """Restore the original ``valkey.StrictValkey`` (no-op if not patched)."""
    if _originals["StrictValkey"] is not None:
        valkey.StrictValkey = _originals["StrictValkey"]  # type: ignore[assignment]
        _originals["StrictValkey"] = None
    _patched["StrictValkey"] = False


def patch_valkey(dbfile: Optional[str] = None) -> None:
    """Patch both valkey.Valkey and valkey.StrictValkey.

    Upstream these are the same class, so both swaps install the same embedded
    class under both names; the pair is kept for API parity with redislite.
    """
    patch_valkey_Valkey(dbfile)
    patch_valkey_StrictValkey(dbfile)


def unpatch_valkey() -> None:
    """Restore both ``valkey.Valkey`` and ``valkey.StrictValkey``."""
    unpatch_valkey_Valkey()
    unpatch_valkey_StrictValkey()
