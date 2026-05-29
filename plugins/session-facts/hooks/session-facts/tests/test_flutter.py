"""detectors/flutter.py: pubspec.yaml からの flutter/dart スタック検出 (#6)。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from core.context import AnalysisConfig, RepoContext
from detectors.flutter import FlutterDetector


def _detect(tmp, pubspec_content=None):
    root = Path(tmp)
    if pubspec_content is not None:
        (root / "pubspec.yaml").write_text(pubspec_content)
    ctx = RepoContext(root=root, config=AnalysisConfig())
    return FlutterDetector().detect(ctx)


class FlutterDetectorTest(unittest.TestCase):
    def test_flutter_app_detected_as_flutter_and_dart(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = _detect(tmp, "dependencies:\n  flutter:\n    sdk: flutter\n")
            self.assertEqual(stack, ["flutter", "dart"])

    def test_pure_dart_package_detected_as_dart_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = _detect(tmp, "name: mylib\ndependencies:\n  http: ^1.0.0\n")
            self.assertEqual(stack, ["dart"])

    def test_top_level_flutter_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack = _detect(tmp, "name: app\nflutter:\n  uses-material-design: true\n")
            self.assertEqual(stack, ["flutter", "dart"])

    def test_no_pubspec_no_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect(tmp, None), [])

    def test_subproject_pubspec_in_monorepo_detected(self):
        # Codex P2 regression: ctx.root is the git root, but the Flutter app
        # lives at apps/mobile/pubspec.yaml. The detector must scan tracked
        # pubspecs, not just the repo-root one.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "apps" / "mobile"
            sub.mkdir(parents=True)
            (sub / "pubspec.yaml").write_text("dependencies:\n  flutter:\n    sdk: flutter\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = ["apps/mobile/pubspec.yaml", "apps/mobile/lib/main.dart"]
            self.assertEqual(FlutterDetector().detect(ctx), ["flutter", "dart"])

    def test_subproject_pure_dart_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "packages" / "core"
            sub.mkdir(parents=True)
            (sub / "pubspec.yaml").write_text("name: core\ndependencies:\n  meta: ^1.0.0\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.tracked_files = ["packages/core/pubspec.yaml"]
            self.assertEqual(FlutterDetector().detect(ctx), ["dart"])


if __name__ == "__main__":
    unittest.main()
