"""dispatcher のフロー全体テスト。各 service の verify() は mock で差し替える。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from core.dispatcher import dispatch  # noqa: E402
from services import ALL as ALL_SERVICES  # noqa: E402


class BaseWithTmpProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.project_dir = Path(self.tmp) / "project"
        self.project_dir.mkdir()
        self.claude_dir = self.project_dir / ".claude"
        self.claude_dir.mkdir()
        self.new_dir = self.claude_dir / "verify-cloud-account"
        self.new_dir.mkdir()

        self._cache_tmp = Path(self.tmp) / "cache_tmp"
        self._cache_tmp.mkdir()

        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.project_dir),
                "TMPDIR": str(self._cache_tmp),
            },
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_accounts(self, data: dict):
        """新パス (`.claude/verify-cloud-account/accounts.local.json`) に書く。"""
        (self.new_dir / "accounts.local.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _write_deprecated_accounts(self, data: dict):
        """旧 deprecated パス (`.claude/accounts.local.json`) に書く。"""
        (self.claude_dir / "accounts.local.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _write_legacy_accounts(self, data: dict):
        """legacy パス (`.claude/accounts.json`) に書く。"""
        (self.claude_dir / "accounts.json").write_text(
            json.dumps(data), encoding="utf-8"
        )


class TestRouting(BaseWithTmpProject):
    def test_no_match_returns_none(self):
        self.assertIsNone(dispatch("git status", str(self.project_dir)))

    def test_readonly_only_returns_none(self):
        self.assertIsNone(dispatch("gh auth status", str(self.project_dir)))

    def test_all_segments_readonly_returns_none(self):
        self.assertIsNone(
            dispatch("gh auth status && gh auth list", str(self.project_dir))
        )

    def test_match_without_accounts_returns_deny(self):
        result = dispatch("gh pr list", str(self.project_dir))
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("accounts.local.json", out["permissionDecisionReason"])


class TestAccountsFile(BaseWithTmpProject):
    def test_malformed_json_returns_deny(self):
        (self.new_dir / "accounts.local.json").write_text(
            "{not json", encoding="utf-8"
        )
        result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("JSON", out["permissionDecisionReason"])

    def test_non_object_returns_deny(self):
        (self.new_dir / "accounts.local.json").write_text(
            '["a", "b"]', encoding="utf-8"
        )
        result = dispatch("gh pr list", str(self.project_dir))
        self.assertEqual(
            result["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_missing_key_returns_deny(self):
        self._write_accounts({"aws": "123456789012"})
        result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("github", out["permissionDecisionReason"])

    def test_invalid_value_type_returns_deny(self):
        self._write_accounts({"github": 12345})
        result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("文字列または", out["permissionDecisionReason"])

    def test_legacy_accounts_json_triggers_warn(self):
        self._write_legacy_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None):
            result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertIn("additionalContext", out)
        self.assertIn("accounts.local.json", out["additionalContext"])


class TestPathMigration(BaseWithTmpProject):
    """新旧パスの 3-tier lookup / 競合検出のテスト (Phase 2)。"""

    def test_new_path_only_is_used(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None):
            result = dispatch("gh pr list", str(self.project_dir))
        self.assertIsNone(result)

    def test_deprecated_path_verifies_with_migration_warn(self):
        """deprecated パスのみ → 動作するが warn で移行案内。"""
        self._write_deprecated_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None):
            result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertIn("additionalContext", out)
        self.assertIn(".claude/verify-cloud-account/accounts.local.json", out["additionalContext"])
        self.assertIn("migrate", out["additionalContext"])

    def test_deprecated_path_with_verify_failure_includes_migration_note(self):
        """deprecated パスで verify 失敗時は deny reason に migration 案内付加。"""
        self._write_deprecated_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value="GitHub 不一致"):
            result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertIn("GitHub 不一致", reason)
        self.assertIn("migrate", reason)

    def test_new_and_deprecated_both_exist_denies(self):
        """新旧両方存在 → fail-closed で deny (D4)。"""
        self._write_accounts({"github": "new-user"})
        self._write_deprecated_accounts({"github": "deprecated-user"})
        result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertIn("複数のパス", reason)
        self.assertIn("(new)", reason)
        self.assertIn("(deprecated)", reason)
        self.assertIn("migrate", reason)

    def test_new_and_legacy_both_exist_denies(self):
        """新 + legacy 両方存在も deny (D4)。"""
        self._write_accounts({"github": "A"})
        self._write_legacy_accounts({"github": "B"})
        result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("複数のパス", out["permissionDecisionReason"])

    def test_conflict_reason_includes_cleanup_hint(self):
        """R4 (P2): conflict 時の deny reason に migrate 後の手動 rm 案内を含む.

        migrate --commit は旧ファイルを残すため、cleanup 案内が無いと
        remediation loop になる。conflict reason に旧ファイルのパスと
        rm コマンドを明示する。"""
        self._write_accounts({"github": "A"})
        self._write_deprecated_accounts({"github": "B"})
        result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        reason = out["permissionDecisionReason"]
        self.assertIn("migrate", reason)
        self.assertIn("rm ", reason)
        # 旧ファイルのパスが reason に明示される
        self.assertIn(".claude/accounts.local.json", reason)

    def test_deprecated_and_legacy_both_exist_denies(self):
        """deprecated + legacy 両方存在も deny (D4)。"""
        self._write_deprecated_accounts({"github": "A"})
        self._write_legacy_accounts({"github": "B"})
        result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("複数のパス", out["permissionDecisionReason"])


class TestServiceInteractions(BaseWithTmpProject):
    def test_verify_success_returns_none(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None):
            result = dispatch("gh pr list", str(self.project_dir))
        self.assertIsNone(result)

    def test_verify_failure_returns_deny(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify",
            return_value="GitHub アカウント不一致: 現在=x, 期待=y",
        ):
            result = dispatch("gh pr list", str(self.project_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("不一致", out["permissionDecisionReason"])

    def test_multiple_services_all_pass(self):
        self._write_accounts({"github": "Mao-o", "gcloud": "my-proj"})
        with mock.patch("services.github.verify", return_value=None), \
             mock.patch("services.gcloud.verify", return_value=None):
            result = dispatch(
                "gh pr list && gcloud run deploy", str(self.project_dir)
            )
        self.assertIsNone(result)

    def test_multiple_services_one_fails(self):
        self._write_accounts({"github": "Mao-o", "gcloud": "my-proj"})
        with mock.patch("services.github.verify", return_value=None), \
             mock.patch("services.gcloud.verify", return_value="GCP不一致"):
            result = dispatch(
                "gh pr list && gcloud run deploy", str(self.project_dir)
            )
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("GCP不一致", out["permissionDecisionReason"])

    def test_wrapper_decomposes_and_verifies(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value=None
        ) as mock_verify:
            result = dispatch("sudo gh pr list", str(self.project_dir))
        self.assertIsNone(result)
        mock_verify.assert_called_once()

    def test_env_prefix_decomposes_and_verifies(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value=None
        ) as mock_verify:
            result = dispatch("FOO=bar gh pr list", str(self.project_dir))
        self.assertIsNone(result)
        mock_verify.assert_called_once()

    def test_cd_chain_decomposes_and_verifies(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value=None
        ) as mock_verify:
            result = dispatch(
                "cd /tmp && gh pr create", str(self.project_dir)
            )
        self.assertIsNone(result)
        mock_verify.assert_called_once()

    def test_same_service_deduplicated(self):
        """同じサービスが複数セグメントで出ても verify は 1 回のみ。"""
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value=None
        ) as mock_verify:
            result = dispatch(
                "gh pr list && gh pr view 123", str(self.project_dir)
            )
        self.assertIsNone(result)
        self.assertEqual(mock_verify.call_count, 1)

    def test_readonly_then_mutating_verifies(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value=None
        ) as mock_verify:
            result = dispatch(
                "gh auth status && gh pr list", str(self.project_dir)
            )
        self.assertIsNone(result)
        mock_verify.assert_called_once()

    def test_npx_firebase_verifies(self):
        self._write_accounts({"firebase": "my-project"})
        with mock.patch(
            "services.firebase.verify", return_value=None
        ) as mock_verify:
            result = dispatch(
                "npx firebase deploy", str(self.project_dir)
            )
        self.assertIsNone(result)
        mock_verify.assert_called_once()


class TestFirebaseResolutionOrderE2E(BaseWithTmpProject):
    """bd_092a232e-629.1: firebase.verify を mock せず subprocess だけ差し替え、
    `.firebaserc` の default と `firebase use` の切替先が食い違うときの決定を
    dispatcher 経由で固定する。"""

    def setUp(self):
        super().setUp()
        # configstore を実環境から読まないよう XDG_CONFIG_HOME を tmp 配下へ。
        self._xdg = Path(self.tmp) / "xdg"
        self._xdg.mkdir()
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._xdg)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_firebaserc(self):
        (self.project_dir / ".firebaserc").write_text(
            json.dumps({"projects": {"default": "proj-dev", "prod": "proj-prod"}}),
            encoding="utf-8",
        )

    def _write_configstore(self, alias_or_project: str):
        store = self._xdg / "configstore" / "firebase-tools.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps(
                {"activeProjects": {os.path.abspath(self.project_dir): alias_or_project}}
            ),
            encoding="utf-8",
        )

    def _run_with_cli_output(self, stdout: str):
        fake = SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        with mock.patch("subprocess.run", return_value=fake):
            return dispatch("firebase deploy", str(self.project_dir))

    def test_npx_without_global_cli_denies_switched_project(self):
        """`npx firebase deploy` で hook の PATH に firebase が無くても、configstore の
        `firebase use prod` 切替を読んで .firebaserc の default (期待値) では allow しない。"""
        self._write_accounts({"firebase": "proj-dev"})
        self._write_firebaserc()
        self._write_configstore("prod")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = dispatch("npx firebase deploy", str(self.project_dir))
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("現在=proj-prod", out["permissionDecisionReason"])

    def test_switched_project_denies_despite_firebaserc_default(self):
        """期待=default (proj-dev) のまま `firebase use prod` 済みなら deploy を deny
        (旧実装は .firebaserc の default で照合し allow = false-allow)。"""
        self._write_accounts({"firebase": "proj-dev"})
        self._write_firebaserc()
        result = self._run_with_cli_output("proj-prod\n")
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("現在=proj-prod", out["permissionDecisionReason"])

    def test_switched_project_allows_when_it_matches_expected(self):
        """期待=proj-prod で `firebase use prod` 済みなら allow
        (旧実装は .firebaserc の default=proj-dev で照合し永久 deny)。"""
        self._write_accounts({"firebase": "proj-prod"})
        self._write_firebaserc()
        self.assertIsNone(self._run_with_cli_output("proj-prod\n"))

    def test_firebase_login_allowed_when_cli_auth_fails_and_configstore_switched(self):
        """未ログイン (`firebase use` が requireAuth で非ゼロ終了) + configstore が期待外
        に切替済みでも、`firebase login` 系は検証せず allow する。login が deny されると
        案内される `firebase use <期待>` も認証必須で失敗し、Claude 内で回復不能になる。
        同じ状態の `firebase deploy` は configstore の切替先で deny される。"""
        self._write_accounts({"firebase": "proj-dev"})
        self._write_firebaserc()
        self._write_configstore("prod")
        fake = SimpleNamespace(stdout="", stderr="Error: not logged in\n", returncode=1)
        for cmd in ("firebase login", "firebase login:ci --no-localhost", "firebase logout"):
            with self.subTest(cmd=cmd):
                with mock.patch("subprocess.run", return_value=fake) as run:
                    self.assertIsNone(dispatch(cmd, str(self.project_dir)))
                run.assert_not_called()
        with mock.patch("subprocess.run", return_value=fake):
            result = dispatch("firebase deploy", str(self.project_dir))
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("現在=proj-prod", out["permissionDecisionReason"])


class TestSelfRemediationFlow(BaseWithTmpProject):
    """deny が案内する切替コマンド (self-remediation) は検証なしで許可される。"""

    def test_switch_to_expected_account_allowed_without_verify(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify") as mock_verify:
            result = dispatch(
                "gh auth switch --hostname github.com --user Mao-o",
                str(self.project_dir),
            )
        self.assertIsNone(result)
        mock_verify.assert_not_called()

    def test_switch_to_other_account_verifies_normally(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value="不一致"
        ) as mock_verify:
            result = dispatch(
                "gh auth switch --user someone-else", str(self.project_dir)
            )
        self.assertIsNotNone(result)
        mock_verify.assert_called_once()

    def test_switch_combined_with_write_verifies_normally(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value="不一致"
        ) as mock_verify:
            result = dispatch(
                "gh auth switch --user Mao-o && gh pr create",
                str(self.project_dir),
            )
        self.assertIsNotNone(result)
        mock_verify.assert_called_once()

    def test_readonly_plus_remediation_allowed(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify") as mock_verify:
            result = dispatch(
                "gh auth status && gh auth switch -u Mao-o",
                str(self.project_dir),
            )
        self.assertIsNone(result)
        mock_verify.assert_not_called()

    def test_remediation_skip_does_not_write_success_cache(self):
        """remediation skip は成功 cache を作らない (直後の write は再検証される)。"""
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify") as mock_verify:
            dispatch("gh auth switch -u Mao-o", str(self.project_dir))
            mock_verify.return_value = "不一致"
            result = dispatch("gh pr create", str(self.project_dir))
        self.assertIsNotNone(result)
        mock_verify.assert_called_once()

    def test_missing_key_still_denies_for_remediation_command(self):
        """期待値が未設定なら切替コマンドも従来どおり設定誘導の deny。"""
        self._write_accounts({"gcloud": "my-proj"})
        result = dispatch(
            "gh auth switch --user Mao-o", str(self.project_dir)
        )
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn('"github" キーがありません', out["permissionDecisionReason"])

    def test_firebase_use_alias_allowed(self):
        self._write_accounts({"firebase": {"default": "proj-dev", "prod": "proj-prod"}})
        with mock.patch("services.firebase.verify") as mock_verify:
            result = dispatch("firebase use prod", str(self.project_dir))
        self.assertIsNone(result)
        mock_verify.assert_not_called()


class TestAncestorLookup(unittest.TestCase):
    """親ディレクトリ遡及による accounts.local.json 発見 (worktree 対応)。

    レイアウトイメージ:
        tmp/parent_repo/                                ← 親 repo (本体)
        tmp/parent_repo/.claude/verify-cloud-account/accounts.local.json
        tmp/parent_repo/worktree-branch/                ← cwd (worktree)
        tmp/parent_repo/worktree-branch/.claude/...     ← (任意で配置)
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

        # 親 repo
        self.parent_dir = Path(self.tmp) / "parent_repo"
        self.parent_dir.mkdir()
        self.parent_claude = self.parent_dir / ".claude"
        self.parent_claude.mkdir()
        self.parent_new_dir = self.parent_claude / "verify-cloud-account"
        self.parent_new_dir.mkdir()

        # worktree (cwd)
        self.worktree_dir = self.parent_dir / "worktree-branch"
        self.worktree_dir.mkdir()

        self._cache_tmp = Path(self.tmp) / "cache_tmp"
        self._cache_tmp.mkdir()

        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.worktree_dir),
                "TMPDIR": str(self._cache_tmp),
            },
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _write_parent_new(self, data: dict):
        (self.parent_new_dir / "accounts.local.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _write_parent_deprecated(self, data: dict):
        (self.parent_claude / "accounts.local.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _write_worktree_new(self, data: dict):
        wt_dir = self.worktree_dir / ".claude" / "verify-cloud-account"
        wt_dir.mkdir(parents=True, exist_ok=True)
        (wt_dir / "accounts.local.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_ancestor_new_path_verifies_silently(self):
        """worktree に accounts なし → 親の新パスを採用し verify 成功 → silent。"""
        self._write_parent_new({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            result = dispatch("gh pr list", str(self.worktree_dir))
        self.assertIsNone(result)
        v.assert_called_once()

    def test_ancestor_new_path_verify_failure_includes_ancestor_note(self):
        """親採用で verify 失敗時、deny reason に親の絶対パスが含まれる。"""
        self._write_parent_new({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value="GitHub 不一致"
        ):
            result = dispatch("gh pr list", str(self.worktree_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertIn("GitHub 不一致", reason)
        # 親遡及採用の注釈 (親 repo の絶対パス) が含まれる
        self.assertIn(str(self.parent_dir), reason)
        self.assertIn("親ディレクトリ", reason)

    def test_worktree_takes_priority_over_ancestor(self):
        """worktree 自身に accounts があれば親は見ない (cwd 優先)。"""
        self._write_parent_new({"github": "parent-user"})
        self._write_worktree_new({"github": "worktree-user"})
        with mock.patch("services.github.verify", return_value=None) as v:
            result = dispatch("gh pr list", str(self.worktree_dir))
        self.assertIsNone(result)
        # worktree 側の値で verify されたことを確認
        self.assertEqual(v.call_args[0][0], "worktree-user")

    def test_ancestor_conflict_returns_deny_with_ancestor_note(self):
        """親階層に複数 tier 同居 → fail-closed deny に親階層注釈付き。"""
        self._write_parent_new({"github": "A"})
        self._write_parent_deprecated({"github": "B"})
        result = dispatch("gh pr list", str(self.worktree_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertIn("複数のパス", reason)
        self.assertIn(str(self.parent_dir), reason)
        self.assertIn("親ディレクトリ", reason)

    def test_no_ancestor_returns_unconfigured_deny(self):
        """親含め一切無い → 通常の「未設定」deny (親注釈なし)。"""
        result = dispatch("gh pr list", str(self.worktree_dir))
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertIn("未設定", reason)
        # 親階層情報は無い
        self.assertNotIn("親ディレクトリ", reason)

    def test_ancestor_deprecated_path_warns_with_both_notes(self):
        """親に deprecated パスがある場合、warn に migration note + 親注釈。"""
        self._write_parent_deprecated({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None):
            result = dispatch("gh pr list", str(self.worktree_dir))
        out = result["hookSpecificOutput"]
        self.assertIn("additionalContext", out)
        ctx = out["additionalContext"]
        # deprecation note と親階層注釈が両方含まれる
        self.assertIn("migrate", ctx)
        self.assertIn("親ディレクトリ", ctx)
        self.assertIn(str(self.parent_dir), ctx)


class TestAncestorDepthLimit(unittest.TestCase):
    """親遡及の max_levels 制限を paths.py 側で検証する単体テスト。"""

    def test_depth_limit_stops_search(self):
        import shutil
        import tempfile
        from core import paths as paths_mod

        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))

        # /tmp/.../a/b/c/d/e/f/g/h/i/j/k/cwd と深くネストして
        # ルート ("/tmp/.../a") に accounts.local.json を置く
        deep = Path(tmp)
        levels = ["L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10", "L11", "cwd"]
        for lv in levels:
            deep = deep / lv
            deep.mkdir()
        anchor = Path(tmp) / "L0"
        (anchor / ".claude" / "verify-cloud-account").mkdir(parents=True)
        (anchor / ".claude" / "verify-cloud-account" / "accounts.local.json").write_text(
            "{}", encoding="utf-8"
        )

        # 制限内 (cwd から 13 階層上は anchor → max_levels=13 で見える)
        found, resolved = paths_mod.discover_accounts_files_with_ancestors(
            str(deep), max_levels=14
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(resolved, anchor.resolve())

        # 制限外 (max_levels=5 だと anchor まで届かない)
        found, resolved = paths_mod.discover_accounts_files_with_ancestors(
            str(deep), max_levels=5
        )
        self.assertEqual(found, [])
        self.assertIsNone(resolved)


class TestCacheIntegration(BaseWithTmpProject):
    def test_second_call_hits_cache(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value=None
        ) as mock_verify:
            dispatch("gh pr list", str(self.project_dir))
            dispatch("gh pr view 1", str(self.project_dir))
        self.assertEqual(mock_verify.call_count, 1)

    def test_accounts_mtime_change_invalidates_cache(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value=None
        ) as mock_verify:
            dispatch("gh pr list", str(self.project_dir))
            # mtime を強制的に変化させる
            accounts_path = self.new_dir / "accounts.local.json"
            stat = accounts_path.stat()
            os.utime(accounts_path, (stat.st_atime + 100, stat.st_mtime + 100))
            dispatch("gh pr list", str(self.project_dir))
        self.assertEqual(mock_verify.call_count, 2)

    def test_failure_is_not_cached(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch(
            "services.github.verify", return_value="不一致"
        ) as mock_verify:
            dispatch("gh pr list", str(self.project_dir))
            dispatch("gh pr view 1", str(self.project_dir))
        self.assertEqual(mock_verify.call_count, 2)


class TestInlineEnvPropagation(BaseWithTmpProject):
    """インライン env がマージされ verify(env=...) に伝播することの統合テスト (要望1)。"""

    def test_inline_env_merged_into_verify(self):
        self._write_accounts({"aws": "123456789012"})
        with mock.patch("services.aws.verify", return_value=None) as mock_verify:
            result = dispatch("AWS_PROFILE=prod aws s3 ls", str(self.project_dir))
        self.assertIsNone(result)
        env = mock_verify.call_args.kwargs.get("env")
        self.assertIsNotNone(env)
        self.assertEqual(env.get("AWS_PROFILE"), "prod")
        # hook プロセスの env もマージされている (PATH 等が温存される)
        self.assertIn("PATH", env)

    def test_no_inline_env_passes_none(self):
        self._write_accounts({"aws": "123456789012"})
        with mock.patch("services.aws.verify", return_value=None) as mock_verify:
            dispatch("aws s3 ls", str(self.project_dir))
        # インライン env 無し → env=None (親環境継承)
        self.assertIsNone(mock_verify.call_args.kwargs.get("env"))

    def test_different_profile_bypasses_cache(self):
        """profile が異なれば cache hit せず再検証される (誤 allow 防止)。"""
        self._write_accounts({"aws": "123456789012"})
        with mock.patch("services.aws.verify", return_value=None) as mock_verify:
            dispatch("AWS_PROFILE=a aws s3 ls", str(self.project_dir))
            dispatch("AWS_PROFILE=b aws s3 ls", str(self.project_dir))
        self.assertEqual(mock_verify.call_count, 2)

    def test_same_profile_uses_cache(self):
        """同一 profile の連続実行は cache hit で verify 1 回。"""
        self._write_accounts({"aws": "123456789012"})
        with mock.patch("services.aws.verify", return_value=None) as mock_verify:
            dispatch("AWS_PROFILE=a aws s3 ls", str(self.project_dir))
            dispatch("AWS_PROFILE=a aws s3 ls", str(self.project_dir))
        self.assertEqual(mock_verify.call_count, 1)

    def test_compound_distinct_profiles_each_verified(self):
        """単一 dispatch の複合コマンドでも profile が異なれば各々検証する (P1 誤 allow 防止)。

        `AWS_PROFILE=prod aws ... && AWS_PROFILE=dev aws ...` で prod だけ検証し
        dev を無検証 allow する穴 (service 単位集約) を塞いだことの回帰テスト。
        """
        self._write_accounts({"aws": "123456789012"})
        with mock.patch("services.aws.verify", return_value=None) as mock_verify:
            dispatch(
                "AWS_PROFILE=prod aws s3 cp a b && AWS_PROFILE=dev aws s3 rm c",
                str(self.project_dir),
            )
        self.assertEqual(mock_verify.call_count, 2)
        seen = {
            (call.kwargs.get("env") or {}).get("AWS_PROFILE")
            for call in mock_verify.call_args_list
        }
        self.assertEqual(seen, {"prod", "dev"})

    def test_compound_same_profile_verified_once(self):
        """複合コマンドでも同一 profile なら (service, env) に集約され verify は 1 回。"""
        self._write_accounts({"aws": "123456789012"})
        with mock.patch("services.aws.verify", return_value=None) as mock_verify:
            dispatch(
                "AWS_PROFILE=prod aws s3 cp a b && AWS_PROFILE=prod aws s3 rm c",
                str(self.project_dir),
            )
        self.assertEqual(mock_verify.call_count, 1)


class TestInfoCommandsReadonly(BaseWithTmpProject):
    """情報系コマンド (version / help) は検証対象外 (要望4)。"""

    def test_command_aws_version_skipped(self):
        self._write_accounts({"aws": "123456789012"})
        # `command` wrapper 剥がし後 `aws --version` → READONLY → 検証スキップ
        self.assertIsNone(dispatch("command aws --version", str(self.project_dir)))

    def test_aws_help_skipped(self):
        self._write_accounts({"aws": "123456789012"})
        self.assertIsNone(dispatch("aws help", str(self.project_dir)))

    def test_gcloud_version_skipped(self):
        self._write_accounts({"gcloud": "p"})
        self.assertIsNone(dispatch("gcloud version", str(self.project_dir)))

    def test_gh_version_skipped(self):
        self._write_accounts({"github": "Mao-o"})
        self.assertIsNone(dispatch("gh --version", str(self.project_dir)))

    def test_kubectl_version_skipped(self):
        self._write_accounts({"kubectl": "ctx"})
        self.assertIsNone(dispatch("kubectl version", str(self.project_dir)))

    def test_firebase_version_skipped(self):
        self._write_accounts({"firebase": "proj"})
        self.assertIsNone(dispatch("firebase --version", str(self.project_dir)))


class TestAccountSwitchInvalidation(BaseWithTmpProject):
    """bd_092a232e-629.3: アカウント状態を変えうるコマンド (切替 / ログイン系) を検出
    したら、当該 service の成功 cache を PreToolUse 時点で破棄し、切替コマンド自身の
    検証成功も cache しない。切替後の最初の write が必ず再検証されることを固定する。
    (0.7.3 までは 30 秒の成功 cache が切替後も有効で、別アカウントの write が通った)"""

    def test_gh_switch_to_other_then_write_reverifies(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            # 1) verify OK → cache 書込
            self.assertIsNone(dispatch("gh pr list", str(self.project_dir)))
            # 2) 期待値以外への切替: cache 破棄 → 実行前の状態を再検証 (OK) → cache なし
            self.assertIsNone(
                dispatch("gh auth switch --user other", str(self.project_dir))
            )
            # 3) 切替後の write は cache hit せず再検証 → 不一致なら deny
            v.return_value = "GitHub [github.com] アカウント不一致: 現在=other, 期待=Mao-o"
            result = dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(v.call_count, 3)
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_switch_itself_does_not_seed_cache(self):
        """切替コマンドの検証成功 (実行前の状態) を cache に残すと、直後の write が
        切替後の状態を検証せずに通る。"""
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            dispatch("gh auth switch --user other", str(self.project_dir))
            dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(v.call_count, 2)

    def test_readonly_login_invalidates_cache(self):
        """`gh auth login --skip-ssh-key` は検証しない (readonly) が、成功 cache は破棄する。"""
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            dispatch("gh pr list", str(self.project_dir))
            self.assertIsNone(dispatch("gh auth login --skip-ssh-key", str(self.project_dir)))
            self.assertEqual(v.call_count, 1)
            dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(v.call_count, 2)

    def test_self_remediation_invalidates_cache(self):
        """期待値への切替 (self-remediation) も無条件に cache を破棄する
        (切替が失敗して状態が変わらなかった場合に古い成功が残らないように)。"""
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            dispatch("gh pr list", str(self.project_dir))
            self.assertIsNone(dispatch("gh auth switch -u Mao-o", str(self.project_dir)))
            self.assertEqual(v.call_count, 1)
            dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(v.call_count, 2)

    def test_every_service_switch_invalidates_cache(self):
        """service ごとの切替 / ログイン系コマンドの後、write が再検証される。"""
        rows = [
            # (accounts key, expected, warm write, switch command, write)
            ("github", "Mao-o", "gh pr list", "gh auth logout", "gh pr create"),
            ("github", "Mao-o", "gh pr list", "gh auth refresh -s repo", "gh pr create"),
            ("gcloud", "my-proj", "gcloud run deploy", "gcloud config set project other", "gcloud run deploy"),
            ("gcloud", "my-proj", "gcloud run deploy", "gcloud config set account x@example.com", "gcloud run deploy"),
            ("gcloud", "my-proj", "gcloud run deploy", "gcloud config configurations activate work", "gcloud run deploy"),
            ("gcloud", "my-proj", "gcloud run deploy", "gcloud auth login", "gcloud run deploy"),
            ("gcloud", "my-proj", "gcloud run deploy", "gcloud auth activate-service-account --key-file=k.json", "gcloud run deploy"),
            ("firebase", "proj-dev", "firebase deploy", "firebase use other", "firebase deploy"),
            ("firebase", "proj-dev", "firebase deploy", "firebase use --clear", "firebase deploy"),
            ("firebase", "proj-dev", "firebase deploy", "firebase login", "firebase deploy"),
            ("firebase", "proj-dev", "firebase deploy", "firebase login:use other@example.com", "firebase deploy"),
            ("kubectl", "ctx", "kubectl apply -f x.yaml", "kubectl config use-context other", "kubectl apply -f x.yaml"),
            ("kubectl", "ctx", "kubectl apply -f x.yaml", "kubectl config set-context --current --namespace=x", "kubectl apply -f x.yaml"),
            ("aws", "123456789012", "aws s3 cp a b", "aws sso login --profile prod", "aws s3 cp a b"),
            ("aws", "123456789012", "aws s3 cp a b", "aws configure", "aws s3 cp a b"),
            ("aws", "123456789012", "aws s3 cp a b", "aws configure set profile.x.region us-east-1", "aws s3 cp a b"),
            ("aws", "123456789012", "aws s3 cp a b", "aws login", "aws s3 cp a b"),
        ]
        for key, expected, warm, switch, write in rows:
            with self.subTest(switch=switch):
                self._write_accounts({key: expected})
                with mock.patch(f"services.{key}.verify", return_value=None) as v:
                    self.assertIsNone(dispatch(warm, str(self.project_dir)))
                    self.assertEqual(v.call_count, 1)
                    self.assertIsNone(dispatch(switch, str(self.project_dir)))
                    after_switch = v.call_count
                    self.assertIsNone(dispatch(write, str(self.project_dir)))
                # 切替後の write は cache hit せず必ず再検証される
                self.assertEqual(v.call_count, after_switch + 1)

    def test_readonly_login_compound_with_write_is_not_cached(self):
        """L2 P1: readonly の login (cands に入らない) と write を同一コマンドで実行
        しても、その service の検証成功は cache しない (判定は service 単位)。"""
        self._write_accounts({"github": "Mao-o", "gcloud": "my-proj"})
        rows = [
            ("github", "gh auth login --with-token < other.txt && gh pr create", "gh pr merge 1"),
            (
                "gcloud",
                "gcloud auth activate-service-account --key-file=sa.json && gcloud run deploy svc",
                "gcloud run deploy svc",
            ),
        ]
        for key, compound, write in rows:
            with self.subTest(compound=compound):
                with mock.patch(f"services.{key}.verify", return_value=None) as v:
                    self.assertIsNone(dispatch(compound, str(self.project_dir)))
                    self.assertEqual(v.call_count, 1)
                    dispatch(write, str(self.project_dir))
                self.assertEqual(v.call_count, 2)

    def test_switch_with_different_inline_env_target_is_not_cached(self):
        """切替セグメントと write セグメントの inline env が異なり別 target になっても、
        同 service の write 側の成功を cache しない。"""
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            self.assertIsNone(
                dispatch(
                    "gh auth switch --user other && GH_HOST=github.com gh pr create",
                    str(self.project_dir),
                )
            )
            self.assertEqual(v.call_count, 2)
            dispatch("GH_HOST=github.com gh pr create", str(self.project_dir))
        self.assertEqual(v.call_count, 3)

    def test_cross_cli_kubeconfig_switch_invalidates_kubectl_cache(self):
        """L2 P2: 別 CLI / plugin が kubeconfig の current-context を書き換える形
        (PATTERNS に一致しない候補を含む) でも kubectl の cache を破棄する。"""
        from core import cache

        self._write_accounts({"kubectl": "ctx", "gcloud": "my-proj", "aws": "123456789012"})
        for switch in (
            "gcloud container clusters get-credentials c --region r",
            "aws eks update-kubeconfig --name c",
            "az aks get-credentials -g rg -n c",
            "kubectx other",
            "kubectl ctx other",
        ):
            with self.subTest(switch=switch):
                cache.invalidate("kubectl")  # 前の subTest の write が残した cache を消す
                with mock.patch("services.kubectl.verify", return_value=None) as v, \
                     mock.patch("services.gcloud.verify", return_value=None), \
                     mock.patch("services.aws.verify", return_value=None):
                    dispatch("kubectl apply -f x.yaml", str(self.project_dir))
                    self.assertEqual(v.call_count, 1)
                    dispatch(switch, str(self.project_dir))
                    # `kubectl ctx other` は kubectl の write として自身も検証される
                    # (cache はしない)。他は kubectl の target にならない
                    after_switch = v.call_count
                    dispatch("kubectl apply -f x.yaml", str(self.project_dir))
                self.assertEqual(v.call_count, after_switch + 1)

    def test_npx_firebase_tools_forms(self):
        """`npx firebase-tools ...` (剥がし後 `firebase-tools ...`) も firebase と同じ
        readonly / self-remediation / 無効化の扱いになる。"""
        self._write_accounts({"firebase": {"default": "proj-dev", "prod": "proj-prod"}})
        with mock.patch("services.firebase.verify", return_value="不一致") as v:
            for cmd in ("npx firebase-tools login", "npx firebase-tools use", "npx firebase-tools use prod"):
                with self.subTest(cmd=cmd):
                    self.assertIsNone(dispatch(cmd, str(self.project_dir)))
            v.assert_not_called()
        with mock.patch("services.firebase.verify", return_value=None) as v:
            dispatch("npx firebase-tools deploy", str(self.project_dir))
            self.assertIsNone(dispatch("npx firebase-tools use other", str(self.project_dir)))
            dispatch("npx firebase-tools deploy", str(self.project_dir))
        self.assertEqual(v.call_count, 3)

    def test_switch_does_not_invalidate_other_services(self):
        self._write_accounts({"github": "Mao-o", "aws": "123456789012"})
        with mock.patch("services.github.verify", return_value=None), \
             mock.patch("services.aws.verify", return_value=None) as aws_v:
            dispatch("aws s3 cp a b", str(self.project_dir))
            dispatch("gh auth switch --user other", str(self.project_dir))
            dispatch("aws s3 cp a b", str(self.project_dir))
        self.assertEqual(aws_v.call_count, 1)

    def test_compound_switch_and_write_is_not_cached(self):
        """`firebase use prod && firebase deploy` (期待値内の切替 + write) は実行前の
        状態で 1 回検証するが、成功を cache しないため次の deploy は再検証される。"""
        self._write_accounts({"firebase": {"default": "proj-dev", "prod": "proj-prod"}})
        with mock.patch("services.firebase.verify", return_value=None) as v:
            self.assertIsNone(
                dispatch("firebase use prod && firebase deploy", str(self.project_dir))
            )
            self.assertEqual(v.call_count, 1)
            dispatch("firebase deploy", str(self.project_dir))
        self.assertEqual(v.call_count, 2)

    def test_display_only_commands_keep_cache(self):
        """状態を変えない readonly (`firebase use` 引数なし / `gh auth status` /
        `gcloud config get-value`) は cache を破棄しない (過剰な再検証を避ける)。"""
        rows = [
            ("github", "Mao-o", "gh pr list", "gh auth status", "gh pr create"),
            ("firebase", "proj-dev", "firebase deploy", "firebase use", "firebase deploy"),
            ("gcloud", "my-proj", "gcloud run deploy", "gcloud config get-value project", "gcloud run deploy"),
            ("kubectl", "ctx", "kubectl apply -f x.yaml", "kubectl config current-context", "kubectl apply -f x.yaml"),
            ("aws", "123456789012", "aws s3 cp a b", "aws sts get-caller-identity", "aws s3 cp a b"),
        ]
        for key, expected, warm, readonly, write in rows:
            with self.subTest(readonly=readonly):
                self._write_accounts({key: expected})
                with mock.patch(f"services.{key}.verify", return_value=None) as v:
                    dispatch(warm, str(self.project_dir))
                    self.assertIsNone(dispatch(readonly, str(self.project_dir)))
                    dispatch(write, str(self.project_dir))
                self.assertEqual(v.call_count, 1)

    def test_switch_invalidates_even_without_accounts_file(self):
        """accounts 未設定 (deny) の状態でも切替コマンドは cache を破棄する
        (設定追加 → 切替 → write の順でも古い成功が残らない)。"""
        from core import cache

        cache.set_success("github", str(self.project_dir), "Mao-o", 1.0)
        # readonly の login は未設定でも allow (deny しない) かつ cache 破棄
        self.assertIsNone(dispatch("gh auth login --skip-ssh-key", str(self.project_dir)))
        self.assertFalse(cache.get_success("github", str(self.project_dir), "Mao-o", 1.0))


class TestLoginCommandsReadonly(BaseWithTmpProject):
    """bd_092a232e-629.2: 認証取得系 (login / logout / configure 等) は資源を変更しない
    ため検証せず allow する。未ログインで検証が失敗する状態でも、deny 文面が案内する
    ログインコマンド自体が deny される remediation loop にならない。"""

    _ACCOUNTS = {
        "github": "Mao-o",
        "aws": "123456789012",
        "gcloud": "my-proj",
        "firebase": "proj-dev",
        "kubectl": "ctx",
    }

    _LOGIN_COMMANDS = [
        "aws sso login",
        "aws sso login --profile prod",
        "aws sso logout",
        "aws login",
        "aws logout",
        "aws configure",
        "aws configure sso",
        "aws configure sso-session",
        "aws configure list-profiles",
        "aws configure set region us-east-1 --profile prod",
        "gh auth login --skip-ssh-key",
        "gh auth login --skip-ssh-key=true",
        "gh auth login --hostname ghe.example.com --skip-ssh-key",
        "gh auth login --with-token < token.txt",
        "gh auth login --with-token=1 < token.txt",
        "gh auth login --git-protocol https",
        "gh auth login -p https --web",
        "gh auth login --git-protocol=https --hostname ghe.example.com",
        "gh auth logout --hostname github.com",
        # `gh auth refresh` はここに置かない (Codex R4 P1) —
        # test_similar_write_commands_still_verified 側で deny を assert する
        "gh auth setup-git",
        "gcloud auth login",
        "gcloud auth login --update-adc",
        "gcloud auth application-default login",
        "gcloud auth activate-service-account --key-file=key.json",
        "gcloud auth revoke",
        "firebase login",
        "firebase login:ci",
        "firebase logout",
        "npx firebase-tools login",
        "npx firebase-tools use",
    ]

    def _patch_all_verify_to_deny(self):
        patchers = []
        for svc in ALL_SERVICES:
            name = svc.__name__.rsplit(".", 1)[-1]
            p = mock.patch(f"services.{name}.verify", return_value="未ログイン (検証失敗)")
            patchers.append((name, p.start()))
            self.addCleanup(p.stop)
        return dict(patchers)

    def test_login_commands_allowed_without_verify(self):
        self._write_accounts(self._ACCOUNTS)
        mocks = self._patch_all_verify_to_deny()
        for cmd in self._LOGIN_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(dispatch(cmd, str(self.project_dir)))
        for name, m in mocks.items():
            self.assertEqual(m.call_count, 0, name)

    def test_login_commands_allowed_without_accounts_file(self):
        """accounts.local.json 未設定でもログイン系は deny しない (初期設定前の
        `aws sso login` 等が止まらない)。"""
        for cmd in self._LOGIN_COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(dispatch(cmd, str(self.project_dir)))

    def test_similar_write_commands_still_verified(self):
        """ログイン系に似た名前の write / 認証情報を出力する読取 / 期待値以外への切替 /
        リモートの認可を変更しうる認証系 (`gh auth refresh` の scope 変更) は
        従来どおり検証される。"""
        self._write_accounts(self._ACCOUNTS)
        self._patch_all_verify_to_deny()
        for cmd in (
            "aws sso-admin create-permission-set --name x",
            "aws sso list-accounts --access-token t",
            "aws configure export-credentials --profile prod",
            "gh repo create foo",
            "gh auth token",
            # SSH 鍵のアップロードが起きうる login 形は readonly にしない (Codex R2 P1-1)
            "gh auth login",
            "gh auth login --web",
            "gh auth login --hostname ghe.example.com",
            "gh auth login --git-protocol ssh",
            "gh auth login -p ssh --skip-ssh-keys",
            # 明示 false は無効 (Codex R3 P1)
            "gh auth login --git-protocol ssh --skip-ssh-key=false",
            "gh auth login --with-token=false < token.txt",
            "gh auth login -p https --git-protocol ssh",
            # `gh auth refresh` はアカウント側の OAuth grant scope を変えうる
            # (Codex R4 P1)。裸形・scope 指定のどちらも検証対象。
            "gh auth refresh",
            "gh auth refresh -s repo",
            "gh auth refresh --scopes admin:org",
            "gh auth refresh --remove-scopes repo",
            "gcloud config set project other",
            "gcloud auth application-default print-access-token",
            "firebase use other",
            "kubectl config use-context other",
        ):
            with self.subTest(cmd=cmd):
                result = dispatch(cmd, str(self.project_dir))
                self.assertIsNotNone(result)
                self.assertEqual(
                    result["hookSpecificOutput"]["permissionDecision"], "deny"
                )


_CLI_CMD_RE = re.compile(
    r"(?<![\w/.-])"                      # 直前が単語 / パス文字でない (ghe.example.com の gh 等を除外)
    r"((?:[A-Z][A-Z0-9_]*=\S+\s+)?"      # 任意の行頭インライン env (AWS_PROFILE=prod)
    r"(?:gh|firebase|aws|gcloud|kubectl)"
    r"(?:\s+[A-Za-z0-9_.:/=<>@-]+)+)"    # ASCII 引数 1 個以上 (日本語に当たった時点で終端)
)


def _guided_commands(reason: str) -> list[str]:
    """deny reason から案内されているコマンド行を抽出する (出現順・重複なし)。

    `(検出コマンド: ...)` (D14) は deny を起こしたコマンド自身なので除外する。
    `&&` は区切りとして扱い、`firebase login && firebase use x` は 2 コマンドになる。
    """
    cmds: list[str] = []
    for line in reason.splitlines():
        if line.lstrip().startswith("(検出コマンド:"):
            continue
        for m in _CLI_CMD_RE.finditer(line):
            cmd = m.group(1).strip()
            if cmd not in cmds:
                cmds.append(cmd)
    return cmds


class TestRemediationGuidanceContract(BaseWithTmpProject):
    """bd_092a232e-629.2 contract: deny 文面が案内するコマンドは必ず allow 経路にある。

    各 service の verify を mock せず subprocess だけ差し替えて実際の deny 文面を
    作り、そこから案内コマンドを抽出して dispatcher に通す。案内コマンドは
    (a) readonly または self-remediation として **検証なしで allow** されるか、
    (b) `AWS_PROFILE=<profile> aws ...` の行頭インライン指定は **その env で検証**
    される (案内どおりに打てば検証に反映される) かのどちらかでなければならない。
    deny → 案内どおり実行 → また deny、の remediation loop をここで機械的に防ぐ。
    """

    def setUp(self):
        super().setUp()
        home = Path(self.tmp) / "home"
        (home / ".aws").mkdir(parents=True)
        xdg = Path(self.tmp) / "xdg"
        xdg.mkdir()
        patcher = mock.patch.dict(
            os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = home

    def _deny_reason(self, command: str, run_mock) -> str:
        with mock.patch("subprocess.run", side_effect=run_mock):
            result = dispatch(command, str(self.project_dir))
        self.assertIsNotNone(result, f"{command} should be denied")
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]

    def _assert_guidance_is_allowed(self, reason: str, run_mock) -> list[str]:
        cmds = _guided_commands(reason)
        self.assertTrue(cmds, f"no command extracted from:\n{reason}")
        for cmd in cmds:
            with self.subTest(guided=cmd):
                if cmd.startswith("AWS_PROFILE="):
                    # 行頭インライン指定は「その profile で検証される」ことが契約
                    profile = cmd.split()[0].split("=", 1)[1]
                    concrete = cmd.replace("aws ...", "aws s3 cp a s3://b")
                    with mock.patch("subprocess.run", side_effect=run_mock) as run:
                        dispatch(concrete, str(self.project_dir))
                    self.assertTrue(run.called, concrete)
                    env = run.call_args.kwargs.get("env") or {}
                    self.assertEqual(env.get("AWS_PROFILE"), profile)
                    continue
                with mock.patch("subprocess.run", side_effect=run_mock) as run:
                    result = dispatch(cmd, str(self.project_dir))
                self.assertIsNone(result, f"guided command denied: {cmd}\n{reason}")
                self.assertFalse(run.called, f"guided command was verified: {cmd}")
        return cmds

    @staticmethod
    def _const(stdout="", stderr="", returncode=0):
        fake = SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
        return lambda *_a, **_k: fake

    # --- GitHub ---

    def test_github_scalar_mismatch(self):
        self._write_accounts({"github": "Mao-o"})
        run = self._const(
            "github.com\n  ✓ Logged in to github.com account other (keyring)\n"
            "  - Active account: true\n"
        )
        reason = self._deny_reason("gh pr create", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("gh auth switch --hostname github.com --user Mao-o", cmds)

    def test_github_host_not_logged_in(self):
        self._write_accounts(
            {"github": {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}}
        )
        run = self._const(
            "github.com\n  ✓ Logged in to github.com account Mao-o (keyring)\n"
            "  - Active account: true\n"
        )
        reason = self._deny_reason("gh pr create", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("gh auth login --hostname ghe.example.com --skip-ssh-key", cmds)

    def test_github_no_active_account(self):
        """未ログイン時の案内は SSH 鍵アップロードが起きない `--skip-ssh-key` 付きの形
        (素の `gh auth login` は readonly ではないので案内すると loop になる)。"""
        self._write_accounts({"github": "Mao-o"})
        run = self._const("", "You are not logged into any GitHub hosts.\n", 1)
        reason = self._deny_reason("gh pr create", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("gh auth login --skip-ssh-key", cmds)
        self.assertNotIn("gh auth login", cmds)

    # --- Firebase ---

    def test_firebase_scalar_mismatch(self):
        self._write_accounts({"firebase": "proj-dev"})
        run = self._const("proj-other\n")
        reason = self._deny_reason("firebase deploy", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("firebase use proj-dev", cmds)

    def test_firebase_dict_mismatch_lists_aliases(self):
        self._write_accounts({"firebase": {"default": "proj-dev", "prod": "proj-prod"}})
        run = self._const("proj-other\n")
        reason = self._deny_reason("firebase deploy", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("firebase use default", cmds)
        self.assertIn("firebase use prod", cmds)

    def test_firebase_unresolved_suggests_login_and_use(self):
        """未ログイン (`firebase use` 非ゼロ終了) + ローカル設定なし → `firebase login`
        と `firebase use <期待>` を案内。両方とも検証なしで通る。"""
        self._write_accounts({"firebase": "proj-dev"})
        run = self._const("", "Error: not logged in\n", 1)
        with mock.patch("services.firebase.shutil.which", return_value="/usr/bin/firebase"):
            reason = self._deny_reason("firebase deploy", run)
            cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("firebase login", cmds)
        self.assertIn("firebase use proj-dev", cmds)

    def test_firebase_dict_unresolved_lists_aliases(self):
        """L2 P2: dict 期待値 + 未解決のときは `firebase use YOUR_PROJECT` (placeholder、
        self-remediation に乗らず loop) ではなく alias ごとの具体コマンドを案内する。"""
        self._write_accounts({"firebase": {"default": "proj-dev", "prod": "proj-prod"}})
        run = self._const("", "Error: not logged in\n", 1)
        with mock.patch("services.firebase.shutil.which", return_value="/usr/bin/firebase"):
            reason = self._deny_reason("firebase deploy", run)
            cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("firebase login", cmds)
        self.assertIn("firebase use default", cmds)
        self.assertIn("firebase use prod", cmds)
        self.assertNotIn("YOUR_PROJECT", reason)

    # --- AWS ---

    def _write_aws_config(self, text: str) -> None:
        (self.home / ".aws" / "config").write_text(text, encoding="utf-8")

    def test_aws_mismatch_with_profile_in_config(self):
        self._write_accounts({"aws": "123456789012"})
        self._write_aws_config(
            "[profile prod]\nsso_session = corp\nsso_account_id = 123456789012\n"
            "[sso-session corp]\nsso_start_url = https://corp.awsapps.com/start\n"
        )
        run = self._const("111111111111\n")
        reason = self._deny_reason("aws s3 cp a s3://b", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("AWS_PROFILE=prod aws ...", cmds)
        self.assertIn("aws sso login --profile prod", cmds)
        # export は「コマンド」として案内しない (Claude Code の Bash では効かない)
        self.assertNotRegex(reason, r"(?m)^\s+export\s")
        # config の他の値 (sso_start_url 等) を文面に漏らさない
        self.assertNotIn("awsapps", reason)

    def test_aws_no_credentials_without_config(self):
        self._write_accounts({"aws": "123456789012"})
        run = self._const("", "Error loading SSO Token: Token for corp does not exist\n", 255)
        reason = self._deny_reason("aws s3 cp a s3://b", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("aws sso login --profile <profile>", cmds)
        self.assertIn("aws configure", cmds)
        self.assertIn("aws configure list-profiles", cmds)

    # --- GCP ---

    def test_gcloud_project_unset(self):
        self._write_accounts({"gcloud": "my-proj"})
        run = self._const("(unset)\n")
        reason = self._deny_reason("gcloud run deploy", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("gcloud config set project my-proj", cmds)

    def test_gcloud_dict_account_mismatch(self):
        self._write_accounts({"gcloud": {"project": "my-proj", "account": "me@example.com"}})

        def run(cmd, **_kw):
            value = "my-proj\n" if cmd[-1] == "project" else "other@example.com\n"
            return SimpleNamespace(stdout=value, stderr="", returncode=0)

        reason = self._deny_reason("gcloud run deploy", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("gcloud config set account me@example.com", cmds)

    # --- kubectl ---

    def test_kubectl_context_mismatch(self):
        self._write_accounts({"kubectl": "prod-ctx"})
        run = self._const("dev-ctx\n")
        reason = self._deny_reason("kubectl apply -f x.yaml", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("kubectl config use-context prod-ctx", cmds)

    def test_kubectl_context_unset(self):
        self._write_accounts({"kubectl": "prod-ctx"})
        run = self._const("", "error: current-context is not set\n", 1)
        reason = self._deny_reason("kubectl apply -f x.yaml", run)
        cmds = self._assert_guidance_is_allowed(reason, run)
        self.assertIn("kubectl config use-context prod-ctx", cmds)

    # --- 未設定 deny の SETUP_HINT ---

    def test_setup_hints_commands_are_readonly(self):
        """accounts 未設定 deny が案内する「現在値の確認コマンド」は accounts 無しでも通る。"""
        run = self._const("", "", 1)
        for write in (
            "gh pr create",
            "firebase deploy",
            "aws s3 cp a s3://b",
            "gcloud run deploy",
            "kubectl apply -f x.yaml",
        ):
            with self.subTest(write=write):
                reason = self._deny_reason(write, run)
                self.assertIn("未設定", reason)
                self._assert_guidance_is_allowed(reason, run)


class TestConcurrentInvalidationRace(BaseWithTmpProject):
    """PR #43 Codex R2 P1-2: 同じ service の Bash hook が並行したとき、切替前の状態を
    検証した結果が切替後に公開されない (epoch + in-flight 窓)。"""

    def test_verify_started_before_invalidate_is_not_published(self):
        """hook B (`gh pr list`) の verify 中に hook A (`gh auth switch`) の PreToolUse が
        cache を無効化 → B の成功は (開始時 epoch が古いので) 書かれない。"""
        from core import cache

        self._write_accounts({"github": "Mao-o"})
        calls = []

        def verify_then_concurrent_switch(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                cache.invalidate("github")  # B の検証中に A の無効化が走った
            return None

        with mock.patch.object(cache, "IN_FLIGHT_SEC", 0), \
             mock.patch("services.github.verify", side_effect=verify_then_concurrent_switch) as v:
            self.assertIsNone(dispatch("gh pr list", str(self.project_dir)))  # B
            self.assertIsNone(dispatch("gh pr create", str(self.project_dir)))  # 切替後の write
        self.assertEqual(v.call_count, 2)

    def test_verify_started_after_invalidate_within_window_is_not_published(self):
        """A: `gh auth switch --user other` の PreToolUse (無効化) → A の切替が完了する前に
        B: `gh pr list` が旧状態を検証して成功 → in-flight 窓内なので entry は書かれず、
        次の write は再検証される。"""
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            self.assertIsNone(dispatch("gh auth switch --user other", str(self.project_dir)))
            self.assertIsNone(dispatch("gh pr list", str(self.project_dir)))  # B (窓内)
            self.assertEqual(v.call_count, 2)
            dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(v.call_count, 3)

    def test_cache_resumes_after_in_flight_window(self):
        """窓を過ぎれば通常どおり成功が cache される (窓内の再検証は一時的なコスト)。"""
        from core import cache

        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value=None) as v:
            dispatch("gh auth switch --user other", str(self.project_dir))
            with mock.patch.object(cache, "IN_FLIGHT_SEC", 0):  # 60 秒経過した状況
                dispatch("gh pr list", str(self.project_dir))
                dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(v.call_count, 2)


class TestLeadingGlobalOptions(BaseWithTmpProject):
    """PR #43 Codex P1: `aws --profile prod sso login` のように CLI 名直後に global
    option が置かれた形 (`aws [global options] <command> <subcommand>`) でも、
    剥がした形で readonly / 切替 (cache 無効化) / self-remediation を判定する。"""

    _ACCOUNTS = {
        "github": "Mao-o",
        "aws": "123456789012",
        "gcloud": "my-proj",
        "firebase": "proj-dev",
        "kubectl": "ctx",
    }

    def test_login_with_leading_options_is_readonly(self):
        self._write_accounts(self._ACCOUNTS)
        rows = [
            ("aws", "aws --profile prod sso login"),
            ("aws", "aws --profile=prod sso login"),
            ("aws", "aws --region us-east-1 --profile prod configure sso"),
            ("aws", "aws --debug --no-verify-ssl --output json sso logout"),
            ("aws", "aws --profile default configure sso"),
            ("aws", "aws --profile prod login"),
            ("gcloud", "gcloud --account me@example.com auth login"),
            ("gcloud", "gcloud --configuration work --quiet auth activate-service-account --key-file=k.json"),
            ("gcloud", "gcloud --project x config get-value project"),
            ("kubectl", "kubectl --context foo -n ns config current-context"),
            ("kubectl", "kubectl --kubeconfig k.yaml config view"),
            ("firebase", "firebase -P prod login"),
            ("firebase", "firebase --debug use"),
            ("firebase", "npx firebase-tools --project prod login:ci"),
        ]
        for key, cmd in rows:
            with self.subTest(cmd=cmd):
                with mock.patch(f"services.{key}.verify", return_value="不一致") as v:
                    self.assertIsNone(dispatch(cmd, str(self.project_dir)))
                v.assert_not_called()

    def test_switch_with_leading_options_invalidates_cache(self):
        """P1 の再現: `aws s3 cp` (検証成功・cache) → `aws --profile default configure sso`
        → `aws s3 cp` が cache hit せず再検証される。"""
        rows = [
            ("aws", "aws s3 cp a b", "aws --profile default configure sso"),
            ("aws", "aws s3 cp a b", "aws --profile prod sso login"),
            ("aws", "aws s3 cp a b", "aws --region us-east-1 --profile=prod sso login"),
            ("gcloud", "gcloud run deploy", "gcloud --project x config set project other"),
            ("gcloud", "gcloud run deploy", "gcloud --quiet auth login"),
            ("kubectl", "kubectl apply -f x.yaml", "kubectl --context foo config use-context other"),
            ("firebase", "firebase deploy", "firebase --project prod use other"),
            ("firebase", "firebase deploy", "firebase -P prod login"),
        ]
        for key, write, switch in rows:
            with self.subTest(switch=switch):
                self._write_accounts({key: self._ACCOUNTS[key]})
                with mock.patch(f"services.{key}.verify", return_value=None) as v:
                    dispatch(write, str(self.project_dir))
                    self.assertEqual(v.call_count, 1)
                    self.assertIsNone(dispatch(switch, str(self.project_dir)))
                    after_switch = v.call_count
                    dispatch(write, str(self.project_dir))
                self.assertEqual(v.call_count, after_switch + 1)

    def test_cross_cli_kubeconfig_switch_with_leading_options(self):
        from core import cache

        self._write_accounts({"kubectl": "ctx", "aws": "123456789012", "gcloud": "my-proj"})
        for switch in (
            "aws --profile prod eks update-kubeconfig --name c",
            "gcloud --project x container clusters get-credentials c --region r",
        ):
            with self.subTest(switch=switch):
                cache.invalidate("kubectl")
                with mock.patch("services.kubectl.verify", return_value=None) as v, \
                     mock.patch("services.aws.verify", return_value=None), \
                     mock.patch("services.gcloud.verify", return_value=None):
                    dispatch("kubectl apply -f x.yaml", str(self.project_dir))
                    dispatch(switch, str(self.project_dir))
                    dispatch("kubectl apply -f x.yaml", str(self.project_dir))
                self.assertEqual(v.call_count, 2)

    def test_self_remediation_with_leading_options(self):
        self._write_accounts(self._ACCOUNTS)
        for key, cmd in (
            ("gcloud", "gcloud --quiet config set project my-proj"),
            ("kubectl", "kubectl --kubeconfig k.yaml config use-context ctx"),
            ("firebase", "firebase --debug use proj-dev"),
        ):
            with self.subTest(cmd=cmd):
                with mock.patch(f"services.{key}.verify", return_value="不一致") as v:
                    self.assertIsNone(dispatch(cmd, str(self.project_dir)))
                v.assert_not_called()

    def test_deny_reason_shows_original_candidate(self):
        """剥がした形は判定にだけ使い、deny 文面の検出コマンドは元の形 (option 付き)。"""
        self._write_accounts({"aws": "123456789012"})
        with mock.patch(
            "services.aws.verify", return_value="AWS アカウント不一致: 現在=x, 期待=y"
        ):
            result = dispatch("aws --profile other s3 rm s3://x", str(self.project_dir))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("検出コマンド: aws --profile other s3 rm s3://x", reason)

    def test_unknown_leading_option_is_verified_conservatively(self):
        """未知の option が先頭にあれば剥がさず通常検証 (readonly に乗らない)。"""
        self._write_accounts({"aws": "123456789012"})
        with mock.patch("services.aws.verify", return_value="不一致") as v:
            result = dispatch("aws --totally-unknown-option sso login", str(self.project_dir))
        self.assertIsNotNone(result)
        v.assert_called_once()

    def test_version_and_help_remain_readonly(self):
        """`aws --version` は剥がすと `aws` になるため元の形でも判定する。"""
        self._write_accounts(self._ACCOUNTS)
        for key, cmd in (
            ("aws", "aws --version"),
            ("gcloud", "gcloud --help"),
            ("github", "gh --version"),
            ("kubectl", "kubectl --help"),
            ("firebase", "firebase --version"),
        ):
            with self.subTest(cmd=cmd):
                with mock.patch(f"services.{key}.verify", return_value="不一致") as v:
                    self.assertIsNone(dispatch(cmd, str(self.project_dir)))
                v.assert_not_called()


class TestDenyProvenance(BaseWithTmpProject):
    """deny に出所タグ (要望3) と検出セグメント (要望5) が含まれる。"""

    def test_deny_has_source_tag(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value="GitHub 不一致"):
            result = dispatch("gh pr create", str(self.project_dir))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[verify-cloud-account]", reason)
        self.assertIn("CLI 本体のエラーではありません", reason)

    def test_deny_has_detected_segment(self):
        self._write_accounts({"github": "Mao-o"})
        with mock.patch("services.github.verify", return_value="GitHub 不一致"):
            result = dispatch("gh pr create", str(self.project_dir))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("検出コマンド: gh pr create", reason)

    def test_unconfigured_deny_also_tagged(self):
        # accounts 未設定 deny にも出所タグが付く (検出セグメントは verify 前なので無し)
        result = dispatch("gh pr create", str(self.project_dir))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[verify-cloud-account]", reason)


if __name__ == "__main__":
    unittest.main()
