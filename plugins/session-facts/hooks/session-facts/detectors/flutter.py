from __future__ import annotations

import re
from typing import List

from core.context import RepoContext
from core.fs import read_text


class FlutterDetector:
    name = "flutter"
    priority = 25

    def detect(self, ctx: RepoContext) -> List[str]:
        pubspec = ctx.root / "pubspec.yaml"
        if not pubspec.exists():
            return []
        text = read_text(pubspec)
        found: List[str] = []
        # The Flutter SDK dependency (``sdk: flutter``) or a top-level
        # ``flutter:`` section distinguishes a Flutter app from a pure Dart
        # package. Either way it is a Dart project, so ``dart`` is always added.
        if re.search(r"(?m)^\s*sdk:\s*flutter\b", text) or re.search(r"(?m)^flutter:\s*$", text):
            found.append("flutter")
        found.append("dart")
        return found


def register():
    return FlutterDetector()
