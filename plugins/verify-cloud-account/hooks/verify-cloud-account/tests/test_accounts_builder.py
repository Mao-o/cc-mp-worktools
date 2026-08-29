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
        self.assertIn("sensitive-files-guardrail", content)
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


class TestGitignore(BaseBuilder):
    """.gitignore 自動エントリ追加の挙動。

    init --commit / migrate --commit で .gitignore に
    accounts.local.json のエントリを追加する (best-effort)。
    .gitignore が存在しない場合は作成しない。
    """

    def _gitignore(self) -> Path:
        return self.project_dir / ".gitignore"

    def test_init_commit_adds_gitignore_entry(self):
        self._gitignore().write_text("node_modules/\n", encoding="utf-8")
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        content = self._gitignore().read_text(encoding="utf-8")
        self.assertIn(".claude/verify-cloud-account/accounts.local.json", content)
        self.assertIn("node_modules/", content)
        self.assertIn("updated:", out)

    def test_init_commit_skips_if_entry_already_present(self):
        self._gitignore().write_text(
            ".claude/verify-cloud-account/accounts.local.json\n",
            encoding="utf-8",
        )
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        content = self._gitignore().read_text(encoding="utf-8")
        self.assertEqual(
            content.count(".claude/verify-cloud-account/accounts.local.json"), 1
        )
        self.assertNotIn("updated:", out)

    def test_comment_line_does_not_count_as_present(self):
        self._gitignore().write_text(
            "# .claude/verify-cloud-account/accounts.local.json\n",
            encoding="utf-8",
        )
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        content = self._gitignore().read_text(encoding="utf-8")
        active = [l for l in content.splitlines()
                  if l.strip() == ".claude/verify-cloud-account/accounts.local.json"]
        self.assertEqual(len(active), 1)
        self.assertIn("updated:", out)

    def test_negation_line_does_not_count_as_present(self):
        self._gitignore().write_text(
            "!.claude/verify-cloud-account/accounts.local.json\n",
            encoding="utf-8",
        )
        code, _out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        content = self._gitignore().read_text(encoding="utf-8")
        self.assertIn("!", content)
        active = [l for l in content.splitlines()
                  if l.strip() == ".claude/verify-cloud-account/accounts.local.json"]
        self.assertEqual(len(active), 1)

    def test_init_commit_no_gitignore_does_not_create(self):
        self.assertFalse(self._gitignore().exists())
        code, _out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertFalse(self._gitignore().exists())

    def test_dry_run_does_not_modify_gitignore(self):
        self._gitignore().write_text("node_modules/\n", encoding="utf-8")
        code, _out, _err = self._run(
            ["init", "--service", "github", "--value", "Mao-o", "--dry-run"]
        )
        self.assertEqual(code, 0)
        content = self._gitignore().read_text(encoding="utf-8")
        self.assertNotIn("accounts.local.json", content)

    def test_migrate_commit_adds_gitignore_entry(self):
        self._gitignore().write_text("*.log\n", encoding="utf-8")
        self._deprecated_path().write_text(
            json.dumps({"github": "u"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        content = self._gitignore().read_text(encoding="utf-8")
        self.assertIn(".claude/verify-cloud-account/accounts.local.json", content)


class TestParseValue(unittest.TestCase):
    """`_parse_value` の direct unit tests (内部バックログ: --value の入力検証)."""

    def test_json_dict_parsed(self):
        self.assertEqual(
            builder._parse_value('{"project":"p","account":"a"}'),
            {"project": "p", "account": "a"},
        )

    def test_plain_string_stays_string(self):
        self.assertEqual(builder._parse_value("Mao-o"), "Mao-o")

    def test_invalid_json_stays_string(self):
        self.assertEqual(builder._parse_value("not-json{"), "not-json{")

    def test_numeric_string_json_stays_string(self):
        """数字だけの username ("12345") が int に化けない (gcloud/github の
        verify() は isinstance str を要求するため、int 化すると永久不一致になる)."""
        value = builder._parse_value("12345")
        self.assertEqual(value, "12345")
        self.assertIsInstance(value, str)

    def test_json_bool_stays_string(self):
        value = builder._parse_value("true")
        self.assertEqual(value, "true")
        self.assertIsInstance(value, str)

    def test_json_list_stays_string(self):
        value = builder._parse_value('["a","b"]')
        self.assertEqual(value, '["a","b"]')
        self.assertIsInstance(value, str)

    def test_json_quoted_string_stays_original_raw(self):
        """JSON として parse すると素の文字列になる形 ('"a"') も、dict では
        ないため生の raw 文字列 (クォート込み) のまま返す。"""
        value = builder._parse_value('"a"')
        self.assertEqual(value, '"a"')


class TestValidateEntryShape(unittest.TestCase):
    """`_validate_entry_shape` の direct unit tests (内部バックログ: 書込前
    スキーマ検証)。services.github / gcloud / aws を実サービスとして使う。"""

    def setUp(self):
        from services import aws, gcloud, github  # noqa: E402 (test-local import)
        self.github = github
        self.gcloud = gcloud
        self.aws = aws

    def test_str_value_always_ok(self):
        self.assertIsNone(builder._validate_entry_shape(self.aws, "123456789012"))
        self.assertIsNone(builder._validate_entry_shape(self.github, "Mao-o"))

    def test_dict_ok_for_service_that_accepts_dict(self):
        self.assertIsNone(
            builder._validate_entry_shape(
                self.github, {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}
            )
        )

    def test_dict_rejected_for_scalar_only_service(self):
        reason = builder._validate_entry_shape(self.aws, {"foo": "bar"})
        self.assertIsNotNone(reason)
        self.assertIn("オブジェクト形式", reason)

    def test_empty_dict_rejected(self):
        reason = builder._validate_entry_shape(self.github, {})
        self.assertIsNotNone(reason)
        self.assertIn("空です", reason)

    def test_non_string_dict_value_rejected(self):
        reason = builder._validate_entry_shape(self.gcloud, {"project": 111})
        self.assertIsNotNone(reason)

    def test_unknown_dict_key_rejected_when_strict(self):
        reason = builder._validate_entry_shape(
            self.gcloud, {"region": "us-central1"}, strict_keys=True
        )
        self.assertIsNotNone(reason)
        self.assertIn("未対応", reason)

    def test_unknown_dict_key_allowed_when_not_strict(self):
        """migrate (strict_keys=False) は gcloud.verify() 同様に未知キーを
        黙って無視するだけで拒否しない。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"project": "p", "region": "us-central1"}, strict_keys=False
        )
        self.assertIsNone(reason)

    def test_all_values_bad_still_rejected_when_not_strict(self):
        """strict_keys=False でも、dict 内の**全キー**が不正 (使える値が
        1 つも無い) なら拒否する — firebase/gcloud の verify() も
        「使える値が 1 つも無ければ拒否」なので、ここは strict_keys に
        関わらず一致する。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"project": 111}, strict_keys=False
        )
        self.assertIsNotNone(reason)

    def test_one_bad_value_among_good_ones_tolerated_when_not_strict(self):
        """内部バックログ (advisor レビュー): firebase.verify() は
        `{"default":"proj-dev","old":null}` のような**部分的に不正**な dict
        を、"old" を無視して "default" だけで成立させる (dict 内に使える値が
        1 つでも残っていれば良い)。migrate (strict_keys=False) がこれより
        厳しいと、verify() が許容する形を migrate だけが拒否する退行になる。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"project": "p", "account": 111}, strict_keys=False
        )
        self.assertIsNone(reason)

    def test_one_bad_value_among_good_ones_still_rejected_when_strict(self):
        """init/set (strict_keys=True) は authoring 時の即時フィードバックを
        優先し、部分的な不正値でも rejectする (migrate だけの leniency)。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"project": "p", "account": 111}, strict_keys=True
        )
        self.assertIsNotNone(reason)

    def test_wrong_top_level_type_rejected(self):
        for bad in ([1, 2], 111, None, True):
            with self.subTest(bad=bad):
                reason = builder._validate_entry_shape(self.github, bad)
                self.assertIsNotNone(reason)

    def test_empty_string_key_rejected_when_strict(self):
        reason = builder._validate_entry_shape(
            self.github, {"": "Mao-o"}, strict_keys=True
        )
        self.assertIsNotNone(reason)
        self.assertIn("空文字", reason)

    def test_empty_string_key_rejected_when_not_strict(self):
        """空文字キーは strict_keys の緩和 (未知キー/個々の値の不正) の
        対象外 — 型不正・空 dict と同じく常に検証する。"""
        reason = builder._validate_entry_shape(
            self.github, {"": "Mao-o"}, strict_keys=False
        )
        self.assertIsNotNone(reason)
        self.assertIn("空文字", reason)


class TestMigrateKeepNewWithoutLoss(unittest.TestCase):
    """`_migrate_keep_new_without_loss` の direct unit tests (内部バックログ:
    advisor レビューで検出した multi-host/multi-alias の情報欠落の修正)。"""

    def test_identical_values(self):
        self.assertTrue(builder._migrate_keep_new_without_loss("a", "a"))
        self.assertTrue(
            builder._migrate_keep_new_without_loss({"x": 1}, {"x": 1})
        )

    def test_single_host_dict_old_matches_new_scalar(self):
        """ticket の実例: old が単一 host/alias の dict で new (scalar) と
        同じ値 → 情報は失われないので True。"""
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                "Mao-o", {"github.com": "Mao-o"}
            )
        )

    def test_multi_host_dict_old_with_extra_value_is_lossy(self):
        """最重要の回帰防止: old が multi-host dict で new (scalar) に
        無い値を持つ場合は False (conflict のまま、silent data loss を防ぐ)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                "Mao-o", {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}
            )
        )

    def test_multi_host_dict_old_with_all_same_value_is_lossy(self):
        """old が複数 host/alias を持つ場合、値が全て new (scalar) と同じでも
        「照合される host 集合」は new では 1 つに縮む (github.verify は
        github.com か最初の active host のみを照合する) ため、値が同じでも
        情報欠落として False (conflict のまま) になる。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                "Mao-o", {"github.com": "Mao-o", "ghe.example.com": "Mao-o"}
            )
        )

    def test_dict_new_superset_of_scalar_old(self):
        """対称ケース: new (dict) が old (scalar) の値を含んでいれば
        new はスーパーセットなので情報は失われない → True。"""
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                {"github.com": "Mao-o"}, "Mao-o"
            )
        )

    def test_dict_new_missing_scalar_old_value_is_lossy(self):
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                {"github.com": "other"}, "Mao-o"
            )
        )

    def test_dict_dict_new_has_all_old_keys_is_not_lossy(self):
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                {"project": "p", "account": "a", "extra": "z"},
                {"project": "p", "account": "a"},
            )
        )

    def test_dict_dict_new_missing_old_key_is_lossy(self):
        """dict-dict でも同じクラスの欠落を防ぐ: old の "account" が new に
        無ければ False (silent drop を許さない)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                {"project": "p"}, {"project": "p", "account": "a"}
            )
        )

    def test_dict_dict_differing_value_is_lossy(self):
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                {"project": "p", "account": "a"},
                {"project": "p", "account": "b"},
            )
        )

    def test_unequal_scalars(self):
        self.assertFalse(builder._migrate_keep_new_without_loss("a", "b"))


class TestInitValueValidation(BaseBuilder):
    """init --value の JSON 解釈と書込前スキーマ検証 (内部バックログ)。"""

    def test_value_json_dict_parsed_and_written_as_dict(self):
        code, _out, _err = self._run(
            [
                "init", "--service", "gcloud",
                "--value", '{"project":"p","account":"a"}', "--commit",
            ]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"gcloud": {"project": "p", "account": "a"}})

    def test_value_non_dict_json_kept_as_string(self):
        code, _out, _err = self._run(
            ["init", "--service", "github", "--value", "12345", "--commit"]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "12345"})
        self.assertIsInstance(data["github"], str)

    def test_value_invalid_json_kept_as_string(self):
        code, _out, _err = self._run(
            ["init", "--service", "github", "--value", "not-json{", "--commit"]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "not-json{"})

    def test_rejects_unsupported_dict_shape_for_scalar_only_service(self):
        code, _out, err = self._run(
            ["init", "--service", "aws", "--value", '{"foo":"bar"}', "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("オブジェクト形式", err)
        self.assertFalse(self._new_path().exists())

    def test_rejects_unknown_dict_key_for_gcloud(self):
        code, _out, err = self._run(
            [
                "init", "--service", "gcloud",
                "--value", '{"region":"us-central1"}', "--commit",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("未対応", err)
        self.assertFalse(self._new_path().exists())

    def test_rejects_non_string_dict_value(self):
        code, _out, err = self._run(
            [
                "init", "--service", "gcloud",
                "--value", '{"project": 111}', "--commit",
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(self._new_path().exists())

    def test_skipped_message_points_to_set_subcommand(self):
        """内部バックログ: skipped 分岐の案内文言が `set` サブコマンドを指す
        (旧文言 'a future switch subcommand' は削除済み)."""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "existing-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["init", "--service", "github", "--value", "new-user", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)
        self.assertIn("set --service github", out)
        self.assertNotIn("future switch subcommand", out)


class TestSet(BaseBuilder):
    """`set` サブコマンド (内部バックログ: builder に期待値の update が無い)。"""

    def test_adds_new_key(self):
        code, out, _err = self._run(
            ["set", "--service", "github", "--value", "Mao-o", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("+ add", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})

    def test_overwrites_existing_scalar_with_different_value(self):
        """set の核心: init が拒否する『既存と異なる値への更新』を実行できる。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "old-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["set", "--service", "github", "--value", "new-user", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("+ new", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "new-user"})

    def test_dry_run_does_not_write(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "old-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["set", "--service", "github", "--value", "new-user", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("(dry-run", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "old-user"})

    def test_unchanged_when_same_value(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "same-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["set", "--service", "github", "--value", "same-user", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("= unchanged", out)

    def test_preserves_other_keys(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "old-user", "aws": "111"}), encoding="utf-8"
        )
        code, _out, _err = self._run(
            ["set", "--service", "github", "--value", "new-user", "--commit"]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "new-user", "aws": "111"})

    def test_from_cli_uses_suggestion(self):
        with mock.patch(
            "services.github.suggest_accounts_entry", return_value="auto-user"
        ):
            code, _out, _err = self._run(
                ["set", "--service", "github", "--from-cli", "--commit"]
            )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "auto-user"})

    def test_from_cli_returns_none_errors(self):
        with mock.patch(
            "services.github.suggest_accounts_entry", return_value=None
        ):
            code, _out, err = self._run(
                ["set", "--service", "github", "--from-cli", "--commit"]
            )
        self.assertEqual(code, 1)
        self.assertIn("CLI から取得できませんでした", err)
        self.assertFalse(self._new_path().exists())

    def test_value_and_from_cli_are_mutually_exclusive(self):
        code, _out, _err = self._run(
            [
                "set", "--service", "github", "--value", "u",
                "--from-cli", "--commit",
            ]
        )
        self.assertEqual(code, 2)

    def test_requires_value_or_from_cli(self):
        code, _out, _err = self._run(["set", "--service", "github", "--commit"])
        self.assertEqual(code, 2)

    def test_host_creates_new_dict(self):
        code, _out, _err = self._run(
            [
                "set", "--service", "github", "--host", "github.com",
                "--value", "Mao-o", "--commit",
            ]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"github.com": "Mao-o"}})

    def test_host_merges_into_existing_dict(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"github.com": "Mao-o"}}), encoding="utf-8"
        )
        code, _out, _err = self._run(
            [
                "set", "--service", "github", "--host", "ghe.example.com",
                "--value", "mao-corp", "--commit",
            ]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(
            data,
            {"github": {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}},
        )

    def test_host_overwrites_existing_host_key_only(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps(
                {"github": {"github.com": "A", "ghe.example.com": "B"}}
            ),
            encoding="utf-8",
        )
        code, _out, _err = self._run(
            [
                "set", "--service", "github", "--host", "github.com",
                "--value", "C", "--commit",
            ]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(
            data, {"github": {"github.com": "C", "ghe.example.com": "B"}}
        )

    def test_host_rejected_when_existing_is_scalar(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--host", "github.com",
                "--value", "other", "--commit",
            ]
        )
        self.assertEqual(code, 1)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})  # 変更されない
        self.assertIn("オブジェクトではありません", err)

    def test_host_rejected_when_existing_is_list(self):
        """非対称の回帰防止: 既存値が list のときも str と同じく明示エラーに
        する (従来は `isinstance(existing_value, str)` しか弾かず、list は
        `base_dict = {}` で黙って dict に作り替えられ既存値を破棄していた)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": ["Mao-o", "other"]}), encoding="utf-8"
        )
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--host", "github.com",
                "--value", "other", "--commit",
            ]
        )
        self.assertEqual(code, 1)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": ["Mao-o", "other"]})  # 変更されない
        self.assertIn("オブジェクトではありません", err)

    def test_host_empty_string_rejected(self):
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--host", "",
                "--value", "Mao-o", "--commit",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--host", err)
        self.assertFalse(self._new_path().exists())

    def test_host_and_from_cli_rejected(self):
        """`--host` + `--from-cli` は builder がどの host の値かを判別できない
        ため明示エラーにする (単一 host ログイン中に別 host の期待値として
        CLI 現在値が書かれてしまう事故の防止)。"""
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--host", "ghe.example.com",
                "--from-cli", "--commit",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--host", err)
        self.assertIn("--value", err)
        self.assertFalse(self._new_path().exists())

    def test_host_rejected_for_scalar_only_service(self):
        code, _out, err = self._run(
            [
                "set", "--service", "aws", "--host", "foo",
                "--value", "111", "--commit",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--host", err)
        self.assertFalse(self._new_path().exists())

    def test_rejects_invalid_shape(self):
        code, _out, err = self._run(
            ["set", "--service", "aws", "--value", '{"foo":"bar"}', "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertFalse(self._new_path().exists())

    def test_rejects_when_legacy_path_exists(self):
        self._deprecated_path().write_text(
            json.dumps({"github": "old-user"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["set", "--service", "github", "--value", "u", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("旧パス", err)
        self.assertFalse(self._new_path().exists())

    def test_hides_values_by_default_and_show_values_reveals(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "old-secret"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            [
                "set", "--service", "github", "--value", "new-secret",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("old-secret", out)
        self.assertNotIn("new-secret", out)

        code, out, _err = self._run(
            [
                "set", "--service", "github", "--value", "new-secret",
                "--dry-run", "--show-values",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("old-secret", out)
        self.assertIn("new-secret", out)


class TestRemove(BaseBuilder):
    """`remove` サブコマンド (内部バックログ: builder に期待値の削除が無い)。"""

    def test_removes_whole_key(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o", "aws": "111"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["remove", "--service", "github", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("- remove", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"aws": "111"})
        self.assertNotIn("github", data)

    def test_noop_when_key_absent(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(json.dumps({"aws": "111"}), encoding="utf-8")
        code, out, _err = self._run(
            ["remove", "--service", "github", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("何もしません", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"aws": "111"})  # 変更なし

    def test_noop_when_file_absent(self):
        code, out, _err = self._run(
            ["remove", "--service", "github", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("何もしません", out)
        self.assertFalse(self._new_path().exists())  # 空ファイルすら作らない

    def test_dry_run_does_not_write(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["remove", "--service", "github", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("(dry-run", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})

    def test_removes_one_host_keeps_dict_for_remaining_hosts(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps(
                {"github": {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}}
            ),
            encoding="utf-8",
        )
        code, _out, _err = self._run(
            [
                "remove", "--service", "github", "--host", "ghe.example.com",
                "--commit",
            ]
        )
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"github.com": "Mao-o"}})

    def test_removes_last_host_deletes_whole_key_not_empty_dict(self):
        """最重要ケース: 最後の host/alias を消したら空 dict `{}` を残さず、
        キー自体を丸ごと削除する。空 dict は github/firebase/gcloud の
        verify() が『オブジェクトが空です』で permanent deny するため。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"github.com": "Mao-o"}, "aws": "111"}),
            encoding="utf-8",
        )
        code, out, _err = self._run(
            [
                "remove", "--service", "github", "--host", "github.com",
                "--commit",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("キー全体を削除", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"aws": "111"})
        self.assertNotIn("github", data)  # {} ではなくキー自体が無い

    def test_host_noop_when_host_absent(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"github.com": "Mao-o"}}), encoding="utf-8"
        )
        code, out, _err = self._run(
            [
                "remove", "--service", "github", "--host", "nonexistent.example.com",
                "--commit",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("何もしません", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"github.com": "Mao-o"}})

    def test_host_empty_string_rejected(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"github.com": "Mao-o"}}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["remove", "--service", "github", "--host", "", "--commit"]
        )
        self.assertEqual(code, 2)
        self.assertIn("--host", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"github.com": "Mao-o"}})  # 変更されない

    def test_host_rejected_when_existing_is_scalar(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["remove", "--service", "github", "--host", "github.com", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("オブジェクトではありません", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})  # 変更されない

    def test_host_rejected_for_scalar_only_service(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(json.dumps({"aws": "111"}), encoding="utf-8")
        code, _out, err = self._run(
            ["remove", "--service", "aws", "--host", "foo", "--commit"]
        )
        self.assertEqual(code, 2)
        self.assertIn("--host", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"aws": "111"})  # 変更されない

    def test_rejects_when_legacy_path_exists(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"aws": "111"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["remove", "--service", "github", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("旧パス", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})  # 変更されない

    def test_hides_values_by_default_and_show_values_reveals(self):
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "secret-user"}), encoding="utf-8"
        )
        code, out, _err = self._run(["remove", "--service", "github", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertNotIn("secret-user", out)

        code, out, _err = self._run(
            ["remove", "--service", "github", "--dry-run", "--show-values"]
        )
        self.assertEqual(code, 0)
        self.assertIn("secret-user", out)


class TestMigrateValueSemantics(BaseBuilder):
    """migrate の意味論的等価比較 + 追加値スキーマ検証 (内部バックログ:
    型不正/等価値を扱えない不具合の修正)。"""

    def test_semantically_equal_scalar_new_and_dict_old_not_conflict(self):
        """new=scalar "Mao-o" / old=dict {"github.com":"Mao-o"} は意味的に
        等価 → conflict にせず new (scalar) 側を維持する。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": {"github.com": "Mao-o"}}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        self.assertNotIn("衝突", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})

    def test_semantically_equal_dict_new_and_scalar_old_not_conflict(self):
        """対称ケース: new=dict {"github.com":"Mao-o"} / old=scalar "Mao-o"
        も意味的に等価 → conflict にせず new (dict) 側を維持する。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"github.com": "Mao-o"}}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        code, _out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"github.com": "Mao-o"}})

    def test_semantically_different_values_still_conflict(self):
        """既存 R9 回帰確認: 意味的にも異なる値は従来どおり conflict。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": {"github.com": "other-user"}}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)

    def test_multi_host_dict_not_silently_collapsed_into_new_scalar(self):
        """advisor レビューで検出した最重要の退行: new=scalar "Mao-o" /
        old={"github.com":"Mao-o","ghe.example.com":"mao-corp"} (multi-host)
        は、github.com だけが一致しても ghe.example.com の情報を失うため
        conflict のままにする (silent data loss を許さない)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "Mao-o"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps(
                {"github": {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}}
            ),
            encoding="utf-8",
        )
        code, _out, err = self._run(["migrate", "--commit", "--show-values"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)
        self.assertIn("mao-corp", err)  # 失われかけた情報が見える
        # new 側 (書込先) は失敗時点で unchanged のまま (書き込まれない)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "Mao-o"})

    def test_dict_dict_new_missing_old_key_is_conflict_not_silently_dropped(self):
        """dict-dict でも同じクラスの情報欠落を防ぐ: new={"project":"p"} /
        old={"project":"p","account":"a"} は project は一致するが account の
        情報が new に無いため conflict のままにする (old の account を黙って
        捨てない)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"gcloud": {"project": "p"}}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"gcloud": {"project": "p", "account": "a"}}),
            encoding="utf-8",
        )
        code, _out, err = self._run(["migrate", "--commit", "--show-values"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)
        # new 側 (書込先) は失敗時点で unchanged のまま (書き込まれない)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"gcloud": {"project": "p"}})

    def test_addition_with_one_bad_value_among_good_ones_is_tolerated(self):
        """内部バックログ (advisor レビュー): firebase.verify() は dict 内に
        使える値 (str かつ非空) が 1 つでも残っていれば動く
        (`{"default":"proj-dev","old":null}` の "old" は黙って無視される)。
        migrate の追加分検証がこれより厳しくなると、verify() が許容する形を
        migrate だけが拒否する退行になるため、tolerate されることを確認する。"""
        self._deprecated_path().write_text(
            json.dumps({"firebase": {"default": "proj-dev", "old": None}}),
            encoding="utf-8",
        )
        code, _out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"firebase": {"default": "proj-dev", "old": None}})

    def test_rejects_invalid_shape_from_old_path(self):
        """旧パスの新規追加キーが list 等の不正型 → 書込前に exit 1
        (dispatcher の deny まで遅延させない)。"""
        self._deprecated_path().write_text(
            json.dumps({"github": ["a", "b"]}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("不正です", err)
        self.assertFalse(self._new_path().exists())

    def test_does_not_block_on_preexisting_new_tier_shape_unrelated_to_migration(self):
        """new 側に (verify() は許容するが builder の strict チェックなら弾く)
        未知キー入り dict が既にあっても、migrate はそれを検証しない —
        migrate が実際に書き込むのは additions だけであり、無関係な既存データを
        理由に統合作業全体を deny してはならない。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"gcloud": {"project": "p", "region": "us-central1"}}),
            encoding="utf-8",
        )
        self._deprecated_path().write_text(
            json.dumps({"aws": "111"}), encoding="utf-8"
        )
        code, _out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(
            data,
            {"gcloud": {"project": "p", "region": "us-central1"}, "aws": "111"},
        )

    def test_addition_with_unknown_dict_key_from_old_path_is_allowed(self):
        """migrate は追加分についても未知キーそのものは拒否しない
        (strict_keys=False) — verify() が黙って無視するだけの形を、migrate に
        限って手動編集必須の壁に変えないため。"""
        self._deprecated_path().write_text(
            json.dumps({"gcloud": {"project": "p", "region": "us-central1"}}),
            encoding="utf-8",
        )
        code, _out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(
            data, {"gcloud": {"project": "p", "region": "us-central1"}}
        )


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
