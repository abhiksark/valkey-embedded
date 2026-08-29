# Git Workflow

`dev` is the integration branch and the base for normal development. `main` is the
release branch.

## Development

1. Inspect `git status --short --branch`, current branch, remote, and relevant recent
   history before editing.
2. Preserve unrelated modified/untracked files. Do not clean, reset, stash, or include
   them to obtain a clean tree.
3. Create a focused branch such as `fix/...`, `feat/...`, `test/...`, `docs/...`, or
   `refactor/...` unless the user explicitly directs work on the current branch.
4. Keep one coherent concern per branch/PR and use Conventional Commit messages in
   imperative mood (`fix:`, `test:`, `docs:`, `build:`, `ci:`, and so on).
5. Run targeted tests and required gates, inspect the final diff, then stage exact paths.
   Avoid `git add .` when unrelated files exist.
6. Commit only after verification. Open a pull request to `dev`; link the issue and state
   red-before-green regression evidence for bug fixes.

## Safety

- Do not commit generated binaries, `package_metadata.json`, `build/`, `dist/`, caches,
  virtual environments, coverage data, or unrelated local configuration.
- Do not amend, rebase, force-push, delete branches, merge a PR, or rewrite shared history
  without explicit approval.
- Do not push commits or tags merely because a local commit was requested; pushing is a
  separate operation.
- Never change issue/milestone state incidentally to a code commit unless requested.
- Before committing, use `git diff --check`, `git diff --cached`, and
  `git status --short` to verify scope.

## Releases

Promote reviewed changes from `dev` to `main` through a release pull request; do not use
`main` for day-to-day development. Release tags are created from the reviewed `main`
release commit. Version bumps, tags, and pushes follow
[Release Rules](releases.md) and always require explicit user approval.
