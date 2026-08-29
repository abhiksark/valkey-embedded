# Release Rules

valkey-embedded uses Semantic Versioning. While below 1.0, minor releases may refine the
API, but every user-visible change is documented.

## Sources of truth

- Package version: `[project].version` in `pyproject.toml`.
- Runtime package version: installed distribution metadata, with generated build metadata
  only as a source-checkout fallback.
- Embedded server pin/hash: `VALKEY_VERSION` and `KNOWN_SHA256` in
  `tools/build_valkey.py`.
- Changelog: `CHANGELOG.md`.
- Tags: `vX.Y.Z`.
- Publishing: `.github/workflows/release.yml` via PyPI trusted publishing.

Generated `src/valkey_embedded/package_metadata.json`, bundled binaries/licenses, wheels,
and sdists are build outputs and must not be committed.

## Before a release

1. Inspect worktree, branch, remote, tags, milestone, and open release blockers. Preserve
   unrelated local files.
2. Confirm the intended SemVer bump and update `pyproject.toml` exactly once.
3. Curate `CHANGELOG.md`; update README/support documentation when compatibility or
   behavior changed.
4. Run all static/default gates from [Verification](verification.md).
5. Run packaging tests from a clean build tree, build sdist/wheel as appropriate, run
   `twine check`, inspect exact contents, and run clean installed-consumer smoke tests.
6. Confirm package metadata, artifact names, runtime `__version__`, and embedded Valkey
   version are consistent.
7. Verify supported Python/platform CI and required release issues are green/closed.

## Publishing

- Do not bump, tag, push, dispatch, or publish without explicit user approval.
- Create `vX.Y.Z` only at the reviewed release commit on `main`.
- Inspect the tag before pushing; push the branch and that specific tag deliberately.
- The tag-triggered workflow builds fresh cibuildwheel artifacts and an sdist, tests them,
  performs trusted publishing, and must remain the only normal PyPI publication path.
- Never replace or move a published tag. Correct a bad release with a new version.
- Retain release artifacts/logs long enough to diagnose platform or supply-chain failures.

## Updating embedded Valkey

A package release and embedded Valkey release are separate version decisions. To update
Valkey:

1. Set `VALKEY_VERSION=<new>` and run `python tools/build_valkey.py` once to obtain the
   fail-closed computed SHA-256 for an unpinned version.
2. Independently verify the upstream release/source, then add its digest to
   `KNOWN_SHA256`.
3. Rebuild without the unpinned bypass and verify binary banner, licenses, and metadata.
4. Run the full test suite, crash/durability tests, replication, clean wheel/sdist consumer
   tests, and supported platform CI.
5. Document the embedded version/security impact in the changelog and release notes.

`VALKEY_ALLOW_UNPINNED=1` is local experimentation only and remains forbidden in CI.
