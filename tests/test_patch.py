"""Tests for symmetric process-global valkey-py monkeypatching."""

import os
import subprocess
import sys
import textwrap
import threading

import pytest
import valkey

import valkey_embedded
from valkey_embedded import patch


def _class_path_state():
    return (
        valkey_embedded.Valkey.dbdir,
        valkey_embedded.Valkey.dbfilename,
        valkey_embedded.Valkey.settingregistryfile,
    )


def _run_child(source):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "child patch scenario failed\nstdout:\n{0}\nstderr:\n{1}".format(
            result.stdout, result.stderr
        )
    )


def test_patch_and_unpatch_restore_originals():
    original = valkey.Valkey
    original_sv = valkey.StrictValkey
    patch.patch_valkey()
    try:
        assert valkey.Valkey is valkey_embedded.Valkey
        assert valkey.StrictValkey is valkey_embedded.StrictValkey
    finally:
        patch.unpatch_valkey()
    assert valkey.Valkey is original
    assert valkey.StrictValkey is original_sv


def test_patch_is_idempotent():
    patch.patch_valkey()
    patch.patch_valkey()
    try:
        assert valkey.Valkey is valkey_embedded.Valkey
        assert valkey.StrictValkey is valkey_embedded.StrictValkey
    finally:
        patch.unpatch_valkey()
        patch.unpatch_valkey()


def test_patched_client_starts_embedded_server():
    patch.patch_valkey()
    try:
        conn = valkey.Valkey()
        try:
            assert conn.ping() is True
        finally:
            conn.close()
    finally:
        patch.unpatch_valkey()


def test_per_class_patch_and_unpatch():
    original = valkey.Valkey
    patch.patch_valkey_Valkey()
    try:
        assert valkey.Valkey is valkey_embedded.Valkey
    finally:
        patch.unpatch_valkey_Valkey()
    assert valkey.Valkey is original


def test_unpatch_when_never_patched_is_safe():
    original = valkey.Valkey
    original_sv = valkey.StrictValkey
    original_state = _class_path_state()

    patch.unpatch_valkey()

    assert valkey.Valkey is original
    assert valkey.StrictValkey is original_sv
    assert _class_path_state() == original_state


def test_patched_strictvalkey_starts_embedded_server():
    patch.patch_valkey()
    try:
        conn = valkey.StrictValkey()
        try:
            assert conn.ping() is True
        finally:
            conn.close()
    finally:
        patch.unpatch_valkey()


def test_dbfile_patch_restores_exact_class_state(tmp_path):
    dbfile = str(tmp_path / "patched.db")
    original_state = _class_path_state()

    patch.patch_valkey(dbfile=dbfile)
    try:
        assert valkey_embedded.Valkey.settingregistryfile == dbfile + ".settings"
        assert valkey_embedded.StrictValkey is valkey_embedded.Valkey
    finally:
        patch.unpatch_valkey()

    assert _class_path_state() == original_state


@pytest.mark.parametrize(
    ("first_unpatch", "second_unpatch", "remaining_name"),
    [
        ("unpatch_valkey_Valkey", "unpatch_valkey_StrictValkey", "StrictValkey"),
        ("unpatch_valkey_StrictValkey", "unpatch_valkey_Valkey", "Valkey"),
    ],
)
def test_shared_alias_state_restores_only_after_last_unpatch(
    tmp_path, first_unpatch, second_unpatch, remaining_name
):
    dbfile = str(tmp_path / "shared.db")
    original_state = _class_path_state()
    original_valkey = valkey.Valkey
    original_strict = valkey.StrictValkey
    patch.patch_valkey(dbfile=dbfile)
    try:
        getattr(patch, first_unpatch)()
        assert getattr(valkey, remaining_name) is valkey_embedded.Valkey
        assert valkey_embedded.Valkey.settingregistryfile == dbfile + ".settings"

        getattr(patch, second_unpatch)()
        assert _class_path_state() == original_state
        assert valkey.Valkey is original_valkey
        assert valkey.StrictValkey is original_strict
    finally:
        patch.unpatch_valkey()


def test_conflicting_active_alias_paths_raise_without_corrupting_state(tmp_path):
    first_db = str(tmp_path / "first.db")
    second_db = str(tmp_path / "second.db")
    original_state = _class_path_state()
    original_strict = valkey.StrictValkey
    patch.patch_valkey_Valkey(first_db)
    try:
        with pytest.raises(ValueError, match="already patched"):
            patch.patch_valkey_StrictValkey(second_db)

        assert valkey.Valkey is valkey_embedded.Valkey
        assert valkey.StrictValkey is original_strict
        assert valkey_embedded.Valkey.settingregistryfile == first_db + ".settings"
    finally:
        patch.unpatch_valkey()
    assert _class_path_state() == original_state


def test_same_active_dbfile_request_is_idempotent(tmp_path):
    dbfile = str(tmp_path / "same.db")
    original_state = _class_path_state()
    patch.patch_valkey_Valkey(dbfile)
    try:
        patch.patch_valkey_Valkey(dbfile)
        patch.patch_valkey_StrictValkey(os.path.relpath(dbfile))
        assert valkey_embedded.Valkey.settingregistryfile == dbfile + ".settings"
    finally:
        patch.unpatch_valkey()
    assert _class_path_state() == original_state


def test_umbrella_partial_install_failure_rolls_back_every_mutation(
    monkeypatch, tmp_path
):
    dbfile = str(tmp_path / "partial.db")
    original_state = _class_path_state()
    original_valkey = valkey.Valkey
    original_strict = valkey.StrictValkey
    real_install = patch._install_upstream

    def fail_second_install(name, cls):
        real_install(name, cls)
        if name == "StrictValkey":
            raise RuntimeError("simulated second-name install failure")

    monkeypatch.setattr(patch, "_install_upstream", fail_second_install)
    with pytest.raises(RuntimeError, match="second-name install failure"):
        patch.patch_valkey(dbfile)

    assert valkey.Valkey is original_valkey
    assert valkey.StrictValkey is original_strict
    assert _class_path_state() == original_state
    assert patch._patched == {"Valkey": False, "StrictValkey": False}


def test_partial_class_configuration_failure_restores_attributes(monkeypatch, tmp_path):
    dbfile = str(tmp_path / "partial.db")
    original_state = _class_path_state()
    original_valkey = valkey.Valkey

    def fail_after_first_attribute(cls, _dbfile):
        cls.dbdir = "partially-mutated"
        raise RuntimeError("simulated class configuration failure")

    monkeypatch.setattr(patch, "_apply_dbfile", fail_after_first_attribute)
    with pytest.raises(RuntimeError, match="class configuration failure"):
        patch.patch_valkey_Valkey(dbfile)

    assert valkey.Valkey is original_valkey
    assert _class_path_state() == original_state
    assert patch._patched["Valkey"] is False


def test_concurrent_conflicting_patches_have_one_winner(tmp_path):
    paths = [str(tmp_path / "one.db"), str(tmp_path / "two.db")]
    functions = [patch.patch_valkey_Valkey, patch.patch_valkey_StrictValkey]
    originals = (valkey.Valkey, valkey.StrictValkey)
    original_state = _class_path_state()
    barrier = threading.Barrier(3)
    results = []

    def worker(function, dbfile):
        barrier.wait(timeout=5)
        try:
            function(dbfile)
        except ValueError:
            results.append(("conflict", dbfile))
        else:
            results.append(("patched", dbfile))

    threads = [
        threading.Thread(target=worker, args=(function, dbfile))
        for function, dbfile in zip(functions, paths)
    ]
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert sorted(result for result, _dbfile in results) == ["conflict", "patched"]
        winning_dbfile = next(
            dbfile for result, dbfile in results if result == "patched"
        )
        assert (
            valkey_embedded.Valkey.settingregistryfile == winning_dbfile + ".settings"
        )
        assert sum(patch._patched.values()) == 1
    finally:
        patch.unpatch_valkey()
        for thread in threads:
            thread.join(timeout=5)

    assert (valkey.Valkey, valkey.StrictValkey) == originals
    assert _class_path_state() == original_state


def test_nondefault_preexisting_class_state_survives_in_child():
    _run_child(
        """
        import os
        import tempfile
        import valkey
        import valkey_embedded
        from valkey_embedded import patch

        cls = valkey_embedded.Valkey
        original_upstream = (valkey.Valkey, valkey.StrictValkey)
        expected = ("custom-dir", "custom.rdb", "custom.registry")
        cls.dbdir, cls.dbfilename, cls.settingregistryfile = expected
        with tempfile.TemporaryDirectory() as directory:
            patch.patch_valkey(os.path.join(directory, "patched.db"))
            patch.unpatch_valkey_StrictValkey()
            assert cls.settingregistryfile.endswith("patched.db.settings")
            patch.unpatch_valkey_Valkey()

        assert (cls.dbdir, cls.dbfilename, cls.settingregistryfile) == expected
        assert (valkey.Valkey, valkey.StrictValkey) == original_upstream
        """
    )


def test_complete_unpatch_is_isolated_in_following_child_scenarios():
    _run_child(
        """
        import os
        import tempfile
        import valkey
        import valkey_embedded
        from valkey_embedded import patch

        cls = valkey_embedded.Valkey
        baseline = (cls.dbdir, cls.dbfilename, cls.settingregistryfile)
        with tempfile.TemporaryDirectory() as directory:
            patched_db = os.path.join(directory, "patched.db")
            patch.patch_valkey(patched_db)
            patch.unpatch_valkey()
            assert (cls.dbdir, cls.dbfilename, cls.settingregistryfile) == baseline

            direct = valkey_embedded.Valkey()
            direct_dir = direct.dbdir
            try:
                assert direct.ping() is True
                assert direct.settingregistryfile is None
                assert not os.path.realpath(direct_dir).startswith(
                    os.path.realpath(directory) + os.sep
                )
            finally:
                direct.close()
            assert not os.path.exists(direct_dir)

            patch.patch_valkey()
            try:
                patched = valkey.Valkey()
                patched_dir = patched.dbdir
                try:
                    assert patched.ping() is True
                    assert patched.settingregistryfile is None
                    assert not os.path.realpath(patched_dir).startswith(
                        os.path.realpath(directory) + os.sep
                    )
                finally:
                    patched.close()
                assert not os.path.exists(patched_dir)
            finally:
                patch.unpatch_valkey()

            assert not os.path.exists(patched_db + ".settings")
            assert (cls.dbdir, cls.dbfilename, cls.settingregistryfile) == baseline
        """
    )
