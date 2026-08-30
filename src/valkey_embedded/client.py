# src/valkey_embedded/client.py
"""Embedded valkey-server lifecycle: start, connect, share, and clean up.

Ported from redislite's proven design, modernized with type hints. Hard-won
fixes preserved deliberately:
  * shutdown signals only a creation-time-verified bundled daemon;
  * versioned managed holders elect the final shared cleanup owner;
  * readiness is polled over the socket, not slept for.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Dict, Iterator, Optional, cast

import psutil
import valkey

import valkey_embedded
from valkey_embedded import configuration

logger = logging.getLogger(__name__)

# patch_valkey() replaces valkey.Valkey at runtime, so keep a stable reference
# for inheritance and explicit upstream lifecycle calls.
_ValkeyClient = valkey.Valkey

DEFAULT_DBFILENAME = "valkey.db"
DEFAULT_START_TIMEOUT = 10
_REGISTRY_VERSION = 2
_REGISTRY_PROBE_TIMEOUT = 0.5
_REGISTRY_POLL_INTERVAL = 0.1
_MAX_REGISTRY_BYTES = 1024 * 1024
_REGISTRY_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_REGISTRY_THREAD_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class _ManagedDaemonIdentity:
    """Anti-PID-reuse identity shared by registry owners and attachers."""

    pid: int
    create_time: float
    run_id: str


@dataclass(frozen=True)
class _RegistryHolder:
    """One live managed client participating in shared lifecycle ownership."""

    token: str
    pid: int
    create_time: float

    def as_dict(self) -> Dict[str, Any]:
        """Return the non-secret JSON representation."""
        return {
            "token": self.token,
            "pid": self.pid,
            "process_create_time": self.create_time,
        }


@dataclass(frozen=True)
class _RegistryRecord:
    """Versioned, endpoint-proven state published for shared attachment."""

    database: str
    identity: _ManagedDaemonIdentity
    pidfile: str
    unixsocket: str
    socket_dir: Optional[str]
    socket_owned: bool
    dbdir: str
    dbfilename: str
    configfile: str
    logfile: str
    holders: "tuple[_RegistryHolder, ...]"

    def as_dict(self) -> Dict[str, Any]:
        """Return the non-secret JSON representation."""
        return {
            "version": _REGISTRY_VERSION,
            "database": self.database,
            "pid": self.identity.pid,
            "process_create_time": self.identity.create_time,
            "run_id": self.identity.run_id,
            "pidfile": self.pidfile,
            "unixsocket": self.unixsocket,
            "socket_dir": self.socket_dir,
            "socket_owned": self.socket_owned,
            "dbdir": self.dbdir,
            "dbfilename": self.dbfilename,
            "configfile": self.configfile,
            "logfile": self.logfile,
            "holders": [holder.as_dict() for holder in self.holders],
        }


class _RegistryNotReady(Exception):
    """A newly launched managed endpoint is not observable yet."""


class _RegistryIdentityMismatch(Exception):
    """Registry/process/endpoint components identify different daemons."""


class ValkeyEmbeddedError(Exception):
    """Base class for all valkey_embedded errors."""


class ServerStartError(ValkeyEmbeddedError):
    """The embedded valkey-server could not become identity-verified and ready."""


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


def _safe_rmdir(path: Optional[str]) -> None:
    """Remove an empty directory; preserve it if unexpected contents exist."""
    if not path:
        return
    try:
        os.rmdir(path)
    except OSError:
        pass


def _required_registry_string(data: Dict[str, Any], key: str) -> str:
    """Read one required non-empty registry string."""
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("registry field {0!r} must be a non-empty string".format(key))
    return value


def _server_identity_fields(info: Dict[str, Any]) -> "tuple[int, str, str]":
    """Parse the strict identity fields returned by ``INFO server``."""
    raw_pid = info["process_id"]
    run_id = info["run_id"]
    config_file = info["config_file"]
    if isinstance(raw_pid, bool):
        raise ValueError("server process ID must not be boolean")
    pid = int(raw_pid)
    if pid <= 0 or not isinstance(run_id, str) or not run_id:
        raise ValueError("server PID and run ID must be populated")
    if not isinstance(config_file, str) or not config_file:
        raise ValueError("server config path must be populated")
    return pid, run_id, config_file


def _parse_registry_holder(raw: Any) -> _RegistryHolder:
    """Parse one managed holder identity."""
    if not isinstance(raw, dict):
        raise ValueError("registry holder must be an object")
    data = cast(Dict[str, Any], raw)
    if set(data) != {"token", "pid", "process_create_time"}:
        raise ValueError("registry holder has unexpected fields")
    pid = data.get("pid")
    create_time = data.get("process_create_time")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("registry holder PID must be a positive integer")
    if (
        not isinstance(create_time, (int, float))
        or isinstance(create_time, bool)
        or create_time <= 0
    ):
        raise ValueError("registry holder process_create_time must be positive")
    return _RegistryHolder(
        token=_required_registry_string(data, "token"),
        pid=pid,
        create_time=float(create_time),
    )


def _parse_registry_record(raw: Any) -> _RegistryRecord:
    """Parse the exact supported registry schema without adopting its paths."""
    if not isinstance(raw, dict):
        raise ValueError("registry root must be an object")
    data = cast(Dict[str, Any], raw)
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("registry version must be an integer")
    if version != _REGISTRY_VERSION:
        raise ValueError("unsupported registry version {0!r}".format(version))
    expected_fields = {
        "version",
        "database",
        "pid",
        "process_create_time",
        "run_id",
        "pidfile",
        "unixsocket",
        "socket_dir",
        "socket_owned",
        "dbdir",
        "dbfilename",
        "configfile",
        "logfile",
        "holders",
    }
    if set(data) != expected_fields:
        raise ValueError("registry has missing or unexpected fields")

    pid = data.get("pid")
    create_time = data.get("process_create_time")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("registry PID must be a positive integer")
    if (
        not isinstance(create_time, (int, float))
        or isinstance(create_time, bool)
        or create_time <= 0
    ):
        raise ValueError("registry process_create_time must be positive")

    socket_dir_value = data.get("socket_dir")
    if socket_dir_value is not None and (
        not isinstance(socket_dir_value, str) or not socket_dir_value
    ):
        raise ValueError("registry socket_dir must be null or a non-empty string")
    socket_owned = data.get("socket_owned")
    if not isinstance(socket_owned, bool):
        raise ValueError("registry socket_owned must be boolean")
    raw_holders = data.get("holders")
    if not isinstance(raw_holders, list) or not raw_holders:
        raise ValueError("registry holders must be a non-empty list")
    holders = tuple(_parse_registry_holder(holder) for holder in raw_holders)
    if len({holder.token for holder in holders}) != len(holders):
        raise ValueError("registry holder tokens must be unique")

    return _RegistryRecord(
        database=_required_registry_string(data, "database"),
        identity=_ManagedDaemonIdentity(
            pid=pid,
            create_time=float(create_time),
            run_id=_required_registry_string(data, "run_id"),
        ),
        pidfile=_required_registry_string(data, "pidfile"),
        unixsocket=_required_registry_string(data, "unixsocket"),
        socket_dir=socket_dir_value,
        socket_owned=socket_owned,
        dbdir=_required_registry_string(data, "dbdir"),
        dbfilename=_required_registry_string(data, "dbfilename"),
        configfile=_required_registry_string(data, "configfile"),
        logfile=_required_registry_string(data, "logfile"),
        holders=holders,
    )


def _read_private_registry(path: str) -> _RegistryRecord:
    """Read one owner-only regular registry without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("registry is not a regular file")
        if file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ValueError("registry is not owner-only")
        if file_stat.st_size > _MAX_REGISTRY_BYTES:
            raise ValueError("registry exceeds the size limit")
        with os.fdopen(fd) as fh:
            fd = -1
            return _parse_registry_record(json.load(fh))
    finally:
        if fd >= 0:
            os.close(fd)


def _registry_thread_lock(path: str) -> threading.RLock:
    """Return the stable same-process lock paired with one registry lockfile."""
    with _REGISTRY_THREAD_LOCKS_GUARD:
        lock = _REGISTRY_THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _REGISTRY_THREAD_LOCKS[path] = lock
        return lock


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

        def info(self, *args: Any, **kwargs: Any) -> Any: ...

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
                path attach to one server, and the last managed holder to
                close shuts it down. None (default) gives a private server
                whose temp directory is removed on exit.
            *args: Forwarded to the underlying valkey-py client.
            serverconfig: valkey.conf overrides rendered into the generated
                config, e.g. ``{"maxmemory": "64mb"}``.
            **kwargs: Forwarded to the underlying valkey-py client
                (e.g. ``decode_responses=True``).

        Raises:
            ServerStartError: The bundled binary is missing or the server
                did not answer PING within ``start_timeout`` seconds.
        """
        self._lifecycle_lock = threading.Lock()
        self._server_config = dict(serverconfig or {})
        self._server_process: Optional[subprocess.Popen[bytes]] = None
        self._daemon_identity: Optional[_ManagedDaemonIdentity] = None
        self.running = False
        # A caller may pass unix_socket_path; otherwise one is computed under
        # dbdir. Either way self.socket_file is canonical and re-injected into
        # kwargs before super().__init__().
        self.socket_file: Optional[str] = kwargs.pop("unix_socket_path", None)
        self._socket_owned = self.socket_file is None
        self._probe_kwargs = {
            key: kwargs[key]
            for key in ("username", "password", "credential_provider")
            if key in kwargs
        }
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
        self._database_identity = os.path.realpath(
            os.path.join(self.dbdir, self.dbfilename)
        )
        self.pidfile = os.path.join(self.dbdir, "valkey.pid")
        self.logfile = os.path.join(self.dbdir, "valkey.log")
        self._socket_dir: Optional[str] = None
        if not self.socket_file:
            self.socket_file, self._socket_dir = _socket_path_for(
                self.dbdir, "valkey.socket"
            )
        self._registry_lockfile = (
            self.settingregistryfile + ".lock" if self.settingregistryfile else None
        )
        self._registry_record: Optional[_RegistryRecord] = None
        self._registry_holder: Optional[_RegistryHolder] = None
        self._preexisting_pidfile_identity: Optional["tuple[int, Optional[float]]"] = (
            None
        )

        atexit.register(self._cleanup)

        started = False
        attached = False
        failure_cleaned = False
        coordination = (
            self._locked_setting_registry()
            if self.settingregistryfile
            else nullcontext()
        )
        try:
            if self.settingregistryfile:
                holder_process = psutil.Process(os.getpid())
                self._registry_holder = _RegistryHolder(
                    token=secrets.token_hex(16),
                    pid=os.getpid(),
                    create_time=holder_process.create_time(),
                )
            with coordination:
                try:
                    initial_socket_dir = self._socket_dir
                    attached = bool(
                        self.settingregistryfile and self._load_setting_registry()
                    )
                    if attached:
                        logger.debug(
                            "Attached to identity-verified shared server via %s",
                            self.settingregistryfile,
                        )
                        if (
                            initial_socket_dir
                            and initial_socket_dir != self._socket_dir
                        ):
                            shutil.rmtree(initial_socket_dir, ignore_errors=True)
                    else:
                        if self.settingregistryfile:
                            self._prepare_stale_runtime_paths()
                        self._start_server()
                        started = True
                        self._daemon_identity = self._wait_for_managed_identity()

                    kwargs["unix_socket_path"] = self.socket_file
                    super().__init__(*args, **kwargs)
                    self._wait_until_ready()
                    if started and self.settingregistryfile:
                        self._save_setting_registry()
                    elif attached and self.settingregistryfile:
                        self._register_registry_holder()
                    self.running = True
                except Exception:
                    # Keep rollback under the same registry lock so another
                    # opener cannot race a half-cleaned failed attempt.
                    self._rollback_failed_initialization(started, attached)
                    failure_cleaned = True
                    raise
        except Exception:
            if not failure_cleaned:
                self._rollback_failed_initialization(started, attached)
            try:
                atexit.unregister(self._cleanup)
            except Exception as exc:  # noqa: BLE001 - best-effort failed-init cleanup
                logger.debug("could not unregister failed initialization: %s", exc)
            raise

    # -- startup ---------------------------------------------------------

    def _rollback_failed_initialization(self, started: bool, attached: bool) -> None:
        """Clean only state proven to belong to this failed constructor."""
        launch_attempted = started or self._server_process is not None
        launcher_failed = bool(
            self._server_process is not None
            and self._server_process.poll() not in (None, 0)
        )
        if launch_attempted:
            if self._daemon_identity is not None:
                self._terminate(self._daemon_identity.pid, grace_period=0)
                self._remove_files()
            elif launcher_failed and not self.settingregistryfile and self.dbdir:
                # A nonzero launcher exit proves that no daemonized child was
                # accepted; the isolated directory belongs to this attempt.
                shutil.rmtree(self.dbdir, ignore_errors=True)
                if self._socket_dir:
                    shutil.rmtree(self._socket_dir, ignore_errors=True)
                    self._socket_dir = None
            else:
                # Never erase paths or signal a process when startup could not
                # prove that the daemon behind them belongs to this attempt.
                logger.warning(
                    "preserving unverified startup state under %s",
                    self.dbdir,
                )
        elif not attached and not self.settingregistryfile and self.dbdir:
            shutil.rmtree(self.dbdir, ignore_errors=True)
        if not launch_attempted and not attached and self._socket_dir:
            shutil.rmtree(self._socket_dir, ignore_errors=True)
            self._socket_dir = None
        if hasattr(self, "connection_pool"):
            try:
                _ValkeyClient.close(self)  # type: ignore[arg-type,no-untyped-call]
            except Exception as exc:  # noqa: BLE001 - preserve startup error
                logger.debug("could not close failed client initialization: %s", exc)

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
        if self._server_process.returncode not in (None, 0):
            raise ServerStartError(
                "valkey-server launcher exited with status {0}; see log at {1}".format(
                    self._server_process.returncode,
                    self.logfile,
                )
            )

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

    def _read_pidfile_value(self, path: Optional[str] = None) -> int:
        """Return a positive unverified pidfile value, or zero."""
        try:
            with open(path or self.pidfile) as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return 0
        return pid if pid > 0 else 0

    def _probe_server_info(self, socket_file: str) -> Dict[str, Any]:
        """PING one Unix endpoint and return its server identity payload."""
        probe_kwargs = dict(self._probe_kwargs)
        probe_kwargs.update(
            {
                "socket_connect_timeout": _REGISTRY_PROBE_TIMEOUT,
                "socket_timeout": _REGISTRY_PROBE_TIMEOUT,
            }
        )
        probe = _ValkeyClient(unix_socket_path=socket_file, **probe_kwargs)
        try:
            if not probe.ping():
                raise _RegistryNotReady("Unix endpoint did not answer PING")
            return cast(Dict[str, Any], probe.info("server"))
        finally:
            probe.close()  # type: ignore[no-untyped-call]

    def _matching_managed_process(
        self, identity: _ManagedDaemonIdentity
    ) -> Optional[psutil.Process]:
        """Return a live bundled process only while creation identity matches."""
        if identity.pid <= 0:
            return None
        try:
            process = psutil.Process(identity.pid)
            if process.create_time() != identity.create_time:
                return None
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            expected_executable = os.path.realpath(
                valkey_embedded.__valkey_executable__
            )
            if (
                not expected_executable
                or os.path.realpath(process.exe()) != expected_executable
            ):
                return None
        except (OSError, psutil.AccessDenied, psutil.NoSuchProcess):
            return None
        return process

    def _current_managed_identity(self) -> _ManagedDaemonIdentity:
        """Prove pidfile, process, config, and Unix endpoint identity once."""
        pid = self._read_pidfile_value()
        if not pid:
            raise _RegistryNotReady("managed pidfile is not ready")
        try:
            process = psutil.Process(pid)
            create_time = process.create_time()
        except psutil.NoSuchProcess as exc:
            raise _RegistryNotReady("pidfile process exited") from exc
        except psutil.AccessDenied as exc:
            raise _RegistryIdentityMismatch(
                "cannot verify pidfile process creation identity"
            ) from exc
        preexisting = getattr(self, "_preexisting_pidfile_identity", None)
        if (
            preexisting is not None
            and preexisting[0] == pid
            and preexisting[1] is not None
            and preexisting[1] == create_time
        ):
            raise _RegistryNotReady("waiting for daemon to replace stale pidfile")
        candidate = _ManagedDaemonIdentity(pid, create_time, "pending")
        if self._matching_managed_process(candidate) is None:
            raise _RegistryIdentityMismatch(
                "pidfile process is not the bundled valkey-server"
            )
        assert self.socket_file is not None
        try:
            socket_stat = os.stat(self.socket_file)
        except OSError as exc:
            raise _RegistryNotReady("managed Unix socket is not ready") from exc
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise _RegistryNotReady("managed Unix socket path is not a socket")

        try:
            info = self._probe_server_info(self.socket_file)
        except (OSError, valkey.exceptions.ValkeyError) as exc:
            raise _RegistryNotReady("managed Unix endpoint is not ready") from exc
        try:
            info_pid, run_id, config_file = _server_identity_fields(info)
        except (KeyError, TypeError, ValueError) as exc:
            raise _RegistryIdentityMismatch(
                "managed endpoint returned incomplete server identity"
            ) from exc
        if info_pid != pid:
            raise _RegistryIdentityMismatch(
                "endpoint PID {0} does not match pidfile PID {1}".format(info_pid, pid)
            )
        if self.configfile is None or os.path.realpath(config_file) != os.path.realpath(
            self.configfile
        ):
            raise _RegistryIdentityMismatch(
                "endpoint reports unexpected config file {0!r}".format(config_file)
            )
        return _ManagedDaemonIdentity(pid, create_time, run_id)

    def _wait_for_managed_identity(self) -> _ManagedDaemonIdentity:
        """Poll boundedly until a newly launched daemon proves its identity."""
        deadline = time.monotonic() + self.start_timeout
        last_probe = "managed pidfile is not ready"
        while time.monotonic() < deadline:
            try:
                return self._current_managed_identity()
            except _RegistryNotReady as exc:
                last_probe = str(exc)
            except _RegistryIdentityMismatch as exc:
                raise ServerStartError(
                    "valkey-server startup identity mismatch: {0}".format(exc)
                ) from exc
            sleep_for = min(
                _REGISTRY_POLL_INTERVAL,
                max(0.0, deadline - time.monotonic()),
            )
            if sleep_for:
                time.sleep(sleep_for)
        raise ServerStartError(
            "valkey-server failed to prove managed identity within {0}s; "
            "last probe: {1}; see log at {2}".format(
                self.start_timeout, last_probe, self.logfile
            )
        )

    @contextmanager
    def _locked_setting_registry(self) -> Iterator[None]:
        """Serialize shared load/recovery/publication across threads/processes."""
        assert self._registry_lockfile is not None
        thread_lock = _registry_thread_lock(self._registry_lockfile)
        deadline = time.monotonic() + max(0.1, float(self.start_timeout))
        if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise ServerStartError("timed out acquiring shared registry thread lock")
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self._registry_lockfile, flags, 0o600)
            except OSError as exc:
                raise ServerStartError(
                    "could not securely open shared registry lock"
                ) from exc
            try:
                lock_stat = os.fstat(fd)
                if (
                    not stat.S_ISREG(lock_stat.st_mode)
                    or lock_stat.st_uid != os.geteuid()
                    or lock_stat.st_nlink != 1
                ):
                    raise ServerStartError(
                        "shared registry lock is not a private regular file"
                    )
                os.fchmod(fd, 0o600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            raise ServerStartError(
                                "timed out acquiring shared registry process lock"
                            ) from exc
                        time.sleep(
                            min(
                                _REGISTRY_POLL_INTERVAL,
                                max(0.0, deadline - time.monotonic()),
                            )
                        )
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            thread_lock.release()

    def _socket_accepts_connections(self, path: str) -> bool:
        """Return whether any process currently accepts this Unix socket."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(_REGISTRY_PROBE_TIMEOUT)
        try:
            probe.connect(path)
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def _allocate_recovery_socket(self) -> None:
        """Choose a fresh owned socket without unlinking an unverified listener."""
        if not self._socket_owned:
            raise ServerStartError(
                "refusing to replace caller-provided live Unix socket {0}".format(
                    self.socket_file
                )
            )
        base = "/tmp" if os.path.isdir("/tmp") else None
        self._socket_dir = tempfile.mkdtemp(prefix="vkey-sock-", dir=base)
        self.socket_file = os.path.join(self._socket_dir, "valkey.socket")

    def _prepare_stale_runtime_paths(self) -> None:
        """Remove only stale managed names before starting registry recovery."""
        # The daemon will rewrite a stale pidfile. Do not unlink it first
        # because an invalid registry has not established who owns it.
        stale_pid = self._read_pidfile_value()
        if stale_pid:
            try:
                process = psutil.Process(stale_pid)
                stale_create_time: Optional[float] = process.create_time()
            except psutil.NoSuchProcess:
                stale_create_time = None
            except psutil.AccessDenied as exc:
                raise ServerStartError(
                    "refusing recovery: stale pidfile process cannot be verified"
                ) from exc
            if (
                stale_create_time is not None
                and self._matching_managed_process(
                    _ManagedDaemonIdentity(stale_pid, stale_create_time, "pending")
                )
                is not None
            ):
                raise ServerStartError(
                    "refusing recovery: stale pidfile names a live bundled process"
                )
            self._preexisting_pidfile_identity = (stale_pid, stale_create_time)

        assert self.socket_file is not None
        if not os.path.exists(self.socket_file):
            return
        try:
            self._probe_server_info(self.socket_file)
        except valkey.exceptions.AuthenticationError as exc:
            raise ServerStartError(
                "refusing registry recovery: a live authenticated Valkey uses "
                "Unix socket {0}".format(self.socket_file)
            ) from exc
        except (OSError, valkey.exceptions.ValkeyError, _RegistryNotReady):
            if self._socket_accepts_connections(self.socket_file):
                self._allocate_recovery_socket()
            elif not self._socket_owned:
                raise ServerStartError(
                    "refusing to remove caller-provided Unix socket {0}".format(
                        self.socket_file
                    )
                )
            else:
                _safe_remove(self.socket_file)
        else:
            raise ServerStartError(
                "refusing registry recovery: an unverified live Valkey uses "
                "Unix socket {0}".format(self.socket_file)
            )

    # -- shared/isolated registry ---------------------------------------

    def _current_registry_record(self) -> _RegistryRecord:
        """Build the record for this already identity-proven daemon."""
        identity = self._daemon_identity
        holder = self._registry_holder
        if (
            identity is None
            or holder is None
            or self.socket_file is None
            or self.configfile is None
        ):
            raise ServerStartError("cannot publish registry before identity proof")
        assert self.dbdir is not None
        return _RegistryRecord(
            database=self._database_identity,
            identity=identity,
            pidfile=os.path.abspath(self.pidfile),
            unixsocket=os.path.abspath(self.socket_file),
            socket_dir=(
                os.path.abspath(self._socket_dir) if self._socket_dir else None
            ),
            socket_owned=self._socket_owned,
            dbdir=os.path.abspath(self.dbdir),
            dbfilename=self.dbfilename,
            configfile=os.path.abspath(self.configfile),
            logfile=os.path.abspath(self.logfile),
            holders=(holder,),
        )

    def _write_setting_registry(self, record: _RegistryRecord) -> None:
        """Atomically publish one private, non-secret registry revision."""
        assert self.settingregistryfile is not None  # caller guards
        directory = os.path.dirname(self.settingregistryfile) or os.getcwd()
        fd, temporary = tempfile.mkstemp(
            prefix=".{0}.".format(os.path.basename(self.settingregistryfile)),
            dir=directory,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fd = -1
                json.dump(record.as_dict(), fh, sort_keys=True, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.settingregistryfile)
            temporary = ""
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                # The registry file itself is already fsynced and atomically
                # replaced. Some POSIX filesystems do not fsync directories.
                logger.debug("could not fsync registry directory: %s", exc)
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary:
                _safe_remove(temporary)
        self._registry_record = record

    def _save_setting_registry(self) -> None:
        """Publish the initial identity-proven shared registry."""
        record = self._current_registry_record()
        if not self._registry_record_is_live(record):
            raise ServerStartError(
                "refusing to publish registry: daemon identity changed"
            )
        self._write_setting_registry(record)

    def _holder_is_live(self, holder: _RegistryHolder) -> bool:
        """Return whether a managed holder's anti-PID-reuse identity is live."""
        try:
            process = psutil.Process(holder.pid)
            return (
                process.create_time() == holder.create_time
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except psutil.NoSuchProcess:
            return False
        except (OSError, psutil.AccessDenied):
            # Unknown ownership must not be treated as abandoned.
            return True

    def _register_registry_holder(self) -> None:
        """Add this client as a managed holder while the registry lock is held."""
        record = self._registry_record
        holder = self._registry_holder
        if (
            record is None
            or holder is None
            or not self._registry_record_is_live(record)
        ):
            raise ServerStartError("shared registry changed during attachment")
        holders = tuple(
            existing
            for existing in record.holders
            if existing.token != holder.token and self._holder_is_live(existing)
        ) + (holder,)
        self._write_setting_registry(replace(record, holders=holders))

    def _registry_paths_match(self, record: _RegistryRecord) -> bool:
        """Validate database identity and package-managed path relationships."""
        dbdir = os.path.realpath(record.dbdir)
        database = os.path.realpath(os.path.join(dbdir, record.dbfilename))
        if (
            record.dbfilename != os.path.basename(record.dbfilename)
            or database != self._database_identity
            or os.path.realpath(record.database) != self._database_identity
        ):
            return False
        expected_paths = (
            (record.pidfile, os.path.join(dbdir, "valkey.pid")),
            (record.configfile, os.path.join(dbdir, "valkey.conf")),
            (record.logfile, os.path.join(dbdir, "valkey.log")),
        )
        if not all(
            os.path.isabs(path)
            for path in (
                record.database,
                record.dbdir,
                record.pidfile,
                record.configfile,
                record.logfile,
            )
        ) or any(
            os.path.realpath(actual) != os.path.realpath(expected)
            for actual, expected in expected_paths
        ):
            return False
        if not os.path.isabs(record.unixsocket):
            return False
        if record.socket_dir is None:
            if record.socket_owned and os.path.realpath(
                record.unixsocket
            ) != os.path.realpath(os.path.join(dbdir, "valkey.socket")):
                return False
            return True
        if not record.socket_owned or not os.path.isabs(record.socket_dir):
            return False
        socket_dir = os.path.realpath(record.socket_dir)
        if (
            os.path.basename(socket_dir).startswith("vkey-sock-")
            and os.path.realpath(os.path.dirname(socket_dir))
            == os.path.realpath("/tmp")
            and os.path.realpath(record.unixsocket)
            == os.path.realpath(os.path.join(socket_dir, "valkey.socket"))
        ):
            return True
        return False

    def _registry_record_is_live(self, record: _RegistryRecord) -> bool:
        """Require process creation identity plus matching endpoint run identity."""
        if not self._registry_paths_match(record):
            return False
        if self._read_pidfile_value(record.pidfile) != record.identity.pid:
            return False
        if self._matching_managed_process(record.identity) is None:
            return False
        try:
            socket_stat = os.stat(record.unixsocket)
            if not stat.S_ISSOCK(socket_stat.st_mode):
                return False
            info = self._probe_server_info(record.unixsocket)
            info_pid, run_id, config_file = _server_identity_fields(info)
            endpoint_matches = (
                info_pid == record.identity.pid
                and run_id == record.identity.run_id
                and os.path.realpath(config_file) == os.path.realpath(record.configfile)
            )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            valkey.exceptions.ValkeyError,
            _RegistryNotReady,
        ):
            return False
        return endpoint_matches

    def _reject_unverified_live_registry_endpoint(
        self, record: _RegistryRecord
    ) -> None:
        """Refuse recovery if an invalid record still names a live Valkey."""
        try:
            self._probe_server_info(record.unixsocket)
        except valkey.exceptions.AuthenticationError as exc:
            raise ServerStartError(
                "invalid registry names a live authenticated Valkey endpoint; "
                "refusing unsafe recovery"
            ) from exc
        except (OSError, valkey.exceptions.ValkeyError, _RegistryNotReady):
            return
        raise ServerStartError(
            "invalid registry names an unverified live Valkey endpoint; "
            "refusing unsafe recovery"
        )

    def _adopt_registry_record(self, record: _RegistryRecord) -> None:
        """Adopt paths only after every record identity check has passed."""
        self.pidfile = record.pidfile
        self.socket_file = record.unixsocket
        self._socket_dir = record.socket_dir
        self._socket_owned = record.socket_owned
        self.dbdir = record.dbdir
        self.dbfilename = record.dbfilename
        self.configfile = record.configfile
        self.logfile = record.logfile
        self._daemon_identity = record.identity
        self._registry_record = record

    def _load_setting_registry(self) -> bool:
        """Validate and adopt one live versioned shared-server registry.

        Returns:
            True only after process creation time, bundled executable, pidfile,
            database/config paths, PING, PID, run ID, and local-only endpoint all
            agree. Invalid or stale records are removed and return False.
        """
        assert self.settingregistryfile is not None  # caller guards
        try:
            record = _read_private_registry(self.settingregistryfile)
        except (OSError, TypeError, ValueError):
            _safe_remove(self.settingregistryfile)
            return False

        if not self._registry_record_is_live(record):
            # Probe before deleting: a live endpoint means ownership is
            # ambiguous, so preserve the record and fail without mutation.
            self._reject_unverified_live_registry_endpoint(record)
            _safe_remove(self.settingregistryfile)
            return False
        self._adopt_registry_record(record)
        return True

    def _read_live_registry_record(self) -> Optional[_RegistryRecord]:
        """Read the current record without deleting or adopting invalid state."""
        assert self.settingregistryfile is not None
        try:
            record = _read_private_registry(self.settingregistryfile)
        except (OSError, TypeError, ValueError):
            return None
        if (
            record.identity != self._daemon_identity
            or not self._registry_record_is_live(record)
        ):
            return None
        return record

    def _release_registry_holder(self) -> bool:
        """Release this holder and return whether it owns final cleanup."""
        holder = self._registry_holder
        if (
            holder is None
            or holder.pid != os.getpid()
            or not self._holder_is_live(holder)
        ):
            logger.warning(
                "preserving shared daemon because this process does not own its holder"
            )
            return False
        record = self._read_live_registry_record()
        if record is None or holder.token not in {
            existing.token for existing in record.holders
        }:
            logger.warning(
                "preserving shared daemon because managed holder state is unverified"
            )
            return False
        remaining = tuple(
            existing
            for existing in record.holders
            if existing.token != holder.token and self._holder_is_live(existing)
        )
        if remaining:
            self._write_setting_registry(replace(record, holders=remaining))
            return False
        return True

    # -- shutdown --------------------------------------------------------

    @property
    def pid(self) -> int:
        """Creation-time-verified daemon PID, or zero when unavailable."""
        identity = getattr(self, "_daemon_identity", None)
        if identity is not None:
            return (
                identity.pid
                if self._matching_managed_process(identity) is not None
                else 0
            )
        # Compatibility for partially initialized test doubles; never used to
        # authorize signalling because _terminate requires a managed identity.
        pid = ValkeyMixin._read_pidfile_value(self)
        return pid if pid and psutil.pid_exists(pid) else 0

    def _endpoint_matches_managed_identity(self) -> bool:
        """Verify the current client endpoint before sending SHUTDOWN."""
        identity = self._daemon_identity
        if identity is None or self._matching_managed_process(identity) is None:
            return False
        try:
            info = cast(Dict[str, Any], self.info("server"))
            info_pid, run_id, _config_file = _server_identity_fields(info)
            return info_pid == identity.pid and run_id == identity.run_id
        except (KeyError, TypeError, ValueError, valkey.exceptions.ValkeyError):
            return False

    def _cleanup(self) -> None:
        """Release this client and safely elect shared final cleanup."""
        with self._lifecycle_lock:
            if not getattr(self, "running", False):
                _ValkeyClient.close(self)  # type: ignore[arg-type,no-untyped-call]
                return

            coordination = (
                self._locked_setting_registry()
                if self.settingregistryfile
                else nullcontext()
            )
            with coordination:
                lifecycle_complete = False
                try:
                    last_client = (
                        self._release_registry_holder()
                        if self.settingregistryfile
                        else True
                    )
                    if last_client:
                        # Capture verified identity BEFORE SHUTDOWN removes pidfile.
                        pid = self.pid
                        endpoint_matches = self._endpoint_matches_managed_identity()
                        if endpoint_matches:
                            try:
                                if self.settingregistryfile:
                                    self.shutdown(save=True)
                                else:
                                    self.shutdown(nosave=True)
                            except Exception as exc:  # noqa: BLE001 - socket closes
                                logger.debug("managed shutdown failed: %s", exc)
                        preserve_socket = bool(
                            not endpoint_matches
                            and self.socket_file
                            and self._socket_accepts_connections(self.socket_file)
                        )
                        self._terminate(pid)
                        self._remove_files(preserve_socket=preserve_socket)
                    lifecycle_complete = True
                finally:
                    # Keep holder release, connection close, and final election
                    # under the cross-process lock.
                    if lifecycle_complete:
                        self.running = False
                        try:
                            atexit.unregister(self._cleanup)
                        except Exception as exc:  # noqa: BLE001 - best effort
                            logger.debug("could not unregister cleanup: %s", exc)
                    _ValkeyClient.close(self)  # type: ignore[arg-type,no-untyped-call]

    def close(self) -> None:
        """Release this client and its embedded-server lifecycle.

        Shared servers remain available until their last client closes.
        Repeated calls are harmless.
        """
        self._cleanup()

    def _terminate(self, pid: int, grace_period: float = 10) -> None:
        """Stop only the creation-time-verified bundled daemon identity."""
        identity = self._daemon_identity
        if not pid or identity is None or pid != identity.pid:
            return
        deadline = time.monotonic() + max(0.0, grace_period)
        while time.monotonic() < deadline:
            if self._matching_managed_process(identity) is None:
                return
            time.sleep(max(0.0, min(0.2, deadline - time.monotonic())))

        process = self._matching_managed_process(identity)
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass
        process = self._matching_managed_process(identity)
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                pass

    def _remove_files(self, preserve_socket: bool = False) -> None:
        """Remove managed runtime files while preserving persistent data."""
        if self.settingregistryfile:
            paths = [self.settingregistryfile, self.pidfile, self.configfile]
            if not preserve_socket and self._socket_owned:
                paths.append(self.socket_file)
            for path in paths:
                _safe_remove(path)
        elif self.dbdir:
            # Isolated: we own the whole temp tree, unless its socket identity
            # was replaced and must be preserved.
            if preserve_socket:
                for path in (self.pidfile, self.configfile):
                    _safe_remove(path)
            else:
                shutil.rmtree(self.dbdir, ignore_errors=True)
        if self._socket_dir and not preserve_socket:
            _safe_rmdir(self._socket_dir)
            self._socket_dir = None

    def __enter__(self) -> "ValkeyMixin":
        """Enter a ``with`` block; the server is already running."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """SQLite-style exit: persist (shutdown saves) and release the server.

        This releases the embedded server itself, not merely the connection
        pool.
        """
        self.close()

    def __del__(self) -> None:
        """Best-effort cleanup if the instance is garbage-collected."""
        try:
            self.close()
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


class Valkey(ValkeyMixin, _ValkeyClient):
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
