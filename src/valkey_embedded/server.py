# src/valkey_embedded/server.py
"""Explicit valkey-server lifecycle control with a TCP endpoint.

`Valkey()` is the auto-managed client: it starts a private, unix-socket-only
server on construction and cleans up at exit. `ValkeyServer` is the other half
of the API -- explicit start/stop control over a server that also listens on
TCP, so *any* Redis-compatible client (or another process, or a non-Python
tool) can connect via host/port.

Hard-won lifecycle details mirror client.py: the daemon pid is read from the
pidfile (never a reset self-pid), and shutdown is graceful-then-terminate.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional

import psutil
import valkey

import valkey_embedded
from valkey_embedded import configuration
from valkey_embedded.client import (
    ServerStartError,
    _missing_binary_message,
    _safe_remove,
    _socket_path_for,
)

DEFAULT_START_TIMEOUT = 10.0


def _find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for a free TCP port by binding to port 0 and reading it back."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class ValkeyServer:
    """An embedded valkey-server with explicit lifecycle and a TCP endpoint.

    Use this when you need a host/port (bring-your-own client, another
    process, a non-Python tool) or explicit start/stop control. For the
    auto-managed, unix-socket-only client, see
    :class:`~valkey_embedded.client.Valkey` and :func:`~valkey_embedded.connect`.
    """

    def __init__(
        self,
        port: Optional[int] = None,
        host: str = "127.0.0.1",
        data_dir: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        persist: bool = False,
        **config_overrides: Any,
    ) -> None:
        """Configure (but do not start) the server.

        Args:
            port: TCP port to listen on. None auto-assigns a free port.
            host: Bind address (defaults to loopback).
            data_dir: Working directory. None creates a temp dir that is removed
                on stop (unless ``persist``).
            config: valkey.conf overrides (e.g. ``{"maxmemory": "100mb"}``).
            persist: Keep the data directory and save the RDB on stop.
            **config_overrides: Additional valkey.conf overrides, merged after
                ``config``.
        """
        self.host = host
        self._requested_port = port
        self.port: Optional[int] = None
        self.persist = persist
        self._config: Dict[str, Any] = dict(config or {})
        self._config.update(config_overrides)
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._atexit_registered = False

        self._owns_dir = data_dir is None
        if data_dir is None:
            self.data_dir = tempfile.mkdtemp(prefix="valkey_embedded-")
        else:
            self.data_dir = os.path.abspath(str(data_dir))
            os.makedirs(self.data_dir, exist_ok=True)

        self.dbfilename = "valkey.db"
        self.pidfile = os.path.join(self.data_dir, "valkey.pid")
        self.logfile = os.path.join(self.data_dir, "valkey.log")
        # Deep data dirs would overflow AF_UNIX's sun_path; relocate the
        # socket to a short private dir in that case (cleaned up on stop).
        self.socket_file, self._socket_dir = _socket_path_for(
            self.data_dir, "valkey.sock"
        )
        self.configfile = os.path.join(self.data_dir, "valkey.conf")

    # -- lifecycle -------------------------------------------------------

    def start(self, timeout: float = DEFAULT_START_TIMEOUT) -> None:
        """Start the server and block until it answers PING (or time out)."""
        if self.is_running():
            return
        self.port = (
            self._requested_port
            if self._requested_port is not None
            else _find_free_port(self.host)
        )
        if self._socket_dir:
            # A prior stop() removes the fallback socket dir; recreate it.
            os.makedirs(self._socket_dir, exist_ok=True)
        overrides: Dict[str, Any] = {
            "dbdir": self.data_dir,
            "dbfilename": self.dbfilename,
            "pidfile": self.pidfile,
            "logfile": self.logfile,
            "unixsocket": self.socket_file,
            "port": str(self.port),
            "bind": self.host,
        }
        overrides.update(self._config)
        with open(self.configfile, "w") as fh:
            fh.write(configuration.config(**overrides))

        executable = valkey_embedded.__valkey_executable__
        if not executable or not os.path.exists(executable):
            raise ServerStartError(_missing_binary_message(executable))
        self._process = subprocess.Popen(
            [executable, self.configfile],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # daemonize yes: the launcher forks the server and exits; reap it.
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - daemon exits fast
            pass

        atexit.register(self.stop)
        self._atexit_registered = True
        self._wait_until_ready(timeout)

    def _wait_until_ready(self, timeout: float) -> None:
        """Poll PING until the server answers or ``timeout`` expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                probe = self.client(socket_connect_timeout=1)
                try:
                    if probe.ping():
                        return
                finally:
                    # valkey-py's close() is untyped; harmless to call.
                    probe.close()  # type: ignore[no-untyped-call]
            except valkey.exceptions.ConnectionError:
                pass
            time.sleep(0.1)
        raise ServerStartError(
            "valkey-server failed to start within {0}s; see log at {1}".format(
                timeout, self.logfile
            )
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Gracefully shut the server down and clean up runtime files."""
        pid = self.pid
        if pid:
            try:
                probe = self.client(socket_connect_timeout=1)
                if self.persist:
                    probe.shutdown(save=True)
                else:
                    probe.shutdown(nosave=True)
            except Exception:  # noqa: BLE001 - server closes the socket on shutdown
                pass
            self._terminate(pid, timeout)
        self._cleanup_files()
        self.port = None
        if self._atexit_registered:
            try:
                atexit.unregister(self.stop)
            except Exception:  # noqa: BLE001
                pass
            self._atexit_registered = False

    def terminate(self) -> None:
        """Kill the server immediately (no save), then clean up.

        Waits for the daemon to actually exit before returning, so callers can
        rely on the pid being gone.
        """
        pid = self.pid
        if pid:
            try:
                proc = psutil.Process(pid)
                proc.kill()
                proc.wait(timeout=5)  # reap; works for the reparented daemon too
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass
        self._cleanup_files()
        self.port = None

    def _terminate(self, pid: int, timeout: float) -> None:
        """Wait for the daemon to exit, escalating to SIGTERM then SIGKILL."""
        if not pid:
            return
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not proc.is_running():
                return
            time.sleep(0.1)
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

    def _cleanup_files(self) -> None:
        """Remove the temp dir (if ours) or just the runtime files."""
        if self._owns_dir and not self.persist:
            shutil.rmtree(self.data_dir, ignore_errors=True)
        else:
            # Keep the data dir (user-owned or persisted); drop runtime files.
            for path in (self.pidfile, self.socket_file, self.configfile):
                _safe_remove(path)
        if self._socket_dir:
            shutil.rmtree(self._socket_dir, ignore_errors=True)

    # -- introspection ---------------------------------------------------

    @property
    def pid(self) -> int:
        """Daemon pid from the pidfile, or 0 if not running."""
        try:
            with open(self.pidfile) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return 0
        return pid if psutil.pid_exists(pid) else 0

    def is_running(self) -> bool:
        """True if the server was started and its daemon pid is alive."""
        return self.port is not None and self.pid != 0

    def client(self, **kwargs: Any) -> valkey.Valkey:
        """Return a valkey-py client connected to this server over TCP."""
        if self.port is None:
            # Misuse, not a start failure -- hence RuntimeError, not
            # ServerStartError.
            raise RuntimeError(
                "server is not running; call start() first or use "
                "ValkeyServer as a context manager"
            )
        return valkey.Valkey(host=self.host, port=self.port, **kwargs)

    @property
    def connection_kwargs(self) -> Dict[str, Any]:
        """Connection parameters for any Redis-compatible client."""
        return {"host": self.host, "port": self.port}

    @property
    def connection_url(self) -> str:
        """A ``valkey://host:port`` URL for this server."""
        return "valkey://{0}:{1}".format(self.host, self.port)

    # -- context manager / finalizer ------------------------------------

    def __enter__(self) -> "ValkeyServer":
        """Start the server on entering a ``with`` block."""
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Stop the server (saving if ``persist``) on leaving the block."""
        self.stop()

    def __del__(self) -> None:
        """Best-effort stop if the instance is garbage-collected."""
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - never raise from __del__
            pass
