# examples/02_persistence.py
"""Persistence across runs: a named RDB file outlives the server process.

Each child process below is a separate "run" of your program. The second run
starts a brand-new server on the same file and still sees the first run's data.

Run:  python examples/02_persistence.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

_WRITER = (
    "from valkey_embedded import Valkey;"
    "c = Valkey({path!r});"
    "c.set('counter', '42');"
    "c.save();"  # force an RDB snapshot before this run ends
    "print('run 1: wrote counter=42')"
)
_READER = (
    "from valkey_embedded import Valkey;"
    "c = Valkey({path!r});"
    "print('run 2: counter is', c.get('counter').decode())"
)


def main() -> None:
    workdir = tempfile.mkdtemp(prefix="valkey_embedded-example-")
    dbfile = os.path.join(workdir, "data.rdb")
    try:
        subprocess.check_call([sys.executable, "-c", _WRITER.format(path=dbfile)])
        subprocess.check_call([sys.executable, "-c", _READER.format(path=dbfile)])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
