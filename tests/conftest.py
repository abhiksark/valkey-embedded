# tests/conftest.py
"""Register the valkey-embedded pytest plugin for source-checkout runs.

When the package is installed, the plugin auto-registers via its ``pytest11``
entry point. From a bare source checkout (PYTHONPATH=src) there is no installed
metadata, so register it here -- but only if the entry point didn't already,
because pluggy raises on registering the same module under a second name.
"""


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("valkey_embedded"):
        from valkey_embedded import pytest_plugin

        config.pluginmanager.register(pytest_plugin, "valkey_embedded")
