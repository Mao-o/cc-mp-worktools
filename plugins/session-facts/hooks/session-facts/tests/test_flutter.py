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


if __name__ == "__main__":
    unittest.main()
