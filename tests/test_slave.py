# tests/test_slave.py
"""Replication parity: a replica started with replicaof syncs from a master.

Both servers enable a loopback TCP port (the default port 0 is unix-socket only,
but REPLICAOF needs host/port). Valkey 8.x defaults to diskless replication with
a delay, so we POLL for link/key state rather than sleeping a fixed interval.
"""

import socket
import time

from valkey.exceptions import ConnectionError as ValkeyConnectionError

from valkey_embedded import Valkey


def _free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _poll(predicate, timeout=20.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (ValkeyConnectionError, KeyError):
            pass
        time.sleep(interval)
    return False


def test_replica_syncs_keys_from_master():
    master_port = _free_port()
    master = Valkey(serverconfig={"port": str(master_port), "bind": "127.0.0.1"})
    replica = Valkey(
        serverconfig={
            "port": str(_free_port()),
            "bind": "127.0.0.1",
            "replicaof": "127.0.0.1 {0}".format(master_port),
        }
    )
    try:
        assert _poll(
            lambda: replica.info("replication")["master_link_status"] == "up"
        ), "replica never reported master_link_status: up"

        master.set("replicated", "yes")
        assert _poll(lambda: replica.get("replicated") == b"yes"), (
            "key did not replicate to the replica"
        )
    finally:
        replica._cleanup()
        master._cleanup()
