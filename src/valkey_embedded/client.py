# src/valkey_embedded/client.py
"""Embedded valkey-server lifecycle: start, connect, share, and clean up.

Ported from redislite's proven design, modernized with type hints. Hard-won
fixes preserved deliberately:
  * shutdown signals the daemon pid read from the pidfile, never a reset
    self-pid (redislite PR #194 self-kill fix);
  * the last connected client (by CLIENT LIST count) performs shutdown;
  * readiness is polled over the socket, not slept for.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, Any, Optional

import psutil
import valkey

import valkey_embedded
from valkey_embedded import configuration

logger = logging.getLogger(__name__)

DEFAULT_DBFILENAME = "valkey.db"
DEFAULT_START_TIMEOUT = 10


class ValkeyEmbeddedError(Exception):
    """Base class for all valkey_embedded errors."""


class ServerStartError(ValkeyEmbeddedError):
    """The embedded valkey-server did not become ready in time."""


# AF_UNIX sun_path is 104 bytes on macOS/BSD (108 on Linux); use the smaller
# bound everywhere so behavior is portable.
_SUN_PATH_LIMIT = 104


def _socket_path_for(directory: str, name: str) -> "tuple[str, Optional[str]]":
    """Socket path under ``directory``, relocated if it would overflow sun_path.

    Deep directories (e.g. pytest's tmp_path on macOS) produce socket paths
    longer than AF_UNIX allows, which the server cannot bind. In that case the
    socket goes into a freshly created short temp dir instead.

    Returns:
        (socket_path, owned_tmp_dir): ``owned_tmp_dir`` is None when the
        socket lives under ``directory``; otherwise it is the fallback dir the
        caller must remove at cleanup.
    """
    candidate = os.path.join(directory, name)
    if len(os.fsencode(candidate)) < _SUN_PATH_LIMIT:
        return candidate, None
    base = "/tmp" if os.path.isdir("/tmp") else None
    short_dir = tempfile.mkdtemp(prefix="vkey-sock-", dir=base)
    return os.path.join(short_dir, name), short_dir


def _missing_binary_message(path: str) -> str:
    """Actionable error text for both pip-install and source-checkout users."""
    return (
        "bundled valkey-server not found at {0!r}. Installed from PyPI? "
        "That's a packaging bug: run 'python -m valkey_embedded.debug' and "
        "report the output at "
        "https://github.com/abhiksark/valkey-embedded/issues. Working from "
        "a source checkout? Build the binary first: "
        "python tools/build_valkey.py".format(path)
    )


def _safe_remove(path: Optional[str]) -> None:
    """Remove a file if it exists; never raise (best-effort cleanup)."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


class ValkeyMixin:
    """Manage a private valkey-server for the client it is mixed into."""

    start_timeout: int = DEFAULT_START_TIMEOUT
    # Class-level so patch.py can set a persistent db location before __init__.
    dbdir: Optional[str] = None
    dbfilename: str = DEFAULT_DBFILENAME
    settingregistryfile: Optional[str] = None

    if TYPE_CHECKING:
        # Provided by the valkey.Valkey host class this mixin is combined
        # with; declared here so the mixin type-checks standalone. Any keeps
        # the declaration compatible with valkey-py's own protocol classes.
        connection_pool: Any

        def ping(self, **kwargs: Any) -> Any: ...

        def client_list(self, *args: Any, **kwargs: Any) -> Any: ...

        def shutdown(self, *args: Any, **kwargs: Any) -> Any: ...

    def __init__(
        self,
        dbfilename: Optional[str] = None,
        *args: Any,
        serverconfig: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """Start (or attach to) an embedded server and connect to it.

        Args:
            dbfilename: RDB file **path** (not a host!). With a path, the
                server is persistent and shareable: instances given the same
                path attach to one server, and the last to close shuts it
                down. None (default) gives a private server whose temp
                directory is removed on exit.
            *args: Forwarded to the underlying valkey-py client.
            serverconfig: valkey.conf overrides rendered into the generated
                config, e.g. ``{"maxmemory": "64mb"}``.
            **kwargs: Forwarded to the underlying valkey-py client
                (e.g. ``decode_responses=True``).

        Raises:
            ServerStartError: The bundled binary is missing or the server
                did not answer PING within ``start_timeout`` seconds.
        """
        self._server_config = dict(serverconfig or {})
        self._server_process: Optional[subprocess.Popen[bytes]] = None
        self.running = False
        # A caller may pass unix_socket_path; otherwise one is computed under
        # dbdir. Either way self.socket_file is canonical and re-injected into
        # kwargs before super().__init__().
        self.socket_file: Optional[str] = kwargs.pop("unix_socket_path", None)
        self.configfile: Optional[str] = None

        # Resolve persistence target: explicit arg > class attr (set via patch).
        if dbfilename:
            abspath = os.path.abspath(dbfilename)
            self.dbdir = os.path.dirname(abspath) or os.getcwd()
            self.dbfilename = os.path.basename(abspath)
            self.settingregistryfile = os.path.join(
                self.dbdir, self.dbfilename + ".settings"
            )
        elif self.settingregistryfile is None:
            # Fully isolated: private temp dir, no cross-process registry.
            self.dbdir = tempfile.mkdtemp(prefix="valkey_embedded-")
            self.dbfilename = DEFAULT_DBFILENAME

        # Every path above guarantees dbdir (patch.py sets it class-level when
        # it sets settingregistryfile); the assert narrows Optional for mypy.
        assert self.dbdir is not None
        os.makedirs(self.dbdir, exist_ok=True)
        self.pidfile = os.path.join(self.dbdir, "valkey.pid")
        self.logfile = os.path.join(self.dbdir, "valkey.log")
        self._socket_dir: Optional[str] = None
        if not self.socket_file:
            self.socket_file, self._socket_dir = _socket_path_for(
                self.dbdir, "valkey.socket"
            )

        atexit.register(self._cleanup)

        started = False
        try:
            if self.settingregistryfile and self._load_setting_registry():
                logger.debug(
                    "Attached to shared server via %s", self.settingregistryfile
                )
                # The registry's socket replaced ours; drop the unused
                # fallback dir if one was created.
                if self._socket_dir:
                    shutil.rmtree(self._socket_dir, ignore_errors=True)
                    self._socket_dir = None
            else:
                self._start_server()
                started = True
                if self.settingregistryfile:
                    self._save_setting_registry()

            kwargs["unix_socket_path"] = self.socket_file
            super().__init__(*args, **kwargs)
            self._wait_until_ready()
        except Exception:
            # Startup failed: tear down only what WE created (never a server we
            # merely attached to), drop the atexit hook, then re-raise so a
            # failed instance leaks no temp dir, process, or callback.
            if started:
                self._terminate(self.pid)
                if self.settingregistryfile:
                    _safe_remove(self.settingregistryfile)
            if not self.settingregistryfile and self.dbdir:
                shutil.rmtree(self.dbdir, ignore_errors=True)
            if self._socket_dir:
                shutil.rmtree(self._socket_dir, ignore_errors=True)
            try:
                atexit.unregister(self._cleanup)
            except Exception:  # noqa: BLE001
                pass
            raise
        self.running = True

    # -- startup ---------------------------------------------------------

    def _start_server(self) -> None:
        """Render valkey.conf and daemonize a private valkey-server."""
        assert self.dbdir is not None  # set in __init__ before any start
        conf = configuration.config(
            dbdir=self.dbdir,
            dbfilename=self.dbfilename,
            unixsocket=self.socket_file,
            pidfile=self.pidfile,
            logfile=self.logfile,
            **self._server_config,
        )
        self.configfile = os.path.join(self.dbdir, "valkey.conf")
        with open(self.configfile, "w") as fh:
            fh.write(conf)

        executable = valkey_embedded.__valkey_executable__
        if not executable or not os.path.exists(executable):
            raise ServerStartError(_missing_binary_message(executable))
        self._server_process = subprocess.Popen(
            [executable, self.configfile],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # `daemonize yes` makes valkey fork the real server while the launcher
        # exits immediately; reap the launcher so it does not linger as a zombie.
        try:
            self._server_process.wait(timeout=self.start_timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - daemon exits fast
            pass

    def _wait_until_ready(self) -> None:
        """Poll PING until the server answers or ``start_timeout`` expires."""
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            try:
                if self.ping():
                    return
            except valkey.exceptions.ConnectionError:
                pass
            time.sleep(0.1)
        raise ServerStartError(
            "valkey-server failed to start within {0}s; see log at {1}".format(
                self.start_timeout, self.logfile
            )
        )

    # -- shared/isolated registry ---------------------------------------

    def _save_setting_registry(self) -> None:
        """Write the .settings file other processes use to attach (mode 600)."""
        assert self.settingregistryfile is not None  # caller guards
        data = {
            "pidfile": self.pidfile,
            "unixsocket": self.socket_file,
            "dbdir": self.dbdir,
            "dbfilename": self.dbfilename,
        }
        fd = os.open(
            self.settingregistryfile,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)

    def _load_setting_registry(self) -> bool:
        """Attach to a live shared server from its .settings file.

        Returns:
            True if the registry points at a running server (state adopted);
            False if absent, unreadable, or the recorded pid is dead.
        """
        assert self.settingregistryfile is not None  # caller guards
        try:
            with open(self.settingregistryfile) as fh:
                data = json.load(fh)
            with open(data["pidfile"]) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError, KeyError):
            return False
        if not psutil.pid_exists(pid):
            return False
        self.pidfile = data["pidfile"]
        self.socket_file = data["unixsocket"]
        self.dbdir = data["dbdir"]
        self.dbfilename = data["dbfilename"]
        return True

    # -- shutdown --------------------------------------------------------

    @property
    def pid(self) -> int:
        """Daemon pid from the pidfile, or 0 if not running."""
        try:
            with open(self.pidfile) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return 0
        return pid if psutil.pid_exists(pid) else 0

    def _connection_count(self) -> int:
        """Number of clients on the server (CLIENT LIST), or 0 if unreachable."""
        try:
            return len(self.client_list())
        except valkey.exceptions.ConnectionError:
            return 0

    def _cleanup(self) -> None:
        """Shut down (if we're the last client), remove files, drop hooks."""
        if not getattr(self, "running", False):
            return
        if self.settingregistryfile:
            # Shared: only the last connected client shuts the server down.
            # (CLIENT LIST count is a heuristic; simple clients hold a single
            # connection, matching redislite's behavior.)
            last_client = self._connection_count() <= 1
        else:
            # Isolated: we are the sole owner, so always shut down.
            last_client = True
        if last_client:
            # Capture the daemon pid BEFORE shutdown clears the pidfile.
            pid = self.pid
            try:
                self.shutdown(save=True)
            except Exception:  # noqa: BLE001 - server may already be gone
                pass
            self._terminate(pid)
            self._remove_files()
        else:
            try:
                self.connection_pool.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.running = False
        try:
            atexit.unregister(self._cleanup)
        except Exception:  # noqa: BLE001
            pass

    def _terminate(self, pid: int) -> None:
        """Wait for the daemon to exit, escalating to SIGTERM then SIGKILL.

        PR #194: signal the captured daemon pid, never self.pid (which may have
        been reset to 0 -> killing our own process group).
        """
        if not pid:
            return
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not proc.is_running():
                return
            time.sleep(0.2)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass
        if proc.is_running():
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass

    def _remove_files(self) -> None:
        """Remove runtime files; keep the RDB only for persistent servers."""
        if self.settingregistryfile:
            # Persistent dir: keep the RDB, drop only runtime files + registry.
            for path in (
                self.settingregistryfile,
                self.pidfile,
                self.socket_file,
                self.configfile,
            ):
                _safe_remove(path)
        elif self.dbdir:
            # Isolated: we own the whole temp tree.
            shutil.rmtree(self.dbdir, ignore_errors=True)
        if self._socket_dir:
            shutil.rmtree(self._socket_dir, ignore_errors=True)
            self._socket_dir = None

    def __enter__(self) -> "ValkeyMixin":
        """Enter a ``with`` block; the server is already running."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """SQLite-style exit: persist (shutdown saves) and release the server.

        This releases the embedded server itself, not merely the connection
        pool.
        """
        self._cleanup()

    def __del__(self) -> None:
        """Best-effort cleanup if the instance is garbage-collected."""
        try:
            self._cleanup()
        except Exception:  # noqa: BLE001 - never raise from __del__
            pass

    # -- diagnostics -----------------------------------------------------

    @property
    def valkey_log(self) -> str:
        """Contents of the server's log file ("" if unreadable)."""
        try:
            with open(self.logfile) as fh:
                return fh.read()
        except OSError:
            return ""


class Valkey(ValkeyMixin, valkey.Valkey):
    """valkey.Valkey backed by an embedded, auto-managed valkey-server.

    The first positional argument is an RDB file **path**, not a host -- the
    embedded server has no host. ``Valkey()`` gives a private server (temp dir
    removed on exit); ``Valkey("/path/db.rdb")`` is persistent and shared
    (instances with the same path attach to one server; the last to close
    shuts it down). The server listens on a unix socket only.

    Related entry points: :func:`connect` opens a file-backed store
    SQLite-style with opt-in crash-safe durability;
    :class:`~valkey_embedded.server.ValkeyServer` provides an explicit
    lifecycle with a TCP endpoint for non-Python or cross-process clients.
    """


# Upstream defines `StrictValkey = Valkey`; mirror that for drop-in parity.
StrictValkey = Valkey


# durable= presets, expressed as serverconfig overrides. The append-only file
# (AOF) is what makes a crash recoverable; appendfsync sets the durability/speed
# trade-off. RDB-only persistence (the default) can lose writes since the last
# snapshot, which is why durability is opt-in.
_DURABLE_PRESETS: dict[Any, dict[str, str]] = {
    True: {"appendonly": "yes", "appendfsync": "everysec"},
    "everysec": {"appendonly": "yes", "appendfsync": "everysec"},
    "always": {"appendonly": "yes", "appendfsync": "always"},
}


def connect(
    path: Optional[str] = None,
    *,
    durable: Any = False,
    serverconfig: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> Valkey:
    """Open an embedded Valkey the way you would open SQLite: one call.

    Args:
        path: RDB file path for a persistent, shareable server; instances
            sharing a path attach to one server. None (the default) gives a
            private, isolated server whose data directory is discarded on exit.
        durable: Crash-safety via the append-only file (AOF). False (default)
            keeps RDB-snapshot persistence only, which can lose writes made
            since the last snapshot. True enables AOF with ``appendfsync
            everysec`` (at most ~1s of writes lost on a crash). "always" fsyncs
            on every write (slowest, strongest). Requires ``path``.
        serverconfig: Extra valkey.conf overrides. These win over the
            ``durable`` preset, so ``durable=True`` plus
            ``serverconfig={"appendfsync": "always"}`` yields ``always``.
        **kwargs: Forwarded to the Valkey client (e.g. ``decode_responses=True``).

    Returns:
        A connected :class:`Valkey`. It is a context manager: leaving the
        ``with`` block persists and releases the embedded server. Call
        ``conn.bgsave()`` to force a snapshot explicitly.

    Raises:
        ValueError: ``durable`` is truthy but no ``path`` was given (an isolated
            server's data directory is removed on exit, so its data could never
            be durable), or ``durable`` is not one of True/False/"everysec"/
            "always".
    """
    if durable:
        if path is None:
            raise ValueError(
                "durable=True requires a path; an isolated server's data "
                "directory is deleted on exit, so its data cannot be durable."
            )
        try:
            preset = _DURABLE_PRESETS[durable]
        except (KeyError, TypeError):
            raise ValueError(
                "durable must be True, False, 'everysec', or 'always'; "
                "got {0!r}".format(durable)
            ) from None
        merged = dict(preset)
        merged.update(serverconfig or {})  # explicit serverconfig wins
        serverconfig = merged

    return Valkey(path, serverconfig=serverconfig, **kwargs)
