# examples/01_basic.py
"""Basic usage: a private embedded server that manages its own lifecycle.

Run:  python examples/01_basic.py
"""

import valkey_embedded
from valkey_embedded import Valkey


def main() -> None:
    conn = Valkey()  # starts a private valkey-server listening on a unix socket
    conn.set("greeting", "hello from valkey_embedded")
    print("ping             ->", conn.ping())
    print("get greeting     ->", conn.get("greeting"))
    print("server pid       ->", conn.pid)
    print("embedded version ->", valkey_embedded.__valkey_server_version__)
    # No teardown needed: the server is shut down and its temporary directory is
    # removed automatically when this process exits.


if __name__ == "__main__":
    main()
