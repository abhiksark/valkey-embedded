"""Reusable ``ValkeyServer`` state-transition and per-run identity contract."""

import atexit
import os
import shutil
import threading

import psutil
import pytest

from valkey_embedded import ServerStartError, ValkeyServer
from valkey_embedded.server import _find_free_port, _ServerState


def _kill_owned_pid(pid):
    if pid <= 0:
        return
    try:
        process = psutil.Process(pid)
        process.kill()
        process.wait(timeout=5)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass


def _runtime_paths(server):
    paths = {
        server.data_dir,
        server.configfile,
        server.logfile,
        server.pidfile,
        server.socket_file,
        server._startup_output_file,
    }
    if server._socket_dir is not None:
        paths.add(server._socket_dir)
    return paths


def test_stop_before_start_then_start_uses_fresh_owned_paths():
    server = ValkeyServer()
    constructed_paths = _runtime_paths(server)
    assert server._state is _ServerState.NEW

    server.stop()
    assert server._state is _ServerState.STOPPED
    assert server.port is None
    assert all(not os.path.exists(path) for path in constructed_paths)

    try:
        server.start()
        assert server._state is _ServerState.RUNNING
        assert server.is_running()
        assert _runtime_paths(server).isdisjoint(constructed_paths)
    finally:
        server.stop(timeout=0)
    assert server._state is _ServerState.STOPPED


def test_terminate_before_start_then_start_uses_fresh_owned_paths():
    server = ValkeyServer()
    constructed_paths = _runtime_paths(server)

    server.terminate()
    server.terminate()
    assert server._state is _ServerState.STOPPED
    assert all(not os.path.exists(path) for path in constructed_paths)

    try:
        server.start()
        assert server.is_running()
        assert _runtime_paths(server).isdisjoint(constructed_paths)
    finally:
        server.stop(timeout=0)


def test_graceful_restart_allocates_new_owned_temp_identity():
    server = ValkeyServer()
    try:
        server.start()
        first_paths = _runtime_paths(server)
        first_pid = server.pid
        first_port = server.port
        server.start()
        assert server.pid == first_pid
        assert server.port == first_port

        server.stop()
        assert server._state is _ServerState.STOPPED
        assert server.port is None
        assert server.pid == 0
        assert not server._atexit_registered
        assert all(not os.path.exists(path) for path in first_paths)

        server.start()
        second_paths = _runtime_paths(server)
        assert second_paths.isdisjoint(first_paths)
        assert server.pid != first_pid
        assert server.client().ping() is True
    finally:
        server.stop(timeout=0)


def test_restart_after_terminate_uses_new_owned_temp_identity():
    server = ValkeyServer()
    first_pid = 0
    try:
        server.start()
        first_pid = server.pid
        first_paths = _runtime_paths(server)

        server.terminate()
        assert server._state is _ServerState.STOPPED
        assert not psutil.pid_exists(first_pid)
        assert all(not os.path.exists(path) for path in first_paths)

        server.start()
        assert server.is_running()
        assert server.pid != first_pid
        assert _runtime_paths(server).isdisjoint(first_paths)
    finally:
        _kill_owned_pid(first_pid)
        server.stop(timeout=0)


def test_persistent_owned_directory_and_data_survive_restart():
    server = ValkeyServer(persist=True)
    workdir = server.data_dir
    try:
        server.start()
        server.client().set("persistent-key", "persistent-value")
        first_paths = _runtime_paths(server)
        server.stop()

        assert os.path.isdir(workdir)
        server.start()
        assert server.data_dir == workdir
        assert _runtime_paths(server) == first_paths
        assert server.client().get("persistent-key") == b"persistent-value"
    finally:
        server.stop(timeout=0)
        shutil.rmtree(workdir, ignore_errors=True)


def test_user_directory_survives_idempotent_transitions(tmp_path):
    data_dir = tmp_path / "caller-data"
    data_dir.mkdir()
    sentinel = data_dir / "caller-owned.txt"
    sentinel.write_text("keep")
    server = ValkeyServer(data_dir=str(data_dir))

    server.stop()
    server.stop()
    server.terminate()
    assert sentinel.read_text() == "keep"

    try:
        server.start()
        server.client().set("discarded-key", "value")
        server.stop()
        assert sentinel.read_text() == "keep"

        server.start()
        assert server.data_dir == str(data_dir)
        assert server.client().get("discarded-key") is None
    finally:
        server.stop(timeout=0)
    assert sentinel.read_text() == "keep"


def test_deep_user_directory_reallocates_fallback_socket_on_restart(tmp_path):
    deep_dir = tmp_path / ("deep-" + ("x" * 90))
    server = ValkeyServer(data_dir=str(deep_dir))
    first_socket_dir = server._socket_dir
    assert first_socket_dir is not None
    try:
        server.start()
        server.stop()
        assert not os.path.exists(first_socket_dir)

        server.start()
        assert server._socket_dir is not None
        assert server._socket_dir != first_socket_dir
        assert server.client().ping() is True
    finally:
        server.stop(timeout=0)
    assert deep_dir.is_dir()


def test_requested_port_is_reused_and_auto_metadata_is_current():
    free_port = _find_free_port()
    fixed = ValkeyServer(port=free_port)
    try:
        fixed.start()
        assert fixed.port == free_port
        fixed.stop()
        assert fixed.port is None
        fixed.start()
        assert fixed.port == free_port
    finally:
        fixed.stop(timeout=0)

    automatic = ValkeyServer()
    try:
        automatic.start()
        first_url = automatic.connection_url
        automatic.stop()
        assert automatic.port is None
        assert automatic.connection_kwargs["port"] is None
        assert automatic.connection_url != first_url

        automatic.start()
        assert automatic.connection_kwargs == {
            "host": automatic.host,
            "port": automatic.port,
        }
        assert automatic.connection_url.endswith(":{0}".format(automatic.port))
    finally:
        automatic.stop(timeout=0)


def test_requested_port_restart_rejects_new_owner_without_adopting_it():
    port = _find_free_port()
    restarting = ValkeyServer(port=port)
    owner = ValkeyServer(port=port)
    owner_pid = 0
    try:
        restarting.start()
        restarting.stop()
        owner.start()
        owner_pid = owner.pid

        with pytest.raises(ServerStartError, match="Address already in use"):
            restarting.start(timeout=2)

        assert restarting._state is _ServerState.STOPPED
        assert restarting.port is None
        assert owner.is_running()
        assert owner.pid == owner_pid
    finally:
        restarting.stop(timeout=0)
        owner.stop(timeout=0)
        _kill_owned_pid(owner_pid)


def test_failed_start_retry_allocates_fresh_owned_paths(monkeypatch):
    server = ValkeyServer()
    failed_paths = _runtime_paths(server)
    original_launch = server._launch_process
    calls = []

    def fail_once():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("simulated launch failure")
        original_launch()

    monkeypatch.setattr(server, "_launch_process", fail_once)
    try:
        with pytest.raises(ServerStartError, match="simulated launch failure"):
            server.start()
        assert server._state is _ServerState.STOPPED
        assert all(not os.path.exists(path) for path in failed_paths)

        server.start()
        assert server._state is _ServerState.RUNNING
        assert _runtime_paths(server).isdisjoint(failed_paths)
    finally:
        server.stop(timeout=0)


def test_context_manager_instance_is_reusable():
    server = ValkeyServer()
    with server:
        first_paths = _runtime_paths(server)
        assert server.is_running()
    assert server._state is _ServerState.STOPPED

    with server:
        assert server.is_running()
        assert _runtime_paths(server).isdisjoint(first_paths)
    assert server._state is _ServerState.STOPPED


def test_external_process_death_is_cleaned_before_restart():
    server = ValkeyServer()
    old_pid = 0
    try:
        server.start()
        old_pid = server.pid
        old_paths = _runtime_paths(server)
        process = psutil.Process(old_pid)
        process.kill()
        process.wait(timeout=5)
        assert not server.is_running()

        server.start()
        assert server.is_running()
        assert server.pid != old_pid
        assert _runtime_paths(server).isdisjoint(old_paths)
    finally:
        _kill_owned_pid(old_pid)
        server.stop(timeout=0)


@pytest.mark.parametrize(
    ("operation", "state"),
    [
        ("start", _ServerState.STARTING),
        ("start", _ServerState.STOPPING),
        ("stop", _ServerState.STARTING),
        ("stop", _ServerState.STOPPING),
        ("terminate", _ServerState.STARTING),
        ("terminate", _ServerState.STOPPING),
    ],
)
def test_reentrant_transitional_operation_is_rejected(operation, state):
    server = ValkeyServer()
    server._state = state
    try:
        with pytest.raises(RuntimeError, match=state.name.lower()):
            getattr(server, operation)()
    finally:
        server._state = _ServerState.NEW
        server.stop()


def test_concurrent_starts_serialize_to_one_daemon(monkeypatch):
    server = ValkeyServer()
    entered_wait = threading.Event()
    release_wait = threading.Event()
    second_attempted = threading.Event()
    errors = []
    launches = []
    original_launch = server._launch_process
    original_wait = server._wait_until_ready

    def tracked_launch():
        original_launch()
        assert server._process is not None
        launches.append(server._process.pid)

    def blocked_wait(timeout):
        entered_wait.set()
        if not release_wait.wait(timeout=5):
            raise RuntimeError("test did not release readiness")
        original_wait(timeout)

    def first_start():
        try:
            server.start()
        except BaseException as exc:
            errors.append(exc)

    def second_start():
        second_attempted.set()
        try:
            server.start()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(server, "_launch_process", tracked_launch)
    monkeypatch.setattr(server, "_wait_until_ready", blocked_wait)
    first = threading.Thread(target=first_start)
    second = threading.Thread(target=second_start)
    try:
        first.start()
        assert entered_wait.wait(timeout=5)
        second.start()
        assert second_attempted.wait(timeout=5)
        release_wait.set()
        first.join(timeout=10)
        second.join(timeout=10)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert len(launches) == 1
        assert server.pid == launches[0]
    finally:
        release_wait.set()
        first.join(timeout=5)
        second.join(timeout=5)
        for pid in launches:
            if pid != server.pid:
                _kill_owned_pid(pid)
        server.stop(timeout=0)


def test_concurrent_stop_waits_for_start_then_cleans_the_run(monkeypatch):
    server = ValkeyServer()
    entered_wait = threading.Event()
    release_wait = threading.Event()
    stop_attempted = threading.Event()
    errors = []
    launched_pids = []
    original_launch = server._launch_process
    original_wait = server._wait_until_ready

    def tracked_launch():
        original_launch()
        assert server._process is not None
        launched_pids.append(server._process.pid)

    def blocked_wait(timeout):
        entered_wait.set()
        if not release_wait.wait(timeout=5):
            raise RuntimeError("test did not release readiness")
        original_wait(timeout)

    def start_server():
        try:
            server.start()
        except BaseException as exc:
            errors.append(exc)

    def stop_server():
        stop_attempted.set()
        try:
            server.stop(timeout=0)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(server, "_launch_process", tracked_launch)
    monkeypatch.setattr(server, "_wait_until_ready", blocked_wait)
    starter = threading.Thread(target=start_server)
    stopper = threading.Thread(target=stop_server)
    try:
        starter.start()
        assert entered_wait.wait(timeout=5)
        stopper.start()
        assert stop_attempted.wait(timeout=5)
        release_wait.set()
        starter.join(timeout=10)
        stopper.join(timeout=10)

        assert not starter.is_alive()
        assert not stopper.is_alive()
        assert errors == []
        assert len(launched_pids) == 1
        assert not psutil.pid_exists(launched_pids[0])
        assert server._state is _ServerState.STOPPED
        assert server.port is None
        assert not os.path.exists(server.data_dir)
    finally:
        release_wait.set()
        starter.join(timeout=5)
        stopper.join(timeout=5)
        for pid in launched_pids:
            _kill_owned_pid(pid)
        server.stop(timeout=0)


def test_atexit_registration_tracks_running_cycles_once(monkeypatch):
    server = ValkeyServer()
    registered = []
    unregistered = []
    real_register = atexit.register
    real_unregister = atexit.unregister

    def track_register(callback):
        registered.append(callback)
        return real_register(callback)

    def track_unregister(callback):
        unregistered.append(callback)
        return real_unregister(callback)

    monkeypatch.setattr(atexit, "register", track_register)
    monkeypatch.setattr(atexit, "unregister", track_unregister)
    try:
        server.start()
        server.start()
        assert len(registered) == 1
        assert server._atexit_registered
        server.stop()
        server.stop()
        assert len(unregistered) == 1
        assert not server._atexit_registered

        server.start()
        assert len(registered) == 2
        assert server._atexit_registered
    finally:
        server.stop(timeout=0)
    assert len(unregistered) == 2


def test_fifty_owned_lifecycle_cycles_leave_no_paths_or_fds():
    server = ValkeyServer()
    process = psutil.Process()
    before_fds = process.num_fds()
    seen_dirs = set()
    try:
        for cycle in range(50):
            server.start()
            current_dir = server.data_dir
            assert current_dir not in seen_dirs
            seen_dirs.add(current_dir)
            assert server.client().ping() is True
            if cycle % 2:
                server.stop(timeout=0)
            else:
                server.terminate()
            assert server._state is _ServerState.STOPPED
            assert server.port is None
            assert not os.path.exists(current_dir)
            assert not server._atexit_registered

        assert process.num_fds() <= before_fds + 1
    finally:
        server.stop(timeout=0)
