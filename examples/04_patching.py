# examples/04_patching.py
"""Drop-in patching: make stock `valkey.Valkey` start an embedded server.

Handy for testing code that constructs `valkey.Valkey()` directly — patch once
and every such call gets a private, auto-managed server.

Run:  python examples/04_patching.py
"""

import valkey

from valkey_embedded.patch import patch_valkey, unpatch_valkey


def main() -> None:
    patch_valkey()
    try:
        conn = valkey.Valkey()  # now embedded and auto-managed
        print("patched valkey.Valkey ping ->", conn.ping())
    finally:
        unpatch_valkey()  # restore the original stock class
    print("unpatched -> valkey.Valkey is the stock client again")


if __name__ == "__main__":
    main()
