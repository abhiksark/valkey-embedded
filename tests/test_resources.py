# tests/test_resources.py
"""Repeated create/destroy cycles must not leak temp dirs, file descriptors, or
orphaned valkey-server processes.
"""

import glob
import os
import tempfile

import psutil

from valkey_embedded import Valkey

_TEMPDIR_GLOB = os.path.join(tempfile.gettempdir(), "valkey_embedded-*")


def test_no_tempdir_leak_over_cycles():
    before = set(glob.glob(_TEMPDIR_GLOB))
    for _ in range(5):
        conn = Valkey()
        conn.ping()
        conn._cleanup()
    leaked = set(glob.glob(_TEMPDIR_GLOB)) - before
    assert leaked == set(), "leaked temp dirs: {0}".format(leaked)


def test_no_orphan_processes_over_cycles():
    pids = []
    for _ in range(5):
        conn = Valkey()
        pids.append(conn.pid)
        conn.ping()
        conn._cleanup()
    survivors = [pid for pid in pids if psutil.pid_exists(pid)]
    assert survivors == [], "orphaned valkey-server pids: {0}".format(survivors)


def test_no_fd_leak_over_cycles():
    proc = psutil.Process()
    # Warm up so first-use allocations don't count as a leak.
    for _ in range(2):
        conn = Valkey()
        conn.ping()
        conn._cleanup()
    baseline = proc.num_fds()
    for _ in range(10):
        conn = Valkey()
        conn.ping()
        conn._cleanup()
    grew = proc.num_fds() - baseline
    # A correct teardown returns to baseline; allow small slack for caching.
    assert grew <= 5, "fd count grew by {0} over 10 cycles".format(grew)
