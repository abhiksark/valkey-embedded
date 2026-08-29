# Lifecycle Rules

Process lifecycle is the package's core correctness boundary. Treat startup, attach,
close, stop, termination, and finalization as concurrent resource-ownership operations.

## API models

- `Valkey()` starts or attaches to an auto-managed Unix-socket server. No path means an
  owned disposable temp tree; a database path means persistent/shared state.
- `connect(path, durable=...)` uses the same managed client with explicit RDB/AOF
  durability presets.
- `ValkeyServer` owns an explicit start/stop API and TCP endpoint for external clients.
  It still uses a private Unix socket for identity/readiness where possible.

Do not blur these models or add TCP exposure to default `Valkey()`.

## Startup identity and rollback

- A port/socket answering is not proof that it belongs to this launch. Validate process
  creation identity and the private endpoint; use Valkey run identity/config where
  available.
- PID existence alone is not identity. Store/compare process creation time or an
  equivalent anti-reuse value before signalling or adopting a daemon.
- Startup is transactional: prepare paths, launch, prove identity/readiness, then commit
  running state. Any exception terminates only the acquired process and rolls back owned
  files, ports, callbacks, and state.
- Capture a bounded redacted log tail before deleting failed-start logs.
- Identity-critical config (`dir`, pid/log/socket paths, bind/port) must not silently make
  object state disagree with the server.

## State transitions

- Model lifecycle state explicitly; do not infer it solely from `port` or a pidfile.
- `start`, `close`, `stop`, and `terminate` are idempotent in documented states.
- Repeated/restart transitions must recompute per-run paths and process identity.
- A failed ownership check remains retryable; do not retire an atexit fallback before
  lifecycle responsibility is resolved.
- `__del__` and atexit are best-effort fallbacks. They cannot guarantee cleanup after
  SIGKILL; document crash boundaries honestly.

## Shared ownership and registry

- Shared lifecycle is based on managed handles, not arbitrary `CLIENT LIST` connection
  counts. External valkey-py clients do not silently become lifecycle owners.
- Serialize concurrent first-open, attach, holder registration/removal, stale recovery,
  and final-close election with a POSIX lock or equivalent supported mechanism.
- Registry state is versioned, validated, private (`0600`), and written atomically.
- Include canonical database identity, verified daemon identity, normalized
  identity-affecting config, and unique holder identity; never include secrets.
- Prune stale holders only after validating process identity. A stale/corrupt registry
  must not permanently block startup or cause an unrelated PID to be signalled.
- Conflicting configurations for one shared path fail clearly rather than being ignored.

## Shutdown and filesystem ownership

- Capture verified daemon identity before graceful shutdown can remove the pidfile.
- Escalation is bounded: graceful shutdown, wait, terminate, wait, then kill if still the
  same process.
- Never signal PID 0 or use a pidfile after identity has been reset/reused.
- Owned disposable directories are removed. User-owned/persistent directories and data
  are preserved; only runtime files owned by this package are removed.
- `durable=False` is RDB snapshot persistence and may lose recent crash-time writes;
  `everysec` and `always` follow documented AOF guarantees. Tests must not claim stronger
  durability than Valkey provides.

## Lifecycle testing

- Use real subprocesses for public lifecycle guarantees and deterministic fakes only for
  hard-to-force escalation/error branches.
- Use barriers/events to force race interleavings; then repeat under a stress marker.
- Every test records owned PID/path state and has a final cleanup guard independent of the
  assertion path.
- Assert both positive behavior and negative resources after close/failure.
- Poll monotonic observable state; never add arbitrary readiness sleeps.
