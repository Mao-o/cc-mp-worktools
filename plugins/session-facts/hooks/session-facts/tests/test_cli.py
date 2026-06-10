"""cli レベルのテスト (v0.5): --emit subagent-json (.1)、--no-recent-commits (.2)、
purpose dirname fallback 省略 (.3)。"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from cli import main


def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _make_repo(tmp) -> Path:
    root = Path(tmp)
    _git(["init", "-b", "main"], root)
    (root / "a.txt").write_text("1\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "first commit"], root)
    return root


def _run_cli(argv) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    assert rc == 0
    return buf.getvalue()


class EmitSubagentJsonTest(unittest.TestCase):
    def test_wraps_output_in_hook_specific_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root), "--emit", "subagent-json"])
            payload = json.loads(out)
            hso = payload["hookSpecificOutput"]
            self.assertEqual(hso["hookEventName"], "SubagentStart")
            self.assertIn("## Project Facts", hso["additionalContext"])

    def test_default_emit_is_plain_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root)])
            self.assertTrue(out.startswith("## Project Facts"))


class NoRecentCommitsFlagTest(unittest.TestCase):
    def test_flag_suppresses_recent_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root), "--no-recent-commits"])
            self.assertNotIn("recent_commits", out)

    def test_default_keeps_recent_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root)])
            self.assertIn("- recent_commits:", out)
            self.assertIn("first commit", out)


class PurposeFallbackTest(unittest.TestCase):
    def test_dirname_fallback_omits_purpose_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("- purpose:", out)

    def test_package_json_description_still_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"name": "x", "description": "Does something useful"})
            )
            out = _run_cli(["--root", str(root)])
            self.assertIn("- purpose: Does something useful", out)


if __name__ == "__main__":
    unittest.main()
