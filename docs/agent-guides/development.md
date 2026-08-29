# Development Rules

## Module responsibilities

- `client.py` owns the auto-managed `Valkey` client, `connect()`, and shared/isolated
  client semantics. Keep the captured upstream `_ValkeyClient` stable so monkeypatching
  cannot change inheritance or explicit upstream lifecycle calls.
- `server.py` owns explicit `ValkeyServer` lifecycle and TCP-facing metadata. Do not copy
  new low-level process logic between it and `client.py`; extract a private shared runtime
  when behavior is common.
- `configuration.py` renders deterministic Valkey configuration. Keep defaults local-only,
  preserve list/`None` semantics, quote/escape path directives safely, and never log
  secret-bearing configuration casually.
- `pytest_plugin.py` owns consumer fixtures. The session server is shared per worker;
  function clients flush keys, while config/ACL/script state remains process-scoped.
- `patch.py` mutates process-global upstream symbols. Every mutation must have symmetric,
  idempotent restoration, including embedded class state.
- `__init__.py` loads build metadata before importing modules that consume the executable
  path. Preserve that import order and keep public exports deliberate.
- `tools/build_valkey.py` is part of the PEP 517 build path and follows the stricter rules
  in [Packaging](packaging.md).

## Public behavior

- Preserve the distinction between `Valkey()` (private Unix socket), `connect(path,
  durable=...)` (SQLite-style persistence), and `ValkeyServer` (explicit TCP endpoint).
- The first `Valkey` positional argument is a database path, not a host. Do not broaden or
  reinterpret it silently.
- Keep public lifecycle methods idempotent. Raise actionable package exceptions for
  startup failures and normal Python errors for clear API misuse.
- Treat persistent/shared configuration conflicts explicitly; do not silently attach to a
  daemon with incompatible identity-affecting settings.
- Update README, docstrings, examples, and changelog when public behavior changes.

## Process and filesystem code

- Track immutable caller intent separately from per-run paths, PIDs, ports, and state.
- Make resource acquisition transactional: every failure path rolls back only resources
  acquired by that attempt.
- Use monotonic deadlines and bounded polling. Never add an unbounded wait or rely on a
  fixed sleep for readiness, replication, or process death.
- Use `subprocess` without `shell=True`; executable and arguments must remain controlled.
- Broad catches are acceptable only in best-effort cleanup/finalizers. Preserve useful
  diagnostics and avoid hiding the primary exception.
- Generated config/registry files may contain sensitive paths or settings; create them
  with deliberate private permissions.

## Python and editing

- Read the current implementation, nearby tests, and relevant public documentation before
  editing. Current code/tests take precedence over historical plans.
- Support Python 3.9 through the newest version declared in `pyproject.toml`; do not use
  syntax or stdlib APIs outside that range without a compatible fallback.
- Keep `mypy --strict` clean. Use precise generics and narrow values explicitly; targeted
  ignores require a rationale and exact error code.
- Follow the Google Python style guide and existing Ruff formatting. Comments explain
  constraints and rationale rather than restating code.
- Prefer small, focused units and established patterns. Avoid a broad refactor unless it
  directly removes duplicated lifecycle risk or the task explicitly calls for it.
