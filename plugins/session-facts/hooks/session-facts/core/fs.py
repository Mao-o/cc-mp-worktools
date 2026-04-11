from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, List, Optional


def read_text(path: Path, limit: int = 120_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def safe_iterdir(path: Path) -> List[Path]:
    try:
        return sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []


def walk_files(root: Path, skip_dirs: Iterable[str], limit: int = 5000) -> List[str]:
    skip = set(skip_dirs)
    results: List[str] = []
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root_str)
            results.append(rel)
            if len(results) >= limit:
                return results
    return results
