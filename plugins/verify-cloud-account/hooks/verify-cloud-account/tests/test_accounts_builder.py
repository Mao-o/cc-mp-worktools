"""accounts_builder の main() ベースのテスト。

subprocess ではなく main() を import で直接呼ぶ (fast + 診断容易)。
tmpdir + CLAUDE_PROJECT_DIR を patch してファイル I/O の実経路を検証する。

D2/D3 特化カバレッジ:
- 書込対象は `accounts.local.json` に固定 (argv 経由で変わらない)
- 既定では値が stdout に出ない
- `--show-values` 明示時のみ露出

migrate 3 シナリオ:
- 新のみ → no-op
- 旧のみ → 旧 → 新にコピー
- 両方 (値一致) → 旧キーだけ merge
- 両方 (値衝突) → deny
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from core import paths  # noqa: E402
from scripts import accounts_builder as builder  # noqa: E402


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class BaseBuilder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.project_dir = Path(self.tmp) / "project"
        self.project_dir.mkdir()
        self.new_dir = self.project_dir / ".claude" / "verify-cloud-account"
        self.claude_dir = self.project_dir / ".claude"
        self.claude_dir.mkdir()

        self._env_patcher = mock.patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(self.project_dir)},
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        code = builder.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def _new_path(self) -> Path:
        return self.new_dir / "accounts.local.json"

    def _deprecated_path(self) -> Path:
        return self.claude_dir / "accounts.local.json"

    def _legacy_path(self) -> Path:
        return self.claude_dir / "accounts.json"


class TestInitDryRun(BaseBuilder):
    def test_dry_run_does_not_write(self):
        code, out, err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("+ add", out)
        self.assertIn("github", out)
        self.assertIn("(dry-run", out)
        self.assertFalse(self._new_path().exists())

    def test_dry_run_is_default(self):
        """--dry-run も --commit も指定しなければ dry-run。"""
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o"]
        )
        self.assertEqual(code, 0)
        self.assertFalse(self._new_path().exists())
        self.assertIn("(dry-run", out)


class TestInitCommit(BaseBuilder):
    def test_commit_writes_new_path(self):
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(self._new_path().exists())
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})
        self.assertIn("written:", out)

    def test_commit_creates_parent_dirs(self):
        """verify-cloud-account/ サブディレクトリが無くても作られる。"""
        self.assertFalse(self.new_dir.exists())
        code, _out, _err = self._run(
            ["init", "--service", "aws", "--value", "123456789012", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(self.new_dir.exists())

    def test_preserves_existing_keys(self):
        """既存キーは init では触らない (別 service を add しても残る)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "existing-user"}), encoding="utf-8"
        )
        code, _out, _err = self._run(
            ["init", "--service", "aws", "--value", "111", "--commit"]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "existing-user", "aws": "111"})

    def test_existing_same_key_same_value_is_noop(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("= unchanged", out)

    def test_existing_same_key_different_value_skipped(self):
        """既存キーで値が異なる場合は skip (overwrite しない)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "existing-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "new-user", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "existing-user"})


class TestInitRefusesOnLegacyPaths(BaseBuilder):
    """R2 (P1) 対応: 旧パス存在時の init refuse + migrate 誘導.

    init が常に新パスに書き込むため、旧パスのみ存在する状態で実行すると
    新パスに 2 つ目のファイルができ、dispatcher の _find_accounts_file が
    複数パス conflict で fail-closed deny に回帰する。これを防ぐため init
    側で旧パス存在を検出して refuse + migrate 誘導する。
    """

    def test_init_refuses_when_only_deprecated_exists(self):
        self._deprecated_path().write_text(
            json.dumps({"github": "old-user"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["init", "--service", "aws", "--value", "111", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("旧パス", err)
        self.assertIn("migrate", err)
        self.assertFalse(self._new_path().exists())  # 新パスは作られない

    def test_init_refuses_when_only_legacy_exists(self):
        self._legacy_path().write_text(
            json.dumps({"github": "older-user"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["init", "--service", "aws", "--value", "111", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("legacy", err)
        self.assertFalse(self._new_path().exists())

    def test_init_refuses_when_new_and_deprecated_both_exist(self):
        """既に競合状態 (new + deprecated 両方) → refuse + migrate 誘導."""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "new-user"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"aws": "111"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["init", "--service", "gcloud", "--value", "p", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("旧パス", err)

    def test_init_succeeds_when_only_new_exists(self):
        """regression: 新パスのみ存在時は通常動作 (旧 R2 fix の副作用なし)."""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "user-a"}), encoding="utf-8"
        )
        code, _out, _err = self._run(
            ["init", "--service", "aws", "--value", "111", "--commit"]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "user-a", "aws": "111"})


class TestInitMalformedJson(BaseBuilder):
    def test_malformed_existing_json_rejected(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text("{not json", encoding="utf-8")
        code, _out, err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("JSON", err)
        self.assertIn("手動で修正", err)

    def test_non_object_existing_rejected(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text("[1,2,3]", encoding="utf-8")
        code, _out, err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("オブジェクト", err)


class TestValueHiding(BaseBuilder):
    def test_default_hides_values(self):
        """D3: --show-values なしでは stdout に value が出ない。"""
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "secret-user", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("secret-user", out)
        self.assertIn("+ add: github", out)
        self.assertIn("value hidden", out)

    def test_show_values_reveals(self):
        """D3: --show-values で value が stdout に出る。"""
        code, out, _err = self._run(
            [
                "init", "--service", "github", "--value", "secret-user",
                "--dry-run", "--show-values",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("secret-user", out)

    def test_commit_hides_values_by_default(self):
        """D3: commit 時の出力も既定で値隠蔽。"""
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "secret-user", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("secret-user", out)


class TestWriteTargetFixed(BaseBuilder):
    def test_writes_only_to_accounts_local_json(self):
        """D2: ACCOUNTS_FILE_NEW が accounts.local.json でなければ assertion 失敗。"""
        evil_path = Path(".claude") / "verify-cloud-account" / "evil.json"
        with mock.patch.object(paths, "ACCOUNTS_FILE_NEW", evil_path):
            with self.assertRaises(AssertionError):
                self._run(
                    ["init", "--service", "github", "--value", "u", "--commit"]
                )

    def test_argv_cannot_redirect_write_target(self):
        """D2: argv に任意のパスフラグが存在しない (argparse で unknown 扱い)。"""
        # --output / --path / --target が accepts されないことを検証
        for flag in ("--output", "--path", "--target", "--file"):
            code, _out, _err = self._run(
                ["init", "--service", "github", "--value", "u", "--commit", flag, "/tmp/evil.json"]
            )
            self.assertEqual(code, 2, f"flag {flag!r} should be rejected by argparse")


class TestInitSuggestion(BaseBuilder):
    def test_suggest_from_service_when_value_absent(self):
        """--value 省略時は suggest_accounts_entry() で自動取得。"""
        with mock.patch(
            "services.github.suggest_accounts_entry", return_value="auto-user"
        ):
            code, _out, _err = self._run(
                ["init", "--service", "github", "--commit"]
            )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "auto-user"})

    def test_suggest_returns_none_errors(self):
        with mock.patch(
            "services.github.suggest_accounts_entry", return_value=None
        ):
            code, _out, err = self._run(
                ["init", "--service", "github", "--commit"]
            )
        self.assertEqual(code, 1)
        self.assertIn("CLI から取得できませんでした", err)
        self.assertFalse(self._new_path().exists())


class TestShow(BaseBuilder):
    def test_show_no_file(self):
        code, out, _err = self._run(["show"])
        self.assertEqual(code, 0)
        self.assertIn("no accounts.local.json", out)

    def test_show_match(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        with mock.patch(
            "services.github.get_active_account",
            return_value={"github.com": "Mao-o"},
        ):
            code, out, _err = self._run(["show"])
        self.assertEqual(code, 0)
        self.assertIn("[match]", out)
        self.assertNotIn("Mao-o", out)  # default: values hidden

    def test_show_mismatch_with_values(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        with mock.patch(
            "services.github.get_active_account",
            return_value={"github.com": "other-user"},
        ):
            code, out, _err = self._run(["show", "--show-values"])
        self.assertEqual(code, 0)
        self.assertIn("[mismatch]", out)
        self.assertIn("Mao-o", out)  # values revealed

    def test_show_match_dict_expected_str_current(self):
        """Firebase の alias map (dict expected) + scalar current が
        map の任意 value に一致 → [match] (Codex P2 / R1 対応)."""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"firebase": {"default": "proj-dev", "prod": "proj-prod"}}),
            encoding="utf-8",
        )
        with mock.patch(
            "services.firebase.get_active_account",
            return_value="proj-dev",
        ):
            code, out, _err = self._run(["show"])
        self.assertEqual(code, 0)
        self.assertIn("[match]", out)
        self.assertNotIn("[mismatch]", out)

    def test_show_mismatch_when_expected_matches_only_non_first_host(self):
        """multi-host で expected が 2 つ目以降のホスト value にしか一致しない場合
        は [mismatch] (R3 / P2: services/github.py::verify と整合)."""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "bob"}), encoding="utf-8"
        )
        with mock.patch(
            "services.github.get_active_account",
            return_value={"github.com": "alice", "ghe": "bob"},
        ):
            code, out, _err = self._run(["show"])
        self.assertEqual(code, 0)
        self.assertIn("[mismatch]", out)

    def test_show_mismatch_dict_expected_str_current_outside_map(self):
        """alias map のいずれの value にも一致しない scalar current → [mismatch]."""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"firebase": {"default": "proj-dev", "prod": "proj-prod"}}),
            encoding="utf-8",
        )
        with mock.patch(
            "services.firebase.get_active_account",
            return_value="proj-staging",
        ):
            code, out, _err = self._run(["show"])
        self.assertEqual(code, 0)
        self.assertIn("[mismatch]", out)

    def test_show_denies_on_path_conflict(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "A"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "B"}), encoding="utf-8"
        )
        code, _out, err = self._run(["show"])
        self.assertEqual(code, 1)
        self.assertIn("複数のパス", err)


class TestMigrateScenarios(BaseBuilder):
    def test_migrate_new_only_noop(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to migrate", out)

    def test_migrate_deprecated_only_copies_to_new(self):
        self._deprecated_path().write_text(
            json.dumps({"github": "legacy-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        self.assertTrue(self._new_path().exists())
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "legacy-user"})
        self.assertIn("merged from deprecated", out)
        self.assertIn("rm ", out)
        self.assertTrue(self._deprecated_path().exists())  # 旧パスは保持

    def test_migrate_legacy_only_copies_to_new(self):
        self._legacy_path().write_text(
            json.dumps({"github": "older-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        self.assertTrue(self._new_path().exists())
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "older-user"})
        self.assertIn("merged from legacy", out)

    def test_migrate_both_paths_same_value_merges(self):
        """新旧両方に同じキーがあり値も同一 → merged (conflict なし)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "same-user"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "same-user", "aws": "111"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "same-user", "aws": "111"})
        self.assertIn("+ merged from deprecated", out)

    def test_migrate_both_paths_conflicting_value_denies(self):
        """D5 / R9: 同一キーで値が衝突 → deny。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "A"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "B"}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)
        self.assertIn("github", err)

    def test_migrate_dry_run_does_not_write(self):
        self._deprecated_path().write_text(
            json.dumps({"github": "u"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(self._new_path().exists())
        self.assertIn("(dry-run", out)

    def test_migrate_conflict_hides_values_by_default(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "secret-A"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "secret-B"}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertNotIn("secret-A", err)
        self.assertNotIn("secret-B", err)

    def test_migrate_conflict_show_values_reveals(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "secret-A"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "secret-B"}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit", "--show-values"])
        self.assertEqual(code, 1)
        self.assertIn("secret-A", err)
        self.assertIn("secret-B", err)


class TestProjectClaudeMd(BaseBuilder):
    """0.3.1 で追加した Claude 向け signpost (`CLAUDE.md`) の同梱挙動。

    `init --commit` / `migrate --commit` で新パスのディレクトリに CLAUDE.md
    を置く (既存ファイルは温存)。dry-run では生成しない。書込失敗は
    best-effort でスキップ (builder 全体は成功させる)。
    """

    def _md_path(self) -> Path:
        return self.new_dir / "CLAUDE.md"

    # --- init ---

    def test_init_commit_creates_claude_md(self):
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(self._md_path().exists())
        content = self._md_path().read_text(encoding="utf-8")
        # signpost として必要な情報が含まれている
        self.assertIn("accounts-init", content)
        self.assertIn("accounts-show", content)
        self.assertIn("accounts-migrate", content)
        self.assertIn("sensitive-files-guard", content)
        self.assertIn("created:", out)

    def test_init_commit_preserves_existing_claude_md(self):
        """既存 CLAUDE.md は上書きしない (ユーザー編集尊重)。"""
        self.new_dir.mkdir(parents=True)
        self._md_path().write_text("# user customized\n", encoding="utf-8")
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self._md_path().read_text(encoding="utf-8"), "# user customized\n"
        )
        self.assertIn("skipped", out)

    def test_init_dry_run_does_not_create_claude_md(self):
        code, _out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--dry-run"]
        )
        self.assertFalse(self._md_path().exists())

    def test_init_commit_when_action_unchanged_still_signposts(self):
        """既存値と同じ (= action=unchanged) でも CLAUDE.md は同梱する。

        既存 accounts.local.json は持っているが CLAUDE.md がまだ無い既存
        ユーザー向けに、再度 init を流せば signpost を後付けできる経路を
        担保する。
        """
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        self.assertFalse(self._md_path().exists())
        code, _out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(self._md_path().exists())

    def test_init_commit_template_missing_does_not_fail(self):
        """template が読めなくても builder 自体は成功する (best-effort)。"""
        with mock.patch.object(
            builder,
            "_PROJECT_CLAUDE_MD_TEMPLATE",
            Path(self.tmp) / "nonexistent" / "template.md",
        ):
            code, out, _err = self._run(
                ["init", "--service", "github", "--value", "Mao-o", "--commit"]
            )
        self.assertEqual(code, 0)
        self.assertTrue(self._new_path().exists())  # JSON は書けている
        self.assertFalse(self._md_path().exists())  # CLAUDE.md は書けていない
        self.assertIn("warning", out)

    # --- migrate ---

    def test_migrate_commit_creates_claude_md(self):
        self._deprecated_path().write_text(
            json.dumps({"github": "u"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        self.assertTrue(self._md_path().exists())
        content = self._md_path().read_text(encoding="utf-8")
        self.assertIn("accounts-migrate", content)
        self.assertIn("created:", out)

    def test_migrate_commit_preserves_existing_claude_md(self):
        self.new_dir.mkdir(parents=True)
        self._md_path().write_text("# existing\n", encoding="utf-8")
        self._deprecated_path().write_text(
            json.dumps({"github": "u"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self._md_path().read_text(encoding="utf-8"), "# existing\n"
        )
        self.assertIn("skipped", out)

    def test_migrate_dry_run_does_not_create_claude_md(self):
        self._deprecated_path().write_text(
            json.dumps({"github": "u"}), encoding="utf-8"
        )
        code, _out, _err = self._run(["migrate", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(self._md_path().exists())


class TestEntriesEqual(unittest.TestCase):
    """`_entries_equal` の direct unit tests (Codex P2 / R1 対応)."""

    def test_dict_expected_str_current_matches_any_value(self):
        """Firebase の alias map (dict expected) + scalar current が
        values の任意に一致 → True."""
        self.assertTrue(
            builder._entries_equal(
                {"default": "proj-dev", "prod": "proj-prod"}, "proj-dev"
            )
        )
        self.assertTrue(
            builder._entries_equal(
                {"default": "proj-dev", "prod": "proj-prod"}, "proj-prod"
            )
        )

    def test_dict_expected_str_current_no_match(self):
        self.assertFalse(
            builder._entries_equal(
                {"default": "proj-dev", "prod": "proj-prod"}, "proj-other"
            )
        )

    def test_str_expected_dict_current_matches(self):
        """対称ケース: scalar expected が dict current の最初のホスト value に一致 → True."""
        self.assertTrue(
            builder._entries_equal("Mao-o", {"github.com": "Mao-o"})
        )

    def test_str_expected_multi_host_dict_current_first_host_only(self):
        """R3 (P2): multi-host current に対して、scalar expected は最初のホスト
        (services/github.py::verify と同じ意味論) のみと比較する."""
        # 最初のホストの value と一致 → True
        self.assertTrue(
            builder._entries_equal(
                "alice", {"github.com": "alice", "ghe": "bob"}
            )
        )
        # 2 つ目以降のホスト value にしか一致しない → False
        # (verify 側でも deny されるため、show と verify の挙動を一致させる)
        self.assertFalse(
            builder._entries_equal(
                "bob", {"github.com": "alice", "ghe": "bob"}
            )
        )

    def test_dict_dict_component_wise(self):
        """dict + dict は component-wise (期待値の全 key が current で一致)."""
        self.assertTrue(
            builder._entries_equal(
                {"project": "p", "account": "a"},
                {"project": "p", "account": "a"},
            )
        )
        self.assertFalse(
            builder._entries_equal(
                {"project": "p", "account": "a"},
                {"project": "p", "account": "b"},
            )
        )

    def test_identical_values(self):
        self.assertTrue(builder._entries_equal("a", "a"))
        self.assertTrue(builder._entries_equal({"x": 1}, {"x": 1}))

    def test_unequal_otherwise(self):
        self.assertFalse(builder._entries_equal("a", "b"))
        self.assertFalse(builder._entries_equal(None, "a"))


if __name__ == "__main__":
    unittest.main()
