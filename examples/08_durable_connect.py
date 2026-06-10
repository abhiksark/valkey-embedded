# examples/08_durable_connect.py
"""SQLite-style connect() with crash-safe durability.

`connect(path, durable=True)` opens a file-backed server with the append-only
file (AOF) enabled, so writes survive a crash -- not just a clean shutdown the
way RDB snapshots do. Used as a context manager, leaving the block persists and
releases the server.

Run:  python examples/08_durable_connect.py
"""

import shutil
import tempfile
from pathlib import Path

import valkey_embedded


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="valkey_embedded-example-"))
    db = str(workdir / "data.db")
    try:
        # Open durably, write, and let the `with` block close (and persist) it.
        with valkey_embedded.connect(db, durable=True) as conn:
            conn.set("user:1", "ada")
            conn.bgsave()  # optional explicit snapshot; AOF already has the write
            print("wrote user:1 = ada, then closed the connection")

        # Re-open the same file: the data is still there.
        with valkey_embedded.connect(db, durable=True) as conn:
            value = conn.get("user:1")
            print("reopened; user:1 =", value.decode() if value else None)
            assert value == b"ada"

        print("durable round-trip succeeded")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
