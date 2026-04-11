#!/usr/bin/env python3
"""Entry point for: python3 ~/.claude/hooks/session-facts [options]"""
from __future__ import annotations

import sys
from pathlib import Path

# Add this directory to sys.path so absolute imports work
# regardless of the directory name (handles hyphens, etc.)
_pkg_dir = str(Path(__file__).resolve().parent)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
