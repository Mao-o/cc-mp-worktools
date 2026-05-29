"""collectors/dependencies.py: requirements/Pipfile/setup.cfg (#7) と
pubspec.yaml (#6) のパーサ + major dependency 収集の統合テスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.dependencies import (
    _collect_major_dependencies,
    parse_pipfile,
    parse_pubspec_deps,
    parse_requirements,
    parse_setup_cfg_requires,
)
from core.context import AnalysisConfig, RepoContext


class ParseRequirementsTest(unittest.TestCase):
    def test_operators_and_comments(self):
        text = """\
flask==3.0.0
celery>=5.3.0
SQLAlchemy~=2.0
redis==5.0.1
alembic
# comment line
-r base.txt
-e .
uvicorn[standard]==0.27.0
"""
        result = dict(parse_requirements(text))
        self.assertEqual(result["flask"], "3.0.0")
        self.assertEqual(result["celery"], "5.3.0")
        self.assertEqual(result["SQLAlchemy"], "2.0")
        self.assertEqual(result["alembic"], "")
        self.assertEqual(result["uvicorn"], "0.27.0")  # extras stripped
        self.assertNotIn("-r", result)
        self.assertNotIn("-e", result)

    def test_empty(self):
        self.assertEqual(parse_requirements(""), [])


class ParsePipfileTest(unittest.TestCase):
    def test_packages_and_dev_packages(self):
        text = """\
[[source]]
url = "https://pypi.org/simple"

[packages]
flask = "*"
celery = ">=5.0"
sqlalchemy = {version = "==1.4", extras = ["asyncio"]}

[dev-packages]
pytest = "*"
"""
        result = dict(parse_pipfile(text))
        self.assertIn("flask", result)
        self.assertEqual(result["flask"], "")  # "*" -> no version digit
        self.assertEqual(result["celery"], "5.0")
        self.assertEqual(result["sqlalchemy"], "1.4")
        self.assertIn("pytest", result)


class ParseSetupCfgTest(unittest.TestCase):
    def test_multiline_install_requires(self):
        text = """\
[metadata]
name = myapp

[options]
packages = find:
install_requires =
    flask>=2.0
    celery
    sqlalchemy==1.4

[options.extras_require]
dev =
    pytest
"""
        result = dict(parse_setup_cfg_requires(text))
        self.assertEqual(result["flask"], "2.0")
        self.assertEqual(result["celery"], "")
        self.assertEqual(result["sqlalchemy"], "1.4")
        # pytest is under extras_require, not install_requires -> excluded
        self.assertNotIn("pytest", result)


class ParsePubspecTest(unittest.TestCase):
    def test_deps_and_dev_deps_with_nested_sdk(self):
        text = """\
name: fxdict_app
environment:
  sdk: '>=3.0.0 <4.0.0'
dependencies:
  flutter:
    sdk: flutter
  firebase_core: ^2.24.0
  dio: ^5.4.0
dev_dependencies:
  flutter_test:
    sdk: flutter
  build_runner: ^2.4.7
"""
        result = dict(parse_pubspec_deps(text))
        self.assertEqual(result["firebase_core"], "^2.24.0")
        self.assertEqual(result["dio"], "^5.4.0")
        self.assertEqual(result["build_runner"], "^2.4.7")
        # flutter / flutter_test have nested 'sdk:' -> recorded with empty version,
        # and their nested 'sdk' line must NOT leak in as a dep.
        self.assertNotIn("sdk", result)
        self.assertEqual(result["flutter"], "")


class CollectMajorDependenciesTest(unittest.TestCase):
    def _ctx(self, tmp, files):
        root = Path(tmp)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        ctx = RepoContext(root=root, config=AnalysisConfig())
        ctx.tracked_files = list(files.keys())
        return ctx

    def test_requirements_in_subdir_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {
                "api/requirements.txt": "flask==3.0\ncelery>=5.3\nsqlalchemy==2.0\n",
                "api/app.py": "x=1\n",
            })
            deps = _collect_major_dependencies(ctx, max_items=8)
            self.assertIn("flask@3.0", deps)
            self.assertIn("celery@5.3", deps)
            self.assertIn("sqlalchemy@2.0", deps)

    def test_pyproject_wins_over_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {
                "pyproject.toml": 'flask = "2.0.0"\n',
                "requirements.txt": "flask==3.0.0\n",
            })
            deps = _collect_major_dependencies(ctx, max_items=8)
            # pyproject version (2.0) wins; requirements (3.0) is deduped out.
            self.assertIn("flask@2.0", deps)
            self.assertNotIn("flask@3.0", deps)

    def test_flutter_pubspec_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {
                "pubspec.yaml": (
                    "dependencies:\n"
                    "  flutter:\n    sdk: flutter\n"
                    "  firebase_core: ^2.24.0\n"
                    "  flutter_riverpod: ^2.4.9\n"
                ),
            })
            deps = _collect_major_dependencies(ctx, max_items=8)
            self.assertIn("firebase_core@2.24", deps)
            self.assertIn("flutter_riverpod@2.4", deps)

    def test_unmatched_deps_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {
                "requirements.txt": "some-internal-pkg==1.0\nflask==3.0\n",
            })
            deps = _collect_major_dependencies(ctx, max_items=8)
            self.assertIn("flask@3.0", deps)
            self.assertFalse(any("internal" in d for d in deps))

    def test_no_python_files_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {"src/index.ts": "export {}\n"})
            self.assertEqual(_collect_major_dependencies(ctx, max_items=8), [])


if __name__ == "__main__":
    unittest.main()
