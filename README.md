# valkey-embedded

[![CI](https://github.com/abhiksark/valkey-embedded/actions/workflows/ci.yml/badge.svg)](https://github.com/abhiksark/valkey-embedded/actions/workflows/ci.yml)
[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD--3--Clause-blue.svg)](LICENSE.txt)
[![Python versions](https://img.shields.io/badge/python-3.9%20%E2%80%93%203.13-blue.svg)](pyproject.toml)

Embedded, auto-managed [Valkey](https://valkey.io/) server for Python. Construct a
client and a private `valkey-server` starts; it shuts down and cleans up when the
last client closes. No separate server install or running daemon required.

```python
from valkey_embedded import Valkey

conn = Valkey()
conn.set("key", "value")
conn.get("key")          # b'value'
# server is stopped and cleaned up when the process exits
```

It is a modern, BSD-3-Clause reimplementation of the
[`redislite`](https://github.com/yahoo/redislite) pattern, targeting Valkey so the
embedded binary can be freely redistributed on PyPI.

## When to use valkey-embedded

valkey-embedded runs the **real Valkey engine** as an **embedded, auto-managed server** on a
**single machine**, persisting via **RDB snapshots** (a cache/buffer, not a durable
system-of-record). Those four properties decide where it fits.

**Strong fit — this is what it's for:**

- **Testing Redis/Valkey code.** Every `Valkey()` is a private, clean server, so tests
  isolate cleanly and run in parallel. Because it's the real engine, expiry, eviction,
  blocking ops, `MULTI`/`EXEC`, and your Lua scripts behave exactly as in production —
  unlike reimplementations that drift.
- **CI without service containers.** No `services: redis`, no docker-compose, no
  testcontainers, no "wait for healthy" — just `pip install` and run.
- **Local development & onboarding.** Clone, run, works. No daemon to install or keep
  alive; the binary is bundled, so it works offline.
- **Demos, tutorials, notebooks.** Ship something that *just runs*, with zero prerequisites.

**Works, with caveats:**

- **Single-box inter-process coordination / queues** (shared mode). Treat it as an
  ephemeral buffer unless you open it with `connect(path, durable=True)`; don't call
  `.shutdown()` from one process while others share the server; set `maxmemory` and TTLs
  to bound memory growth.
- **Local cache for a single-node app or CLI.** Reach for it when you specifically want
  Redis data structures, pub/sub, or TTL semantics — otherwise SQLite is simpler.

**Wrong tool — use something else:**

| You want… | Use instead |
|---|---|
| Production serving, HA, or multi-host | a real Valkey/Redis deployment |
| A durable system-of-record | PostgreSQL |
| A durable single-file *in-process* database | SQLite |
| Cross-machine shared state | a real Valkey/Redis deployment |
| Windows-native (without WSL) | a real Valkey/Redis deployment |

In short: valkey-embedded owns the "I need the real Valkey engine, on one box, with zero infra
— mostly for testing and local dev" niche. It is an embedded *server*, not an in-process
database.

## Install

```bash
pip install valkey-embedded          # prebuilt wheels: Linux x86_64, macOS 14+ arm64
```

> *Publishing in progress:* the first release is not on PyPI yet. Until then,
> install from source — `pip install git+https://github.com/abhiksark/valkey-embedded`
> — which compiles the embedded Valkey (needs `gcc`/`clang` and `make`).

Other POSIX platforms build Valkey from source at install time (needs `gcc`/`clang`
and `make`). Windows is unsupported (WSL works).

## Usage

- **Isolated server:** `Valkey()` — a private server per instance.
- **Persistent / shared:** `Valkey("/path/to/db.rdb")` — persists across runs;
  instances sharing the path attach to one server (last to close shuts it down).

  > Note: the first positional argument is the **RDB file path**, not `host` — the
  > embedded server has no host. Pass server overrides via
  > `serverconfig={"maxmemory": "64mb"}`.

- **SQLite-style open with durability:** `connect()` is sugar over `Valkey()` that
  reads like `sqlite3.connect` — open a file-backed store, opt into crash-safety, and
  release it by leaving a `with` block:

  ```python
  import valkey_embedded

  with valkey_embedded.connect("data.db", durable=True) as db:
      db.set("user:1", "ada")     # AOF-backed: survives a crash, not just a clean exit
  # leaving the block persists and stops the embedded server
  ```

  `durable=True` enables the append-only file with `appendfsync everysec` (≤1s of
  writes lost on a crash); `durable="always"` fsyncs every write. Default
  (`durable=False`) is RDB snapshots only, which can lose writes since the last
  snapshot. `durable=` requires a path — an isolated server's data is discarded on
  exit. Force a snapshot anytime with `db.bgsave()`.

- **Explicit server with a TCP endpoint:** `ValkeyServer` is the other half of
  the API — you control start/stop, and the server listens on host/port so *any*
  Redis-compatible client (or another process) can connect:

  ```python
  from valkey_embedded import ValkeyServer

  with ValkeyServer() as server:          # port auto-assigned; or ValkeyServer(port=6380)
      print(server.connection_url)        # valkey://127.0.0.1:<port>
      client = server.client()            # built-in valkey-py client
      client.set("k", "v")
      # bring your own: valkey.Valkey(**server.connection_kwargs)
  ```

  Explicit form: `server = ValkeyServer(); server.start(); ...; server.stop()`
  (`is_running()`, `terminate()`, `persist=True`, `data_dir=…`, `config={…}` too).

  > `Valkey()` stays unix-socket-only with no TCP listener (private by default);
  > reach for `ValkeyServer` when you need a port.

- **Pytest fixtures:** installing the package registers fixtures automatically —
  no conftest wiring. One server runs per test session; each test gets a client
  with a clean keyspace (`FLUSHALL` on setup):

  ```python
  def test_thing(valkey_client):           # client to the session server, keys wiped per test
      valkey_client.set("k", "v")
      assert valkey_client.get("k") == b"v"

  def test_other(valkey_server, valkey_url):
      assert valkey_server.is_running()    # the session ValkeyServer; valkey_url is its URL

  def test_pristine(valkey_server_factory):
      private = valkey_server_factory(persist=False)   # fresh server just for this test
      assert private.client().ping()
  ```

  Only keys are reset between tests; server-level state (`CONFIG SET`, loaded Lua
  scripts) persists for the session — use `valkey_server_factory` when a test
  needs a pristine process. Under pytest-xdist each worker gets its own server.

- **Command line:** run a server in the foreground:

  ```bash
  valkey-embedded --port 6380        # or: python -m valkey_embedded
  ```

- **Diagnostics:** `python -m valkey_embedded.debug`

- **Errors:** failures to start the embedded server raise
  `valkey_embedded.ServerStartError`; its base class `ValkeyEmbeddedError` catches
  every error this library raises. (Command errors from the server itself are
  raised by `valkey-py` as usual.)

- **Also exported:** `StrictValkey` (alias of `Valkey`, mirroring valkey-py);
  `durable="everysec"` as the explicit spelling of `durable=True`;
  `__valkey_executable__` (path to the bundled `valkey-server`) and
  `__valkey_server_version__` (its version, e.g. `8.1.8`). A bundled
  `valkey-cli` ships next to the server binary for manual inspection:

  ```python
  import os, valkey_embedded
  cli = os.path.join(os.path.dirname(valkey_embedded.__valkey_executable__), "valkey-cli")
  ```

## Examples

Runnable scripts for each feature live in [`examples/`](examples/) (basic usage,
persistence, shared vs. isolated servers, patching, `serverconfig`, replication,
and diagnostics):

```bash
python examples/01_basic.py
```

## Migration from redislite

| redislite | valkey-embedded |
|---|---|
| `redislite.Redis` | `valkey_embedded.Valkey` |
| `redislite.patch.patch_redis` | `valkey_embedded.patch.patch_valkey` |

Valkey is wire-compatible with Redis and `valkey-py` is forked from `redis-py`, so
application logic is unchanged — only imports and class names differ.

## Versioning & support

valkey-embedded follows [Semantic Versioning](https://semver.org/). While on
`0.x` the API may still change between minor versions; any change is recorded in
[CHANGELOG.md](CHANGELOG.md). Supported Python versions are 3.9–3.13 on Linux and
macOS (14+ arm64); Windows is supported only under WSL.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, the test tiers, and the
procedure for pinning a new Valkey release. Participation is governed by our
[Code of Conduct](CODE_OF_CONDUCT.md); security issues go through
[SECURITY.md](SECURITY.md).

## License

valkey-embedded is licensed under [BSD-3-Clause](LICENSE.txt). Built wheels
bundle a compiled `valkey-server` binary, also BSD-3-Clause; its license — and
the notices of the third-party code statically linked into it (Lua,
hdr_histogram, fpconv, linenoise) — ship as `valkey_embedded/bin/VALKEY_COPYING.txt`
alongside the binary. The `valkey` Python client is a runtime dependency, not
redistributed here.

## Trademark

valkey-embedded is an independent project and is not affiliated with, sponsored, or
endorsed by the Valkey project or the Linux Foundation. Valkey is a trademark of
LF Projects, LLC.
