"""同梱スクリプトのテスト。gh は偽物 (呼び出しを記録するだけ) に差し替えて動かす。"""
from __future__ import annotations

import json
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
if sys.argv[1:3] == ["pr", "view"] and os.environ.get("FAKE_HEAD"):
    print(os.environ["FAKE_HEAD"])
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

    def comments(self):
        return [c for c in self.calls() if c[:2] == ["pr", "comment"]]

    def test_trigger_posts_summary_then_bare_mention(self):
        # サマリには依頼時の head を HTML コメントで書き添え、@codex review は単独で送る
        self.env["FAKE_HEAD"] = "abc123def456"
        r = self.run_script("pr-codex-trigger.sh", "12", "R1 の 2 件に対応しました")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.comments(),
            [
                ["pr", "comment", "12", "--body", "R1 の 2 件に対応しました\n\n<!-- codex-review-head: abc123def456 -->"],
                ["pr", "comment", "12", "--body", "@codex review"],
            ],
        )

    def test_trigger_with_summary_file_and_without_summary(self):
        summary = self.tmp / "summary.md"
        summary.write_text("- fixed\n", encoding="utf-8")
        self.env["FAKE_HEAD"] = "abc123def456"
        self.run_script("pr-codex-trigger.sh", "3", str(summary))
        self.run_script("pr-codex-trigger.sh", "3")
        self.assertEqual(
            self.comments(),
            [
                ["pr", "comment", "3", "--body", "- fixed\n\n<!-- codex-review-head: abc123def456 -->"],
                ["pr", "comment", "3", "--body", "@codex review"],
                ["pr", "comment", "3", "--body", "レビュー対象の head: `abc123def456`\n<!-- codex-review-head: abc123def456 -->"],
                ["pr", "comment", "3", "--body", "@codex review"],
            ],
        )

    def test_trigger_without_head_posts_only_what_was_given(self):
        # head が取れないとき (gh の失敗など) は書き添えを省く
        self.run_script("pr-codex-trigger.sh", "4", "summary")
        self.run_script("pr-codex-trigger.sh", "4")
        self.assertEqual(
            self.comments(),
            [
                ["pr", "comment", "4", "--body", "summary"],
                ["pr", "comment", "4", "--body", "@codex review"],
                ["pr", "comment", "4", "--body", "@codex review"],
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



# status 用の偽 gh: URL ごとに用意した JSON を返し、--jq は本物の jq で評価する
_STATUS_GH = """#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
if args[:2] == ["repo", "view"]:
    print("o/r"); sys.exit(0)
if args[:1] != ["api"]:
    sys.exit(0)
url = next(a for a in args[1:] if a.startswith("repos/")).split("?")[0]
data = json.load(open(os.environ["FAKE_GH_DATA"], encoding="utf-8")).get(url, [])
if "--jq" in args:
    expr = args[args.index("--jq") + 1]
    r = subprocess.run(["jq", "-r", expr], input=json.dumps(data), capture_output=True, text=True)
    sys.stdout.write(r.stdout)
else:
    print(json.dumps(data))
"""

_BOT = {"login": "chatgpt-codex-connector[bot]"}
_CI = {"login": "ci-helper[bot]"}
_ME = {"login": "me"}


@unittest.skipIf(_BASH is None or os.name == "nt" or shutil.which("jq") is None, "bash と jq が必要")
class StatusVerdictTest(unittest.TestCase):
    """最新の @codex review 以降の Codex の応答だけで verdict を出す。"""

    def verdict(self, data: dict) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(_STATUS_GH.format(python=sys.executable), encoding="utf-8")
            gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
            fixture = Path(tmp) / "data.json"
            fixture.write_text(json.dumps(data), encoding="utf-8")
            env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_DATA": str(fixture)}
            r = subprocess.run([_BASH, str(_SCRIPTS / "pr-codex-status.sh"), "5"], env=env, capture_output=True,
                               text=True, encoding="utf-8", timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return next(line for line in r.stdout.splitlines() if line.startswith("Codex verdict:"))

    @staticmethod
    def base(**over) -> dict:
        data = {
            "repos/o/r/issues/5/comments": [
                {"id": 1, "body": "@codex review", "created_at": "2026-01-02T00:00:00Z", "user": _ME},
            ],
            "repos/o/r/issues/5/reactions": [
                # 前のサイクルの 👍 が PR body に残っている
                {"content": "+1", "created_at": "2026-01-01T00:00:00Z", "user": _BOT},
            ],
            "repos/o/r/issues/comments/1/reactions": [],
            "repos/o/r/pulls/5/reviews": [],
            "repos/o/r/pulls/5/comments": [],
            "repos/o/r/pulls/5": {"created_at": "2025-12-31T00:00:00Z", "head": {"sha": "abc1234"}},
            "repos/o/r/commits/abc1234": {"commit": {"committer": {"date": "2026-01-01T12:00:00Z"}}},
        }
        data.update(over)
        return data

    def test_old_thumbs_up_does_not_pass_a_new_cycle(self):
        self.assertIn("NO REACTION", self.verdict(self.base()))
        eyes = [{"content": "eyes", "created_at": "2026-01-02T00:01:00Z", "user": _BOT}]
        self.assertIn("PROCESSING", self.verdict(self.base(**{"repos/o/r/issues/comments/1/reactions": eyes})))

    def test_new_cycle_results(self):
        up = [{"content": "+1", "created_at": "2026-01-02T00:05:00Z", "user": _BOT}]
        self.assertIn("PASSED", self.verdict(self.base(**{"repos/o/r/issues/comments/1/reactions": up})))
        review = [{"id": 9, "submitted_at": "2026-01-02T00:05:00Z", "state": "COMMENTED", "commit_id": "abc", "user": _BOT}]
        self.assertIn("REVIEWED", self.verdict(self.base(**{"repos/o/r/pulls/5/reviews": review})))

    def test_reactions_on_other_surfaces_count(self):
        # 最新サイクルの 👍 が trigger 以外のコメントや inline comment に付いても拾う
        up = [{"content": "+1", "created_at": "2026-01-02T00:05:00Z", "user": _BOT}]
        old = [{"content": "+1", "created_at": "2026-01-01T00:05:00Z", "user": _BOT}]
        comments = [*self.base()["repos/o/r/issues/5/comments"],
                    {"id": 7, "body": "summary", "created_at": "2026-01-02T00:00:30Z", "user": _ME}]
        self.assertIn("PASSED", self.verdict(self.base(**{
            "repos/o/r/issues/5/comments": comments, "repos/o/r/issues/comments/7/reactions": up})))
        inline = [{"id": 8, "created_at": "2026-01-01T00:00:00Z", "commit_id": "abc", "path": "x", "line": 1, "body": "b"}]
        self.assertIn("PASSED", self.verdict(self.base(**{
            "repos/o/r/pulls/5/comments": inline, "repos/o/r/pulls/comments/8/reactions": up})))
        # 前のサイクルのものは数えない
        self.assertIn("NO REACTION", self.verdict(self.base(**{
            "repos/o/r/pulls/5/comments": inline, "repos/o/r/pulls/comments/8/reactions": old})))

    def test_push_after_review_is_stale(self):
        # 👍 の後に commit を push したら、再 trigger するまで PASSED にしない
        up = [{"content": "+1", "created_at": "2026-01-02T00:05:00Z", "user": _BOT}]
        pushed = {"repos/o/r/issues/comments/1/reactions": up,
                  "repos/o/r/commits/abc1234": {"commit": {"committer": {"date": "2026-01-02T00:10:00Z"}}}}
        self.assertIn("STALE", self.verdict(self.base(**pushed)))
        # 最新の head に対する Codex の review があれば古くない
        review = [{"id": 9, "submitted_at": "2026-01-02T00:20:00Z", "state": "COMMENTED", "commit_id": "abc1234", "user": _BOT}]
        self.assertIn("REVIEWED", self.verdict(self.base(**pushed, **{"repos/o/r/pulls/5/reviews": review})))

    def test_requested_head_decides_staleness(self):
        # 依頼時の head (サマリの書き添え) と現在の head が違えば、commit の日時が古くても STALE
        up = [{"content": "+1", "created_at": "2026-01-02T00:05:00Z", "user": _BOT}]
        def with_marker(sha):
            summary = {"id": 0, "body": f"R1 対応\n\n<!-- codex-review-head: {sha} -->",
                       "created_at": "2026-01-01T23:59:59Z", "user": _ME}
            return self.base(**{"repos/o/r/issues/5/comments": [summary, *self.base()["repos/o/r/issues/5/comments"]],
                                "repos/o/r/issues/comments/1/reactions": up})
        self.assertIn("STALE", self.verdict(with_marker("def5678")))
        self.assertIn("PASSED", self.verdict(with_marker("abc1234")))

    def test_same_second_error_counts(self):
        comments = self.base()["repos/o/r/issues/5/comments"]
        err = {"id": 2, "body": "Unknown error", "created_at": "2026-01-02T00:00:00Z", "user": _BOT}
        self.assertIn("ERROR", self.verdict(self.base(**{"repos/o/r/issues/5/comments": [*comments, err]})))

    def test_instant_reaction_on_trigger_counts(self):
        # trigger と同じ秒に付いた trigger への 👍 も数える
        up = [{"content": "+1", "created_at": "2026-01-02T00:00:00Z", "user": _BOT}]
        self.assertIn("PASSED", self.verdict(self.base(**{"repos/o/r/issues/comments/1/reactions": up})))

    def test_errors_only_from_the_connector(self):
        comments = self.base()["repos/o/r/issues/5/comments"]
        codex_err = {"id": 2, "body": "Unknown error", "created_at": "2026-01-02T00:01:00Z", "user": _BOT}
        ci_ok = {"id": 3, "body": "build ok", "created_at": "2026-01-02T00:02:00Z", "user": _CI}
        ci_limit = {"id": 4, "body": "rate limit exceeded", "created_at": "2026-01-02T00:03:00Z", "user": _CI}
        # 後から来た他の bot のコメントで Codex のエラーが隠れない
        self.assertIn("ERROR", self.verdict(self.base(**{"repos/o/r/issues/5/comments": [*comments, codex_err, ci_ok]})))
        # 他の bot の rate limit を Codex のエラーと取り違えない
        self.assertIn("NO REACTION", self.verdict(self.base(**{"repos/o/r/issues/5/comments": [*comments, ci_limit]})))


if __name__ == "__main__":
    unittest.main()
