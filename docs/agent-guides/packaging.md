# Packaging Rules

The package downloads, compiles, executes, and redistributes native Valkey binaries.
Build and artifact changes are supply-chain-sensitive.

## Generated assets

These are generated/ignored and must not be committed:

- `src/valkey_embedded/bin/valkey-server`
- `src/valkey_embedded/bin/valkey-cli`
- `src/valkey_embedded/bin/VALKEY_COPYING.txt`
- `src/valkey_embedded/package_metadata.json`
- `build/`, `dist/`, `*.egg-info`, caches, coverage files, and virtual environments

The sdist excludes compiled binaries/metadata and includes the build hook. Wheels include
current binaries, complete bundled licenses, relative metadata, and `py.typed`.

## Source build controls

- Keep the upstream version SHA-256 pinned; checksum mismatch and unknown versions fail
  closed.
- `VALKEY_ALLOW_UNPINNED=1` is local experimentation only and is refused in CI.
- Download with a bounded timeout over HTTPS.
- Tar extraction must reject traversal through names, absolute paths, symlinks, and
  hardlinks on every supported Python, including fallback paths without `filter="data"`.
- Preflight POSIX, `make`, and C compiler availability before downloading.
- Compile with explicit arguments and no shell; preserve the documented allocator/TLS
  choices unless intentionally changed and tested.
- Missing expected binaries or license inputs is a build failure, not a note-only success.

## Metadata and build cache

- Package version comes from `pyproject.toml`; embedded Valkey version comes from the
  verified binary/pin. Keep them separate and consistent with artifact metadata.
- Store only package-relative executable paths; never bake a build-host absolute path into
  a wheel.
- Reuse a cached binary only after checking server, CLI, licenses, metadata schema/project
  version, executable modes, embedded version, and platform/architecture.
- A package-version bump refreshes metadata even when the Valkey binary is reused.
- Build from a clean/isolated tree so stale `build/lib.*` content cannot enter artifacts.

## Wheel contract

- Allowed top-level namespaces are `valkey_embedded/` and one matching `.dist-info/`.
- Reject stale `valkeylite/`, tests, examples, caches, source-only helpers, duplicate
  metadata, and absolute paths.
- Verify executable modes, complete license text, `RECORD`, platform tags, and dynamic
  library policy.
- Inspect every wheel supplied to a test, not only the newest/last filename.
- Test each wheel as an installed consumer: import/version, start/set/get/close,
  CLI/debug, pytest entry point, and `pip check`.

## Sdist and release validation

- Verify source-only contents and install the sdist in a clean environment with the
  expected toolchain.
- Do not broadly skip build/install failures in release-capable jobs.
- Use cibuildwheel artifacts for publishable manylinux/macOS wheels; a local
  `linux_x86_64` wheel is only a development artifact.
- Run `twine check`, platform binary audits, full tests, and clean-consumer smoke before
  publishing.
- Follow [Release Rules](releases.md) for version/tag/publish operations and the Valkey
  pinning procedure.
