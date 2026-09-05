"""core/pytest_config.py: python_files from the four config files
(internal backlog joa.30)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from core.pytest_config import DEFAULT_PYTHON_FILES, matches_python_files, python_files_patterns


class PythonFilesPatternsTest(unittest.TestCase):
    def test_default_when_nothing_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(python_files_patterns(Path(tmp)), DEFAULT_PYTHON_FILES)

    def test_pytest_ini_wins_over_pyproject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pytest.ini").write_text("[pytest]\npython_files = check_*.py spec_*.py\n")
            pyproject = "[tool.pytest.ini_options]\npython_files = [\"other_*.py\"]\n"
            self.assertEqual(python_files_patterns(root, pyproject), ("check_*.py", "spec_*.py"))

    def test_pyproject_array_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyproject = "[tool.pytest.ini_options]\npython_files = [\"a_*.py\", \"b_*.py\"]\n"
            self.assertEqual(python_files_patterns(Path(tmp), pyproject), ("a_*.py", "b_*.py"))

    def test_tox_ini_and_setup_cfg_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tox.ini").write_text("[tox]\nenvlist = py\n[pytest]\npython_files = t_*.py\n")
            self.assertEqual(python_files_patterns(root), ("t_*.py",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "setup.cfg").write_text("[tool:pytest]\npython_files = s_*.py\n")
            self.assertEqual(python_files_patterns(root), ("s_*.py",))

    def test_matches_uses_basename(self):
        self.assertTrue(matches_python_files("a/b/test_x.py", DEFAULT_PYTHON_FILES))
        self.assertTrue(matches_python_files("a/b/x_test.py", DEFAULT_PYTHON_FILES))
        self.assertFalse(matches_python_files("a/test_dir/x.py", DEFAULT_PYTHON_FILES))


if __name__ == "__main__":
    unittest.main()
