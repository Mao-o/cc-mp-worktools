from __future__ import annotations

import re
from pathlib import Path
from typing import List

from core.context import RepoContext
from core.fs import read_text

_FLUTTER_SDK_RE = re.compile(r"(?m)^\s*sdk:\s*flutter\b")
_FLUTTER_SECTION_RE = re.compile(r"(?m)^flutter:\s*$")
_MAX_PUBSPEC_SCAN = 12


class FlutterDetector:
    name = "flutter"
    priority = 25

    def detect(self, ctx: RepoContext) -> List[str]:
        # Scan every tracked pubspec.yaml, not just the repo-root one: in a
        # monorepo the Flutter app often lives at apps/<name>/pubspec.yaml while
        # ctx.root is the git root. This keeps the stack detector consistent
        # with the dependency collector, which already scans tracked pubspecs.
        pubspec_paths = [p for p in ctx.tracked_files if Path(p).name == "pubspec.yaml"]
        if not pubspec_paths and (ctx.root / "pubspec.yaml").exists():
            pubspec_paths = ["pubspec.yaml"]
        if not pubspec_paths:
            return []

        is_flutter = False
        for rel in pubspec_paths[:_MAX_PUBSPEC_SCAN]:
            text = read_text(ctx.root / rel)
            # The Flutter SDK dependency (``sdk: flutter``) or a top-level
            # ``flutter:`` section distinguishes a Flutter app from a pure Dart
            # package.
            if _FLUTTER_SDK_RE.search(text) or _FLUTTER_SECTION_RE.search(text):
                is_flutter = True
                break

        found: List[str] = []
        if is_flutter:
            found.append("flutter")
        found.append("dart")  # any pubspec.yaml means it is a Dart project
        return found


def register():
    return FlutterDetector()
