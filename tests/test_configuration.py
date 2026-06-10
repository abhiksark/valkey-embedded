# tests/test_configuration.py
"""Tests for valkey.conf default settings and rendering."""

from valkey_embedded import configuration


def test_defaults_use_modern_directive_names():
    defaults = configuration.DEFAULT_VALKEY_SETTINGS
    # Modern names present.
    assert "replica-read-only" in defaults
    assert "hash-max-listpack-entries" in defaults
    assert "zset-max-listpack-entries" in defaults
    # Legacy aliases absent.
    assert not any("slave-" in key for key in defaults)
    assert not any("ziplist" in key for key in defaults)


def test_security_defaults():
    defaults = configuration.DEFAULT_VALKEY_SETTINGS
    assert defaults["port"] == "0"
    assert defaults["unixsocketperm"] == "700"
    assert defaults["daemonize"] == "yes"


def test_settings_merges_overrides():
    merged = configuration.settings(loglevel="debug", databases="4")
    assert merged["loglevel"] == "debug"
    assert merged["databases"] == "4"
    # Untouched default preserved.
    assert merged["port"] == "0"


def test_settings_none_removes_key():
    merged = configuration.settings(timeout=None)
    assert "timeout" not in merged


def test_settings_does_not_mutate_defaults():
    configuration.settings(loglevel="debug")
    assert configuration.DEFAULT_VALKEY_SETTINGS["loglevel"] == "notice"


def test_config_renders_scalar_and_skips_none():
    text = configuration.config()
    assert "port 0" in text
    assert "unixsocketperm 700" in text
    # bind defaults to None -> never rendered.
    assert "bind " not in text


def test_config_expands_list_directive():
    text = configuration.config()
    assert "save 900 1" in text
    assert "save 300 100" in text


def test_config_quotes_path_directives():
    text = configuration.config(dbdir="/tmp/has space")
    assert 'dir "/tmp/has space"' in text


def test_config_maps_dbdir_to_dir():
    text = configuration.config(dbdir="/tmp/x")
    assert 'dir "/tmp/x"' in text
    assert "dbdir" not in text


def test_settings_deep_copies_list_values():
    merged = configuration.settings()
    merged["save"].append("1 1")
    assert configuration.DEFAULT_VALKEY_SETTINGS["save"] == [
        "900 1",
        "300 100",
        "60 200",
        "15 1000",
    ]


def test_config_explicit_dir_overrides_dbdir():
    text = configuration.config(dir="/data")
    assert 'dir "/data"' in text
    assert "dbdir" not in text


def test_config_skips_empty_string_value():
    # Default timeout is "0" (rendered); an empty-string override drops the line.
    text = configuration.config(timeout="")
    assert "\ntimeout " not in "\n" + text


def test_config_quotes_overridden_path_directive():
    text = configuration.config(logfile="/var/log/valkey.log")
    assert 'logfile "/var/log/valkey.log"' in text


def test_config_quotes_appendfilename_by_default():
    text = configuration.config()
    assert 'appendfilename "appendonly.aof"' in text


def test_config_none_removes_rendered_directive():
    text = configuration.config(save=None)
    assert "\nsave " not in "\n" + text
