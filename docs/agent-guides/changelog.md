# Changelog Rules

`CHANGELOG.md` follows Keep a Changelog and summarizes user-visible releases rather than
copying Git history.

- Add unreleased user-facing behavior under `## [Unreleased]` using `Added`, `Changed`,
  `Fixed`, `Deprecated`, `Removed`, or `Security` where useful.
- Describe the effect on users: lifecycle, persistence, compatibility, installation,
  packaging, fixtures, or security. Combine related implementation commits.
- Do not add entries for tests, internal refactors, formatting, or agent documentation
  unless they change supported behavior or contributor workflow materially.
- Link issue/PR numbers when useful, but make each bullet understandable without opening
  the link.
- Preserve existing hand-written context and SemVer/0.x caveats.
- At release time, move the curated Unreleased content under
  `## [X.Y.Z] - YYYY-MM-DD`, then leave a fresh `## [Unreleased]` section.
- Keep the `v` prefix for Git tags only; changelog headings use the plain version.
- Ensure README/version support claims and the changelog agree before release.
