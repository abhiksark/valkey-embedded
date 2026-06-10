# src/valkey_embedded/__main__.py
"""Run an embedded valkey-server in the foreground.

    python -m valkey_embedded            # auto-assign a free port
    python -m valkey_embedded --port 6380
    valkey-embedded --help               # installed console script

The server runs until interrupted (Ctrl+C / SIGTERM), then shuts down and
cleans up. For environment diagnostics, see `python -m valkey_embedded.debug`.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import List, Optional

import valkey_embedded
from valkey_embedded.server import ValkeyServer


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the foreground-server CLI."""
    parser = argparse.ArgumentParser(
        prog="valkey-embedded",
        description="Run an embedded Valkey server in the foreground.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port to listen on (default: auto-assign a free port)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "working directory, kept after exit "
            "(default: a temp dir, removed unless --persist)"
        ),
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help=(
            "save the dataset (RDB) on exit; without this flag data is "
            "discarded even with --data-dir"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="valkey-embedded {0} (Valkey {1})".format(
            valkey_embedded.__version__,
            valkey_embedded.__valkey_server_version__,
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run a foreground server until SIGINT/SIGTERM; returns the exit code."""
    args = _build_parser().parse_args(argv)

    server = ValkeyServer(
        port=args.port,
        host=args.host,
        data_dir=args.data_dir,
        persist=args.persist,
    )
    server.start()
    print(
        "valkey-embedded listening on {0} (pid {1})".format(
            server.connection_url, server.pid
        ),
        flush=True,
    )
    print("press Ctrl+C to stop", flush=True)

    stopping = {"flag": False}

    def _request_stop(signum: int, frame: object) -> None:
        """Signal handler: flag the loop to exit (cleanup runs in finally)."""
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        while not stopping["flag"] and server.is_running():
            time.sleep(0.25)
    finally:
        server.stop()
        print("valkey-embedded stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
