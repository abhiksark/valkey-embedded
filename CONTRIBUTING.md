# Contributing to valkey-embedded

Thanks for considering a contribution! This document covers everything you need
to get a working dev environment, run the tests, and land a change.

## Dev setup

```bash
git clone https://github.com/abhiksark/valkey-embedded
cd valkey-embedded
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]        # compiles the embedded Valkey: needs make + a C compiler
```

The editable install downloads the pinned Valkey source tarball, verifies its
SHA-256, and compiles `valkey-server` into `src/valkey_embedded/bin/` (a few
minutes, once). On Debian/Ubuntu: `sudo apt install make gcc`. On macOS:
`xcode-select --install`. Windows is unsupported — use WSL.

Alternatively, run from a bare source checkout without installing:

```bash
pip install valkey psutil pytest
python tools/build_valkey.py          # build the embedded server once
PYTHONPATH=src python -m pytest
```

## Running the tests

```bash
pytest                  # fast tier (default; `slow` is deselected)
pytest -m slow          # heavy/network tier: sdist build, clean-venv install
pytest -m packaging     # build-artifact inspection (offline, fast)
pytest -m examples      # runs every script in examples/ end-to-end
```

The full suite must be green before a PR is merged. CI runs the fast tier on
Python 3.9 and 3.13 (Linux + macOS), plus lint and packaging jobs.

## Lint and formatting

```bash
ruff check src/ tests/ examples/ tools/
ruff format src/ tests/ examples/ tools/
check-manifest
```

CI enforces all three. Python code follows the
[Google Python style guide](https://google.github.io/styleguide/pyguide.html);
docstrings are Google-style.

## Commit conventions

Conventional commits (`feat:`, `fix:`, `build:`, `ci:`, `docs:`, `test:`,
`style:`, `chore:`), imperative mood, with a body explaining *why* when the
change isn't self-evident. Breaking changes use `!` (e.g. `feat!:`).

## Pinning a new Valkey release

The embedded server version is set by `VALKEY_VERSION` (default pinned in
`tools/build_valkey.py`). To bump it:

1. Set `VALKEY_VERSION=<new>` and run `python tools/build_valkey.py` once — the
   build refuses to compile an unpinned download and prints the tarball's
   SHA-256.
2. Record that hash in `KNOWN_SHA256` in `tools/build_valkey.py`.
3. Rebuild and run the full suite (`pytest -m "slow or not slow"`).

## Pull requests

- One logical change per PR; keep diffs reviewable.
- Add or update tests for any behavior change.
- Update README/CHANGELOG when the public surface changes.
- CI must be green.

## Releasing (maintainers)

One-time setup:

1. Create the GitHub repository (`abhiksark/valkey-embedded`) and push.
2. On PyPI, create the `valkey-embedded` project and add a **trusted
   publisher** for this repo's `release.yml` workflow, environment `pypi`.
3. In the GitHub repo settings, create the `pypi` environment.

Per release:

1. Update `CHANGELOG.md` (move Unreleased to the new version) and bump
   `version` in `pyproject.toml`.
2. `git tag vX.Y.Z && git push --tags` — the release workflow builds wheels +
   sdist, runs the suite against each wheel, `twine check`s the artifacts, and
   publishes to PyPI via trusted publishing.
