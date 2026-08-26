"""Canonical import wrapper for the frozen Desktop Engine executable."""

from __future__ import annotations

from openkb.desktop_engine import main

if __name__ == "__main__":
    raise SystemExit(main())
