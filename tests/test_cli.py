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
