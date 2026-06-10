# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| latest 0.x release | yes |
| older releases | no |

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, use
GitHub's private vulnerability reporting: on the repository page, go to
**Security → Report a vulnerability**. You can expect an acknowledgement
within 7 days.

Reports of particular interest for this package:

- Anything that weakens the build-time supply chain (the SHA-256-pinned
  Valkey tarball download in `tools/build_valkey.py`).
- Socket-permission or data-exposure issues in the embedded server defaults
  (`Valkey()` is unix-socket-only with `unixsocketperm 700` by design).
- Vulnerabilities in the bundled, statically linked `valkey-server` binary —
  these are usually upstream Valkey issues; report them upstream as well, and
  here so a rebuilt release can ship promptly.
