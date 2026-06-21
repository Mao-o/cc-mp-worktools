"""dispatcher のフロー全体テスト。各 service の verify() は mock で差し替える。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core.dispatcher import dispatch  # noqa: E402


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
