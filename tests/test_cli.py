# tests/test_cli.py
"""The foreground server CLI: `python -m valkey_embedded` / `valkey-embedded`."""

import os
import signal
import socket
import subprocess
import sys
import time

import pytest
import valkey

from valkey_embedded.__main__ import _build_parser, main
from valkey_embedded.server import _find_free_port

_ENV = dict(os.environ, PYTHONPATH=os.path.join(os.getcwd(), "src"))


def test_version_flag_exits_zero(capsys):
    # argparse --version prints and raises SystemExit(0).
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "valkey-embedded" in capsys.readouterr().out


def test_help_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "--port" in capsys.readouterr().out


def test_parser_defaults_and_overrides():
    args = _build_parser().parse_args([])
    assert args.port is None and args.host == "127.0.0.1" and args.persist is False
    args = _build_parser().parse_args(
        ["--port", "6390", "--host", "0.0.0.0", "--persist"]
    )
    assert args.port == 6390 and args.host == "0.0.0.0" and args.persist is True


def test_main_runs_until_signal_and_stops(monkeypatch, capsys, tmp_path):
    handlers = {}
    instances = []

    class FakeServer:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.connection_url = "valkey://127.0.0.1:6381"
            self.pid = 4242
            self.start_calls = 0
            self.stop_calls = 0
            self.running_checks = 0
            instances.append(self)

        def start(self):
            self.start_calls += 1

        def is_running(self):
            self.running_checks += 1
            return True

        def stop(self):
            self.stop_calls += 1

    def capture_handler(signum, handler):
        handlers[signum] = handler

    def request_stop(_seconds):
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr("valkey_embedded.__main__.ValkeyServer", FakeServer)
    monkeypatch.setattr(signal, "signal", capture_handler)
    monkeypatch.setattr(time, "sleep", request_stop)

    data_dir = str(tmp_path / "data")
    result = main(
        [
            "--port",
            "6381",
            "--host",
            "0.0.0.0",
            "--data-dir",
            data_dir,
            "--persist",
        ]
    )

    assert result == 0
    assert len(instances) == 1
    server = instances[0]
    assert server.kwargs == {
        "port": 6381,
        "host": "0.0.0.0",
        "data_dir": data_dir,
        "persist": True,
    }
    assert server.start_calls == 1
    assert server.running_checks == 1
    assert server.stop_calls == 1
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    output = capsys.readouterr().out
    assert "valkey-embedded listening on valkey://127.0.0.1:6381 (pid 4242)" in output
    assert "press Ctrl+C to stop" in output
    assert "valkey-embedded stopped" in output


def test_main_stops_server_when_polling_fails(monkeypatch):
    instances = []

    class FailingServer:
        connection_url = "valkey://127.0.0.1:6382"
        pid = 4343

        def __init__(self, **_kwargs):
            self.stop_calls = 0
            instances.append(self)

        def start(self):
            return None

        def is_running(self):
            raise RuntimeError("poll failed")

        def stop(self):
            self.stop_calls += 1

    monkeypatch.setattr("valkey_embedded.__main__.ValkeyServer", FailingServer)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)

    with pytest.raises(RuntimeError, match="poll failed"):
        main(["--port", "6382"])

    assert len(instances) == 1
    assert instances[0].stop_calls == 1


def _wait_for_line(proc, needle, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            return None
        if needle in line:
            return line
    return None


def test_foreground_run_serves_then_stops_on_sigterm():
    port = _find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "valkey_embedded", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_ENV,
    )
    try:
        assert _wait_for_line(proc, "listening on"), "server never reported readiness"
        client = valkey.Valkey(host="127.0.0.1", port=port, socket_connect_timeout=2)
        try:
            assert client.ping() is True
        finally:
            client.close()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    # The port is free again -> the server actually stopped.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        assert s.connect_ex(("127.0.0.1", port)) != 0
