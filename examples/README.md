# valkey-embedded examples

Runnable scripts demonstrating valkey-embedded's API. Each is self-contained and
cleans up after itself. They are executed by `tests/test_examples.py`, so they
stay working as the library evolves.

Run any one directly:

```bash
python examples/01_basic.py
```

Or run them all via the test suite:

```bash
pytest -m examples
```

| Script | Demonstrates |
|---|---|
| `01_basic.py` | A private, auto-managed server: `set`/`get`/`ping`, no teardown needed |
| `02_persistence.py` | A named RDB file surviving across two separate runs (processes) |
| `03_shared_and_isolated.py` | Same path → one shared server; no path → private servers |
| `04_patching.py` | `patch_valkey()` / `unpatch_valkey()` for drop-in use of `valkey.Valkey` |
| `05_serverconfig.py` | Per-instance `valkey.conf` overrides via `serverconfig=` |
| `06_replication.py` | A replica syncing from a master via `replicaof` (state polled) |
| `07_debug_info.py` | Environment diagnostics (same as `python -m valkey_embedded.debug`) |
| `08_durable_connect.py` | SQLite-style `connect(path, durable=True)` with crash-safe (AOF) persistence |
| `09_server_api.py` | Explicit `ValkeyServer` lifecycle with a TCP endpoint + bring-your-own client |

> In normal use you never stop the server explicitly — it shuts down and removes
> its working directory when your process exits. The few `_cleanup()` calls in
> these examples exist only so a script can remove a *named* temp directory
> before returning.
