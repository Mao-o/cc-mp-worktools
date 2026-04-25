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


if __name__ == "__main__":
    unittest.main()
