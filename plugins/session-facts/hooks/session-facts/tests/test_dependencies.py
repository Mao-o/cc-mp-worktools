"""collectors/dependencies.py: requirements/Pipfile/setup.cfg (#7) と
pubspec.yaml (#6) のパーサ + major dependency 収集の統合テスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.dependencies import (
    _collect_major_dependencies,
    _is_dev_requirements,
    parse_pep621_deps,
    parse_pep621_optional_deps,
    parse_pipfile,
    parse_pipfile_grouped,
    parse_poetry_deps,
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
                # A bare `flask = "2.0.0"` is not under [project]/[tool.poetry],
                # so the table-scoped parser correctly ignores it; declare it
                # the realistic PEP 621 way instead.
                "pyproject.toml": '[project]\ndependencies = ["flask==2.0.0"]\n',
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

    def test_runtime_deps_surface_after_allowlist(self):
        # Hybrid behaviour (v0.6): a non-allow-list runtime dep is no longer
        # dropped — it surfaces as tier-1, after allow-list (tier-0) matches.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {
                "requirements.txt": "some-internal-pkg==1.0\nflask==3.0\n",
            })
            deps = _collect_major_dependencies(ctx, max_items=8)
            self.assertIn("flask@3.0", deps)
            self.assertIn("some-internal-pkg@1.0", deps)
            # allow-list (flask) sorts before the non-allow runtime dep.
            self.assertLess(deps.index("flask@3.0"), deps.index("some-internal-pkg@1.0"))

    def test_hybrid_order_allowlist_then_runtime_then_dev(self):
        # Regression for the original miss: a non-allow-list dep (kaggle)
        # declared in requirements must appear, ordered after allow-list
        # (fastapi) and before dev tooling (pytest).
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {
                "pyproject.toml": (
                    "[project]\n"
                    'dependencies = ["fastapi==0.110.0", "kaggle==1.6.0"]\n'
                    "[project.optional-dependencies]\n"
                    'dev = ["pytest==8.0.0"]\n'
                ),
            })
            deps = _collect_major_dependencies(ctx, max_items=8)
            self.assertIn("fastapi@0.110", deps)
            self.assertIn("kaggle@1.6", deps)
            self.assertIn("pytest@8.0", deps)
            self.assertLess(deps.index("fastapi@0.110"), deps.index("kaggle@1.6"))
            self.assertLess(deps.index("kaggle@1.6"), deps.index("pytest@8.0"))

    def test_tier0_not_dropped_when_collected_late(self):
        # A late allow-list match (Go go.mod) survives the cap even when many
        # earlier tier-1 Python deps would otherwise fill all slots.
        with tempfile.TemporaryDirectory() as tmp:
            reqs = "\n".join(f"pkg-{i}==1.0" for i in range(10))
            ctx = self._ctx(tmp, {
                "requirements.txt": reqs + "\n",
                "go.mod": "require (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n",
            })
            deps = _collect_major_dependencies(ctx, max_items=3)
            # gin (allow-list, tier-0) leads despite being collected last.
            self.assertEqual(deps[0], "gin@1.9")
            self.assertEqual(len(deps), 3)

    def test_no_python_files_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {"src/index.ts": "export {}\n"})
            self.assertEqual(_collect_major_dependencies(ctx, max_items=8), [])


class ParsePep621Test(unittest.TestCase):
    def test_multiline_extras_marker_comment(self):
        text = """\
[project]
name = "demo"
requires-python = ">=3.9"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]==0.27.0",  # asgi server
    "httpx; python_version >= '3.9'",
    'kaggle==1.6.0',
]
[tool.other]
dependencies = ["should-not-be-seen"]
"""
        result = dict(parse_pep621_deps(text))
        self.assertEqual(result["fastapi"], "0.110")
        self.assertEqual(result["uvicorn"], "0.27.0")  # extras stripped
        self.assertEqual(result["httpx"], "")  # env marker -> no version
        self.assertEqual(result["kaggle"], "1.6.0")
        # the array under [tool.other] is a different table -> excluded
        self.assertNotIn("should-not-be-seen", result)

    def test_inline_array(self):
        text = '[project]\ndependencies = ["flask==2.0.0", "redis"]\n'
        result = dict(parse_pep621_deps(text))
        self.assertEqual(result["flask"], "2.0.0")
        self.assertEqual(result["redis"], "")

    def test_dynamic_dependencies_returns_empty(self):
        text = '[project]\nname = "x"\ndynamic = ["dependencies"]\n'
        self.assertEqual(parse_pep621_deps(text), [])

    def test_no_project_table_returns_empty(self):
        self.assertEqual(parse_pep621_deps('[tool.poetry]\nname = "x"\n'), [])

    def test_optional_dependencies_flattened(self):
        text = """\
[project]
dependencies = ["flask"]
[project.optional-dependencies]
dev = [
    "pytest==8.0.0",
    "ruff",
]
docs = ["mkdocs"]
"""
        opt = dict(parse_pep621_optional_deps(text))
        self.assertEqual(opt["pytest"], "8.0.0")
        self.assertIn("ruff", opt)
        self.assertIn("mkdocs", opt)
        # the [project] dependencies array must not leak into optional parsing
        self.assertNotIn("flask", opt)


class ParsePoetryTest(unittest.TestCase):
    def test_runtime_dev_group_and_python_excluded(self):
        text = """\
[tool.poetry]
name = "demo"

[tool.poetry.dependencies]
python = "^3.11"
flask = "2.0.0"
requests = {version = "^2.28", optional = true}

[tool.poetry.dev-dependencies]
black = "^23.0"

[tool.poetry.group.test.dependencies]
pytest = "^8.0"
"""
        runtime, dev = parse_poetry_deps(text)
        runtime_d = dict(runtime)
        dev_d = dict(dev)
        self.assertEqual(runtime_d["flask"], "2.0.0")
        self.assertEqual(runtime_d["requests"], "^2.28")  # inline table version
        self.assertNotIn("python", runtime_d)  # python pin excluded
        # legacy dev-dependencies and modern group.* both land in dev
        self.assertIn("black", dev_d)
        self.assertIn("pytest", dev_d)
        # dev-group deps are NOT reported as runtime (the old latent bug)
        self.assertNotIn("pytest", runtime_d)
        self.assertNotIn("black", runtime_d)

    def test_no_poetry_tables_returns_empty(self):
        runtime, dev = parse_poetry_deps('[project]\nname = "x"\n')
        self.assertEqual(runtime, [])
        self.assertEqual(dev, [])


class ParsePipfileGroupedTest(unittest.TestCase):
    def test_groups_separated(self):
        text = """\
[packages]
flask = "*"
celery = ">=5.0"

[dev-packages]
pytest = "*"
"""
        grouped = parse_pipfile_grouped(text)
        pkg_names = {n for n, _ in grouped["packages"]}
        dev_names = {n for n, _ in grouped["dev-packages"]}
        self.assertEqual(pkg_names, {"flask", "celery"})
        self.assertEqual(dev_names, {"pytest"})

    def test_parse_pipfile_flattens_grouped(self):
        text = "[packages]\nflask = \"*\"\n[dev-packages]\npytest = \"*\"\n"
        names = {n for n, _ in parse_pipfile(text)}
        self.assertEqual(names, {"flask", "pytest"})


class IsDevRequirementsTest(unittest.TestCase):
    def test_plain_requirements_is_runtime(self):
        self.assertFalse(_is_dev_requirements("requirements.txt"))
        self.assertFalse(_is_dev_requirements("api/requirements.txt"))

    def test_dev_and_test_variants(self):
        self.assertTrue(_is_dev_requirements("requirements-dev.txt"))
        self.assertTrue(_is_dev_requirements("dev-requirements.txt"))
        self.assertTrue(_is_dev_requirements("requirements-test.txt"))
        self.assertTrue(_is_dev_requirements("requirements/dev.txt"))


class HybridDevTieringTest(unittest.TestCase):
    def _ctx(self, tmp, files):
        root = Path(tmp)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        ctx = RepoContext(root=root, config=AnalysisConfig())
        ctx.tracked_files = list(files.keys())
        return ctx

    def test_dev_requirements_file_tiers_after_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, {
                "requirements.txt": "kaggle==1.6.0\n",
                "requirements-dev.txt": "some-linter==1.0\n",
            })
            deps = _collect_major_dependencies(ctx, max_items=8)
            self.assertIn("kaggle@1.6", deps)
            self.assertIn("some-linter@1.0", deps)
            # runtime requirement sorts before the dev-requirements entry
            self.assertLess(deps.index("kaggle@1.6"), deps.index("some-linter@1.0"))


if __name__ == "__main__":
    unittest.main()
