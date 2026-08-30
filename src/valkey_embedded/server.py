"""Explicit valkey-server lifecycle control with a TCP endpoint.

`Valkey()` is the auto-managed client: it starts a private, unix-socket-only
server on construction and cleans up at exit. `ValkeyServer` is the other half
of the API -- explicit start/stop control over a server that also listens on
TCP, so *any* Redis-compatible client (or another process, or a non-Python
tool) can connect via host/port.

A successful start proves that the direct child process, private Unix socket,
pidfile, and public TCP endpoint all identify the same Valkey run. Endpoint
reachability alone is never treated as ownership.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, Optional, Set, Tuple, cast

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
_LOG_TAIL_BYTES = 4096
_PROBE_INTERVAL = 0.1

# These settings define process ownership or public object state. Letting an
# arbitrary config entry replace one would make paths, PID, or endpoint metadata
# disagree with the daemon that ValkeyServer manages. ``include`` is reserved
# because an included file could override every other managed directive.
_MANAGED_CONFIG_KEYS = frozenset(
    {
        "bind",
        "daemonize",
        "dbdir",
        "dbfilename",
        "dir",
        "include",
        "logfile",
        "pidfile",
        "port",
        "unixsocket",
        "unixsocketperm",
    }
)
_SECRET_CONFIG_KEYS = frozenset({"masterauth", "requirepass"})
_LOGGER = logging.getLogger(__name__)


class _ServerState(Enum):
    """Explicit same-process lifecycle state for ``ValkeyServer``."""

    NEW = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()


@dataclass(frozen=True)
class _DaemonIdentity:
    """Identity proven across the child process and both server endpoints."""

    pid: int
    create_time: float
    run_id: str


@dataclass
class _StartupFileSnapshot:
    """Caller file state that a failed startup attempt must restore."""

    runtime_paths: Set[str]
    config_content: Optional[bytes]
    config_mode: Optional[int]
    append_files: Dict[str, Tuple[int, int]]


class _StartupNotReady(Exception):
    """A startup identity component is not observable yet."""


class _StartupIdentityMismatch(Exception):
    """Observed startup components identify different processes or runs."""


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
    process, a non-Python tool) or explicit start/stop control. Instances are
    reusable across serialized start/stop/terminate cycles. For the
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
                Process identity, endpoint, and managed path directives cannot
                be overridden here; use the corresponding constructor argument.
            persist: Keep the data directory and save the RDB on stop.
            **config_overrides: Additional valkey.conf overrides, merged after
                ``config``.

        Raises:
            ValueError: A config override attempts to replace a lifecycle setting
                managed by ``ValkeyServer``.
        """
        self.host = host
        self._requested_port = port
        self.port: Optional[int] = None
        self.persist = persist
        self._config: Dict[str, Any] = dict(config or {})
        self._config.update(config_overrides)
        self._validate_config_overrides()

        self._process: Optional[subprocess.Popen[bytes]] = None
        self._process_create_time: Optional[float] = None
        self._identity: Optional[_DaemonIdentity] = None
        self._atexit_registered = False
        self._state = _ServerState.NEW
        self._lifecycle_lock = threading.RLock()

        self._owns_dir = data_dir is None
        if data_dir is None:
            self.data_dir = tempfile.mkdtemp(prefix="valkey_embedded-")
        else:
            self.data_dir = os.path.abspath(str(data_dir))
            os.makedirs(self.data_dir, exist_ok=True)
        self._retained_data_dir = (
            self.data_dir if not self._owns_dir or self.persist else None
        )

        self.dbfilename = "valkey.db"
        self._set_run_paths()

    def _set_run_paths(self) -> None:
        """Derive one run's managed paths together from its data directory."""
        self.pidfile = os.path.join(self.data_dir, "valkey.pid")
        self.logfile = os.path.join(self.data_dir, "valkey.log")
        self._startup_output_file = os.path.join(
            self.data_dir, "valkey.startup-output.log"
        )
        # Deep data dirs would overflow AF_UNIX's sun_path; relocate the
        # socket to a short private dir in that case (cleaned up on stop).
        self.socket_file, self._socket_dir = _socket_path_for(
            self.data_dir, "valkey.sock"
        )
        self.configfile = os.path.join(self.data_dir, "valkey.conf")

    def _prepare_run_paths(self, reuse_constructed_paths: bool) -> None:
        """Allocate or recreate all paths for one lifecycle run."""
        if reuse_constructed_paths:
            os.makedirs(self.data_dir, exist_ok=True)
            if self._socket_dir:
                os.makedirs(self._socket_dir, mode=0o700, exist_ok=True)
            return

        if self._socket_dir:
            shutil.rmtree(self._socket_dir, ignore_errors=True)
        if self._owns_dir and not self.persist:
            self.data_dir = tempfile.mkdtemp(prefix="valkey_embedded-")
        else:
            assert self._retained_data_dir is not None
            self.data_dir = self._retained_data_dir
            os.makedirs(self.data_dir, exist_ok=True)
        self._set_run_paths()

    def _validate_config_overrides(self) -> None:
        """Reject config keys that could desynchronize managed lifecycle state."""
        invalid = sorted(
            key for key in self._config if key.lower() in _MANAGED_CONFIG_KEYS
        )
        if invalid:
            raise ValueError(
                "config overrides managed lifecycle setting(s): {0}; use the "
                "ValkeyServer constructor arguments instead".format(", ".join(invalid))
            )

    # -- lifecycle -------------------------------------------------------

    def start(self, timeout: float = DEFAULT_START_TIMEOUT) -> None:
        """Start and prove the identity of this instance's daemon.

        Readiness is established first through this instance's private Unix
        socket, then by matching the run ID and PID over its public TCP endpoint.
        A foreign process listening on the requested port cannot satisfy start.

        Args:
            timeout: Maximum seconds to wait for the identity proof.

        Raises:
            ServerStartError: The process exits, times out, or cannot prove that
                its private and public endpoints belong to this launch.
            ValueError: ``timeout`` is negative.
        """
        if timeout < 0:
            raise ValueError("timeout must be non-negative")

        with self._lifecycle_lock:
            if self._state is _ServerState.RUNNING:
                if self.is_running():
                    return
                # The proven process died outside this API. Retire that run's
                # files and callback before allocating a new run identity.
                self._finish_runtime_cleanup()
            if self._state in (_ServerState.STARTING, _ServerState.STOPPING):
                raise RuntimeError(
                    "cannot start ValkeyServer while it is {0}".format(
                        self._state.name.lower()
                    )
                )

            reuse_constructed_paths = self._state is _ServerState.NEW
            self._state = _ServerState.STARTING
            self._identity = None
            self._process = None
            self._process_create_time = None
            self.port = None

            snapshot: Optional[_StartupFileSnapshot] = None
            try:
                self._prepare_run_paths(reuse_constructed_paths)
                self.port = (
                    self._requested_port
                    if self._requested_port is not None
                    else _find_free_port(self.host)
                )
                snapshot = self._snapshot_startup_files()
                self._prepare_runtime_paths()
                # Stale runtime paths removed by preparation are not caller files to
                # preserve if this attempt recreates them and then fails.
                snapshot.runtime_paths = {
                    path
                    for path in (self.pidfile, self.socket_file)
                    if os.path.exists(path)
                }
                self._write_config()
                self._launch_process()
                self._wait_until_ready(timeout)
                self._register_atexit()
            except BaseException as exc:
                # Roll back only the process and files acquired by this attempt.
                # BaseException is deliberate: KeyboardInterrupt/SystemExit must not
                # strand a direct child process or atexit callback.
                failure = self._normalize_startup_exception(exc)
                self._rollback_failed_start(snapshot)
                self._state = _ServerState.STOPPED
                if failure is exc:
                    raise
                raise failure from exc
            self._state = _ServerState.RUNNING

    def _prepare_runtime_paths(self) -> None:
        """Refuse live runtime state and remove only demonstrably stale files."""
        recorded_pid = self._read_pidfile()
        if recorded_pid and psutil.pid_exists(recorded_pid):
            raise ServerStartError(
                "refusing to start: managed pidfile {0} identifies live process "
                "{1}; use a different data_dir or stop its owner first".format(
                    self.pidfile, recorded_pid
                )
            )
        _safe_remove(self.pidfile)

        if os.path.exists(self.socket_file):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(self.socket_file)
            except OSError:
                _safe_remove(self.socket_file)
            else:
                raise ServerStartError(
                    "refusing to start: managed Unix socket {0} is already "
                    "accepting connections".format(self.socket_file)
                )
            finally:
                probe.close()

    def _snapshot_startup_files(self) -> _StartupFileSnapshot:
        """Capture caller-owned files before this attempt mutates them."""
        config_content: Optional[bytes] = None
        config_mode: Optional[int] = None
        if os.path.exists(self.configfile):
            config_stat = os.stat(self.configfile)
            with open(self.configfile, "rb") as fh:
                config_content = fh.read()
            config_mode = config_stat.st_mode & 0o7777

        append_files: Dict[str, Tuple[int, int]] = {}
        for path in (self.logfile, self._startup_output_file):
            if os.path.exists(path):
                file_stat = os.stat(path)
                append_files[path] = (file_stat.st_size, file_stat.st_mode & 0o7777)

        return _StartupFileSnapshot(
            runtime_paths={
                path
                for path in (self.pidfile, self.socket_file)
                if os.path.exists(path)
            },
            config_content=config_content,
            config_mode=config_mode,
            append_files=append_files,
        )

    def _write_config(self) -> None:
        """Render a private config whose identity settings cannot be replaced."""
        assert self.port is not None
        overrides: Dict[str, Any] = {
            "bind": self.host,
            "daemonize": "no",
            "dbdir": self.data_dir,
            "dbfilename": self.dbfilename,
            "logfile": self.logfile,
            "pidfile": self.pidfile,
            "port": str(self.port),
            "unixsocket": self.socket_file,
            "unixsocketperm": "700",
        }
        overrides.update(self._config)
        rendered = configuration.config(**overrides)
        fd = os.open(
            self.configfile,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fd = -1
                fh.write(rendered)
        finally:
            if fd >= 0:
                os.close(fd)

    def _launch_process(self) -> None:
        """Launch Valkey as a direct child and capture its anti-reuse identity."""
        executable = valkey_embedded.__valkey_executable__
        if not executable or not os.path.exists(executable):
            raise ServerStartError(_missing_binary_message(executable))
        try:
            output_fd = os.open(
                self._startup_output_file,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                os.fchmod(output_fd, 0o600)
                with os.fdopen(output_fd, "ab", buffering=0) as output:
                    output_fd = -1
                    process = subprocess.Popen(
                        [executable, self.configfile],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    )
            finally:
                if output_fd >= 0:
                    os.close(output_fd)
        except OSError as exc:
            raise ServerStartError(
                "could not launch bundled valkey-server at {0!r}: {1}".format(
                    executable, exc
                )
            ) from exc
        self._process = process
        try:
            self._process_create_time = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            # The readiness loop reports the direct child's exit status and log.
            process.poll()

    def _wait_until_ready(self, timeout: float) -> None:
        """Prove process, pidfile, private socket, and TCP endpoint identity."""
        deadline = time.monotonic() + timeout
        last_probe = "private Unix socket is not ready"

        while True:
            process = self._process
            if process is None:
                raise self._startup_error("valkey-server process was not launched")
            returncode = process.poll()
            if returncode is not None:
                raise self._startup_error(
                    "valkey-server exited with status {0} before startup identity "
                    "was verified".format(returncode)
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            probe_timeout = max(0.01, min(1.0, remaining))
            try:
                identity = self._probe_private_identity(probe_timeout)
                self._verify_tcp_identity(identity, probe_timeout)
                if self._matching_process(identity.pid, identity.create_time) is None:
                    raise _StartupIdentityMismatch(
                        "the launched PID changed or exited during readiness"
                    )
            except _StartupNotReady as exc:
                last_probe = str(exc)
            except _StartupIdentityMismatch as exc:
                raise self._startup_error(
                    "valkey-server startup identity mismatch: {0}".format(exc)
                ) from exc
            else:
                self._identity = identity
                return

            sleep_for = min(_PROBE_INTERVAL, max(0.0, deadline - time.monotonic()))
            if sleep_for:
                time.sleep(sleep_for)

        raise self._startup_error(
            "valkey-server failed to start and prove daemon identity within "
            "{0}s; last probe: {1}".format(timeout, last_probe)
        )

    def _probe_private_identity(self, timeout: float) -> _DaemonIdentity:
        """Read and validate identity through this instance's Unix socket."""
        process = self._process
        if process is None:
            raise _StartupNotReady("direct child process is unavailable")

        recorded_pid = self._read_pidfile()
        if not recorded_pid:
            raise _StartupNotReady("managed pidfile is not ready")
        if recorded_pid != process.pid:
            raise _StartupIdentityMismatch(
                "pidfile PID {0} does not match launched PID {1}".format(
                    recorded_pid, process.pid
                )
            )

        probe = self._private_client(
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        try:
            if not probe.ping():
                raise _StartupNotReady("private Unix socket did not answer PING")
            info = cast(Dict[str, Any], probe.info("server"))
        except valkey.exceptions.ValkeyError as exc:
            raise _StartupNotReady(
                "private Unix socket is not ready: {0}".format(exc)
            ) from exc
        finally:
            probe.close()  # type: ignore[no-untyped-call]

        try:
            info_pid = int(info["process_id"])
            info_port = int(info["tcp_port"])
            run_id = str(info["run_id"])
            config_file = str(info["config_file"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _StartupIdentityMismatch(
                "private endpoint returned incomplete server identity"
            ) from exc
        if info_pid != process.pid:
            raise _StartupIdentityMismatch(
                "private endpoint PID {0} does not match launched PID {1}".format(
                    info_pid, process.pid
                )
            )
        if info_port != self.port:
            raise _StartupIdentityMismatch(
                "private endpoint reports TCP port {0}, expected {1}".format(
                    info_port, self.port
                )
            )
        if not run_id:
            raise _StartupIdentityMismatch("private endpoint returned an empty run ID")
        if os.path.realpath(config_file) != os.path.realpath(self.configfile):
            raise _StartupIdentityMismatch(
                "private endpoint reports unexpected config file {0!r}".format(
                    config_file
                )
            )

        create_time = self._process_create_time
        if create_time is None:
            try:
                create_time = psutil.Process(process.pid).create_time()
            except psutil.NoSuchProcess as exc:
                raise _StartupNotReady("launched process exited") from exc
            self._process_create_time = create_time
        if self._matching_process(process.pid, create_time) is None:
            raise _StartupIdentityMismatch(
                "launched process creation identity no longer matches"
            )
        return _DaemonIdentity(process.pid, create_time, run_id)

    def _verify_tcp_identity(self, identity: _DaemonIdentity, timeout: float) -> None:
        """Require the public TCP endpoint to report the private run identity."""
        probe = self._tcp_client(
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        try:
            if not probe.ping():
                raise _StartupNotReady("TCP endpoint did not answer PING")
            info = cast(Dict[str, Any], probe.info("server"))
        except valkey.exceptions.ValkeyError as exc:
            raise _StartupNotReady(
                "TCP endpoint is not ready: {0}".format(exc)
            ) from exc
        finally:
            probe.close()  # type: ignore[no-untyped-call]

        try:
            tcp_pid = int(info["process_id"])
            tcp_run_id = str(info["run_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _StartupIdentityMismatch(
                "TCP endpoint returned incomplete server identity"
            ) from exc
        if tcp_pid != identity.pid or tcp_run_id != identity.run_id:
            raise _StartupIdentityMismatch(
                "TCP endpoint belongs to PID/run ID {0}/{1}, expected {2}/{3}".format(
                    tcp_pid,
                    tcp_run_id,
                    identity.pid,
                    identity.run_id,
                )
            )

    def _normalize_startup_exception(self, exc: BaseException) -> BaseException:
        """Wrap unexpected startup failures without replacing control signals."""
        if isinstance(exc, ServerStartError) or not isinstance(exc, Exception):
            return exc
        return self._startup_error(
            "valkey-server startup failed: {0}: {1}".format(type(exc).__name__, exc)
        )

    def _startup_error(self, reason: str) -> ServerStartError:
        """Create an actionable bounded error before failed files are removed."""
        endpoint_port: object = self.port if self.port is not None else "<unassigned>"
        message = "{0} for {1}:{2}".format(reason, self.host, endpoint_port)
        log_tail = self._redacted_log_tail()
        startup_tail = self._redacted_file_tail(self._startup_output_file)
        if log_tail:
            message += "\nvalkey.log tail ({0}):\n{1}".format(self.logfile, log_tail)
        if startup_tail:
            message += "\nvalkey startup output tail ({0}):\n{1}".format(
                self._startup_output_file, startup_tail
            )
        if not log_tail and not startup_tail:
            message += "; no valkey startup output was captured"
        return ServerStartError(message)

    def _redacted_log_tail(self) -> str:
        """Return the bounded, redacted managed Valkey log tail."""
        return self._redacted_file_tail(self.logfile)

    def _redacted_file_tail(self, path: str) -> str:
        """Return a bounded file tail with configured secrets removed."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - _LOG_TAIL_BYTES))
                tail = fh.read(_LOG_TAIL_BYTES).decode("utf-8", errors="replace")
        except OSError:
            return ""

        for key, value in self._config.items():
            if key.lower() not in _SECRET_CONFIG_KEYS:
                continue
            values = value if isinstance(value, list) else [value]
            for secret in values:
                if secret:
                    tail = tail.replace(str(secret), "<redacted>")
        return tail.strip()

    def _rollback_failed_start(self, snapshot: Optional[_StartupFileSnapshot]) -> None:
        """Terminate only this attempt's child and restore caller file state."""
        process = self._process
        create_time = self._process_create_time
        try:
            if process is not None and create_time is not None:
                self._terminate_process(process.pid, create_time, timeout=0)
            elif process is not None:
                # poll() identifies whether this exact child is still waitable. If
                # alive, capture creation time before signalling to prevent PID reuse.
                if process.poll() is None:
                    try:
                        create_time = psutil.Process(process.pid).create_time()
                    except psutil.NoSuchProcess:
                        create_time = None
                    if create_time is not None:
                        self._terminate_process(process.pid, create_time, timeout=0)
        except Exception as exc:  # noqa: BLE001 - preserve the startup failure
            _LOGGER.warning("failed to terminate owned startup process: %s", exc)

        if process is not None:
            try:
                process.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            if self._owns_dir and not self.persist:
                shutil.rmtree(self.data_dir, ignore_errors=True)
            elif snapshot is not None:
                # Persistent and caller-owned directories survive, while files
                # mutated or generated by this attempt return to their old state.
                self._restore_startup_files(snapshot)
            if self._socket_dir:
                shutil.rmtree(self._socket_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - preserve the startup failure
            _LOGGER.warning("failed to restore startup files: %s", exc)
        finally:
            self._unregister_atexit()
            self._reset_runtime_state()

    def _restore_startup_files(self, snapshot: _StartupFileSnapshot) -> None:
        """Restore caller files and remove files generated by a failed attempt."""
        for path in (self.pidfile, self.socket_file):
            if path not in snapshot.runtime_paths:
                _safe_remove(path)

        if snapshot.config_content is None:
            _safe_remove(self.configfile)
        else:
            fd = os.open(
                self.configfile,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                snapshot.config_mode or 0o600,
            )
            try:
                if snapshot.config_mode is not None:
                    os.fchmod(fd, snapshot.config_mode)
                with os.fdopen(fd, "wb") as fh:
                    fd = -1
                    fh.write(snapshot.config_content)
            finally:
                if fd >= 0:
                    os.close(fd)

        for path in (self.logfile, self._startup_output_file):
            previous = snapshot.append_files.get(path)
            if previous is None:
                _safe_remove(path)
                continue
            size, mode = previous
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, mode)
            try:
                os.ftruncate(fd, size)
                os.fchmod(fd, mode)
            finally:
                os.close(fd)

    def stop(self, timeout: float = 5.0) -> None:
        """Gracefully stop this run; repeated calls are bounded no-ops."""
        with self._lifecycle_lock:
            if self._state is _ServerState.STOPPED:
                return
            if self._state is _ServerState.NEW:
                self._discard_unstarted_paths()
                return
            if self._state in (_ServerState.STARTING, _ServerState.STOPPING):
                raise RuntimeError(
                    "cannot stop ValkeyServer while it is {0}".format(
                        self._state.name.lower()
                    )
                )

            self._state = _ServerState.STOPPING
            identity = self._identity
            try:
                if identity is not None and self._matching_process(
                    identity.pid, identity.create_time
                ):
                    try:
                        probe = self._private_client(
                            socket_connect_timeout=1,
                            socket_timeout=1,
                        )
                        try:
                            info = cast(Dict[str, Any], probe.info("server"))
                            endpoint_matches = (
                                int(info["process_id"]) == identity.pid
                                and str(info["run_id"]) == identity.run_id
                            )
                            if endpoint_matches:
                                if self.persist:
                                    probe.shutdown(save=True)
                                else:
                                    probe.shutdown(nosave=True)
                        finally:
                            probe.close()  # type: ignore[no-untyped-call]
                    except Exception as exc:  # noqa: BLE001 - socket closes on shutdown
                        _LOGGER.debug("graceful shutdown probe failed: %s", exc)
                    self._terminate_process(identity.pid, identity.create_time, timeout)
            finally:
                self._finish_runtime_cleanup()

    def terminate(self) -> None:
        """Kill this run immediately; repeated calls are bounded no-ops."""
        with self._lifecycle_lock:
            if self._state is _ServerState.STOPPED:
                return
            if self._state is _ServerState.NEW:
                self._discard_unstarted_paths()
                return
            if self._state in (_ServerState.STARTING, _ServerState.STOPPING):
                raise RuntimeError(
                    "cannot terminate ValkeyServer while it is {0}".format(
                        self._state.name.lower()
                    )
                )

            self._state = _ServerState.STOPPING
            identity = self._identity
            try:
                if identity is not None:
                    process = self._matching_process(identity.pid, identity.create_time)
                    if process is not None:
                        try:
                            process.kill()
                            process.wait(timeout=5)
                        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                            pass
            finally:
                self._finish_runtime_cleanup()

    def _discard_unstarted_paths(self) -> None:
        """Move NEW to STOPPED without deleting caller-owned runtime names."""
        if self._owns_dir and not self.persist:
            shutil.rmtree(self.data_dir, ignore_errors=True)
        if self._socket_dir:
            shutil.rmtree(self._socket_dir, ignore_errors=True)
        self._unregister_atexit()
        self._reset_runtime_state()
        self._state = _ServerState.STOPPED

    def _finish_runtime_cleanup(self) -> None:
        """Retire all state belonging to a started or externally dead run."""
        try:
            self._cleanup_files()
        finally:
            self._unregister_atexit()
            self._reset_runtime_state()
            self._state = _ServerState.STOPPED

    def _terminate_process(self, pid: int, create_time: float, timeout: float) -> None:
        """Stop one creation-time-verified process with bounded escalation."""
        if pid <= 0:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._matching_process(pid, create_time) is None:
                return
            time.sleep(min(_PROBE_INTERVAL, deadline - time.monotonic()))

        process = self._matching_process(pid, create_time)
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass

        process = self._matching_process(pid, create_time)
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass

    def _matching_process(
        self, pid: int, create_time: float
    ) -> Optional[psutil.Process]:
        """Return the direct child only while its anti-reuse identity matches."""
        process = self._process
        if process is None or process.pid != pid or process.poll() is not None:
            return None
        try:
            candidate = psutil.Process(pid)
            if candidate.create_time() != create_time:
                return None
            if not candidate.is_running() or candidate.status() == psutil.STATUS_ZOMBIE:
                return None
        except psutil.NoSuchProcess:
            return None
        return candidate

    def _register_atexit(self) -> None:
        """Register one fallback callback for a launched process."""
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

    def _unregister_atexit(self) -> None:
        """Remove this instance's fallback callback if registered."""
        if not self._atexit_registered:
            return
        try:
            atexit.unregister(self.stop)
        except Exception as exc:  # noqa: BLE001 - best-effort interpreter cleanup
            _LOGGER.debug("could not unregister atexit callback: %s", exc)
        self._atexit_registered = False

    def _reset_runtime_state(self) -> None:
        """Clear per-run state only after process responsibility is resolved."""
        self.port = None
        self._identity = None
        self._process = None
        self._process_create_time = None

    def _cleanup_files(self) -> None:
        """Remove the temp dir (if ours) or just the managed runtime files."""
        if self._owns_dir and not self.persist:
            shutil.rmtree(self.data_dir, ignore_errors=True)
        else:
            # Keep caller-owned or persisted data and logs; drop runtime files.
            for path in (self.pidfile, self.socket_file, self.configfile):
                _safe_remove(path)
        if self._socket_dir:
            shutil.rmtree(self._socket_dir, ignore_errors=True)

    # -- introspection ---------------------------------------------------

    def _read_pidfile(self) -> int:
        """Return the unverified positive pidfile value, or zero."""
        try:
            with open(self.pidfile) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return 0
        return pid if pid > 0 else 0

    @property
    def pid(self) -> int:
        """Verified daemon PID, or zero outside the live RUNNING state."""
        identity = self._identity
        if self._state is not _ServerState.RUNNING or identity is None:
            return 0
        if self._matching_process(identity.pid, identity.create_time) is None:
            return 0
        return identity.pid

    def is_running(self) -> bool:
        """Return whether RUNNING still has its identity-proven direct child."""
        return (
            self._state is _ServerState.RUNNING
            and self.port is not None
            and self.pid != 0
        )

    def _private_client(self, **kwargs: Any) -> valkey.Valkey:
        """Return an internal client for this instance's private Unix socket."""
        return valkey.Valkey(unix_socket_path=self.socket_file, **kwargs)

    def _tcp_client(self, **kwargs: Any) -> valkey.Valkey:
        """Return an unchecked TCP client used only during identity proof."""
        assert self.port is not None
        return valkey.Valkey(host=self.host, port=self.port, **kwargs)

    def client(self, **kwargs: Any) -> valkey.Valkey:
        """Return a TCP client only while this server's identity is live."""
        if not self.is_running():
            # Misuse or a dead owned process, not a new start failure.
            raise RuntimeError(
                "server is not running; call start() first or use "
                "ValkeyServer as a context manager"
            )
        return self._tcp_client(**kwargs)

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
