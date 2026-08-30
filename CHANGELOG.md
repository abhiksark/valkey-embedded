# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(0.x releases may change the API; changes are always noted here).

## [Unreleased]

### Fixed

- `ValkeyServer.start()` now proves PID, private-socket, and TCP run identity
  before reporting readiness, so an occupied port cannot attach callers to an
  unrelated Valkey or fake listener.
- Failed `ValkeyServer.start()` calls now transactionally terminate their owned
  child, restore caller-owned files, remove generated runtime state, reset the
  object, and include bounded redacted startup diagnostics.
- `ValkeyServer` now has an explicit serialized lifecycle and can restart after
  `stop()`, `terminate()`, failed startup, or external process death; each
  disposable run receives fresh paths while persistent paths and data are reused.
- `Valkey.close()` now stops the embedded server when the last client closes,
  releases its connection pool, and cleans up owned runtime files immediately.

## [0.1.0] - 2026-06-18

First release.

### Added

- `Valkey()` — a `valkey.Valkey` client backed by a private, auto-managed
  embedded `valkey-server` (Valkey 8.1.8, compiled from a SHA-256-pinned
  source tarball). Unix-socket only; shuts down and cleans up with the last
  client.
- Shared mode: `Valkey("/path/to/db.rdb")` persists across runs; instances
  sharing a path attach to one server.
- `connect(path, durable=...)` — SQLite-style opener; `durable=True`/
  `"everysec"`/`"always"` enable crash-safe AOF persistence.
- `ValkeyServer` — explicit lifecycle (`start`/`stop`/`terminate`), TCP
  host/port with auto-assigned free ports, `client()`, `connection_url`,
  `connection_kwargs`.
- Pytest plugin (auto-registered): session-scoped `valkey_server`,
  per-test-clean `valkey_client`, `valkey_url`, and `valkey_server_factory`.
- CLI: `valkey-embedded` / `python -m valkey_embedded` foreground server.
- `patch_valkey()` / `unpatch_valkey()` for drop-in use of `valkey.Valkey`.
- Diagnostics module: `python -m valkey_embedded.debug`.
