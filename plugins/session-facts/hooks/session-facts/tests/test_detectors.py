"""detectors/firebase.py: consolidated Firebase detection via
core.firebase.has_firebase()/has_firebase_functions() (internal backlog:
firebase.json/.firebaserc, firebase-admin, firebase-functions as its own
distinguishable tag, Python requirements/pyproject, and pubspec
firebase_core were previously only recognized by collectors/repo_notes.py's
separate, broader check -- this detector missed all of them and only ever
emitted a bare "firebase" tag).

The detection cases are tables: (label, {file name: content}, tracked_files,
expected tags). The label is the name the case had as a separate test.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import _testutil  # noqa: F401  (sys.path 整備)

from core.context import AnalysisConfig, RepoContext
from core.firebase import has_firebase
from detectors.firebase import FirebaseDetector


def _detect(root: Path, tracked_files: Optional[List[str]] = None) -> List[str]:
    ctx = RepoContext(root=root, config=AnalysisConfig())
    if tracked_files is not None:
        ctx.tracked_files = tracked_files
    return FirebaseDetector().detect(ctx)


Case = Tuple[str, Dict[str, Union[str, bytes]], Optional[List[str]], List[str]]


def _write(root: Path, files: Dict[str, Union[str, bytes]]) -> None:
    for name, content in files.items():
        if isinstance(content, bytes):
            (root / name).write_bytes(content)
        else:
            (root / name).write_text(content)


def _run_cases(test: unittest.TestCase, cases: Sequence[Case]) -> None:
    for label, files, tracked, expected in cases:
        with test.subTest(label):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write(root, files)
                test.assertEqual(_detect(root, tracked), expected)


_FB = ["firebase"]


class FirebaseDetectorTest(unittest.TestCase):
    def test_no_signal_root_manifests_and_npm_dependency_names(self):
        _run_cases(self, [
            ("test_no_signal_is_not_detected", {}, None, []),
            # --- pre-existing behavior: must still work after the
            # has_firebase() consolidation ---
            ("test_root_firebase_npm_dependency_still_detected",
             {"package.json": json.dumps({"dependencies": {"firebase": "^10.0.0"}})},
             None, _FB),
            ("test_scoped_firebase_package_still_detected",
             {"package.json": json.dumps({"dependencies": {"@firebase/app": "^0.10.0"}})},
             None, _FB),
            # --- condition 1: firebase.json / .firebaserc existence ---
            # internal backlog: a Cloud Functions-only repo's firebase-functions
            # dependency lives in functions/package.json, not the tracked root
            # one (not scanned) -- but firebase.json at the root is enough on
            # its own, so the repo still isn't missed entirely.
            ("test_firebase_json_alone_is_detected",
             {"firebase.json": "{}"}, None, _FB),
            ("test_firebaserc_alone_is_detected",
             {".firebaserc": '{"projects": {"default": "x"}}'}, None, _FB),
            # --- condition 2: firebase-admin / firebase-functions / @firebase/*
            # npm deps ---
            ("test_firebase_admin_npm_dependency_is_detected",
             {"package.json": json.dumps({"dependencies": {"firebase-admin": "^12.0.0"}})},
             None, _FB),
            ("test_firebase_functions_dependency_adds_distinct_tag",
             {"package.json": json.dumps({"dependencies": {"firebase-functions": "^4.0.0"}})},
             None, ["firebase", "firebase-functions"]),
            # ctx.all_deps (core/context.py) is a dict keyed by the exact
            # package.json dependency name, so this side never had the
            # substring hole the Python side had; locks that in.
            ("test_npm_similar_package_name_is_not_detected",
             {"package.json": json.dumps({"dependencies": {"firebase-admin-helper": "^1.0.0"}})},
             None, []),
        ])

    def test_python_dependencies_in_pyproject_and_requirements(self):
        _run_cases(self, [
            # --- condition 3: Python firebase-admin (pyproject.toml /
            # requirements*.txt) ---
            ("test_python_pyproject_firebase_admin_is_detected",
             {"pyproject.toml": '[project]\ndependencies = ["firebase-admin>=6.0"]\n'},
             None, _FB),
            ("test_python_requirements_firebase_admin_is_detected",
             {"requirements.txt": "firebase-admin==6.5.0\n"},
             ["requirements.txt"], _FB),
            # collectors/dependencies.py's own requirements-file matcher already
            # treats any "requirements*.txt" basename as a requirements file
            # (not just the exact "requirements.txt"); this check mirrors that.
            ("test_python_requirements_variant_name_firebase_admin_is_detected",
             {"requirements-prod.txt": "firebase-admin==6.5.0\n"},
             ["requirements-prod.txt"], _FB),
            # --- exact dependency-name match, not substring: a manifest merely
            # containing "firebase-admin"/"firebase-functions" as a substring
            # (unrelated package name, comment, tool-config mention, ...) must
            # not tag the repo as Firebase. core/firebase.py's Python-dependency
            # check used to be a raw substring search over the whole
            # pyproject.toml/requirements.txt text. ---
            ("test_pyproject_name_field_substring_is_not_detected",
             {"pyproject.toml":
              '[project]\nname = "firebase-admin-helper"\nversion = "0.1.0"\n'},
             None, []),
            # A tool config referencing the *import path* of a package the
            # project doesn't actually declare as a dependency must not count.
            ("test_pyproject_mypy_override_mention_is_not_detected",
             {"pyproject.toml":
              '[tool.mypy]\n'
              '[[tool.mypy.overrides]]\n'
              'module = "firebase_admin.*"\n'
              'ignore_missing_imports = true\n'},
             None, []),
            ("test_requirements_similar_package_name_is_not_detected",
             {"requirements.txt": "not-firebase-admin==1.0.0\n"},
             ["requirements.txt"], []),
            # --- exact-match coverage across the common Python
            # dependency-declaration tables (PEP 621 / Poetry / PEP 735 / uv /
            # PDM / Hatch) -- each of these was a true positive under the old
            # substring search, so each must remain one under the new
            # exact-name parser. ---
            ("test_pyproject_optional_dependencies_group_is_detected",
             {"pyproject.toml":
              '[project.optional-dependencies]\nadmin = ["firebase-admin>=6.0"]\n'},
             None, _FB),
            ("test_pyproject_poetry_dependencies_is_detected",
             {"pyproject.toml":
              '[tool.poetry.dependencies]\npython = "^3.11"\nfirebase-admin = "^6.0"\n'},
             None, _FB),
            ("test_pyproject_poetry_legacy_dev_dependencies_is_detected",
             {"pyproject.toml":
              '[tool.poetry.dev-dependencies]\nfirebase-admin = "^6.0"\n'},
             None, _FB),
            ("test_pyproject_poetry_group_dependencies_is_detected",
             {"pyproject.toml":
              '[tool.poetry.group.dev.dependencies]\nfirebase-admin = "^6.0"\n'},
             None, _FB),
            ("test_pyproject_dependency_groups_pep735_is_detected",
             {"pyproject.toml":
              '[dependency-groups]\ntest = ["firebase-admin>=6.0"]\n'},
             None, _FB),
            # PEP 735 lets a group include another via {include-group = "..."};
            # resolving that reference is out of scope, but it must not crash --
            # the other, literal string entry in the same array is still read.
            ("test_pyproject_dependency_groups_include_group_ref_is_skipped_not_crashed",
             {"pyproject.toml":
              '[dependency-groups]\n'
              'base = ["pytest"]\n'
              'test = [{include-group = "base"}, "firebase-admin"]\n'},
             None, _FB),
            ("test_pyproject_uv_dev_dependencies_is_detected",
             {"pyproject.toml":
              '[tool.uv]\ndev-dependencies = ["firebase-admin>=6.0"]\n'},
             None, _FB),
            ("test_pyproject_pdm_dev_dependencies_group_is_detected",
             {"pyproject.toml":
              '[tool.pdm.dev-dependencies]\ntest = ["firebase-admin>=6.0"]\n'},
             None, _FB),
            ("test_pyproject_hatch_env_dependencies_is_detected",
             {"pyproject.toml":
              '[tool.hatch.envs.default]\ndependencies = ["firebase-admin>=6.0"]\n'},
             None, _FB),
            # firebase-functions is also a real PyPI package (the Python Cloud
            # Functions runtime); it earns the general "firebase" tag the same
            # as firebase-admin, but not the npm-scoped "firebase-functions"
            # tag (has_firebase_functions() stays root-package.json-only by
            # design).
            ("test_pyproject_python_firebase_functions_dependency_is_detected",
             {"pyproject.toml":
              '[project]\ndependencies = ["firebase-functions>=0.1.0"]\n'},
             None, _FB),
            ("test_pyproject_case_and_dot_variant_name_is_detected",
             {"pyproject.toml": '[project]\ndependencies = ["Firebase.Admin>=6.0"]\n'},
             None, _FB),
            ("test_requirements_extras_specifier_and_marker_is_detected",
             {"requirements.txt":
              'firebase-admin[async]==6.5.0; python_version >= "3.9"\n'},
             ["requirements.txt"], _FB),
            ("test_requirements_underscore_variant_name_is_detected",
             {"requirements.txt": "firebase_admin==6.5.0\n"},
             ["requirements.txt"], _FB),
            ("test_requirements_comment_and_option_lines_are_ignored",
             {"requirements.txt":
              "# a comment\n-r other.txt\nfirebase-admin==6.5.0  # needed\n"},
             ["requirements.txt"], _FB),
        ])

    def test_requirements_legacy_vcs_egg_fragments(self):
        # Legacy VCS "#egg=" requirement lines: the line starts with "git+"
        # (or an editable "-e" flag), not a package-name token, so the
        # ordinary _DEP_NAME_RE-anchored extraction reads "git" off the
        # scheme, or the whole line is skipped outright as an option line
        # ("-e ..."). The old, pre-tomllib substring search still caught
        # these via a bare "firebase-admin" match anywhere in the manifest
        # text; the structured per-line parser reopened this as a narrow
        # regression until the "#egg=" fragment extraction was added.
        reqs = ["requirements.txt"]
        _run_cases(self, [
            ("test_requirements_legacy_vcs_egg_fragment_is_detected",
             {"requirements.txt":
              "git+https://example.com/x/firebase-admin.git#egg=firebase-admin\n"},
             reqs, _FB),
            ("test_requirements_editable_legacy_vcs_egg_fragment_is_detected",
             {"requirements.txt":
              "-e git+https://example.com/x/firebase-admin.git#egg=firebase-admin\n"},
             reqs, _FB),
            # Guards against reopening the substring hole through the back
            # door: the egg value itself must still match "firebase-admin"
            # exactly, not merely contain it.
            ("test_requirements_legacy_vcs_egg_fragment_similar_name_is_not_detected",
             {"requirements.txt":
              "git+https://example.com/x/something.git#egg=not-firebase-admin\n"},
             reqs, []),
            # The egg name stops at the first "[" (extras) or "&" (a following
            # URL-query-style fragment, e.g. "&subdirectory=..."); also doubles
            # as underscore-variant normalization coverage.
            ("test_requirements_legacy_vcs_egg_fragment_extras_and_marker_is_detected",
             {"requirements.txt":
              "git+https://example.com/x/firebase_admin.git"
              "#egg=firebase_admin[extra]&subdirectory=x\n"},
             reqs, _FB),
            # A line that is itself a full comment (starts with "#") must not
            # be treated as an active dependency just because its text happens
            # to contain "#egg=" further along.
            ("test_requirements_commented_out_egg_fragment_is_not_detected",
             {"requirements.txt":
              "# git+https://example.com/x/firebase-admin.git#egg=firebase-admin\n"},
             reqs, []),
        ])

    def test_malformed_pyproject_folds_into_no_python_signal(self):
        # Malformed / pathological pyproject.toml must fold into "no Python
        # signal", not crash the detector or hide unrelated signals.
        #
        # tomllib itself raises RecursionError (not TOMLDecodeError) on
        # sufficiently deep nesting -- confirmed empirically against this
        # Python's tomllib. Must still fold into "not detected", not raise.
        depth = 3000
        _run_cases(self, [
            ("test_pyproject_malformed_toml_does_not_crash",
             {"pyproject.toml": '[project\nname = "x"\n'}, None, []),
            ("test_pyproject_malformed_toml_does_not_hide_other_firebase_signal",
             {"pyproject.toml": '[project\nname = "x"\n', "firebase.json": "{}"},
             None, _FB),
            ("test_pyproject_pathological_nesting_does_not_crash",
             {"pyproject.toml":
              "[project]\ndependencies = [" + "[" * depth + "]" * depth + "]\n"},
             None, []),
        ])

    def test_flutter_pubspec_firebase_core(self):
        pub = ["pubspec.yaml"]
        _run_cases(self, [
            # --- condition 4: Flutter pubspec.yaml firebase_core ---
            ("test_flutter_pubspec_firebase_core_is_detected",
             {"pubspec.yaml": "name: app\ndependencies:\n  firebase_core: ^2.0.0\n"},
             pub, _FB),
            ("test_unrelated_pubspec_is_not_detected",
             {"pubspec.yaml": "name: app\ndependencies:\n  http: ^1.0.0\n"},
             pub, []),
            ("test_dev_dependencies_pubspec_firebase_core_is_detected",
             {"pubspec.yaml": "name: app\ndev_dependencies:\n  firebase_core: ^2.0.0\n"},
             pub, _FB),
            ("test_dependency_overrides_pubspec_firebase_core_is_detected",
             {"pubspec.yaml":
              "name: app\n"
              "dependencies:\n"
              "  http: ^1.0.0\n"
              "dependency_overrides:\n"
              "  firebase_core: ^2.0.0\n"},
             pub, _FB),
            # --- internal backlog: the pre-fix regex (``^\s*firebase_core\s*:``,
            # multiline) matched "firebase_core:" key syntax on any line of the
            # file, independent of which YAML section -- or whether any real
            # section -- it fell under. Reproduced empirically against the
            # pre-fix core/firebase.py: both of the first two cases below
            # returned ["firebase"] (must be []). (A single-line flow-style
            # variant of the same idea, e.g.
            # ``custom_tool: { firebase_core: disabled }``, was checked too and
            # does NOT reproduce under the old regex -- "firebase_core" there is
            # not at the start of a line, so ``^\s*firebase_core`` never matched
            # it. The block-style shapes are the actual reproduction.) ---
            ("test_firebase_core_under_unrelated_top_level_section_is_not_detected",
             {"pubspec.yaml":
              "name: app\n"
              "dependencies:\n"
              "  http: ^1.0.0\n"
              "custom_tool:\n"
              "  firebase_core: disabled\n"},
             pub, []),
            ("test_firebase_core_inside_flutter_section_is_not_detected",
             {"pubspec.yaml":
              "name: app\n"
              "dependencies:\n"
              "  http: ^1.0.0\n"
              "flutter:\n"
              "  firebase_core: something\n"},
             pub, []),
            # "firebase_core" appears only as a path *value* substring, one
            # indent level deeper than dependencies:'s direct children (it is
            # config for the sibling "some_pkg" dependency, not a dependency
            # name).
            ("test_firebase_core_nested_under_sibling_dependency_is_not_detected",
             {"pubspec.yaml":
              "name: app\n"
              "dependencies:\n"
              "  some_pkg:\n"
              "    path: ../firebase_core\n"},
             pub, []),
            # A "dependencies:" key only starts a real pubspec section at
            # column 0; the same key name nested under an unrelated top-level
            # section (flutter:) is that section's own config, not a dependency
            # manifest section, and must not be scanned as one.
            ("test_firebase_core_dependencies_key_nested_under_unrelated_section_is_not_detected",
             {"pubspec.yaml":
              "name: app\n"
              "flutter:\n"
              "  dependencies:\n"
              "    firebase_core: ^2.0.0\n"},
             pub, []),
            ("test_commented_out_firebase_core_pubspec_entry_is_not_detected",
             {"pubspec.yaml":
              "name: app\n"
              "dependencies:\n"
              "  # firebase_core: ^2.0.0\n"
              "  http: ^1.0.0\n"},
             pub, []),
            # A sibling dependency's own nested multi-line config (git:/url:,
            # deeper than the section's dependency-entry indent) must not shift
            # what that indent is for a later, real sibling at the original
            # depth.
            ("test_firebase_core_after_sibling_nested_block_is_still_detected",
             {"pubspec.yaml":
              "name: app\n"
              "dependencies:\n"
              "  some_pkg:\n"
              "    git:\n"
              "      url: https://example.com/x/some_pkg.git\n"
              "  firebase_core: ^2.0.0\n"},
             pub, _FB),
            ("test_mixed_tab_and_space_indentation_across_sections_is_detected",
             {"pubspec.yaml":
              "name: app\n"
              "dependencies:\n"
              "  http: ^1.0.0\n"
              "dev_dependencies:\n"
              "\tfirebase_core: ^2.0.0\n"},
             pub, _FB),
            ("test_crlf_line_endings_pubspec_firebase_core_is_detected",
             {"pubspec.yaml":
              b"name: app\r\ndependencies:\r\n  firebase_core: ^2.0.0\r\n"},
             pub, _FB),
        ])


class HasFirebaseMemoizationTest(unittest.TestCase):
    """has_firebase() runs on every hook invocation from two call sites
    (the always-on detector, and repo_notes.py which runs on any repo with
    tracked files); ctx.results caches the result so the Python-dependency
    scan (up to 6 requirements*.txt reads) does not repeat within one run.

    Checked through behavior (the disk changes between the two calls), not
    by reading the cache key."""

    def test_true_result_is_reused_after_the_manifest_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "firebase.json").write_text("{}")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            self.assertTrue(has_firebase(ctx))
            (root / "firebase.json").unlink()
            self.assertTrue(has_firebase(ctx))

    def test_false_result_is_not_recomputed_as_true_after_a_manifest_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RepoContext(root=root, config=AnalysisConfig())
            self.assertFalse(has_firebase(ctx))
            (root / "firebase.json").write_text("{}")  # would be True if rescanned
            self.assertFalse(has_firebase(ctx))

    def test_memo_does_not_outlive_its_context(self):
        # The memo is per run (one RepoContext), not process-wide: a fresh
        # context on the same root rescans.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(has_firebase(RepoContext(root=root, config=AnalysisConfig())))
            (root / "firebase.json").write_text("{}")
            self.assertTrue(has_firebase(RepoContext(root=root, config=AnalysisConfig())))


if __name__ == "__main__":
    unittest.main()
