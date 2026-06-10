# valkey-embedded — OSS-Readiness Audit

**Date:** 2026-06-10 · **Commit:** `724099145d387bae3e262781bc434c8fb6ff1638`
**Bar:** exemplary OSS project (httpx / rich / attrs class)
**Method:** three-track review (hygiene & community · packaging/CI/supply chain ·
code quality & API) over a tool baseline: ruff, mypy --strict, bandit, pip-audit,
build/twine, check-manifest, pyroma, interrogate, zizmor.

## Executive summary

The engineering core is genuinely strong: a real pinned-and-checksummed Valkey build,
crash-verified durability, a clean ruff lint, a 117-test suite, junk-free wheels, no
dependency CVEs, and no script-injection surfaces in CI. But the project is **not yet
publishable**. The README's install instructions are false today (nothing on PyPI, no
publish job in release.yml, and the GitHub repo the metadata points at doesn't exist),
and the documented dev test command crashes for any contributor who has ever built the
package (pytest plugin double-registration). The public API still leaks the old project
name in its two exception classes — the last cheap moment to fix that is now. The entire
community layer (CONTRIBUTING, CHANGELOG, CoC, SECURITY, templates, docs site, coverage)
is absent, which at the chosen bar produces most of the High findings. Supply chain is
the inverse picture: the tarball pinning is better than most projects, while the
workflows ignore the basics (no `permissions:`, floating action tags). 55 raw findings
were verified; none were refuted; 7 were merged as cross-track duplicates, and the
Track 3 dead-code clean-bill was added as T3-18 at spec review — 49 entries below.

| Track | Grade | Blockers | High | Medium | Low | Info |
|---|---|---|---|---|---|---|
| T1 OSS hygiene & community | D | 1 | 6 | 6 | 3 | 2 |
| T2 Packaging, CI & supply chain | C− | 0 | 4 | 5 | 5 | 4 |
| T3 Code quality & API | C | 1 | 4 | 3 | 1 | 4 |
| **Total** | | **2** | **14** | **14** | **9** | **10** |

## Tool substitutions

None — the full battery ran (ruff, mypy --strict, bandit, pip-audit, build, twine,
check-manifest, pyroma 8/10, interrogate 47%, zizmor 22 findings).

## Findings

### Track 1 — OSS hygiene & community

#### [T1-01] README install instructions are false: not on PyPI, and release.yml never publishes — **Blocker**
**Evidence:** README.md:64 (`pip install valkey-embedded  # prebuilt wheels…`) and README.md:67–68 (source-build-at-install claim, which requires an sdist on PyPI). `curl https://pypi.org/pypi/valkey-embedded/json` → 404. `.github/workflows/release.yml` contains no publish step (only `upload-artifact`); `git tag` is empty. Cross-ref T2-03 (the pipeline fix).
**Recommendation:** Add the publish job (T2-03) and publish 0.1.0, or rewrite the Install section to install-from-source instructions so the README is true the moment the repo goes public.
**Effort:** M · **Roadmap:** before-public

#### [T1-02] pyproject Homepage/Source URLs point to a GitHub repo that does not exist — **High**
**Evidence:** pyproject.toml:40–41 → `https://github.com/abhiksark/valkey-embedded`; `curl` → 404; `git remote -v` is empty.
**Recommendation:** Create the `abhiksark/valkey-embedded` repo (or point URLs at the real destination) before tagging anything.
**Effort:** S · **Roadmap:** before-public

#### [T1-03] No CONTRIBUTING.md — **High**
**Evidence:** Absent from `git ls-files`; repo root holds only README.md, LICENSE.txt, MANIFEST.in, pyproject.toml, setup.py. httpx, rich, and attrs all ship one.
**Recommendation:** Write CONTRIBUTING.md: dev setup (`pip install -e .[dev]` — note it compiles Valkey, needs gcc/make), test tiers (`pytest`, `-m slow`, `-m packaging`), the Valkey-pinning procedure (currently README-only), and PR expectations.
**Effort:** M · **Roadmap:** before-public

#### [T1-04] No CHANGELOG.md and no release-notes story — **High**
**Evidence:** No CHANGELOG*/CHANGES*/HISTORY* in `git ls-files`; release.yml creates no GitHub Release. All three exemplar projects maintain a changelog.
**Recommendation:** Add CHANGELOG.md with `## 0.1.0 (unreleased)` now; pick the ongoing mechanism (Keep-a-Changelog vs towncrier) and have release.yml attach notes to a GitHub Release on tag.
**Effort:** S · **Roadmap:** before-public

#### [T1-05] No CODE_OF_CONDUCT.md — **High**
**Evidence:** Absent from repo and `.github/` (which contains only `workflows/`). All three exemplar projects have one.
**Recommendation:** Add Contributor Covenant 2.1 with a real contact address.
**Effort:** S · **Roadmap:** before-public

#### [T1-06] No SECURITY.md despite the package downloading and executing compiled binaries — **Medium**
**Evidence:** No SECURITY.md anywhere. The threat surface is real: tools/build_valkey.py downloads a tarball at build/install time and the package executes the bundled binary; users need a private channel for e.g. a checksum-bypass report.
**Recommendation:** Add SECURITY.md (supported-versions table + reporting via GitHub Security Advisories); enable private vulnerability reporting on the repo once it exists.
**Effort:** S · **Roadmap:** before-public

#### [T1-07] No issue templates or PR template — **Medium**
**Evidence:** `.github/` contains only the two workflows. debug.py:2 says it exists "for bug reports", but nothing routes reporters to it.
**Recommendation:** Add `.github/ISSUE_TEMPLATE/bug.yml` (required field: `python -m valkey_embedded.debug` output), `feature.yml`, `config.yml`, and a short PR template.
**Effort:** S · **Roadmap:** before-v1.0

#### [T1-08] No FUNDING.yml — **Info**
**Evidence:** Absent from `.github/`. All three exemplar projects carry sponsor links.
**Recommendation:** Add `github: [abhiksark]` if sponsorship is wanted; otherwise consciously skip.
**Effort:** S · **Roadmap:** aspirational

#### [T1-09] README has zero badges; CI, license, and Python-version badges are possible today — **Medium**
**Evidence:** `grep -c 'badge\|shields' README.md` → 0. Possible now: CI status, license, Python 3.9–3.13. Blocked on other findings: PyPI version (T1-01), coverage (T1-10), docs (T1-11).
**Recommendation:** Add CI + license + python-versions badges now; add PyPI/coverage badges in the PRs that enable them.
**Effort:** S · **Roadmap:** before-public

#### [T1-10] No coverage measurement anywhere (CI or local) — **High**
**Evidence:** No coverage tooling in workflows or pyproject (the only `cov` hit in ci.yml is the word "covers" in a comment). httpx enforces 100%; rich and attrs gate on coverage.
**Recommendation:** Add pytest-cov + `[tool.coverage]` config, run CI under coverage, upload to Codecov. Subprocess-heavy daemon paths will need `concurrency`/`sigterm` settings or explicit omits.
**Effort:** M · **Roadmap:** before-v1.0

#### [T1-11] No docs site, no versioning policy, no support/deprecation policy — **High**
**Evidence:** `docs/` holds only internal planning markdown (T1-18); no mkdocs/sphinx/readthedocs config; no SemVer/CalVer or Python-support statement anywhere. attrs/httpx/rich have all four.
**Recommendation:** Minimum for launch (S, do before-public): a "Versioning & support" README section (SemVer intent, 0.x instability caveat, Python window). The docs site itself (MkDocs Material mirroring README + API reference) is the L-sized fast-follow.
**Effort:** L · **Roadmap:** before-v1.0

#### [T1-12] Bundled VALKEY_COPYING.txt omits licenses of code statically linked into valkey-server (Lua, hdr_histogram, …) — **Medium**
**Evidence:** tools/build_valkey.py:126–130 copies only Valkey's top-level COPYING. `strings src/valkey_embedded/bin/valkey-server | grep lua_getmetatable` → present: the redistributed binary embeds Lua (MIT), hdr_histogram, fpconv, linenoise, whose notices live in Valkey's `deps/` and are not shipped. (jemalloc excluded — `MALLOC=libc`.) Binary-redistribution clauses require reproducing those notices.
**Recommendation:** Extend build_valkey.py to concatenate the `deps/*` license files from the pinned tarball into VALKEY_COPYING.txt; mention "and statically linked dependencies" in LICENSE.txt's third-party section.
**Effort:** M · **Roadmap:** before-public

#### [T1-13] License story coherent but invisible from the README — **Low**
**Evidence:** Core is sound: pyproject SPDX `BSD-3-Clause`, LICENSE.txt:26–35 documents the bundled binary, MANIFEST.in prunes binaries from the sdist while wheels carry VALKEY_COPYING.txt. But README's only license mentions are passing phrases (lines 16–18); no "## License" section says a third-party binary ships inside the wheel.
**Recommendation:** Add a short README License section: project BSD-3-Clause; bundled valkey-server BSD-3-Clause with its license at `valkey_embedded/bin/VALKEY_COPYING.txt`.
**Effort:** S · **Roadmap:** before-public

#### [T1-14] Trademark disclaimer lacks mark-ownership attribution — **Low**
**Evidence:** README.md:166–169 disclaims affiliation (good nominative use) but omits the standard attribution line LF trademark guidelines expect.
**Recommendation:** Append "Valkey is a trademark of LF Projects, LLC." (verify exact holder string against valkey.io/legal).
**Effort:** S · **Roadmap:** before-public

#### [T1-15] README under-documents the exported API — **Medium** *(merged: T1-15 + T3-10)*
**Evidence:** Exported but absent from README: `StrictValkey`, both exception classes, `__valkey_executable__`, `__valkey_server_version__`; the `valkey_server` fixture alias (pytest_plugin.py:29–32); `durable="everysec"` (client.py `_DURABLE_PRESETS`). No Errors section. The wheel also bundles `valkey-cli` (1.8 MB) that no doc or API mentions. Nothing is exported that shouldn't be.
**Recommendation:** Add an "API & errors" README section covering the exceptions (post-T3-02 rename), `StrictValkey`, the dunders, all fixtures, `durable="everysec"`, and either document or stop bundling `valkey-cli`.
**Effort:** S · **Roadmap:** before-v1.0

#### [T1-16] macOS wheel floor (14+) not stated in the README platform claim — **Low**
**Evidence:** README.md:64 says "macOS arm64" with no OS version; release.yml:25 sets `MACOSX_DEPLOYMENT_TARGET: "14.0"`, so wheels refuse macOS 12/13.
**Recommendation:** State "macOS 14+ arm64" in Install, or lower the target.
**Effort:** S · **Roadmap:** before-v1.0

#### [T1-18] Internal planning documents are tracked and would ship with the public repo — **Medium** *(raised from Info at calibration)*
**Evidence:** `git ls-files docs/` → 5 internal planning/spec documents (using the old "valkeylite" name), in the location users expect real documentation.
**Recommendation:** Move the internal planning documents out of the tree (or stop tracking them) before going public; reserve `docs/` for the docs site.
**Effort:** S · **Roadmap:** before-public

#### [T1-19] Verified-accurate README claims — for the record — **Info**
**Evidence:** Every remaining factual claim checked against code and confirmed: Valkey 8.1.8 pin and checksum refusal; `patch_valkey`; pytest11 auto-registration; `durable=True` → AOF everysec + path requirement; `Valkey()` unix-socket-only `port 0`; ValkeyServer surface; shared-path attach/last-closer-shutdown; CLI entry points; debug module; examples list. Deviations are exactly T1-01, T1-15, T1-16.
**Recommendation:** None — recorded so the false-claim list is known to be exhaustive.
**Effort:** S · **Roadmap:** —

### Track 2 — Packaging, CI & supply chain

#### [T2-01] No `permissions:` block in either workflow — **High**
**Evidence:** `grep -c permissions .github/workflows/*.yml` → 0 in both; GITHUB_TOKEN runs at default scope. zizmor: 10 medium findings (excessive-permissions, artipacked/credential persistence).
**Recommendation:** Top-level `permissions: contents: read` in both; per-job grants only as needed (`id-token: write` on the future publish job); `persist-credentials: false` on checkouts.
**Effort:** S · **Roadmap:** before-public

#### [T2-02] All actions pinned to floating tags, not commit SHAs — **High**
**Evidence:** 9 `uses:` entries across ci.yml/release.yml, all `@v4`/`@v5`/`@v2.21` (zizmor: 9 high `unpinned-uses`). Tags are mutable; this workflow produces release artifacts.
**Recommendation:** Pin every action to a full commit SHA with a `# vX.Y.Z` comment; add Dependabot for the `github-actions` ecosystem.
**Effort:** S · **Roadmap:** before-public

#### [T2-03] release.yml builds but never publishes — no trusted publishing, no twine check, no attestation — **High**
**Evidence:** release.yml ends at `upload-artifact`; no `pypa/gh-action-pypi-publish`, no `id-token: write`, no `environment:`, no `twine check` step. Cross-ref T1-01.
**Recommendation:** Add a `publish` job gated on the builds: `environment: pypi`, `permissions: id-token: write`, download artifacts, `twine check`, publish via trusted publishing (never token secrets). Optionally `actions/attest-build-provenance`.
**Effort:** M · **Roadmap:** before-public

#### [T2-04] sdist contents drift: ships an unrunnable test suite, omits examples/, check-manifest fails — **Medium** *(merged: T2-04 + T2-05 + T3-06 + T3-13)*
**Evidence:** sdist contains all 18 `tests/test_*.py` but not `tests/conftest.py` or `tests/__init__.py` (fixtures never register) and not `examples/` (which `test_examples.py` executes). `check-manifest -v` FAILS: 17 tracked files missing (10 examples, 5 internal planning docs, 2 tests). This also drives the pyroma deduction (8/10 — metadata itself is complete).
**Recommendation:** Decide the sdist test story: ship a runnable suite (`recursive-include examples …`, include conftest/__init__) or ship none (httpx ships none) — the half-state is the worst option. Prune the internal planning docs either way. Declare intentional exclusions in `[tool.check-manifest] ignore` and add check-manifest to CI.
**Effort:** S · **Roadmap:** before-public

#### [T2-06] `project.urls` missing Documentation, Changelog, and Issues entries — **Medium**
**Evidence:** pyproject.toml carries only Homepage and Source (identical URLs). Exemplar projects ship Changelog + Documentation + Issues. Depends on T1-04 (changelog) and T1-02 (repo).
**Recommendation:** Add the three URLs in the same change that creates the changelog and repo.
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-07] CI tests only Python 3.9 and 3.13; 3.10–3.12 are exercised only on release tags — **Medium**
**Evidence:** ci.yml:15–17 `python: ["3.9", "3.13"]` vs classifiers 3.9–3.13. A 3.11-only regression merges untested and is first caught during a release. Also: 3.9 is past EOL (Oct 2025); 3.14 is stable and absent from both classifiers and `CIBW_BUILD`.
**Recommendation:** Full matrix on PRs (compile cost is cacheable — see T2-17) or a weekly scheduled full-matrix run; revisit the 3.9/3.14 window.
**Effort:** M · **Roadmap:** before-v1.0

#### [T2-08] `pip install` on an unsupported platform dies with an opaque traceback after a wasted download — **High**
**Evidence:** setup.py:30 calls `build_valkey.build(...)` unconditionally; `_compile()` runs `make` with no platform/toolchain preflight. On Windows or a gcc-less box, the user gets `FileNotFoundError: 'make'` buried in pip's backend traceback. This path is load-bearing: wheels cover only Linux x86_64 + macOS 14 arm64 (T2-16), so Intel macOS, aarch64 Linux, and Alpine users all hit it.
**Recommendation:** Fail fast at the top of the build: check `sys.platform` and `shutil.which("make")`/`which("cc")`, raising a clear, named error with remediation before any download.
**Effort:** S · **Roadmap:** before-public

#### [T2-09] Tarball extraction is path-traversal-unguarded on Python 3.9–3.11 despite the filter backport — **Medium**
**Evidence:** tools/build_valkey.py:84–88 applies `filter="data"` only on ≥3.12, bare `extractall` otherwise. The `filter` parameter was backported to 3.9.17/3.10.12/3.11.4 (CVE-2007-4559), so the version gate is wrong for current interpreters. Mitigated by the SHA pin in the normal path; fully exposed under `VALKEY_ALLOW_UNPINNED=1`.
**Recommendation:** `try: tf.extractall(into, filter="data") except TypeError: <fallback>`, or set `tarfile.TarFile.extraction_filter = tarfile.data_filter` where available.
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-10] Checksum pinning works as designed, but the bypass env var silently disables all verification (and the download has no timeout) — **Low**
**Evidence:** Verified by direct invocation: unpinned and mismatched hashes both `SystemExit`; with `VALKEY_ALLOW_UNPINNED=1` junk is accepted with only a stdout WARNING (build_valkey.py:69–71). Download URL is HTTPS. `urlopen` (line 48) has no `timeout=`.
**Recommendation:** Refuse the bypass when `CI` is set, or require `VALKEY_SHA256=<hex>` instead of fully-unverified mode; add a download timeout.
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-11] Pinned artifact is GitHub's auto-generated tag tarball — not guaranteed byte-stable — **Info**
**Evidence:** build_valkey.py:36–42; the comment already acknowledges it. Failure mode is loud (fail-closed), just disruptive.
**Recommendation:** Switch to an uploaded release asset if valkey-io publishes one.
**Effort:** S · **Roadmap:** aspirational

#### [T2-12] Version hardcoded in two places with a third consumer — **Medium** *(merged: T2-12 + T1-17)*
**Evidence:** pyproject.toml:7 `version = "0.1.0"` and tools/build_valkey.py:135 `"valkey_embedded_version": "0.1.0"`, consumed as the `__version__` fallback in `__init__.py:31`. Bumping pyproject alone ships a wheel whose metadata JSON claims the old version.
**Recommendation:** Single-source it: build_valkey.py reads pyproject via `tomllib`, or drop the JSON key and rely on `importlib.metadata`. (setuptools-scm `dynamic = ["version"]` is the attrs/rich pattern.)
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-13] package_metadata.json bakes the builder's absolute path into shipped wheels — **Low**
**Evidence:** The freshly built wheel contains `"valkey_executable": "/tmp/build-via-sdist-yknqp06m/.../bin/valkey-server"` — an ephemeral build-host path, meaningless on install machines (runtime prefers the bundled binary) and fatal to byte-reproducibility.
**Recommendation:** Store a package-relative path (`bin/valkey-server`) or drop the key; resolve relative to the package dir at runtime.
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-14] `dev` and `test` extras are identical duplicates — **Low**
**Evidence:** pyproject.toml:28–29 — both are `["pytest>=7.4"]`; no lint/type/build tooling in either despite shipping py.typed.
**Recommendation:** `test = ["pytest>=7.4"]`; `dev = ["valkey-embedded[test]", "ruff", "mypy", "build", "check-manifest"]`.
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-15] Missing classifiers: `Python :: 3 :: Only`, `Typing :: Typed`, `Intended Audience :: Developers`, `Framework :: Pytest` — **Low**
**Evidence:** pyproject.toml:14–24. Ships py.typed without `Typing :: Typed`; ships a pytest11 plugin without `Framework :: Pytest` (standard for plugin discoverability). Note: the *absence* of a `License ::` classifier is correct (PEP 639 SPDX form).
**Recommendation:** Add the four; consider `Operating System :: MacOS` since macOS wheels are released.
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-16] cibuildwheel stale and wheel matrix narrower than the metadata implies — **Low**
**Evidence:** release.yml:19 `pypa/cibuildwheel@v2.21` (Sep 2024, no cp314); `CIBW_SKIP: "*-musllinux_*"`, Linux x86_64 only, macOS arm64 only — Intel macOS, Alpine, aarch64 fall back to the sdist compile path (see T2-08) with no warning.
**Recommendation:** Upgrade + SHA-pin cibuildwheel; broaden arches (native arm runners / macos-13) or document the wheel matrix explicitly in the README.
**Effort:** M · **Roadmap:** aspirational

#### [T2-17] ci.yml lacks a concurrency group and any build cache — **Info**
**Evidence:** No `concurrency:` block; every matrix cell compiles Valkey from source, uncached, on every push.
**Recommendation:** `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }`; cache the compiled binary keyed on VALKEY_VERSION + OS.
**Effort:** S · **Roadmap:** before-v1.0

#### [T2-18] No script-injection surfaces in workflows (verified) — **Info**
**Evidence:** All `${{ }}` interpolations reference only `matrix.*` and `runner.os`; no `github.event.*` or `github.head_ref` reaches `run:` blocks.
**Recommendation:** Keep it that way; T2-01/T2-02 are the actual exposure.
**Effort:** S · **Roadmap:** —

#### [T2-19] sdist build depends on `tools/` being present at backend load time — fragile but currently correct — **Info**
**Evidence:** setup.py:20–21 does `sys.path.insert(0, …/tools); import build_valkey` at import time; a future MANIFEST.in edit dropping `recursive-include tools *.py` makes every sdist un-buildable.
**Recommendation:** Add a packaging test asserting `tools/build_valkey.py` exists inside the built sdist.
**Effort:** S · **Roadmap:** before-v1.0

### Track 3 — Code quality & API

#### [T3-01] Documented dev test command crashes: pytest plugin double-registration — **Blocker**
**Evidence:** Reproduced at audit time: `PYTHONPATH=src .venv/bin/python -m pytest -p no:cacheprovider -q` aborts with `ValueError: Plugin already registered under a different name: valkey_embedded.pytest_plugin=<module …>`. Root cause: tests/conftest.py:10 `pytest_plugins = […]` collides with the pytest11 entry point exposed by stale egg-info on `PYTHONPATH=src` — and `src/` currently holds **two** stale egg-infos (`valkey_embedded.egg-info`, plus the pre-rename `valkeylite.egg-info`). The conftest docstring's "pytest de-duplicates the plugin" is wrong: pluggy de-duplicates same-name registrations only; the same module under a second name raises. Any contributor who has ever run `python -m build` or `pip install -e .` hits this. The suite itself is healthy: with the entry-point copy blocked, `117 passed, 2 skipped, 3 deselected`.
**Recommendation:** Replace the conftest `pytest_plugins` line with conditional registration (`pytest_configure` checking `config.pluginmanager.has_plugin("valkey_embedded")`); delete both stale egg-infos; add a CI job that runs the suite with the package installed editable to lock the fix in.
**Effort:** S · **Roadmap:** before-public

#### [T3-02] Public exception classes still carry the old project name (`ValkeyLite*`) — **High**
**Evidence:** client.py:36 `class ValkeyLiteException`, client.py:40 `class ValkeyLiteServerStartError`; exported in `__all__` (__init__.py:56–57), reused in server.py. These are the exact names users will put in `except:` clauses — renaming after release is a breaking change. This is the last cheap moment.
**Recommendation:** Rename to `ValkeyEmbeddedError` / `ServerStartError` pre-release; optionally keep the old names as deprecated aliases for one release. Update `__all__`, server.py, and the README (with T1-15's Errors section).
**Effort:** S · **Roadmap:** before-public

#### [T3-03] All pytest fixtures are function-scoped — every test pays full server start/stop — **High** *(merged: T3-03 + T3-11)*
**Evidence:** pytest_plugin.py:22–48 — all four fixtures are plain function-scoped; each test boots and tears down a server (~0.3–0.5 s), so a 200-test consumer suite pays minutes of churn. Comparable plugins (pytest-postgresql, pytest-redis) use session process + per-test reset. Naming compounds it: the `valkey_embedded` fixture shadows the package name, and `valkey_server` is a pure alias — two public names for one object, only partially documented. An improved fixture architecture was already designed and approved during development.
**Recommendation:** Implement the approved redesign: session-scoped `valkey_server` as the primary, function-scoped `valkey_client` with FLUSHALL-per-test, `valkey_server_factory` for isolation-demanding tests, `valkey_url` derived; drop or deprecate the `valkey_embedded` alias pre-release; document scope semantics per fixture.
**Effort:** M · **Roadmap:** before-public

#### [T3-04] Ships py.typed but fails `mypy --strict` with 40 errors in 5 files — **High**
**Evidence:** /tmp/audit battery: `Found 40 errors in 5 files (checked 8 source files)`. Categories: unnarrowed `Optional[str]` dbdir (~10 arg-type errors in client.py); `ValkeyMixin` typed standalone so host-class methods are `attr-defined` errors; bare generics (`dict`, `Popen`); patch.py monkeypatch assignments (12); untyped fixture defs; missing `types-psutil`. The *consumer-facing* surface does type-check clean (verified with a strict-mode consumer smoke file), so this is High not Blocker.
**Recommendation:** Add `types-psutil` to dev extras; parameterize generics; narrow `dbdir`; give `ValkeyMixin` a typing-only protocol for the host API; annotate fixtures; targeted ignores in patch.py (monkeypatching is inherently untypable). Gate `mypy --strict src` in CI.
**Effort:** M · **Roadmap:** before-v1.0

#### [T3-05] Docstring coverage 47% — the flagship class's constructor is undocumented — **High**
**Evidence:** interrogate: TOTAL 47.0% (min 80): __main__/debug/patch 25%, client 33%, __init__ 50%, server 61%. Concretely: `ValkeyMixin.__init__` (the signature behind `Valkey(...)`) has no docstring, so `help(Valkey)` documents neither `dbfilename` nor `serverconfig`. `connect()` and `ValkeyServer.__init__` show the project can write exemplary docstrings; the rest doesn't meet that bar.
**Recommendation:** Google-style docstrings on every public callable, prioritizing `ValkeyMixin.__init__`, patch.py, debug, and `main()`. Add interrogate (fail-under 80 → 95) to CI.
**Effort:** M · **Roadmap:** before-v1.0

#### [T3-07] Formatting not enforced: `ruff format --check` would reformat 12 of 38 files — **Medium**
**Evidence:** Battery: "12 files would be reformatted" (client.py, server.py, patch.py, 7 tests, 2 examples, tools/build_valkey.py). Lint is clean; no format gate exists, so the tree drifted.
**Recommendation:** Run `ruff format` once; add `ruff format --check` + `ruff check` to CI (and a `[tool.ruff]` config block so the style is pinned, not implicit).
**Effort:** S · **Roadmap:** before-public

#### [T3-08] Valkey-vs-connect-vs-ValkeyServer not discoverable from docstrings; the RDB-path positional footgun is README-only — **Medium**
**Evidence:** client.py:325 — `Valkey`'s entire docstring is one line; with the missing `__init__` docstring, `help(Valkey)` never reveals that the first positional arg is an RDB **path, not host** — a trap the README warns about (README.md:76–78) but the code does not. No signposting from `Valkey` toward `connect()` (durability) or `ValkeyServer` (TCP).
**Recommendation:** Expand the `Valkey` class docstring: first-arg semantics, isolated-vs-shared behavior, one-line cross-references to `connect()` and `ValkeyServer`; mirror a back-reference from `ValkeyServer`.
**Effort:** S · **Roadmap:** before-v1.0

#### [T3-09] Two error messages leave a new user stranded — **Medium**
**Evidence:** (1) client.py:148 / server.py:121: `"bundled valkey-server not found; build it with tools/build_valkey.py"` — a pip-install user has no `tools/`; no mention of the debug module or filing an issue. (2) server.py:246: `ValkeyServer.client()` raises `ValkeyLiteServerStartError("server is not started")` — wrong exception semantics (nothing failed to start) and no remedy stated. Counter-examples done right: the startup-timeout error and `connect()`'s ValueErrors.
**Recommendation:** (1) Two-audience message: packaging-bug path (`python -m valkey_embedded.debug` + file an issue) vs source-checkout path. (2) `RuntimeError` or a dedicated error: "server is not running; call start() first or use it as a context manager."
**Effort:** S · **Roadmap:** before-v1.0

#### [T3-12] CLI: `--persist` misleading with user `--data-dir`; `--version` embeds the raw server banner — **Low**
**Evidence:** Help says `--persist  keep the data directory and save the RDB on exit`, but a user-supplied `--data-dir` is always kept; what `--persist` actually toggles is `SHUTDOWN SAVE` vs `NOSAVE` (server.py:163–167) — so `--data-dir ./d` without `--persist` silently discards writes. `--version` prints the garbled "…(Valkey Valkey server v=8.1.8…)" because the metadata stores the full banner.
**Recommendation:** Reword both help strings; record just `8.1.8` in package_metadata.json so `--version` prints "valkey-embedded 0.1.0 (Valkey 8.1.8)"; keep the full banner in the debug module.
**Effort:** S · **Roadmap:** before-v1.0

#### [T3-14] bandit: 14 Low findings, all expected for an embedded-server package — **Info**
**Evidence:** All B404/B603 (subprocess with a package-controlled binary, no shell, no untrusted input — the package's purpose) and B110 (deliberate best-effort cleanup paths, each already commented). No Medium/High.
**Recommendation:** Add `[tool.bandit]` skips (with a why-comment) or targeted `# nosec` so consumers and CI get a clean run instead of re-triaging.
**Effort:** S · **Roadmap:** before-v1.0

#### [T3-15] pip-audit, twine, and wheel contents clean — **Info**
**Evidence:** No known dependency vulnerabilities; both artifacts pass `twine check`; wheel contains exactly the expected files (binary, license, py.typed, metadata JSON), zero junk. Local wheel tag `linux_x86_64` is PyPI-unpublishable but release builds use cibuildwheel/manylinux.
**Recommendation:** None — baseline context.
**Effort:** — · **Roadmap:** —

#### [T3-18] No dead code found (verified) — **Info**
**Evidence:** Track 3 swept every module for unreachable/vestigial code from the redislite port: `StrictValkey` (client.py:329) and the patch.py function pairs are deliberate redislite/valkey-py parity (documented in patch.py:67–74); the settings-registry mechanism (client.py:180–209) is live (shared mode); the redislite-PR-#194 lifecycle comments are provenance, not cruft; `_dummy.c` intentionally forces platform-tagged wheels.
**Recommendation:** None.
**Effort:** — · **Roadmap:** —

#### [T3-16] Consumer-position type-check is clean — **Info**
**Evidence:** A strict-mypy smoke file importing `Valkey`, `ValkeyServer`, `connect` (with `durable=`) type-checks clean; the only reported error was a bare `dict` annotation in the audit's own smoke file. Package-internal strict failures are T3-04.
**Recommendation:** None.
**Effort:** — · **Roadmap:** —

## Phased roadmap

### Before going public
- [ ] T1-01 Make the README install story true (publish, or rewrite Install) — *Blocker*
- [ ] T3-01 Fix pytest plugin double-registration + remove both stale egg-infos — *Blocker*
- [ ] T1-02 Create the GitHub repo the metadata points at
- [ ] T3-02 Rename `ValkeyLite*` exceptions (last cheap moment)
- [ ] T3-03 Implement the approved fixture redesign (fixture names are public API)
- [ ] T2-01 Workflow `permissions:` blocks
- [ ] T2-02 SHA-pin all actions
- [ ] T2-03 Publish job with trusted publishing
- [ ] T2-08 Fail-fast platform/toolchain preflight in the build
- [ ] T2-04 Settle the sdist contents story (runnable suite or none)
- [ ] T1-03 CONTRIBUTING.md · T1-04 CHANGELOG.md · T1-05 CODE_OF_CONDUCT.md · T1-06 SECURITY.md
- [ ] T1-12 Bundle statically-linked deps' license notices
- [ ] T1-13 README License section · T1-14 trademark attribution line
- [ ] T1-09 CI/license/python badges
- [ ] T1-18 Remove internal planning docs from the tree
- [ ] T3-07 One-time `ruff format` + CI format gate
- [ ] (from T1-11) S-sized "Versioning & support" README section

### Before v1.0
- [ ] T1-10 Coverage measurement + gate
- [ ] T1-11 Docs site
- [ ] T3-04 `mypy --strict` clean + CI gate
- [ ] T3-05 Docstring coverage ≥80% + interrogate gate
- [ ] T2-07 Full CI Python matrix (with T2-17's cache) · T2-17 concurrency + build cache
- [ ] T2-09 tarfile filter fallback · T2-10 harden/narrow the unpinned bypass
- [ ] T2-12 Single-source the version · T2-13 relative path in metadata JSON
- [ ] T2-14 dev/test extras · T2-15 classifiers · T2-06 project.urls · T2-19 sdist tools/ test
- [ ] T1-07 Issue/PR templates · T1-15 README API & errors section · T1-16 macOS floor
- [ ] T3-08 Class docstring cross-references · T3-09 error message rewrites · T3-12 CLI help/version
- [ ] T3-14 bandit config

### Aspirational
- [ ] T2-16 Broader wheel matrix (aarch64, Intel macOS, musllinux) + cp314
- [ ] T2-11 Immutable upstream release asset (upstream-dependent)
- [ ] T1-08 FUNDING.yml

## Dropped findings

None refuted — all 55 raw findings survived verification. 7 were merged as duplicates:
T1-17→T2-12 (version drift), T2-05+T3-06+T3-13→T2-04 (sdist/check-manifest/pyroma),
T3-10→T1-15 (README API gaps), T3-11→T3-03 (fixture naming folded into redesign),
T3-17→T2-01/T2-02 (zizmor results owned by Track 2). One evidence upgrade at
verification: T2-13's wheel was confirmed to embed an ephemeral `/tmp/build-via-sdist-*`
path, stronger than the original claim.
