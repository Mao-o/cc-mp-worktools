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

    # --- exact dependency-name match, not substring: a manifest merely
    # containing "firebase-admin"/"firebase-functions" as a substring
    # (unrelated package name, comment, tool-config mention, ...) must not
    # tag the repo as Firebase. core/firebase.py's Python-dependency check
    # used to be a raw substring search over the whole
    # pyproject.toml/requirements.txt text. ---

    def test_pyproject_name_field_substring_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "firebase-admin-helper"\nversion = "0.1.0"\n'
            )
            self.assertEqual(_detect(root), [])

    def test_pyproject_mypy_override_mention_is_not_detected(self):
        # A tool config referencing the *import path* of a package the
        # project doesn't actually declare as a dependency must not count.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.mypy]\n'
                '[[tool.mypy.overrides]]\n'
                'module = "firebase_admin.*"\n'
                'ignore_missing_imports = true\n'
            )
            self.assertEqual(_detect(root), [])

    def test_requirements_similar_package_name_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("not-firebase-admin==1.0.0\n")
            self.assertEqual(_detect(root, tracked_files=["requirements.txt"]), [])

    def test_npm_similar_package_name_is_not_detected(self):
        # ctx.all_deps (core/context.py) is a dict keyed by the exact
        # package.json dependency name, so this side never had the
        # analogous substring hole; locks that in.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"dependencies": {"firebase-admin-helper": "^1.0.0"}})
            )
            self.assertEqual(_detect(root), [])

    # --- exact-match coverage across the common Python
    # dependency-declaration tables (PEP 621 / Poetry / PEP 735 / uv / PDM
    # / Hatch) -- each of these was a true positive under the old substring
    # search, so each must remain one under the new exact-name parser. ---

    def test_pyproject_optional_dependencies_group_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project.optional-dependencies]\nadmin = ["firebase-admin>=6.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_poetry_dependencies_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.poetry.dependencies]\npython = "^3.11"\nfirebase-admin = "^6.0"\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_poetry_legacy_dev_dependencies_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.poetry.dev-dependencies]\nfirebase-admin = "^6.0"\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_poetry_group_dependencies_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.poetry.group.dev.dependencies]\nfirebase-admin = "^6.0"\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_dependency_groups_pep735_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[dependency-groups]\ntest = ["firebase-admin>=6.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_dependency_groups_include_group_ref_is_skipped_not_crashed(self):
        # PEP 735 lets a group include another via {include-group = "..."};
        # resolving that reference is out of scope, but it must not crash --
        # the other, literal string entry in the same array is still read.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[dependency-groups]\n'
                'base = ["pytest"]\n'
                'test = [{include-group = "base"}, "firebase-admin"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_uv_dev_dependencies_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.uv]\ndev-dependencies = ["firebase-admin>=6.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_pdm_dev_dependencies_group_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.pdm.dev-dependencies]\ntest = ["firebase-admin>=6.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_hatch_env_dependencies_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[tool.hatch.envs.default]\ndependencies = ["firebase-admin>=6.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_python_firebase_functions_dependency_is_detected(self):
        # firebase-functions is also a real PyPI package (the Python Cloud
        # Functions runtime); it earns the general "firebase" tag the same
        # as firebase-admin, but not the npm-scoped "firebase-functions" tag
        # (has_firebase_functions() stays root-package.json-only by design).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["firebase-functions>=0.1.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_case_and_dot_variant_name_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\ndependencies = ["Firebase.Admin>=6.0"]\n'
            )
            self.assertEqual(_detect(root), ["firebase"])

    def test_requirements_extras_specifier_and_marker_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                'firebase-admin[async]==6.5.0; python_version >= "3.9"\n'
            )
            self.assertEqual(
                _detect(root, tracked_files=["requirements.txt"]), ["firebase"]
            )

    def test_requirements_underscore_variant_name_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("firebase_admin==6.5.0\n")
            self.assertEqual(
                _detect(root, tracked_files=["requirements.txt"]), ["firebase"]
            )

    def test_requirements_comment_and_option_lines_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "# a comment\n-r other.txt\nfirebase-admin==6.5.0  # needed\n"
            )
            self.assertEqual(
                _detect(root, tracked_files=["requirements.txt"]), ["firebase"]
            )

    # --- legacy VCS "#egg=" requirement lines: the line starts with "git+"
    # (or an editable "-e" flag), not a package-name token, so the ordinary
    # _DEP_NAME_RE-anchored extraction reads "git" off the scheme, or the
    # whole line is skipped outright as an option line ("-e ..."). The old,
    # pre-tomllib substring search still caught these via a bare
    # "firebase-admin" match anywhere in the manifest text; the structured
    # per-line parser reopened this as a narrow regression until the
    # "#egg=" fragment extraction below was added. ---

    def test_requirements_legacy_vcs_egg_fragment_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "git+https://example.com/x/firebase-admin.git#egg=firebase-admin\n"
            )
            self.assertEqual(
                _detect(root, tracked_files=["requirements.txt"]), ["firebase"]
            )

    def test_requirements_editable_legacy_vcs_egg_fragment_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "-e git+https://example.com/x/firebase-admin.git#egg=firebase-admin\n"
            )
            self.assertEqual(
                _detect(root, tracked_files=["requirements.txt"]), ["firebase"]
            )

    def test_requirements_legacy_vcs_egg_fragment_similar_name_is_not_detected(self):
        # Guards against reopening the substring hole through the back door:
        # the egg value itself must still match "firebase-admin" exactly,
        # not merely contain it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "git+https://example.com/x/something.git#egg=not-firebase-admin\n"
            )
            self.assertEqual(_detect(root, tracked_files=["requirements.txt"]), [])

    def test_requirements_legacy_vcs_egg_fragment_extras_and_marker_is_detected(self):
        # The egg name stops at the first "[" (extras) or "&" (a following
        # URL-query-style fragment, e.g. "&subdirectory=..."); also doubles
        # as underscore-variant normalization coverage.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "git+https://example.com/x/firebase_admin.git"
                "#egg=firebase_admin[extra]&subdirectory=x\n"
            )
            self.assertEqual(
                _detect(root, tracked_files=["requirements.txt"]), ["firebase"]
            )

    def test_requirements_commented_out_egg_fragment_is_not_detected(self):
        # A line that is itself a full comment (starts with "#") must not
        # be treated as an active dependency just because its text happens
        # to contain "#egg=" further along.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "# git+https://example.com/x/firebase-admin.git#egg=firebase-admin\n"
            )
            self.assertEqual(_detect(root, tracked_files=["requirements.txt"]), [])

    # --- malformed / pathological pyproject.toml must fold into "no
    # Python signal", not crash the detector or hide unrelated signals ---

    def test_pyproject_malformed_toml_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project\nname = "x"\n')
            self.assertEqual(_detect(root), [])

    def test_pyproject_malformed_toml_does_not_hide_other_firebase_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project\nname = "x"\n')
            (root / "firebase.json").write_text("{}")
            self.assertEqual(_detect(root), ["firebase"])

    def test_pyproject_pathological_nesting_does_not_crash(self):
        # tomllib itself raises RecursionError (not TOMLDecodeError) on
        # sufficiently deep nesting -- confirmed empirically against this
        # Python's tomllib. Must still fold into "not detected", not raise.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            depth = 3000
            text = "[project]\ndependencies = [" + "[" * depth + "]" * depth + "]\n"
            (root / "pyproject.toml").write_text(text)
            self.assertEqual(_detect(root), [])

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
