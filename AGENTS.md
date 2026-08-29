# AGENTS.md

This repository packages a real Valkey server as an embedded, auto-managed Python
runtime. Preserve process identity, lifecycle cleanup, local-only security defaults,
and clean binary distribution while keeping the public API small and compatible with
supported Python and valkey-py versions.

## Non-negotiable rules

- Preserve unrelated modified and untracked files; inspect the worktree before editing
  and stage only intended paths.
- Keep the product boundary clear: this is for tests, CI, local development, demos, and
  non-critical single-node tooling—not an HA production datastore or system of record.
- A successful start must prove the identity of this instance's daemon; endpoint
  reachability or the existence of a PID alone is insufficient.
- Never signal PID 0, an unverified/reused PID, or a process not proven to be owned by the
  current lifecycle operation.
- Respect filesystem ownership: remove owned temporary state, preserve user-owned data,
  and keep persistent RDB/AOF data according to the documented durability contract.
- Keep `Valkey()` Unix-socket-only by default; TCP belongs to the explicit
  `ValkeyServer` API and remains loopback-bound unless the caller deliberately overrides it.
- Treat shared registry/open/close behavior as concurrent cross-process code. Do not use
  raw connection counts as a substitute for synchronization or managed ownership.
- Never commit generated Valkey binaries, bundled generated licenses, build metadata,
  wheels, sdists, caches, or virtual environments.
- Maintain Python 3.9 compatibility, strict typing, Google-style docstrings, and focused,
  readable code without speculative abstractions.
- Add regression tests for behavior changes, use bounded polling rather than fixed sleeps,
  and ensure failed tests cannot leave owned daemons or temp directories behind.
- Use Conventional Commit messages and update `CHANGELOG.md` for user-visible behavior.

## Task-specific guides

Read the smallest relevant set. Code changes normally require Development,
Verification, and Git workflow; process/registry changes require Lifecycle; build or
artifact changes require Packaging; releases require Changelog and Release.

- [Development rules](docs/agent-guides/development.md): module responsibilities, API,
  configuration, typing, and editing conventions.
- [Verification rules](docs/agent-guides/verification.md): targeted tests, full gates,
  artifact checks, and leak-safe process testing.
- [Lifecycle rules](docs/agent-guides/lifecycle.md): startup identity, state transitions,
  shared ownership, cleanup, and durability invariants.
- [Packaging rules](docs/agent-guides/packaging.md): pinned Valkey builds, generated
  assets, wheel/sdist contracts, and supply-chain controls.
- [Git workflow](docs/agent-guides/git-workflow.md): branches, staging, commits, pull
  requests, and protected operations.
- [Changelog rules](docs/agent-guides/changelog.md): curated `Unreleased` and release
  entries.
- [Release rules](docs/agent-guides/releases.md): versioning, validation, tags, and PyPI
  publishing boundaries.
