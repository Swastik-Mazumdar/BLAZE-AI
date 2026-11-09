#!/usr/bin/env python3
"""Double-clickable / PyInstaller entry point for Blaze AI."""

from blaze_ai.core.net import ensure_ca_bundle

# Do this before any network-capable module is imported so TLS downloads work
# inside the packaged app on macOS and Windows.
ensure_ca_bundle()

from blaze_ai.app import main

if __name__ == "__main__":
    raise SystemExit(main())
