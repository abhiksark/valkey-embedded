# examples/05_serverconfig.py
"""Per-instance server settings via `serverconfig` (rendered into valkey.conf).

Run:  python examples/05_serverconfig.py
"""

from valkey_embedded import Valkey


def main() -> None:
    conn = Valkey(
        serverconfig={"maxmemory": "64mb", "maxmemory-policy": "allkeys-lru"},
        decode_responses=True,
    )
    print("maxmemory        ->", conn.config_get("maxmemory")["maxmemory"])
    print(
        "maxmemory-policy ->", conn.config_get("maxmemory-policy")["maxmemory-policy"]
    )
    # Server stops automatically at process exit.


if __name__ == "__main__":
    main()
