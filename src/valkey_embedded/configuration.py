# src/valkey_embedded/configuration.py
"""Default valkey-server settings and valkey.conf rendering."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Dict, List, Optional, Union

logger = logging.getLogger(__name__)

SettingValue = Union[str, List[str], None]

# Modern Valkey directive names only (replica-*, *-listpack-*); no legacy aliases.
# Path/runtime directives (dir, dbfilename, logfile, pidfile, unixsocket) carry
# placeholder values here and are overridden per-instance at start time.
DEFAULT_VALKEY_SETTINGS: Dict[str, SettingValue] = {
    "activerehashing": "yes",
    "aof-load-truncated": "yes",
    "aof-rewrite-incremental-fsync": "yes",
    "appendfilename": "appendonly.aof",
    "appendfsync": "everysec",
    "appendonly": "no",
    "auto-aof-rewrite-min-size": "64mb",
    "auto-aof-rewrite-percentage": "100",
    "bind": None,
    "busy-reply-threshold": "5000",
    "daemonize": "yes",
    "databases": "16",
    "dbdir": "./",
    "dbfilename": "valkey.db",
    "hash-max-listpack-entries": "512",
    "hash-max-listpack-value": "64",
    "hll-sparse-max-bytes": "3000",
    "hz": "10",
    "latency-monitor-threshold": "0",
    "list-max-listpack-size": "128",  # max entries per listpack node
    "logfile": "valkey.log",
    "loglevel": "notice",
    "no-appendfsync-on-rewrite": "no",
    "notify-keyspace-events": '""',
    "pidfile": "valkey.pid",
    "port": "0",
    "rdbchecksum": "yes",
    "rdbcompression": "yes",
    "repl-disable-tcp-nodelay": "no",
    "replica-priority": "100",
    "replica-read-only": "yes",
    "replica-serve-stale-data": "yes",
    "save": ["900 1", "300 100", "60 200", "15 1000"],
    "set-max-intset-entries": "512",
    "slowlog-log-slower-than": "10000",
    "slowlog-max-len": "128",
    "stop-writes-on-bgsave-error": "yes",
    "tcp-backlog": "511",
    "tcp-keepalive": "0",
    "timeout": "0",
    "unixsocket": "valkey.socket",
    "unixsocketperm": "700",
    "zset-max-listpack-entries": "128",
    "zset-max-listpack-value": "64",
}

# Directives whose values are filesystem paths; quoted when rendered.
_PATH_DIRECTIVES = frozenset(
    {"dir", "dbfilename", "logfile", "pidfile", "unixsocket", "appendfilename"}
)


def settings(**overrides: SettingValue) -> Dict[str, SettingValue]:
    """Merge overrides onto the defaults.

    A None override removes that directive. Returns a fresh dict; the module
    defaults are never mutated.
    """
    merged = deepcopy(DEFAULT_VALKEY_SETTINGS)
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _config_line(setting: str, value: str) -> str:
    """One valkey.conf directive line; path values are quoted."""
    if setting in _PATH_DIRECTIVES:
        return '{setting} "{value}"'.format(setting=setting, value=value)
    return "{setting} {value}".format(setting=setting, value=value)


def config(**overrides: SettingValue) -> str:
    """Render valkey.conf text from the merged settings.

    Valkey names the working directory `dir`; callers pass `dbdir`, which is
    mapped onto `dir` here. List values emit one directive line per element.
    None/empty values are skipped.
    """
    config_dict = settings(**overrides)
    # Valkey names the working dir `dir`; callers pass `dbdir`. An explicit
    # `dir` override wins (and dbdir is dropped so it never renders as a bogus
    # directive); otherwise dbdir is mapped onto dir.
    if "dir" in config_dict:
        config_dict.pop("dbdir", None)
    elif "dbdir" in config_dict:
        config_dict["dir"] = config_dict.pop("dbdir")

    lines: List[str] = []
    for key in sorted(config_dict):
        value: Optional[SettingValue] = config_dict[key]
        if not value:
            continue
        if isinstance(value, list):
            for item in value:
                lines.append(_config_line(key, item))
        else:
            lines.append(_config_line(key, value))
    rendered = "\n".join(lines) + "\n"
    logger.debug("Rendered valkey.conf:\n%s", rendered)
    return rendered
