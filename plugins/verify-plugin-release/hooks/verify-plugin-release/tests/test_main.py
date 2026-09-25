"""__main__.py (PreToolUse hook の入口) を subprocess で実行する end-to-end テスト。"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from _testutil import FAILING_TEST, bump, commit_all, make_marketplace, sh, write, write_json

_PKG = Path(__file__).resolve().parent.parent


# 手元の global gitignore (__pycache__ 等) に結果が左右されないよう、git の global 設定と
# 既定の excludes (XDG_CONFIG_HOME/git/ignore) を空にして CI と同じ条件で hook を動かす
_EMPTY_XDG = Path(tempfile.mkdtemp(prefix="vpr-xdg-"))
_EMPTY_GITCONFIG = _EMPTY_XDG / "gitconfig"
_EMPTY_GITCONFIG.write_text("", encoding="utf-8")


def run_hook(payload: dict | str, env_extra: dict | None = None) -> dict | None:
    env = {k: v for k, v in os.environ.items() if k not in ("VERIFY_PLUGIN_RELEASE_MODE", "GH_HOST")}
    env["GIT_CONFIG_GLOBAL"] = str(_EMPTY_GITCONFIG)
    env["XDG_CONFIG_HOME"] = str(_EMPTY_XDG)
    env.update(env_extra or {})
    data = payload if isinstance(payload, str) else json.dumps(payload)
    r = subprocess.run(
        [sys.executable, str(_PKG)],
        input=data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


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

    def test_complete_release_is_allowed_silently_or_with_context(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")
        out = run_hook(bash("gh pr create -t x -b y", self.root))
        self.assertNotEqual(self.decision(out), "deny")

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
        remote = Path(self._tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
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
        # 1 回目の検査でテストを走らせても、2 回目に「未 commit の変更」と判定しない
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")
        for _ in range(2):
            self.assertNotEqual(self.decision(run_hook(bash("gh pr create -t x", self.root))), "deny")

    def _release(self):
        write(self.root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
        write(self.root, "plugins/alpha/CHANGELOG.md", "# Changelog\n\n## 0.2.0\n")
        bump(self.root, "alpha", "0.2.0")
        commit_all(self.root, "release")

    def test_pr_command_in_substitution_is_denied(self):
        self._release()
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
                self.assertEqual(self.decision(run_hook(bash(cmd, self.root))), "deny")
        # 文中の言及は PR 操作ではない
        self.assertIsNone(run_hook(bash('git commit -m "run gh pr create later"', self.root)))

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
        r = subprocess.run(
            [sys.executable, str(_PKG)],
            input=payload.encode("utf-8"),
            capture_output=True,
            env={k: v for k, v in os.environ.items() if k not in ("VERIFY_PLUGIN_RELEASE_MODE", "PYTHONUTF8")},
            timeout=120,
        )
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


class ReadyRefsTest(unittest.TestCase):
    """`gh pr ready` の PR 参照と手元の照合 (gh を差し替えて in-process で確かめる)。"""

    def setUp(self):
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
        self._env = mock.patch.dict(os.environ, {}, clear=False)
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


class ManualCheckTest(unittest.TestCase):
    def test_manual_check_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_marketplace(Path(tmp) / "repo", ["alpha"])
            write_json(root, ".claude/verify-plugin-release.json", {"fetch": False})
            sh(root, "switch", "-q", "-c", "feat")
            write(root, "plugins/alpha/hooks/alpha/__main__.py", "print('x')\n")
            commit_all(root, "change")
            r = subprocess.run(
                [sys.executable, str(_PKG), "check", str(root)],
                capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("FAIL  version[alpha]", r.stdout)


if __name__ == "__main__":
    unittest.main()
