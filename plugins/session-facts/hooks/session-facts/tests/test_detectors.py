"""detectors/firebase.py: consolidated Firebase detection via
core.firebase.has_firebase()/has_firebase_functions() (internal backlog:
firebase.json/.firebaserc, firebase-admin, firebase-functions as its own
distinguishable tag, Python requirements/pyproject, and pubspec
firebase_core were previously only recognized by collectors/repo_notes.py's
separate, broader check -- this detector missed all of them and only ever
emitted a bare "firebase" tag)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

import _testutil  # noqa: F401  (sys.path 整備)

from core.context import AnalysisConfig, RepoContext
from core.firebase import has_firebase
from detectors.firebase import FirebaseDetector


def _detect(root: Path, tracked_files: Optional[List[str]] = None) -> List[str]:
    ctx = RepoContext(root=root, config=AnalysisConfig())
    if tracked_files is not None:
        ctx.tracked_files = tracked_files
    return FirebaseDetector().detect(ctx)


class FirebaseDetectorTest(unittest.TestCase):
    def test_no_signal_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_detect(Path(tmp)), [])

    # --- pre-existing behavior: must still work after the has_firebase()
    # consolidation ---

    def test_root_firebase_npm_dependency_still_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"firebase": "^10.0.0"}})
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_scoped_firebase_package_still_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"@firebase/app": "^0.10.0"}})
            )
            self.assertEqual(_detect(root), ["firebase"])

    # --- condition 1: firebase.json / .firebaserc existence ---

    def test_firebase_json_alone_is_detected(self):
        # internal backlog: a Cloud Functions-only repo's firebase-functions
        # dependency lives in functions/package.json, not the tracked root
        # one (not scanned) -- but firebase.json at the root is enough on
        # its own, so the repo still isn't missed entirely.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "firebase.json").write_text("{}")
            self.assertEqual(_detect(root), ["firebase"])

    def test_firebaserc_alone_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".firebaserc").write_text('{"projects": {"default": "x"}}')
            self.assertEqual(_detect(root), ["firebase"])

    # --- condition 2: firebase-admin / firebase-functions / @firebase/* npm deps ---

    def test_firebase_admin_npm_dependency_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"firebase-admin": "^12.0.0"}})
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_firebase_functions_dependency_adds_distinct_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"firebase-functions": "^4.0.0"}})
            )
            self.assertEqual(_detect(root), ["firebase", "firebase-functions"])

    def test_firebase_without_functions_dependency_has_no_functions_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"firebase": "^10.0.0"}})
            )
            self.assertEqual(_detect(root), ["firebase"])

    # --- condition 3: Python firebase-admin (pyproject.toml / requirements*.txt) ---

    def test_python_pyproject_firebase_admin_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["firebase-admin>=6.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_python_requirements_firebase_admin_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("firebase-admin==6.5.0\n")
            self.assertEqual(
                _detect(root, tracked_files=["requirements.txt"]), ["firebase"]
            )

    def test_python_requirements_variant_name_firebase_admin_is_detected(self):
        # collectors/dependencies.py's own requirements-file matcher already
        # treats any "requirements*.txt" basename as a requirements file
        # (not just the exact "requirements.txt"); this check mirrors that.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements-prod.txt").write_text("firebase-admin==6.5.0\n")
            self.assertEqual(
                _detect(root, tracked_files=["requirements-prod.txt"]), ["firebase"]
            )

    # --- condition 4: Flutter pubspec.yaml firebase_core ---

    def test_flutter_pubspec_firebase_core_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pubspec.yaml").write_text(
                "name: app\ndependencies:\n  firebase_core: ^2.0.0\n"
            )
            self.assertEqual(
                _detect(root, tracked_files=["pubspec.yaml"]), ["firebase"]
            )

    def test_unrelated_pubspec_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pubspec.yaml").write_text(
                "name: app\ndependencies:\n  http: ^1.0.0\n"
            )
            self.assertEqual(_detect(root, tracked_files=["pubspec.yaml"]), [])


class HasFirebaseMemoizationTest(unittest.TestCase):
    """has_firebase() runs on every hook invocation from two call sites
    (the always-on detector, and repo_notes.py which runs on any repo with
    tracked files); ctx.results caches the result so the Python-dependency
    scan (up to 6 requirements*.txt reads) does not repeat within one run."""

    def test_result_is_cached_on_ctx_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "firebase.json").write_text("{}")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            self.assertTrue(has_firebase(ctx))
            self.assertIs(ctx.results.get("has_firebase"), True)

    def test_cached_false_is_not_recomputed_as_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "firebase.json").write_text("{}")  # would be True if scanned
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.results["has_firebase"] = False
            self.assertFalse(has_firebase(ctx))

    def test_cached_true_short_circuits_recomputation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # no firebase signal on disk at all
            ctx = RepoContext(root=root, config=AnalysisConfig())
            ctx.results["has_firebase"] = True
            self.assertTrue(has_firebase(ctx))


if __name__ == "__main__":
    unittest.main()
