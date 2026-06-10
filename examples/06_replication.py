# examples/06_replication.py
"""Replication: a replica syncs from a master via `replicaof`.

Both servers open a loopback TCP port (the default `port 0` is unix-socket only,
but REPLICAOF needs a host:port). Sync state is polled, never slept on.

Run:  python examples/06_replication.py
"""

import socket
import time

from valkey.exceptions import ConnectionError as ValkeyConnectionError

from valkey_embedded import Valkey


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _poll(predicate, timeout=20.0, interval=0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (ValkeyConnectionError, KeyError):
            pass
        time.sleep(interval)
    return False


def main() -> None:
    master_port = _free_port()
    master = Valkey(serverconfig={"port": str(master_port), "bind": "127.0.0.1"})
    replica = Valkey(
        serverconfig={
            "port": str(_free_port()),
            "bind": "127.0.0.1",
            "replicaof": "127.0.0.1 {0}".format(master_port),
        }
    )

    linked = _poll(lambda: replica.info("replication")["master_link_status"] == "up")
    print("replica linked to master  ->", linked)

    master.set("song", "valkey lullaby")
    replicated = _poll(lambda: replica.get("song") == b"valkey lullaby")
    print("key replicated to replica ->", replicated)
    # Both servers stop automatically at process exit.


if __name__ == "__main__":
    main()
