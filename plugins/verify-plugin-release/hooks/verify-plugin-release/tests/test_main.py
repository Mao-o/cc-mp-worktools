"""__main__.py (PreToolUse hook の入口) を subprocess で実行する end-to-end テスト。"""
from __future__ import annotations

import importlib.util
import io
import json
import ntpath
import os
import posixpath
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

import _testutil  # noqa: F401
from command import find_invocations
from _testutil import (
    FAILING_TEST,
    HermeticGitTestCase,
    bump,
    commit_all,
    init_bare_origin,
    launch_hook,
    make_marketplace,
    run_hook,
    sh,
    write,
    write_json,
)

_PKG = Path(__file__).resolve().parent.parent


# hook を subprocess で起動する経路は `_testutil.launch_hook` (`run_hook` もこの上に作ってある) だけ。
# 起動のたびに env を組む (`hook_process_env`): git の global 設定は `HERMETIC_GIT_ENV` の
# `GIT_CONFIG_GLOBAL` (tests 配下の fixture。自動 maintenance を止める設定だけを持つ) に、
# 既定の除外ファイル (`XDG_CONFIG_HOME/git/ignore`) は空の dir に固定し、手元の global gitignore
# (`__pycache__` 等) に結果が左右されない CI と同じ条件で動かす。PATH からも claude を外し、
# CI と同じく `claude plugin validate` は SKIP になる (`_testutil.path_without_claude`)。env の中身は `test_hermetic_env.py` が見る


def bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def shell_path(path: str | Path) -> str:
    """コマンドに引用なしで埋めるパス。シェルは `\\` を外すので、Windows のパスは `/` で書く (`C:\\a` は `C:a` と読まれる)。"""
    return Path(path).as_posix()


class MainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_marketplace(Path(self._tmp.name) / "repo", ["alpha"])
        # validate は claude の有無で SKIP / WARN が変わるが、どちらも deny には効かない
        write_json(self.root, ".claude/verify-plugin-release.json", {"fetch": False})
        sh(self.root, "switch", "-q", "-c", "feat")

    def tearDown(self):
        self._tmp.cleanup()

    def decision(self, out: dict | None) -> str | None:
        return None if out is None else out["hookSpecificOutput"].get("permissionDecision")

    def test_unrelated_command_is_silent(self):
        self.assertIsNone(run_hook(bash("git status", self.root)))
        self.assertIsNone(run_hook({"tool_name": "Read", "tool_input": {}}))
        self.assertIsNone(run_hook("not json"))

    def test_incomplete_release_is_denied(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out = run_hook(bash("gh pr create -t x -b y", self.root))
        self.assertEqual(self.decision(out), "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("version[alpha]", reason)
        self.assertIn("changelog[alpha]", reason)

    def test_draft_and_warn_mode_do_not_block(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out = run_hook(bash("gh pr create --draft -t x", self.root))
        self.assertIsNone(self.decision(out))
        self.assertIn("version[alpha]", out["hookSpecificOutput"]["additionalContext"])
        out = run_hook(bash("gh pr create -t x", self.root), {"VERIFY_PLUGIN_RELEASE_MODE": "warn"})
        self.assertIsNone(self.decision(out))
        self.assertIsNone(run_hook(bash("gh pr create -t x", self.root), {"VERIFY_PLUGIN_RELEASE_MODE": "off"}))

    def test_broken_config_is_denied(self):
        write(self.root, ".claude/verify-plugin-release.json", "{")
        commit_all(self.root, "broken config")
        out = run_hook(bash("gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("ゲートを完了できなかった", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_cd_into_repo_is_followed(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out = run_hook(bash(f'cd "{self.root}" && gh pr create -t x', self.root.parent))
        self.assertEqual(self.decision(out), "deny")

    def test_head_other_than_checkout_is_denied(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")
        # checkout は条件を満たしていても、--head が別 branch なら中身が一致しない
        out = run_hook(bash("gh pr create --head other -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("--head", out["hookSpecificOutput"]["permissionDecisionReason"])
        # --head を明示すると gh は push しないので、origin/<head> が手元の HEAD と一致して初めて通る
        out = run_hook(bash("gh pr create --head feat -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        remote = init_bare_origin(Path(self._tmp.name), "remote.git")
        sh(self.root, "remote", "add", "origin", str(remote))
        sh(self.root, "push", "-q", "origin", "main", "feat")
        out = run_hook(bash("gh pr create --head feat -t x", self.root))
        self.assertNotEqual(self.decision(out), "deny")
        write(self.root, "plugins/alpha/hooks/alpha/extra.py", "y = 2\n")
        commit_all(self.root, "unpushed")
        out = run_hook(bash("gh pr create --head feat -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("push", out["hookSpecificOutput"]["permissionDecisionReason"])
        # owner:branch は fork 側の branch なので、名前が同じでも手元とは一致しない
        out = run_hook(bash("gh pr create --head someone:feat -t x", self.root))
        self.assertEqual(self.decision(out), "deny")

    def test_every_invocation_in_compound_command_is_checked(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        # 先頭の draft 作成だけを見ると止めずに通してしまう
        out = run_hook(bash("gh pr create --draft -t x && gh pr ready", self.root))
        self.assertEqual(self.decision(out), "deny")

    def test_repo_flag_must_match_origin(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")
        sh(self.root, "remote", "add", "origin", "https://github.com/Demo-Org/demo-market.git")
        for cmd in ("gh -R demo-org/demo-market pr create -t x", "gh pr create --repo github.com/Demo-Org/demo-market"):
            with self.subTest(cmd=cmd):
                self.assertNotEqual(self.decision(run_hook(bash(cmd, self.root))), "deny")
        for cmd in (
            "gh -R other/fork pr create -t x",
            "gh -R enterprise.example/Demo-Org/demo-market pr create -t x",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.decision(run_hook(bash(cmd, self.root))), "deny")
        # host を省いた形は GH_HOST を host とみなす (gh と同じ)
        out = run_hook(bash("gh -R demo-org/demo-market pr create -t x", self.root), {"GH_HOST": "enterprise.example"})
        self.assertEqual(self.decision(out), "deny")
        out = run_hook(bash("gh -R other/fork pr create -t x", self.root))
        self.assertIn("PR の作成先", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_ready_without_pr_lookup_is_denied(self):
        # remote の無い repo では gh pr view が必ず失敗する = PR の base が分からない
        out = run_hook(bash("gh pr ready", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("gh pr view", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_uncommitted_config_cannot_weaken_the_gate(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        write(self.root, "plugins/alpha/hooks/alpha/tests/test_x.py", FAILING_TEST)
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release with failing test")
        write_json(self.root, ".claude/verify-plugin-release.json", {"fetch": False, "test_command": False})
        out = run_hook(bash("gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("tests[alpha]", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_gate_does_not_trip_over_its_own_bytecode(self):
        # 1 回目の検査が走らせたテストの bytecode を、2 回目が「未 commit の変更」と判定して止めていた
        # (CI で発生)。守りは 2 つあり、片方だけ壊れても気付けるよう別々に確かめる:
        # (1) 既にある bytecode は未 commit の変更に数えない (2) ゲートはテストに bytecode を書かせない
        # 条件を満たした release が止められないこと (PASS の経路) もここで確かめる
        self._release()
        stray = self.root / "plugins/alpha/hooks/alpha/tests/__pycache__/stale.cpython-0.pyc"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"")
        # 実行環境の PYTHONDONTWRITEBYTECODE に頼らず、ゲート自身が抑止していることを見る
        # (-B は hook 自身の .pyc を plugin のソースに書かせないため)
        out = run_hook(
            bash("gh pr create -t x -b y", self.root),
            python_flags=("-B",),
            drop=("VERIFY_PLUGIN_RELEASE_MODE", "GH_HOST", "PYTHONDONTWRITEBYTECODE"),
        )
        self.assertNotEqual(self.decision(out), "deny")
        self.assertEqual([p.name for p in stray.parent.iterdir()], [stray.name])
        self.assertEqual([p for p in self.root.rglob("__pycache__") if p != stray.parent], [])

    def _release(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")

    def test_gh_repo_selectors_other_than_flag(self):
        self._release()
        sh(self.root, "remote", "add", "origin", "https://github.com/Demo-Org/demo-market.git")
        out = run_hook(bash("GH_REPO=other/project gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        out = run_hook(bash("gh pr create -t x", self.root), {"GH_REPO": "other/project"})
        self.assertEqual(self.decision(out), "deny")
        # 値に置換や空白がある代入の後ろは、解析できないので止める (入れ子の形を素通しにしない)
        for cmd in (
            "GH_REPO=$(basename $(pwd)) gh pr create -t x",
            "GH_TOKEN=$(gh auth token) gh pr create -t x",
        ):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("解析できない", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertNotEqual(
            self.decision(run_hook(bash("gh pr create -t x", self.root), {"GH_REPO": "Demo-Org/demo-market"})),
            "deny",
        )
        # gh repo set-default は remote.<name>.gh-resolved に保存される
        sh(self.root, "remote", "add", "upstream", "git@github.com:Upstream-Org/demo-market.git")
        sh(self.root, "config", "remote.upstream.gh-resolved", "base")
        out = run_hook(bash("gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("set-default", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_cd_back_to_the_root_runs_the_gate(self):
        # 最後の cd (`../..`) だけで解決すると repo の外になり、ゲートが走らずに通っていた
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        for cmd in (
            "cd plugins/alpha && python3 -m unittest && cd ../.. && gh pr create -t x",
            "cd plugins && cd alpha && cd ../.. && gh pr create -t x",
        ):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("version[alpha]", out["hookSpecificOutput"]["permissionDecisionReason"])
        # 合成した先が repo の外なら従来どおり何もしない
        self.assertIsNone(run_hook(bash("cd plugins && cd ../.. && gh pr create -t x", self.root)))

    def test_composed_cd_follows_links_like_the_shell(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        outside = Path(self._tmp.name) / "outside" / "deep"
        outside.mkdir(parents=True)
        # (1) 移動先が repo を指す symlink
        (Path(self._tmp.name) / "repo-link").symlink_to(self.root)
        out = run_hook(bash("cd repo-link && cd plugins && cd .. && gh pr create -t x", Path(self._tmp.name)))
        self.assertEqual(self.decision(out), "deny")
        # (2) 途中の dir が repo の外を指す symlink: `..` は shell と同じく symlink を辿る前の親 (= repo root)
        link = self.root / "ext"
        link.symlink_to(outside)
        out = run_hook(bash("cd ext && cd .. && gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        out = run_hook(bash("cd ext/.. && gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        # symlink の先そのものは repo の外
        self.assertIsNone(run_hook(bash("cd plugins && cd ../ext && gh pr create -t x", self.root)))

    def test_composed_cd_with_a_case_insensitive_name(self):
        probe = Path(self._tmp.name) / "CaseProbe"
        probe.mkdir()
        if not (Path(self._tmp.name) / "caseprobe").exists():
            self.skipTest("大文字小文字を区別する FS")
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out = run_hook(bash("cd PLUGINS && cd ALPHA && cd ../.. && gh pr create -t x", self.root))
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("version[alpha]", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unresolvable_composed_cd_is_denied(self):
        cases = {
            "missing step": ("cd no-such-dir && cd .. && gh pr create -t x", "存在しない"),
            "cd after a subshell": ("(cd plugins/alpha) && cd ../.. && gh pr create -t x", "静的に解決できない"),
            "variable then relative": ('cd "$WT" && cd sub && gh pr create -t x', "静的に解決できない"),
            "oldpwd": ("cd plugins && cd - && gh pr create -t x", "静的に解決できない"),
        }
        for label, (cmd, why) in cases.items():
            with self.subTest(label):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("cd の移動先", reason)
                self.assertIn(why, reason)

    def test_heredoc_body_with_an_apostrophe_runs_the_gate(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        for cmd in (
            "cat > b.md <<'EOF'\nIt's done\nEOF\ngh pr create -t x --body-file b.md",
            "gh pr create -t x --body-file - <<'EOF'\nI don't know\nEOF",
        ):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("version[alpha]", reason)
                self.assertNotIn("解析できない", reason)

    def test_unresolvable_cd_is_denied(self):
        for cmd in ('cd "$WT" && gh pr create -t x', "cd no-such-dir && gh pr create -t x"):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("cd の移動先", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_cd_in_an_assignment_value_keeps_the_gate_on_the_cwd(self):
        # 値の中の cd はサブシェルで走り、gh は元の cwd で走る (repo の外への移動と読むと素通しになる)
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out_dir = shell_path(self._tmp.name)
        for cmd in (
            'ROOT="$(cd .. && pwd)" gh pr create --base main -t x',
            f'ROOT="$(cd {out_dir} && pwd)" gh pr create --base main -t x',
            f'X="$(cd {out_dir})" gh pr create --base main -t x',
        ):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("version[alpha]", out["hookSpecificOutput"]["permissionDecisionReason"])
        # 値を読めない形は、検査先を決めずに止める
        for cmd in (
            f"X=$(cd {out_dir} && pwd) gh pr create --base main -t x",
            f"X=`cd {out_dir}` gh pr create --base main -t x",
            f"(cd {out_dir}) && X=$(a b) gh pr create --base main -t x",
            f"cd {out_dir} && X=$(a b) gh pr create --base main -t x",
        ):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("解析できない", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_cd_that_does_not_move_the_shell_keeps_the_gate_on_the_cwd(self):
        # gh は元の cwd (不備のある repo) で走る。cd の先を検査先と読むと、repo の外を見て素通しになる
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out_dir = shell_path(self._tmp.name)
        for cmd in (
            f"(cd {out_dir} && true) && gh pr create -t x",
            f"(cd {out_dir}) && gh pr create -t x",
            f"(cd {out_dir}) && X=1 gh pr create -t x",
            f"(cd {out_dir} && git push) && gh pr create -t x",
            f"ROOT=$(cd {out_dir} && pwd); gh pr create -t x",
            f"ROOT=$(cd {out_dir} && pwd)\ngh pr create -t x",
            f"X=$(cd {out_dir}); gh pr create -t x",
            f"cd {out_dir} | cat; gh pr create -t x",
            f"cd {out_dir} & gh pr create -t x",
            f"env cd {out_dir}; gh pr create -t x",
            f"nohup -- cd {out_dir}; gh pr create -t x",
        ):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("version[alpha]", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_cd_that_may_not_run_stops_before_the_gate(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out_dir = shell_path(self._tmp.name)
        for cmd in (
            f"false && cd {out_dir}; gh pr create -t x",
            f"false && \ncd {out_dir}; gh pr create -t x",
            f"false && # c\ncd {out_dir}; gh pr create -t x",
            f"true || \ncd {out_dir}; gh pr create -t x",
            f"true | cd {out_dir}; gh pr create -t x",
            f"if false; then cd {out_dir}; fi; gh pr create -t x",
            f"X=$(a b) cd {out_dir}; gh pr create -t x",
            f"$x cd {out_dir}; gh pr create -t x",
        ):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("cd の移動先", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertIn("静的に解決できない", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertIn("cd <絶対パス> && gh", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_outside_the_repo_forms_that_may_enter_it_stop(self):
        # hook の cwd が repo の外でも、移動の読み落としで素通しにしない
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        out_dir = self.root.parent
        root = shell_path(self.root)
        cases = {
            f"builtin cd {root}; gh pr create -t x": "version[alpha]",
            f"cd {root} || cd /; gh pr create -t x": "静的に解決できない",
            f"if pushd {root}; then gh pr create -t x; fi": "pushd / popd",
            f"command pushd {root}; gh pr create -t x": "pushd / popd",
            f"Y=1 pushd {root}; gh pr create -t x": "pushd / popd",
            f"'pushd' {root}; gh pr create -t x": "pushd / popd",
            f"! pushd {root}; gh pr create -t x": "pushd / popd",
            f"time pushd {root}; gh pr create -t x": "pushd / popd",
            "true && ${x} gh pr create -t x": "解析できない",
            "$x gh pr create -t x": "解析できない",
            "env -i gh pr create -t x": "解析できない",
            "case a in a) gh pr create -t x;; esac": "解析できない",
        }
        for cmd, why in cases.items():
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, out_dir))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn(why, out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_everyday_forms_are_not_stopped_when_the_release_is_complete(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")
        self.assertIsNone(self.decision(run_hook(bash("gh pr create --base main -t x", self.root))))
        for cmd in (
            "cd plugins/alpha && cd ../.. && gh pr create --base main -t x",
            "cd plugins; cd ..; gh pr create --base main -t x",
            "ROOT=$(cd .. && pwd); gh pr create --base main -t x",
            "(cd plugins && git status) && gh pr create --base main -t x",
            "(cd plugins); gh pr create --base main -t x",
            "git status && gh pr create --base main -t x",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.decision(run_hook(bash(cmd, self.root))))
        # 同じ形で repo の外へ移る cd は移動として読む
        self.assertIsNone(run_hook(bash(f"cd {shell_path(self._tmp.name)}; gh pr create --base main -t x", self.root)))

    def test_japanese_payload_as_utf8_bytes(self):
        # Windows では stdin の既定 codec が UTF-8 でないため、日本語タイトルで
        # decode に失敗して素通りする経路があった。bytes で渡して確かめる
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.root, "change without bump")
        payload = json.dumps(bash('gh pr create --title "機能追加: 検証ゲート"', self.root), ensure_ascii=False)
        r = launch_hook(input=payload.encode("utf-8"), text=False, drop=("VERIFY_PLUGIN_RELEASE_MODE", "PYTHONUTF8"))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.decode("utf-8"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_non_plugin_repo_is_ignored(self):
        other = Path(self._tmp.name) / "plain"
        other.mkdir()
        sh(other, "init", "-q", "-b", "main")
        self.assertIsNone(run_hook(bash("gh pr create -t x", other)))

    def test_outside_git_is_ignored(self):
        outside = Path(self._tmp.name) / "nogit"
        outside.mkdir()
        self.assertIsNone(run_hook(bash("gh pr create -t x", outside), {"GIT_CEILING_DIRECTORIES": self._tmp.name}))


def _load_entry():
    spec = importlib.util.spec_from_file_location("vpr_entry", _PKG / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReadyRefsTest(HermeticGitTestCase):
    """`gh pr ready` の PR 参照と手元の照合 (gh を差し替えて in-process で確かめる)。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_marketplace(Path(self._tmp.name) / "repo", ["alpha"])
        write_json(self.root, ".claude/verify-plugin-release.json", {"fetch": False})
        sh(self.root, "switch", "-q", "-c", "feat")
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")
        self.sha = sh(self.root, "rev-parse", "HEAD").strip()
        self.mod = _load_entry()
        # in-process のゲートも CI と同じく claude を見つけない (validate は SKIP)
        self._env = mock.patch.dict(os.environ, {"PATH": _testutil.path_without_claude()}, clear=False)
        self._env.start()
        os.environ.pop("VERIFY_PLUGIN_RELEASE_MODE", None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def decide(self, refs):
        with mock.patch.object(self.mod, "_pr_refs", return_value=refs):
            out = self.mod.evaluate("gh pr ready 7", str(self.root))
        return None if out is None else out["hookSpecificOutput"].get("permissionDecision")

    def test_matching_branch_and_commit_passes(self):
        self.assertNotEqual(self.decide(("feat", "main", self.sha)), "deny")

    def test_other_branch_is_denied(self):
        self.assertEqual(self.decide(("other", "main", self.sha)), "deny")

    def test_repo_lookup_failure_is_denied(self):
        from runner import GateTimeout

        with mock.patch.object(self.mod, "git", side_effect=GateTimeout("slow fs")):
            out = self.mod.evaluate("gh pr create -t x", str(self.root))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_ready_url_for_other_repo_is_denied(self):
        sh(self.root, "remote", "add", "origin", "git@github.com:Fork-Owner/demo-market.git")
        with mock.patch.object(self.mod, "_pr_refs", return_value=("feat", "main", self.sha)):
            out = self.mod.evaluate("gh pr ready https://github.com/Upstream/demo-market/pull/7", str(self.root))
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            out = self.mod.evaluate("gh pr ready https://github.com/fork-owner/demo-market/pull/7", str(self.root))
            self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_unpushed_commit_is_denied(self):
        self.assertEqual(self.decide(("feat", "main", "0" * 40)), "deny")


class UnsureCdPlacesTest(HermeticGitTestCase):
    """確かでない cd の後ろの PR 操作: 居うる cwd がどれも plugin repo でなければ何もしない (in-process)。

    世界は plugin repo でない repo (plain) と repo の外 (out)。どちらにも `sub` がある。plugin repo は別の場所に置き、
    cd で入りうるときだけ止まることを見る。
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        top = Path(os.path.realpath(self._tmp.name))
        self.plugin = make_marketplace(top / "plugin", ["alpha"])
        (self.plugin / "sub").mkdir()
        self.plain = top / "plain"
        (self.plain / "sub").mkdir(parents=True)
        sh(self.plain, "init", "-q", "-b", "main")
        self.out = top / "out"
        (self.out / "sub").mkdir(parents=True)
        self.top = top
        self.mod = _load_entry()
        # top の親から上を探させない (`cd ..` で top に入る形がある。TMPDIR が git repo の中でも、top を repo の外にする)
        self._env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(top.parent)}, clear=False)
        self._env.start()
        for name in ("VERIFY_PLUGIN_RELEASE_MODE", "CDPATH"):
            os.environ.pop(name, None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def outcome(self, cmd: str, cwd: Path) -> str:
        try:
            out = self.mod.evaluate(cmd, str(cwd))
        except Exception as e:  # noqa: BLE001 - hook が例外で終わるとコマンドがそのまま走る
            self.fail(f"evaluate が例外を投げた: {e!r}")
        if out is None:
            return "none"
        reason = out["hookSpecificOutput"].get("permissionDecisionReason", "")
        # 確かでない cd の停止 (理由は長いと切り詰められ、後ろの「静的に解決できない」が落ちることがあるので、頭で見る)
        unsure = reason.startswith("[verify-plugin-release] ゲートを完了できなかった: RuntimeError: cd の移動先 (")
        return "stop" if unsure and "が存在しない" not in reason else f"other: {reason[:80]}"

    def test_no_place_is_a_plugin_repo(self):
        for world in (self.plain, self.out):
            # `(cd sub); cd sub` の、前の cd の先から当てた組み合わせ (sub/sub) も在る (無いと止める)
            (world / "sub" / "sub").mkdir()
            other = shell_path(self.out if world == self.plain else self.plain)
            for cmd in (
                "git push && cd sub; gh pr create -t x",
                "[ -d sub ] && cd sub; gh pr create -t x",
                "true | cd sub; gh pr create -t x",
                "if false; then cd sub; fi; gh pr create -t x",
                "cd sub || cd ..; gh pr create -t x",
                "(cd sub); cd sub; gh pr create -t x",
                "cd sub | cat; cd sub; gh pr create -t x",
                f"false && cd {other}; gh pr create -t x",
                f"cd sub || cd {other}/sub; gh pr create -t x",
                "git push && cd sub && gh pr ready 3; cd ..; gh pr create -t x",
            ):
                with self.subTest(world=world.name, cmd=cmd):
                    self.assertEqual(self.outcome(cmd, world), "none")

    def test_a_place_missing_at_hook_time_stops(self):
        # hook の時点で行き先が無い組み合わせが 1 つでもあれば止める。同じコマンドの前のほうで作られて (`git clone URL d` /
        # `mkdir -p d`) shell が入りうるので、確かな cd の「移動先が存在しない」と同じ扱い
        for world in (self.plain, self.out):
            other = shell_path(self.out if world == self.plain else self.plain)
            for cmd in (
                "[ -d nope ] && cd nope; gh pr create -t x",
                "[ -d nope ] && cd ./nope; gh pr create -t x",
                f"git push && cd {other}/nope; gh pr create -t x",
                # 前の cd の先から当てた組み合わせ (sub/sub) だけが無い形も止める
                "(cd sub); cd sub; gh pr create -t x",
                "cd sub | cat; cd sub; gh pr create -t x",
            ):
                with self.subTest(world=world.name, cmd=cmd):
                    self.assertEqual(self.outcome(cmd, world), "stop")

    def test_a_place_made_by_an_earlier_command_stops(self):
        # 行き先が hook の時点で無くても、前のコマンド (git clone / git worktree add / mkdir) が作ると shell はそこに入る。
        # 作られる dir は plugin repo の clone / worktree / plugin repo の中
        plugin = shell_path(self.plugin)
        wt = shell_path(self.top / "wt")
        for world in (self.plain, self.out):
            for cmd in (
                f"git clone -q {plugin} mp && cd mp; gh pr create --fill",
                f"git clone -q {plugin} mp && cd mp || exit 1; gh pr create --fill",
                f"if [ -d mp ]; then cd mp; else git clone -q {plugin} mp && cd mp; fi; gh pr create --fill",
                f"mkdir -p {plugin}/newsub && cd {plugin}/newsub; gh pr create --fill",
                f"git -C {plugin} worktree add -q {wt} -b feat && cd {wt}; gh pr create --fill",
            ):
                with self.subTest(world=world.name, cmd=cmd):
                    self.assertEqual(self.outcome(cmd, world), "stop")

    def test_a_place_may_be_a_plugin_repo(self):
        plugin = shell_path(self.plugin)
        for world in (self.plain, self.out):
            for cmd in (
                f"git push && cd {plugin}; gh pr create -t x",
                f"cd sub || cd {plugin}; gh pr create -t x",
                f"[ -d x ] && cd {plugin}/sub; gh pr create -t x",
                # 括弧の中やパイプの左の cd も候補に入れる (止める側)
                f"(cd {plugin} && make); git push && cd sub; gh pr create -t x",
                f"cd {plugin} | cat; git push && cd sub; gh pr create -t x",
            ):
                with self.subTest(world=world.name, cmd=cmd):
                    self.assertEqual(self.outcome(cmd, world), "stop")
        # hook の cwd が plugin repo なら、cd が走らない場合に備えて止める (従来どおり)
        self.assertEqual(self.outcome("git push && cd sub; gh pr create -t x", self.plugin), "stop")
        self.assertEqual(self.outcome(f"git push && cd {shell_path(self.plain)}; gh pr create -t x", self.plugin), "stop")

    def test_places_that_cannot_be_listed_stop(self):
        for world in (self.plain, self.out):
            for cmd in (
                "git push && cd; gh pr create -t x",
                'git push && cd "$WT"; gh pr create -t x',
                "for i in 1; do cd sub; done; gh pr create -t x",
                "git push && chdir sub; gh pr create -t x",
                "git push && cd ~-; gh pr create -t x",
                "eval \"$X\"; git push && cd sub; gh pr create -t x",
            ):
                with self.subTest(world=world.name, cmd=cmd):
                    self.assertEqual(self.outcome(cmd, world), "stop")

    def test_places_the_shell_may_reach_another_way_stop(self):
        # bash の CDPATH は相対の名前を cwd より先に CDPATH の下で探す (行き先が cwd の下にあっても)。zsh は cwd に無ければ
        # CDPATH を引く。`..` を含む引数は、論理パスが無いと bash が物理パスで入り直す (行き先が無いので止まる)
        (self.top / "nope").mkdir()
        cases = (
            ("git push && cd nope; gh pr create -t x", {"CDPATH": str(self.top)}),
            ("git push && cd sub; gh pr create -t x", {"CDPATH": str(self.top)}),
            ("git push && cd sub/nope; gh pr create -t x", {"CDPATH": str(self.top)}),
            # CDPATH を引かない形と相対の名前が混じる形 (どの行き先も在る)。相対の名前が 1 つでもあれば止める
            (f"git push && cd {shell_path(self.out)} && cd sub; gh pr create -t x", {"CDPATH": str(self.top)}),
            ("git push && cd sub/../nope; gh pr create -t x", {}),
            ("git push && cd ../plain/../nope2; gh pr create -t x", {}),
        )
        for cmd, env in cases:
            with self.subTest(cmd=cmd, env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(self.outcome(cmd, self.plain), "stop")
        # CDPATH があっても、`./` `../` で始まる形・`~`・絶対パスは CDPATH を引かない (行き先は在る dir で見る)
        with mock.patch.dict(os.environ, {"CDPATH": str(self.top)}):
            for cmd in (
                "git push && cd ./sub; gh pr create -t x",
                "git push && cd ../out; gh pr create -t x",
                f"git push && cd {shell_path(self.out)}/sub; gh pr create -t x",
            ):
                with self.subTest(cmd=cmd):
                    self.assertEqual(self.outcome(cmd, self.plain), "none")

    def test_cdpath_rules(self):
        with mock.patch.object(os, "path", posixpath):
            for step, searches in (
                ("a", True), ("a/b", True), ("", True), ("C:/x", True), ("\\x", True),
                ("./a", False), ("../a", False), (".", False), ("..", False),
                ("/x", False), ("~", False), ("~/a", False), ("a/../b", True), ("/x/..", False),
            ):
                with self.subTest(step=step):
                    self.assertEqual(self.mod._searches_cdpath(step), searches)

    def test_every_subsequence_is_listed(self):
        # 前の cd が走らなくても後ろの cd は走りうる (`(cd sub); cd x` の x は hook の cwd の下)
        (self.plain / "x").mkdir()
        (self.plain / "sub" / "x").mkdir()
        dirs = self.mod._possible_dirs(self.plain, ("sub", "x"))
        self.assertIsNotNone(dirs)
        self.assertEqual(set(dirs), {self.plain, self.plain / "sub", self.plain / "x", self.plain / "sub" / "x"})

    def test_too_many_places_stop(self):
        # 上限は 16 (hook の cwd を含む)。同じ行き先は 1 つと数える
        dirs = [self.top / f"d{i}" for i in range(16)]
        for d in dirs:
            d.mkdir()
        cmd = " || ".join(f"cd {shell_path(d)}" for d in dirs) + "; gh pr create -t x"
        self.assertEqual(self.outcome(cmd, self.plain), "stop")
        cmd = " || ".join(f"cd {shell_path(d)}" for d in dirs[:15]) + "; gh pr create -t x"
        self.assertEqual(self.outcome(cmd, self.plain), "none")
        cmd = " || ".join(f"cd {shell_path(dirs[0])}" for _ in range(20)) + "; gh pr create -t x"
        self.assertEqual(self.outcome(cmd, self.plain), "none")

    def test_lookup_errors_stop(self):
        cmd = "git push && cd sub; gh pr create -t x"
        real = self.mod._repo_root

        def failing(d, dl):
            if Path(d).name == "sub":
                raise self.mod.RepoLookupError("broken")
            return real(d, dl)

        with mock.patch.object(self.mod, "_repo_root", failing):
            self.assertEqual(self.outcome(cmd, self.plain), "stop")
        for err in (
            PermissionError(13, "Permission denied"),
            OSError(63, "File name too long"),
            RuntimeError("Symlink loop"),
            ValueError("embedded null byte"),
        ):
            with self.subTest(err=err), mock.patch.object(Path, "is_dir", side_effect=err):
                self.assertEqual(self.outcome(cmd, self.plain), "stop")

    def test_draft_and_warn_report_without_stopping(self):
        plugin = shell_path(self.plugin)
        out = self.mod.evaluate(f"git push && cd {plugin}; gh pr create --draft -t x", str(self.plain))
        self.assertIsNotNone(out)
        self.assertIn("cd の移動先 (", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.mod.evaluate("git push && cd sub; gh pr create --draft -t x", str(self.plain)))


class _FakeWindowsPath(PureWindowsPath):
    """Windows のパスの読みを POSIX でも確かめるための Path (dir の有無は REAL で決める)。"""

    REAL: set[str] = set()

    def is_dir(self) -> bool:
        return ntpath.normcase(str(self)) in self.REAL

    def resolve(self):
        return self


class WindowsDriveSpellingTest(unittest.TestCase):
    """Git Bash の `/c/...` を `C:/...` と読む (`os.path` を ntpath に差し替え、git は起動しない)。

    差し替える前は `C:\\c\\Users\\...` (hook の cwd のドライブの下の `c`) と読み、存在しないとして止めていた。
    """

    PLUGIN = "C:\\Users\\x\\repo"
    OTHER = "C:\\Users\\x\\other"
    HOME = "C:\\Users\\me\\proj"

    def setUp(self):
        self.mod = _load_entry()
        # `C:\tmp` も在る: ドライブの無い `/tmp` を Python は `C:\tmp` と読むが、Git Bash は Windows の一時 dir に入る
        _FakeWindowsPath.REAL = {
            ntpath.normcase(p) for p in (self.PLUGIN, self.OTHER, self.HOME, "C:\\Users\\x", "C:\\", "C:\\tmp")
        }
        for patcher in (
            mock.patch.object(os, "path", ntpath),
            mock.patch.object(self.mod, "Path", _FakeWindowsPath),
            mock.patch.object(self.mod, "_repo_root", self._root),
            mock.patch.object(self.mod.layout, "is_plugin_repo", lambda r: ntpath.normcase(str(r)) == ntpath.normcase(self.PLUGIN)),
            mock.patch.object(self.mod, "_load_config", lambda r, d: (self.mod.config.Config(), None)),
            mock.patch.object(self.mod, "_other_default_remote", lambda r, d: None),
            mock.patch.object(self.mod.gate, "run_gate", side_effect=RuntimeError("GATE-RAN")),
            mock.patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\me"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("CDPATH", "GH_REPO"):
            os.environ.pop(name, None)
        self.seen: list[str] = []

    def _root(self, workdir, dl):
        self.seen.append(str(workdir))
        w = ntpath.normcase(str(workdir))
        for r in (self.PLUGIN, self.OTHER):
            if w == ntpath.normcase(r) or w.startswith(ntpath.normcase(r) + "\\"):
                return _FakeWindowsPath(r)
        if w in _FakeWindowsPath.REAL:
            return None
        raise self.mod.RepoLookupError(f"cwd が無い: {workdir}")

    def outcome(self, cmd: str, cwd: str = HOME) -> str:
        [inv] = find_invocations(cmd)
        out = self.mod._evaluate_one(inv, cwd, "enforce", self.mod.Deadline(30))
        if out is None:
            return "none"
        reason = out["hookSpecificOutput"].get("permissionDecisionReason", "")
        return "gate" if "GATE-RAN" in reason else f"stop: {reason}"

    def test_native_spelling(self):
        for text, want in (
            ("/c/Users/x", "C:/Users/x"),
            ("/C/Users/x", "C:/Users/x"),
            ("/d/work", "D:/work"),
            ("/c", "C:/"),
            ("/c/", "C:/"),
            ("/c//x", "C:/x"),
            ("/c\n", "/c\n"),
            ("/cd/x", "/cd/x"),
            ("/tmp", "/tmp"),
            ("c/x", "c/x"),
            ("C:/x", "C:/x"),
            ("~/x", "~/x"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.mod._native(text), want)

    def test_posix_spelling_is_left_alone(self):
        with mock.patch.object(os, "path", posixpath):
            for text in ("/c/Users/x", "/C/x", "/c", "/c/", "/d/work"):
                with self.subTest(text=text):
                    self.assertEqual(self.mod._native(text), text)

    def test_drive_cd_is_inspected_only_in_a_plugin_repo(self):
        for cmd, want, where in (
            ("cd /c/Users/x/repo && gh pr create -t x", "gate", self.PLUGIN),
            ("cd /C/Users/x/repo && gh pr create -t x", "gate", self.PLUGIN),
            ("cd /c/Users/x && cd repo && gh pr create -t x", "gate", self.PLUGIN),
            ("cd /c && cd Users/x/repo && gh pr create -t x", "gate", self.PLUGIN),
            ("cd /c/Users/x/other && gh pr create -t x", "none", self.OTHER),
            ("cd /c/Users/me/proj && gh pr create -t x", "none", self.HOME),
        ):
            with self.subTest(cmd=cmd):
                self.seen.clear()
                self.assertEqual(self.outcome(cmd), want)
                self.assertEqual(self.seen, [where])

    def test_hook_cwd_in_git_bash_spelling(self):
        self.assertEqual(self.outcome("gh pr create -t x", "/c/Users/x/repo"), "gate")
        self.assertEqual(self.outcome("gh pr create -t x", "/c/Users/x/other"), "none")

    def test_unsure_drive_cd(self):
        self.assertEqual(self.outcome("git push && cd /c/Users/x/other; gh pr create -t x"), "none")
        self.assertIn("静的に解決できない", self.outcome("git push && cd /c/Users/x/repo; gh pr create -t x"))
        # ドライブの無い絶対パスは Git Bash が別の場所 (`/tmp` は Windows の一時 dir) に読み替えるので、`C:\tmp` が在っても止める
        self.assertIn("静的に解決できない", self.outcome("git push && cd /tmp; gh pr create -t x"))
        # hook の cwd も Git Bash の綴りで読む
        self.assertEqual(self.outcome("git push && cd /c/Users/x/other; gh pr create -t x", "/c/Users/me/proj"), "none")
        self.assertIn("静的に解決できない", self.outcome("git push && cd /c/nope; gh pr create -t x", "/c/Users/x/repo"))
        # hook の時点で無い行き先は、前のコマンドが作りうるので止める
        self.assertIn("静的に解決できない", self.outcome("git push && cd /c/nope; gh pr create -t x"))
        # ドライブのある絶対パスと `~` は CDPATH を引かない。相対の名前は CDPATH の下で見つかりうる
        with mock.patch.dict(os.environ, {"CDPATH": "C:\\Users\\x"}):
            self.assertEqual(self.outcome("git push && cd /c/Users/x/other; gh pr create -t x"), "none")
            self.assertEqual(self.outcome("git push && cd C:/Users/x/other; gh pr create -t x"), "none")
            self.assertEqual(self.outcome("git push && cd ~/proj; gh pr create -t x"), "none")
            self.assertIn("静的に解決できない", self.outcome("git push && cd nope; gh pr create -t x"))

    def test_drive_less_absolute_path_is_not_listed(self):
        # `/tmp` と `\tmp` は Python が `C:\tmp` と読み (在る)、Git Bash は別の場所に入るので挙げない。ドライブのある形は挙げる
        start = _FakeWindowsPath(self.HOME)
        for history in (("/tmp",), ("\\tmp",), ("C:/tmp", "/tmp")):
            with self.subTest(history=history):
                self.assertIsNone(self.mod._possible_dirs(start, history))
        dirs = self.mod._possible_dirs(start, ("C:/tmp",))
        self.assertIsNotNone(dirs)
        self.assertEqual({ntpath.normcase(str(d)) for d in dirs}, {ntpath.normcase(self.HOME), ntpath.normcase("C:\\tmp")})

    def test_cdpath_rules(self):
        for step, searches in (
            ("a", True), ("/c/x", False), ("C:/x", False), ("C:\\x", False), ("\\x", False),
            ("/tmp", False), ("~/a", False), ("./a", False), ("/c/x/../y", False),
        ):
            with self.subTest(step=step):
                self.assertEqual(self.mod._searches_cdpath(step), searches)


@unittest.skipUnless(os.name == "nt", "Git Bash の綴りは Windows でだけ意味を持つ")
class RealWindowsDriveTest(HermeticGitTestCase):
    """Windows の実際のパスで、`cd /c/...` の先が plugin repo なら検査し、そうでなければ何もしない。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        top = Path(os.path.realpath(self._tmp.name))
        self.plugin = make_marketplace(top / "repo", ["alpha"])
        write_json(self.plugin, ".claude/verify-plugin-release.json", {"fetch": False})
        sh(self.plugin, "switch", "-q", "-c", "feat")
        write(self.plugin, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        commit_all(self.plugin, "change without bump")
        self.plain = top / "plain"
        self.plain.mkdir()
        sh(self.plain, "init", "-q", "-b", "main")
        self.mod = _load_entry()
        self._env = mock.patch.dict(os.environ, {"PATH": _testutil.path_without_claude()}, clear=False)
        self._env.start()
        for name in ("VERIFY_PLUGIN_RELEASE_MODE", "CDPATH"):
            os.environ.pop(name, None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    @staticmethod
    def git_bash(path: Path) -> str:
        drive, rest = os.path.splitdrive(str(path))
        return "/" + drive.rstrip(":").lower() + rest.replace("\\", "/")

    def test_drive_cd(self):
        out = self.mod.evaluate(f"cd {self.git_bash(self.plugin)} && gh pr create -t x", str(self.plain))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("version[alpha]", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.mod.evaluate(f"cd {self.git_bash(self.plain)} && gh pr create -t x", str(self.plugin)))
        self.assertIsNone(self.mod.evaluate(f"git push && cd {self.git_bash(self.plain)}; gh pr create -t x", str(self.plain)))


class UnresolvedCommandTest(unittest.TestCase):
    """PR 操作の位置を特定できないコマンドは repo を見る前に止める (evaluate を in-process で呼ぶ)。

    hook の入出力 (stdin の JSON・deny の出力形式) は MainTest が subprocess で確かめている。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()  # git repo の外。万一ゲートまで進んでも何も検査しない
        self.mod = _load_entry()
        self._env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self._tmp.name}, clear=False)
        self._env.start()
        os.environ.pop("VERIFY_PLUGIN_RELEASE_MODE", None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_pr_command_in_substitution_is_denied(self):
        for cmd in (
            'url="$(gh pr create --title x --body y)"',
            "url=`gh pr create -t x`",
            "f() { gh pr create; }; f",
            "pushd /tmp && gh pr create -t x",
            "bash -c 'gh pr create -t x'",
            'sh -lc "cd x && gh pr ready"',
            "eval 'gh pr new'",
        ):
            with self.subTest(cmd=cmd):
                out = self.mod.evaluate(cmd, self._tmp.name)
                self.assertEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        # 文中の言及は PR 操作ではない
        self.assertIsNone(self.mod.evaluate('git commit -m "run gh pr create later"', self._tmp.name))

    def test_too_large_command_is_denied_with_body_file_guidance(self):
        from command import _READ_LIMIT

        large = "x" * (_READ_LIMIT + 1)
        out = self.mod.evaluate(f"gh pr create --body '{large}'", self._tmp.name)
        decision = (out or {}).get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        self.assertIn("--body-file", decision.get("permissionDecisionReason", ""))
        self.assertIsNone(self.mod.evaluate(f"echo '{large}'", self._tmp.name))


class FailClosedTest(unittest.TestCase):
    """hook が例外で終わるとコマンドがそのまま走るので、想定外の例外は止める側に倒す (in-process)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / "sub").mkdir()
        self.mod = _load_entry()
        self._env = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self._tmp.name}, clear=False)
        self._env.start()
        os.environ.pop("VERIFY_PLUGIN_RELEASE_MODE", None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def decision(self, out):
        return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision")

    def test_cd_lookup_errors_are_denied(self):
        # 権限の無い dir / 長すぎる名前 / symlink の循環で is_dir / resolve が投げる (Python 3.11〜3.13)
        for name, err in (
            ("is_dir", PermissionError(13, "Permission denied")),
            ("is_dir", OSError(63, "File name too long")),
            ("resolve", OSError(62, "Too many levels of symbolic links")),
            ("resolve", RuntimeError("Symlink loop")),
        ):
            with self.subTest(name=name, err=err):
                with mock.patch.object(Path, name, side_effect=err):
                    try:
                        out = self.mod.evaluate("cd sub && gh pr create -t x", self._tmp.name)
                    except Exception as e:  # noqa: BLE001
                        self.fail(f"evaluate が例外を投げた: {e!r}")
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("cd の移動先", out["hookSpecificOutput"]["permissionDecisionReason"])

    def run_main(self, payload: dict) -> dict | None:
        stdin = mock.Mock()
        stdin.buffer.read.return_value = json.dumps(payload).encode("utf-8")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "argv", ["verify-plugin-release"]):
            try:
                self.mod.main()
            except Exception as e:  # noqa: BLE001
                self.fail(f"main が例外を投げた: {e!r}")
        return json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None

    def test_unexpected_error_in_evaluate_is_denied(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "gh pr create -t x"}, "cwd": self._tmp.name}
        with mock.patch.object(self.mod, "evaluate", side_effect=ValueError("boom")):
            out = self.run_main(payload)
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("ValueError: boom", out["hookSpecificOutput"]["permissionDecisionReason"])
        # warn では止めずに伝える
        os.environ["VERIFY_PLUGIN_RELEASE_MODE"] = "warn"
        with mock.patch.object(self.mod, "evaluate", side_effect=ValueError("boom")):
            out = self.run_main(payload)
        self.assertIsNone(self.decision(out))
        self.assertIn("ValueError: boom", out["hookSpecificOutput"]["additionalContext"])


class ManualCheckTest(unittest.TestCase):
    def test_manual_check_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_marketplace(Path(tmp) / "repo", ["alpha"])
            write_json(root, ".claude/verify-plugin-release.json", {"fetch": False})
            sh(root, "switch", "-q", "-c", "feat")
            write(root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
            commit_all(root, "change")
            r = launch_hook(["check", str(root)], drop=())
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("FAIL  version[alpha]", r.stdout)
            r = launch_hook(["check", "--strict-validate", "--base", "main", str(root)], drop=())
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("base: main", r.stdout)


if __name__ == "__main__":
    unittest.main()
