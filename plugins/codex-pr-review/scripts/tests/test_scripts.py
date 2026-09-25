"""同梱スクリプトのテスト。gh は偽物 (呼び出しを記録するだけ) に差し替えて動かす。"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
_BASH = shutil.which("bash")

_FAKE_GH = """#!{python}
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
"""


@unittest.skipIf(_BASH is None or os.name == "nt", "bash が必要")
class ScriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(_FAKE_GH.format(python=sys.executable), encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "gh.log"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_LOG": str(self.log)}

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, name, *args, cwd=None):
        return subprocess.run(
            [_BASH, str(_SCRIPTS / name), *args],
            cwd=cwd, env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )

    def calls(self):
        import json

        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_all_scripts_parse(self):
        for script in sorted(_SCRIPTS.glob("*.sh")):
            with self.subTest(script=script.name):
                r = subprocess.run([_BASH, "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_trigger_posts_summary_then_bare_mention(self):
        r = self.run_script("pr-codex-trigger.sh", "12", "R1 の 2 件に対応しました")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.calls(),
            [
                ["pr", "comment", "12", "--body", "R1 の 2 件に対応しました"],
                ["pr", "comment", "12", "--body", "@codex review"],
            ],
        )

    def test_trigger_with_summary_file_and_without_summary(self):
        summary = self.tmp / "summary.md"
        summary.write_text("- fixed\n", encoding="utf-8")
        self.run_script("pr-codex-trigger.sh", "3", str(summary))
        self.run_script("pr-codex-trigger.sh", "3")
        self.assertEqual(
            self.calls(),
            [
                ["pr", "comment", "3", "--body-file", str(summary)],
                ["pr", "comment", "3", "--body", "@codex review"],
                ["pr", "comment", "3", "--body", "@codex review"],
            ],
        )

    def test_trigger_requires_pr_number(self):
        self.assertNotEqual(self.run_script("pr-codex-trigger.sh").returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_commit_safe_uses_message_file(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@e"],
                     ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        (repo / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        msg = self.tmp / "msg.txt"
        msg.write_text("feat: 日本語の件名\n\n本文\n", encoding="utf-8")
        r = self.run_script("pr-commit-safe.sh", str(msg), cwd=repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        subject = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(subject.strip(), "feat: 日本語の件名")
        self.assertFalse(msg.exists())  # --keep なしでは消す


if __name__ == "__main__":
    unittest.main()
