# examples/07_debug_info.py
"""Environment diagnostics for bug reports.

Equivalent to `python -m valkey_embedded.debug`; prints valkey_embedded/Valkey versions,
the binary path, platform, and whether the embedded server is runnable.

Run:  python examples/07_debug_info.py
"""

from valkey_embedded.debug import print_debug_info


def main() -> None:
    print_debug_info()


if __name__ == "__main__":
    main()
