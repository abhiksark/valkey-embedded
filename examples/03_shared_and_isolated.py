# examples/03_shared_and_isolated.py
"""Shared vs. isolated servers.

Clients built with the SAME db path attach to ONE shared server; clients built
with no path each get a private server with its own keyspace.

Run:  python examples/03_shared_and_isolated.py
"""

import os
import shutil
import tempfile

from valkey_embedded import Valkey


def main() -> None:
    workdir = tempfile.mkdtemp(prefix="valkey_embedded-example-")
    dbfile = os.path.join(workdir, "shared.rdb")
    a = b = None
    try:
        # Same path -> b attaches to a's already-running server.
        a = Valkey(dbfile)
        b = Valkey(dbfile)
        a.set("x", "from-a")
        print("shared:   same server pid?     ->", a.pid == b.pid)
        print("shared:   b sees a's write     ->", b.get("x"))

        # No path -> each client gets a private, independent server.
        first = Valkey()
        second = Valkey()
        first.set("only", "first")
        print("isolated: different pids?      ->", first.pid != second.pid)
        print("isolated: second sees nothing  ->", second.get("only"))
    finally:
        # Stop the shared clients so the named temp dir can be removed now.
        # Private servers (first/second) are cleaned up automatically at exit.
        for conn in (b, a):
            if conn is not None:
                conn._cleanup()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
