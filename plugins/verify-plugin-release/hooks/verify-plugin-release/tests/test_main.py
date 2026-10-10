"""__main__.py (PreToolUse hook の入口) を subprocess で実行する end-to-end テスト。"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
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

    def test_unresolvable_cd_is_denied(self):
        for cmd in ('cd "$WT" && gh pr create -t x', "cd no-such-dir && gh pr create -t x"):
            with self.subTest(cmd=cmd):
                out = run_hook(bash(cmd, self.root))
                self.assertEqual(self.decision(out), "deny")
                self.assertIn("cd の移動先", out["hookSpecificOutput"]["permissionDecisionReason"])

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
