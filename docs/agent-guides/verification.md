# Verification Rules

Test the narrowest affected behavior first, then run the repository gates appropriate to
scope. Activate `.venv` or prefix commands with `.venv/bin/`.

## Required code gates

For a normal source change, run:

```bash
ruff check src/ tests/ examples/ tools/
ruff format --check src/ tests/ examples/ tools/
mypy --strict src/valkey_embedded
interrogate -c pyproject.toml src/valkey_embedded
check-manifest
coverage erase
coverage run -m pytest -m "not slow"
coverage report
```

Coverage is statement-and-branch aware and must remain at least 90%. More importantly,
every changed lifecycle/error branch needs a behavior assertion; do not add weak tests
only to raise the number.

## Test tiers

```bash
pytest                         # default; excludes `slow`
pytest -m packaging            # inspect existing wheel artifacts, offline
pytest -m examples             # run examples as living documentation
pytest -m slow                 # network/build/clean-install tier
pytest -m "slow or not slow"   # all tests
```

Run `pytest -m "slow or packaging" tests/test_packaging.py` for packaging changes. Run
`twine check dist/*` only against freshly built intended artifacts. Source-build and
release validation may require `make`, a C compiler, network access, and clean environments.

## Process-test safety

- Capture owned PIDs and paths before assertions and use `try/finally` cleanup guards.
- Poll with a monotonic deadline; every subprocess and thread join has a timeout.
- Never kill processes found only by a global name match. Signal only identities created
  and recorded by the test.
- Assert negative resources after cleanup: daemon dead, owned temp/socket directories
  absent, runtime registry removed, and no meaningful FD growth.
- Include `valkey.log`, child stdout/stderr, PID, paths, and random seed in failure output.
- Race tests must use barriers/events for deterministic interleavings before adding
  repeated stress coverage.

## Artifact and consumer changes

- Build from a clean or isolated build tree; stale `build/` content must not influence a
  wheel.
- Inspect every produced wheel, exact top-level namespaces, file modes, metadata/version
  consistency, licenses, and forbidden files.
- Install the wheel/sdist into a clean consumer environment and test import, start,
  set/get, close, CLI/debug, pytest entry-point discovery, and `pip check`.
- Do not turn an expected-capability build failure into `pytest.skip`; skip only when the
  test's documented external prerequisite is genuinely unavailable.

## Documentation-only changes

Run `git diff --check`, inspect rendered Markdown structure, and resolve every changed
relative link from its source file. The Python suite is unnecessary unless executable
examples, commands, or behavior contracts changed.
