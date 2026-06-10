# examples/09_server_api.py
"""Explicit server lifecycle with a TCP endpoint via ValkeyServer.

Unlike Valkey() (auto-managed, unix-socket-only), ValkeyServer listens on
host/port, so any Redis-compatible client can connect. You control start/stop.

Run:  python examples/09_server_api.py
"""

import valkey

from valkey_embedded import ValkeyServer


def main() -> None:
    # Port auto-assigned (or pass ValkeyServer(port=6380)). Context manager
    # starts on enter and stops + cleans up on exit.
    with ValkeyServer() as server:
        print("listening on", server.connection_url, "(pid", str(server.pid) + ")")

        # 1) Built-in client helper.
        client = server.client()
        client.set("via", "server.client()")
        print("server.client() get:", client.get("via").decode())
        client.close()

        # 2) Bring your own client using the connection kwargs.
        byo = valkey.Valkey(**server.connection_kwargs)
        print("BYO client ping:", byo.ping())
        byo.close()

    print("server stopped:", not server.is_running())


if __name__ == "__main__":
    main()
