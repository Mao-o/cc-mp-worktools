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
import itertools
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


# TestBuilderAcceptedShapesPassVerify の全数探索 (Codex R3 P2) が使う値種別:
# 不在 / 正常な非空文字列 / 空文字列 / None / 非文字列 truthy (int)。
# 空白のみの文字列は `v.strip()` 判定で "" と同じ分岐を通るため別枠は起こさない。
_ABSENT = object()
_DICT_VALUE_KINDS = (_ABSENT, "sample-value", "", None, 123)


def _iter_dict_shapes(keys):
    """`keys` の全部分集合 (空集合含む) × 各キーの値種別の直積を生成する。

    各キーが独立に `_DICT_VALUE_KINDS` の 5 状態 (不在含む) を取るとみなし、
    `len(keys)` 個の直積 (5**len(keys) 通り) を作る。「部分集合 × 4 値種別
    (不在キーを除く) の直積」と数は一致する (二項定理 Σ_k C(n,k) 4^k = 5^n)。
    """
    for combo in itertools.product(_DICT_VALUE_KINDS, repeat=len(keys)):
        yield {k: v for k, v in zip(keys, combo) if v is not _ABSENT}


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
        """D2: 任意パスへ書込先を向けるフラグは存在しない (argparse で unknown)。

        D14 で `--path` を追加したが、受け付けるのは dispatcher が読む配置
        (`_PATH_TIERS`) だけで、任意パスは exit 2 で拒否する
        (TestExplicitPathArgument が範囲を固定する)。ここでは `--path` 以外の
        経路が増えていないことだけを見る。
        """
        for flag in ("--output", "--target", "--file"):
            code, _out, _err = self._run(
                ["init", "--service", "github", "--value", "u", "--commit", flag, "/tmp/evil.json"]
            )
            self.assertEqual(code, 2, f"flag {flag!r} should be rejected by argparse")

    def test_path_flag_cannot_target_arbitrary_location(self):
        """D2/D14: `--path` は accounts.local.json の 3-tier 配置に限る。"""
        evil = Path(self.tmp) / "evil.json"
        code, _out, _err = self._run(
            [
                "init", "--service", "github", "--value", "u", "--commit",
                "--path", str(evil),
            ]
        )
        self.assertEqual(code, 2)
        self.assertFalse(evil.exists())


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
        # 他 plugin を名指ししない (D9 疎結合方針 / 内部バックログ): 生成される
        # signpost は特定のセキュリティ系 hook 名ではなく一般化した表現を使う。
        self.assertIn("セキュリティ系 hook", content)
        self.assertNotIn("sensitive-files-guardrail", content)
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
    スキーマ検証 + Codex R1 P2 の service ごとの DICT_VALUE_CHECK 契約)。
    services.github / gcloud / firebase / aws を実サービスとして使う。"""

    def setUp(self):
        from services import aws, firebase, gcloud, github  # noqa: E402
        self.github = github
        self.gcloud = gcloud
        self.firebase = firebase
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
        厳しいと、verify() が許容する形を migrate だけが拒否する退行になる。

        Codex R1 P2 の修正で、この leniency は service の DICT_VALUE_CHECK
        契約の範囲に限定した。この test は**契約が "none" の firebase**
        (docstring が元々説明していた service) で leniency を固定する。
        修正前はここで gcloud + truthy 非 str (`{"account":111}`) を使って
        おり、gcloud.verify() が形を理由に reject する形を「許容される」と
        主張してしまっていた (= test が不具合を固定していた)。gcloud の
        truthy 非 str が reject されることは
        `test_gcloud_truthy_non_str_value_rejected_when_not_strict` で固定する。
        """
        reason = builder._validate_entry_shape(
            self.firebase, {"default": "proj-dev", "old": None}, strict_keys=False
        )
        self.assertIsNone(reason)

    def test_gcloud_falsy_value_tolerated_when_not_strict(self):
        """gcloud.verify() は `if project_want:` / `if account_want:` で値を拾う
        ため falsy な値は黙って無視する (DICT_VALUE_CHECK="truthy")。
        許可キーであっても falsy なら migrate は通す。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"project": "p", "account": None}, strict_keys=False
        )
        self.assertIsNone(reason)

    def test_gcloud_truthy_non_str_value_rejected_when_not_strict(self):
        """Codex R1 P2 の指摘そのもの: `{"project":"p","account":123}` は
        lenient 検証を通っていたが、gcloud.verify() は truthy な非 str の
        account を「文字列で指定してください」で reject する。書込時に止めないと
        次の gcloud 操作で初めて deny される (実行時に化ける)。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"project": "p", "account": 123}, strict_keys=False
        )
        self.assertIsNotNone(reason)
        self.assertIn("account", reason)

    def test_gcloud_truthy_non_str_on_unknown_key_tolerated_when_not_strict(self):
        """未知キーの値は形を問わない — gcloud.verify() は DICT_ALLOWED_KEYS
        以外のキーをそもそも読まないため、値が壊れていても deny 要因にならない
        (ここで弾くと verify() が許す形を migrate だけが拒否する退行になる)。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"project": "p", "region": 123}, strict_keys=False
        )
        self.assertIsNone(reason)

    def test_gcloud_unknown_key_only_rejected_when_not_strict(self):
        """Codex R3 P2 の指摘そのもの: `{"region": "us-central1"}` は
        project も account も無いのに、未知キー "region" の非空文字列値を
        「使える値」として good_values に数えて受理していた。
        gcloud.verify() は DICT_ALLOWED_KEYS 外のキーをそもそも読まないため、
        project も account も無いとして deny する — migrate は成功と報告した
        のに、書いた entry がそのままでは使えない状態になる。
        `test_gcloud_truthy_non_str_on_unknown_key_tolerated_when_not_strict`
        (未知キーの値の**形**は問わない) とは区別すること — こちらは
        「使える値の**在り処**」の話で、未知キーの値だけでは good_values の
        対象にならない。"""
        reason = builder._validate_entry_shape(
            self.gcloud, {"region": "us-central1"}, strict_keys=False
        )
        self.assertIsNotNone(reason)
        self.assertIn("project", reason)
        self.assertIn("account", reason)

    def test_github_non_str_value_rejected_when_not_strict(self):
        """github.verify() は dict の**全キー**の値を isinstance(str) で検査し、
        1 つでも非 str なら (falsy な None でも) その host のエラーを積む
        (DICT_VALUE_CHECK="all")。migrate でも素通ししない。"""
        for bad in (123, None, ["x"], {"y": "z"}):
            with self.subTest(bad=bad):
                reason = builder._validate_entry_shape(
                    self.github,
                    {"github.com": "USER", "ghe.example.com": bad},
                    strict_keys=False,
                )
                self.assertIsNotNone(reason)

    def test_firebase_truthy_non_str_value_tolerated_when_not_strict(self):
        """firebase.verify() は使えない値を filter で捨てるだけで、値の形を
        理由に deny しない (DICT_VALUE_CHECK="none")。falsy / truthy を問わず
        migrate は通す。"""
        for bad in (123, None, ["x"]):
            with self.subTest(bad=bad):
                reason = builder._validate_entry_shape(
                    self.firebase,
                    {"default": "proj-dev", "old": bad},
                    strict_keys=False,
                )
                self.assertIsNone(reason)

    def test_blank_dict_value_rejected_regardless_of_strictness(self):
        """空文字・空白のみの**値**は空文字キーと同じ失敗形 (実在の CLI 値と
        一致し得ず永久 deny) なので、DICT_VALUE_CHECK 契約に関わらず常に弾く。
        `--host` 経由 / `--value` の dict 直接指定 / migrate の取り込みの
        全経路がここを通る。"""
        for svc_name, svc, shape in (
            ("github", self.github, {"github.com": "USER", "ghe.example.com": ""}),
            ("github", self.github, {"github.com": "   "}),
            ("gcloud", self.gcloud, {"project": "p", "account": "  "}),
            ("firebase", self.firebase, {"default": "proj-dev", "prod": ""}),
        ):
            for strict in (True, False):
                with self.subTest(svc=svc_name, shape=shape, strict=strict):
                    reason = builder._validate_entry_shape(
                        svc, shape, strict_keys=strict
                    )
                    self.assertIsNotNone(reason)
                    self.assertIn("空文字", reason)

    def test_empty_scalar_rejected(self):
        """Codex R1 P2: `set --service aws --value "$UNSET_VAR"` で空文字が
        書けていた。dispatcher は `entry == ""` をキー欠落と同じ扱いにして
        恒久 deny するため、書込時に弾く (dict の値・キーと同じ規則)。"""
        for svc_name, svc in (
            ("aws", self.aws),
            ("github", self.github),
            ("gcloud", self.gcloud),
        ):
            for bad in ("", "   ", "\t\n"):
                with self.subTest(svc=svc_name, bad=repr(bad)):
                    reason = builder._validate_entry_shape(svc, bad)
                    self.assertIsNotNone(reason)
                    self.assertIn("空文字", reason)

    def test_scalar_with_surrounding_whitespace_still_accepted(self):
        """非空であれば前後の空白は許す (値そのものは書き換えない) — 判定は
        `strip()` 後の非空だけで、正規化はしない。"""
        self.assertIsNone(builder._validate_entry_shape(self.aws, " 123456789012 "))

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

    def test_whitespace_only_key_rejected_when_strict(self):
        """空白のみのキーは空文字と同じ失敗形 (github.verify() が実在しない
        host/alias として永久 deny する) なので、strict/lenient 問わず弾く。
        `--host` の CLI guard (空文字ガード) を経由しない `--value` 直接指定や
        migrate の取り込みでもここで弾かれる。"""
        reason = builder._validate_entry_shape(
            self.github, {"   ": "Mao-o"}, strict_keys=True
        )
        self.assertIsNotNone(reason)
        self.assertIn("空文字", reason)

    def test_whitespace_only_key_rejected_when_not_strict(self):
        reason = builder._validate_entry_shape(
            self.github, {"   ": "Mao-o"}, strict_keys=False
        )
        self.assertIsNotNone(reason)
        self.assertIn("空文字", reason)


class TestBuilderAcceptedShapesPassVerify(unittest.TestCase):
    """D10 の不変条件を service × 形状の表で固定する (Codex R1 P2)。

    **不変条件**: `_validate_entry_shape()` が受理した形は、その service の
    `verify()` が**形を理由に** deny しない。

    構成上の要点: 各ケースの active 値は**その期待値を満たすように mock する**。
    そのため verify() が非 None を返したら、それは値の不一致ではなく形の拒否で
    あることが構成から保証される (エラー文面の文字列マッチで「形の deny」と
    「値の deny」を選り分けない = 判定の抜けを作らない)。
    クラウド CLI は起動せず、各 service の現在値取得関数だけを差し替える。
    """

    # (service 名, 期待値, その期待値を満たす active 値, strict でも受理されるか)
    ACCEPTED = [
        # --- scalar ---
        ("aws", "123456789012", "123456789012", True),
        ("kubectl", "my-context", "my-context", True),
        ("github", "USER", {"github.com": "USER"}, True),
        ("gcloud", "my-project", {"project": "my-project", "account": None}, True),
        ("firebase", "proj-dev", "proj-dev", True),
        # --- 正常 dict ---
        ("github", {"github.com": "USER"}, {"github.com": "USER"}, True),
        (
            "github",
            {"github.com": "USER", "ghe.example.com": "example-org"},
            {"github.com": "USER", "ghe.example.com": "example-org"},
            True,
        ),
        (
            "gcloud",
            {"project": "p", "account": "me@example.com"},
            {"project": "p", "account": "me@example.com"},
            True,
        ),
        (
            "firebase",
            {"default": "proj-dev", "prod": "proj-prod"},
            "proj-dev",
            True,
        ),
        # --- 部分欠落 (dict の一部キーだけ) ---
        ("gcloud", {"project": "p"}, {"project": "p", "account": None}, True),
        (
            "gcloud",
            {"account": "me@example.com"},
            {"project": None, "account": "me@example.com"},
            True,
        ),
        # --- falsy 値 (lenient のみ。verify() が黙って無視する形) ---
        (
            "gcloud",
            {"project": "p", "account": None},
            {"project": "p", "account": None},
            False,
        ),
        ("firebase", {"default": "proj-dev", "old": None}, "proj-dev", False),
        # --- truthy 非 str (lenient のみ。verify() が読まない/捨てる位置に限る) ---
        (
            "gcloud",
            {"project": "p", "region": 123},
            {"project": "p", "account": None},
            False,
        ),
        ("firebase", {"default": "proj-dev", "old": 123}, "proj-dev", False),
        # --- 未知キー (lenient のみ) ---
        (
            "gcloud",
            {"project": "p", "region": "us-central1"},
            {"project": "p", "account": None},
            False,
        ),
    ]

    # 逆側: builder が拒否する形。verify() が形を理由に deny することも併せて
    # 確認し、拒否が過剰でない (実在の穴を塞いでいる) ことを示す。
    REJECTED_AND_DENIED_BY_VERIFY = [
        # Codex R1 P2 の指摘そのもの
        (
            "gcloud",
            {"project": "p", "account": 123},
            {"project": "p", "account": None},
        ),
        # Codex R3 P2 の指摘そのもの: 未知キーのみで project/account が無い
        (
            "gcloud",
            {"region": "us-central1"},
            {"project": "p", "account": None},
        ),
        (
            "github",
            {"github.com": "USER", "ghe.example.com": 123},
            {"github.com": "USER", "ghe.example.com": "example-org"},
        ),
        (
            "github",
            {"github.com": "USER", "ghe.example.com": None},
            {"github.com": "USER", "ghe.example.com": "example-org"},
        ),
        # 空 dict は全 service の verify() が拒否する
        ("github", {}, {"github.com": "USER"}),
        ("gcloud", {}, {"project": "p", "account": None}),
    ]

    def _verify_with_active(self, svc_name: str, expected, active):
        """active 値を mock して verify() を呼ぶ (CLI は起動しない)。"""
        from services import aws, firebase, gcloud, github, kubectl  # noqa: E402

        if svc_name == "github":
            with mock.patch.object(
                github, "_fetch_active_accounts", return_value=(active, None)
            ):
                return github.verify(expected, "/p")
        if svc_name == "gcloud":
            def fake_get(key, env=None, configuration=None):
                return active.get(key), None
            with mock.patch.object(gcloud, "_get", side_effect=fake_get):
                return gcloud.verify(expected, "/p")
        if svc_name == "firebase":
            with mock.patch.object(
                firebase, "_resolve", return_value=(active, None)
            ):
                return firebase.verify(expected, "/p")
        if svc_name == "aws":
            with mock.patch.object(
                aws, "_run_sts_get_caller_identity", return_value=(active, None, "")
            ):
                return aws.verify(expected, "/p")
        if svc_name == "kubectl":
            with mock.patch.object(
                kubectl, "_run_current_context", return_value=(active, None)
            ):
                return kubectl.verify(expected, "/p")
        raise AssertionError(f"unknown service {svc_name}")

    def test_mock_harness_actually_reaches_verify(self):
        """negative control: mock が効いていて verify() が値の判定まで到達して
        いることを確かめる (全ケースが素通しで None になっていたら、この表は
        何も検証していない)。期待値を満たさない active では deny されること。"""
        for svc_name, expected, _active, _strict in self.ACCEPTED:
            with self.subTest(svc=svc_name, expected=expected):
                bad_active = {
                    "github": {"github.com": "someone-else"},
                    "gcloud": {"project": "other-proj", "account": "x@example.com"},
                    "firebase": "other-proj",
                    "aws": "999999999999",
                    "kubectl": "other-context",
                }[svc_name]
                self.assertIsNotNone(
                    self._verify_with_active(svc_name, expected, bad_active)
                )

    def test_accepted_shapes_are_not_denied_for_shape(self):
        """表の全ケースで builder が受理し、かつ verify() が None を返す。"""
        for svc_name, expected, active, strict_ok in self.ACCEPTED:
            svc = builder._SERVICE_BY_KEY[svc_name]
            with self.subTest(svc=svc_name, expected=expected):
                self.assertIsNone(
                    builder._validate_entry_shape(svc, expected, strict_keys=False),
                    "builder (lenient) が受理するはずの形を拒否した",
                )
                if strict_ok:
                    self.assertIsNone(
                        builder._validate_entry_shape(
                            svc, expected, strict_keys=True
                        ),
                        "builder (strict) が受理するはずの形を拒否した",
                    )
                self.assertIsNone(
                    self._verify_with_active(svc_name, expected, active),
                    "builder が受理した形を verify() が deny した (不変条件違反)",
                )

    def test_rejected_shapes_would_have_been_denied_by_verify(self):
        """builder が拒否する形は、verify() 側でも実際に deny される
        (過剰な拒否ではなく、実行時 deny に化ける穴を書込時に塞いでいる)。"""
        for svc_name, expected, active in self.REJECTED_AND_DENIED_BY_VERIFY:
            svc = builder._SERVICE_BY_KEY[svc_name]
            with self.subTest(svc=svc_name, expected=expected):
                self.assertIsNotNone(
                    builder._validate_entry_shape(svc, expected, strict_keys=False),
                    "builder (lenient) が verify() の deny する形を素通しした",
                )
                self.assertIsNotNone(
                    self._verify_with_active(svc_name, expected, active),
                    "verify() が deny しない形を builder が拒否している (過剰)",
                )

    # dict を受け付ける service ごとの全数探索キー全体 (Codex R3 P2)。
    # DICT_ALLOWED_KEYS 宣言時 (gcloud) は「許可キー全部 + 未知キー 1 つ」、
    # 未宣言時 (github/firebase) は許可/未知の区別が無いため代表キー 2 つ
    # (複数キー相互作用 — DICT_VALUE_CHECK が全キーに一貫して効くか — の確認用)。
    _EXHAUSTIVE_DICT_UNIVERSE = {
        "gcloud": ("project", "account", "region"),
        "github": ("host-a", "host-b"),
        "firebase": ("alias-a", "alias-b"),
    }

    def _active_from_expected(self, svc_name: str, expected: dict):
        """accepted shape (使える値が 1 つ以上ある) から、それに一致する
        active を合成する。`_verify_with_active` の mock 経路の形に合わせ、
        firebase は scalar (現在値 1 つ)、gcloud/github は dict を返す。
        builder が受理した形である前提なので、使える値は必ず 1 つ以上ある。
        """
        if svc_name == "gcloud":
            def usable(key):
                v = expected.get(key)
                return v if isinstance(v, str) and v.strip() else None
            return {"project": usable("project"), "account": usable("account")}
        if svc_name == "github":
            return {
                k: v for k, v in expected.items() if isinstance(v, str) and v.strip()
            }
        if svc_name == "firebase":
            for v in expected.values():
                if isinstance(v, str) and v.strip():
                    return v
            raise AssertionError(
                f"accepted firebase shape {expected!r} has no usable value"
            )
        raise AssertionError(
            f"unsupported service for exhaustive shape test: {svc_name}"
        )

    def test_accepted_shapes_exhaustive_enumeration_pass_verify(self):
        """`ACCEPTED` の手書き表は「気付いた形」しか拾えない。Codex R3 P2:
        `{"region": "us-central1"}` だけの gcloud dict は表に無く、
        `good_values` が未知キーの値まで数える不具合を検出できなかった
        (project も account も無いのに builder は受理し、gcloud.verify() は
        両方無いとして deny していた)。

        dict を受け付ける各 service について、`_EXHAUSTIVE_DICT_UNIVERSE` の
        キー全体の全部分集合 × 値種別 {正常 str, "", None, 123} の直積
        (`_iter_dict_shapes`) を全数探索し、builder (strict_keys 両方の値) が
        受理した形はすべて verify() が形を理由に deny しないことを確認する
        (D10/D12 の不変条件)。aws/kubectl (ACCEPTS_DICT=False) と scalar 値の
        ケースは対象外 (`ACCEPTED` の手書き表がそちらを担当する)。
        """
        accepted_counts = {svc_name: 0 for svc_name in self._EXHAUSTIVE_DICT_UNIVERSE}
        checked = 0
        for svc_name, keys in self._EXHAUSTIVE_DICT_UNIVERSE.items():
            svc = builder._SERVICE_BY_KEY[svc_name]
            for expected in _iter_dict_shapes(keys):
                checked += 1
                for strict_keys in (False, True):
                    reason = builder._validate_entry_shape(
                        svc, expected, strict_keys=strict_keys
                    )
                    if reason is not None:
                        continue
                    active = self._active_from_expected(svc_name, expected)
                    with self.subTest(
                        svc=svc_name, expected=expected, strict_keys=strict_keys
                    ):
                        verify_reason = self._verify_with_active(
                            svc_name, expected, active
                        )
                        self.assertIsNone(
                            verify_reason,
                            "builder "
                            f"({'strict' if strict_keys else 'lenient'}) が"
                            f"受理した形を verify() が形を理由に deny した: "
                            f"{expected!r} -> {verify_reason!r}",
                        )
                    accepted_counts[svc_name] += 1
        expected_checked = sum(
            5 ** len(keys) for keys in self._EXHAUSTIVE_DICT_UNIVERSE.values()
        )
        self.assertEqual(
            checked,
            expected_checked,
            "全数探索が生成した形の総数が想定 (5**len(keys) の合計) と異なる"
            " (_iter_dict_shapes の実装ミスの可能性)",
        )
        for svc_name, count in accepted_counts.items():
            with self.subTest(svc=svc_name):
                self.assertGreater(
                    count,
                    0,
                    f"{svc_name} の全数探索が受理形を 1 件も見つけていない"
                    " (universe 設定ミスで検査が空振りしている可能性)",
                )

    def test_empty_scalar_rejected_for_every_service(self):
        """空文字 scalar はどの service でも書けてはならない。

        根拠: dispatcher は空文字の entry をキーの欠落と同じ扱いにするため、
        書けてしまうと以後その service は設定を直すまで恒久 deny になる
        (verify() に到達すらしない)。dispatcher 側の挙動そのものは
        test_dispatcher.py の担当なので、ここでは「builder が書かせない」ことだけを
        固定する。
        """
        for svc_name in ("aws", "github", "gcloud", "firebase", "kubectl"):
            svc = builder._SERVICE_BY_KEY[svc_name]
            with self.subTest(svc=svc_name):
                self.assertIsNotNone(builder._validate_entry_shape(svc, ""))


class TestMigrateKeepNewWithoutLoss(unittest.TestCase):
    """`_migrate_keep_new_without_loss` の direct unit tests (内部バックログ:
    advisor レビューで検出した multi-host/multi-alias の情報欠落の修正 +
    Codex R1 P1: scalar↔dict 比較に host 意味論を持たせた D11)。"""

    def setUp(self):
        from services import firebase, gcloud, github  # noqa: E402
        self.github = github
        self.gcloud = gcloud
        self.firebase = firebase

    def test_identical_values(self):
        self.assertTrue(
            builder._migrate_keep_new_without_loss(self.github, "a", "a")
        )
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                self.gcloud, {"project": "p"}, {"project": "p"}
            )
        )

    def test_github_com_dict_old_is_still_conflict_with_new_scalar(self):
        """Codex R4 P1: github の scalar は `{"github.com": <同値>}` とも等価に
        ならない。`github.verify()` の str 分岐は「github.com が active なら
        それ、無ければ**最初の active host**」を照合する動的な意味論
        (services/github.py の str 分岐) なので、ghe だけが active な環境では
        scalar "USER" は allow・`{"github.com":"USER"}` は deny になる。
        静的なキー宣言では等価と言い切れないので、github は
        SCALAR_EQUIVALENT_DICT_KEY を宣言せず両方向 conflict に倒す
        (R1 では True としていた組を反転させた)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github, "USER", {"github.com": "USER"}
            )
        )

    def test_multi_host_dict_old_with_extra_value_is_lossy(self):
        """最重要の回帰防止: old が multi-host dict で new (scalar) に
        無い値を持つ場合は False (conflict のまま、silent data loss を防ぐ)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github,
                "USER",
                {"github.com": "USER", "ghe.example.com": "example-org"},
            )
        )

    def test_multi_host_dict_old_with_all_same_value_is_lossy(self):
        """old が複数 host/alias を持つ場合、値が全て new (scalar) と同じでも
        「照合される host 集合」は new では 1 つに縮む (github.verify は
        github.com か最初の active host のみを照合する) ため、値が同じでも
        情報欠落として False (conflict のまま) になる。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github,
                "USER",
                {"github.com": "USER", "ghe.example.com": "USER"},
            )
        )

    def test_github_dict_new_with_github_com_is_still_conflict_with_scalar_old(self):
        """対称ケース (Codex R4 P1): dict `{"github.com":"USER"}` は
        「github.com にログイン済みで USER であること」を要求するが、scalar
        "USER" は github.com が無ければ最初の active host で満たされる。
        dict 側のほうが厳しいので old (scalar) の制約自体は失われないが、
        **等価ではない**ため R4 では両方向 conflict に倒し、利用者に選ばせる
        (R1 では True としていた組を反転させた)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github, {"github.com": "USER"}, "USER"
            )
        )

    def test_dict_new_missing_scalar_old_value_is_lossy(self):
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github, {"github.com": "other"}, "USER"
            )
        )

    def test_dict_dict_new_has_all_old_keys_is_not_lossy(self):
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                self.gcloud,
                {"project": "p", "account": "a", "extra": "z"},
                {"project": "p", "account": "a"},
            )
        )

    def test_dict_dict_new_missing_old_key_is_lossy(self):
        """dict-dict でも同じクラスの欠落を防ぐ: old の "account" が new に
        無ければ False (silent drop を許さない)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.gcloud, {"project": "p"}, {"project": "p", "account": "a"}
            )
        )

    def test_dict_dict_old_key_absent_in_new_with_null_value_is_lossy(self):
        """Codex R4 P2: old が `{"ghe.example.com": null}` のような壊れた値を
        持ち new がその host を**省略**している場合、`new_val.get(k) == v` は
        「欠落した key の None」と「保存された null」が両方 None になるため
        真になり、conflict が検出されなかった。しかも old 側の値は addition
        ではないので `_validate_entry_shape` の検査も通らず、host entry が
        黙って失われたまま「旧ファイルを削除してよい」と案内していた。
        値を比較する前に `k in new_val` (キーの存在) を要求する。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github,
                {"github.com": "USER"},
                {"github.com": "USER", "ghe.example.com": None},
            )
        )

    def test_dict_dict_old_null_value_present_in_new_is_not_lossy(self):
        """対になる正ケース: new が同じキーを同じ値 (null) で持っていれば
        情報は失われないので True のまま (キー存在チェックが過剰に
        conflict を増やしていないことを固定する)。"""
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                self.github,
                {"github.com": "USER", "ghe.example.com": None},
                {"github.com": "USER", "ghe.example.com": None},
            )
        )

    def test_dict_dict_differing_value_is_lossy(self):
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.gcloud,
                {"project": "p", "account": "a"},
                {"project": "p", "account": "b"},
            )
        )

    def test_unequal_scalars(self):
        self.assertFalse(
            builder._migrate_keep_new_without_loss(self.github, "a", "b")
        )

    # --- D11 / Codex R1 P1: scalar↔dict は host 意味論で判定する ---

    def test_single_non_scalar_key_dict_old_is_conflict(self):
        """P1 の指摘そのもの: scalar "USER" は github.com (active なら) を
        照合するが `{"ghe.example.com":"USER"}` は名指しの GHE ホストを照合する。
        値が一致し dict が単一キーでも**制約が違う**ので conflict に倒す
        (修正前は len(old)==1 かつ値一致で True = GHE の制約を無警告で破棄)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github, "USER", {"ghe.example.com": "USER"}
            )
        )

    def test_dict_new_without_scalar_key_does_not_cover_scalar_old(self):
        """逆方向 (dict-new / str-old)。修正前は `any(v == old)` で値がどこかに
        あれば True にしていたため、old の scalar が照合していた github.com の
        制約が new に無くても非損失と判定していた (S3 で「既知の制限」として
        残していた分岐。D11 で解消)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.github, {"ghe.example.com": "USER"}, "USER"
            )
        )

    def test_gcloud_scalar_key_is_project(self):
        """gcloud.verify() の scalar 分岐は project だけを照合する
        (SCALAR_EQUIVALENT_DICT_KEY = "project")。"""
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                self.gcloud, "my-project", {"project": "my-project"}
            )
        )
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                self.gcloud,
                {"project": "my-project", "account": "me@example.com"},
                "my-project",
            )
        )

    def test_gcloud_scalar_new_drops_account_constraint(self):
        """scalar new は account の制約を持てないので、old が account を
        持っていれば conflict (情報欠落)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.gcloud,
                "my-project",
                {"project": "my-project", "account": "me@example.com"},
            )
        )

    def test_gcloud_dict_new_on_other_key_does_not_cover_scalar_old(self):
        """old scalar は project の制約。new が account にだけ同じ値を持っていても
        引き継ぎにならない (照合先が違う)。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.gcloud, {"account": "my-project"}, "my-project"
            )
        )

    def test_firebase_cross_type_is_conflict(self):
        """firebase は SCALAR_EQUIVALENT_DICT_KEY を宣言しない — dict キーは
        alias 名で verify() の verdict (`current in valid`) に効かない一方、
        is_self_remediation が `firebase use <alias>` の対象として読む情報を
        持つため、畳み込むと自己回復の案内先が消える。値が一致しても両方向
        conflict に倒し、利用者に選ばせる。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.firebase, "proj-dev", {"default": "proj-dev"}
            )
        )
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.firebase, {"default": "proj-dev"}, "proj-dev"
            )
        )

    def test_firebase_same_type_comparisons_unaffected(self):
        """キー未宣言でも同型比較 (dict-dict / 完全一致) は従来どおり働く。"""
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                self.firebase, {"default": "proj-dev"}, {"default": "proj-dev"}
            )
        )
        self.assertTrue(
            builder._migrate_keep_new_without_loss(
                self.firebase,
                {"default": "proj-dev", "prod": "proj-prod"},
                {"default": "proj-dev"},
            )
        )
        self.assertFalse(
            builder._migrate_keep_new_without_loss(
                self.firebase, {"default": "proj-dev"}, {"default": "other"}
            )
        )

    # (service 名, scalar 値, dict 値,
    #  scalar-new/dict-old の非損失, dict-new/scalar-old の非損失)
    SCALAR_DICT_MATRIX = [
        # github: SCALAR_EQUIVALENT_DICT_KEY を宣言しない (Codex R4 P1) ため
        # 等価に見える組も含め両方向 conflict。
        ("github", "USER", {"github.com": "USER"}, False, False),
        ("github", "USER", {"ghe.example.com": "USER"}, False, False),
        (
            "github",
            "USER",
            {"github.com": "USER", "ghe.example.com": "USER"},
            False,
            False,
        ),
        # gcloud: scalar 分岐が静的に project だけを照合するので "project" は等価。
        ("gcloud", "my-project", {"project": "my-project"}, True, True),
        ("gcloud", "my-project", {"account": "my-project"}, False, False),
        (
            "gcloud",
            "my-project",
            {"project": "my-project", "account": "me@example.com"},
            # scalar-new は account の制約を持てない (情報欠落) / dict-new は
            # old scalar の project 制約を含む (非損失)
            False,
            True,
        ),
        # firebase: dict キー (alias) が verdict に効かないので未宣言 → 両方向 conflict。
        ("firebase", "proj-dev", {"default": "proj-dev"}, False, False),
        # dict を受け付けない service も同様 (キー未宣言なので常に conflict)。
        ("aws", "123456789012", {"default": "123456789012"}, False, False),
        ("kubectl", "my-context", {"default": "my-context"}, False, False),
    ]

    def test_scalar_dict_matrix_both_directions(self):
        """service × 両方向の表で scalar↔dict の非損失判定を固定する。

        github が `SCALAR_EQUIVALENT_DICT_KEY` を撤回した (Codex R4 P1) 結果を
        含め、どの service のどちらの方向が conflict になるかを 1 か所で読める
        ようにする。"""
        for svc_name, scalar, dct, scalar_new_ok, dict_new_ok in self.SCALAR_DICT_MATRIX:
            svc = builder._SERVICE_BY_KEY[svc_name]
            with self.subTest(svc=svc_name, dct=dct, direction="scalar-new/dict-old"):
                self.assertEqual(
                    builder._migrate_keep_new_without_loss(svc, scalar, dct),
                    scalar_new_ok,
                )
            with self.subTest(svc=svc_name, dct=dct, direction="dict-new/scalar-old"):
                self.assertEqual(
                    builder._migrate_keep_new_without_loss(svc, dct, scalar),
                    dict_new_ok,
                )

    def test_unknown_service_cross_type_is_conflict(self):
        """service が None (accounts.local.json に未知のキーがある) の場合も
        scalar↔dict は conflict。SCALAR_EQUIVALENT_DICT_KEY を読めないので
        等価性を主張できない。"""
        self.assertFalse(
            builder._migrate_keep_new_without_loss(None, "USER", {"a": "USER"})
        )
        self.assertFalse(
            builder._migrate_keep_new_without_loss(None, {"a": "USER"}, "USER")
        )
        self.assertTrue(builder._migrate_keep_new_without_loss(None, "x", "x"))


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

    def test_rejects_empty_scalar_value(self):
        """init 経路も同じ `_validate_entry_shape` を通る (ガードは 1 箇所)。"""
        code, _out, err = self._run(
            ["init", "--service", "aws", "--value", "", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("空文字", err)
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

    def test_overwrites_null_entry(self):
        """`{"github": null}` のような壊れた entry も set で上書き (修復)
        できることを固定する (Codex R2 P2)。remove は `existing.get()` が
        null と欠落キーを区別できず削除できないバグだったが、set は
        commit 時に `updated[service_key] = new_entry` を既存値の有無に
        関係なく書くため、この形はもともと修復できていた — action の表示は
        `existing_value is None` (= 欠落と同じ判定) により『+ add』になる。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": None, "aws": "111"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["set", "--service", "github", "--value", "new-user", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("+ add", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "new-user", "aws": "111"})

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

    def test_rejects_empty_scalar_value(self):
        """Codex R1 P2: `--value "$UNSET_VAR"` が空文字に展開されると
        `"aws": ""` を書けてしまい、dispatcher がキー欠落と同じ扱いで恒久 deny
        する状態を builder 自身が作っていた。書込前に exit 1 で止める。"""
        for bad in ("", "   "):
            with self.subTest(bad=repr(bad)):
                code, _out, err = self._run(
                    ["set", "--service", "aws", "--value", bad, "--commit"]
                )
                self.assertEqual(code, 1)
                self.assertIn("空文字", err)
                self.assertFalse(self._new_path().exists())

    def test_rejects_empty_scalar_value_via_host(self):
        """--host 経由 (dict の 1 キー) でも同じガードが効く。"""
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--host", "github.com",
                "--value", "  ", "--commit",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("空文字", err)
        self.assertFalse(self._new_path().exists())

    def test_rejects_whitespace_only_dict_key_via_value(self):
        """`--host` の空文字ガードは `--host` 経由の書込にしか効かない。
        `--value` で dict を直接渡す経路は別なので、`_validate_entry_shape`
        側でも空白のみのキーを弾けていることを end-to-end で確認する
        (弾けないと `{"github.com のつもりが空白": "..."}` が書き込まれ、
        github.verify() が実在しない host として永久 deny する)。"""
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--value", '{"   ":"USER"}',
                "--commit",
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(self._new_path().exists())
        self.assertIn("空文字", err)

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

    def test_removes_key_with_null_value(self):
        """`{"github": null}` のような壊れた entry (dispatcher がキー欠落と
        同じ扱いで絶対 deny する形) も、キーごと削除できる (Codex R2 P2)。
        修正前は `existing.get(service_key) is None` で判定しており、null
        値と『キーが存在しない』を区別できず、この test_noop_when_key_absent
        と同じ『何もしません』分岐に落ちて何も削除されなかった。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": None, "aws": "111"}), encoding="utf-8"
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

    def test_host_rejected_when_existing_is_null(self):
        """null (壊れた entry) は dict ではないので、--host 指定時は scalar と
        同じ『オブジェクトではありません』エラーに倒す (Codex R2 P2)。この形を
        削除するには --host を外す (test_removes_key_with_null_value)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": None}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["remove", "--service", "github", "--host", "github.com", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("オブジェクトではありません", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": None})  # 変更されない

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

    def test_removes_host_drops_whole_key_when_remaining_has_no_usable_value(self):
        """`--host` 削除後の残り dict も、最後の 1 つを消したときと同じ基準
        (`_validate_entry_shape(strict_keys=False)`) で検証する。gcloud は
        `DICT_ALLOWED_KEYS = {"project", "account"}` を宣言しており、
        `region` は verify() が読まない未知キーなので、"project" を消すと
        残りは `{"region": "x"}` になり許可キーに使える値が 1 つも無くなる。
        この形をそのまま書くと `gcloud.verify()` は project も account も
        無いとして deny する (書込は成功したのに実行時に fail-closed deny)。
        最後の host/alias を消したときと同じくキー自体を削除するのが正しい。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"gcloud": {"project": "p", "region": "x"}, "aws": "111"}),
            encoding="utf-8",
        )
        code, out, _err = self._run(
            ["remove", "--service", "gcloud", "--host", "project", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("キーごと削除", out)
        self.assertIn("有効な値がありません", out)  # validator の理由を明示する
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"aws": "111"})
        self.assertNotIn("gcloud", data)  # {"region": "x"} ではなくキー自体が無い

    def test_removes_host_keeps_remaining_dict_when_shape_still_accepted(self):
        """残り dict に許可キー (account) の使える値が残っていれば、従来どおり
        dict のまま書く (キーごと削除しない)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"gcloud": {"project": "p", "account": "a"}}),
            encoding="utf-8",
        )
        code, out, _err = self._run(
            ["remove", "--service", "gcloud", "--host", "project", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("キーごと削除", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"gcloud": {"account": "a"}})

    def test_removes_host_unaffected_for_service_without_dict_allowed_keys(self):
        """`DICT_ALLOWED_KEYS` を宣言しない service (github) は影響を受けない
        — 残ったキー (ghe.example.com) がどんな名前でも、非空文字列の値で
        あれば従来どおり『使える値がある』として dict のまま残す。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps(
                {"github": {"github.com": "U", "ghe.example.com": "U"}}
            ),
            encoding="utf-8",
        )
        code, out, _err = self._run(
            [
                "remove", "--service", "github", "--host", "github.com",
                "--commit",
            ]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("キーごと削除", out)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"ghe.example.com": "U"}})

    def test_removes_host_drop_whole_key_dry_run_hides_value_in_reason(self):
        """dry-run でも同じ判定・同じメッセージになり (書込前に検証するため)、
        かつ理由メッセージ (validator の shape_error) が dict の値そのものを
        含まない (D3: 既定では値を stdout に出さない) ことを確認する。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"gcloud": {"project": "super-secret-proj", "region": "x"}}),
            encoding="utf-8",
        )
        code, out, _err = self._run(
            ["remove", "--service", "gcloud", "--host", "project", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("(dry-run", out)
        self.assertIn("キーごと削除", out)
        self.assertNotIn("super-secret-proj", out)  # D3: 理由に値そのものは出ない
        # dry-run なので書込は行われない (既存値のまま)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(
            data, {"gcloud": {"project": "super-secret-proj", "region": "x"}}
        )


class TestMigrateValueSemantics(BaseBuilder):
    """migrate の意味論的等価比較 + 追加値スキーマ検証 (内部バックログ:
    型不正/等価値を扱えない不具合の修正)。"""

    def test_github_scalar_new_and_github_com_dict_old_is_conflict(self):
        """Codex R4 P1 の end-to-end (R1 では非衝突としていた組を反転)。
        new=scalar "USER" / old=dict {"github.com":"USER"} は値が一致しても
        **制約が違う** — scalar は「github.com が active ならそれ、無ければ
        最初の active host」を照合するので、ghe だけが active な環境では
        scalar は allow・dict は deny になる。等価と決め打ちして畳み込むと
        明示された github.com の要求が黙って消えるため conflict に倒す。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "USER"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": {"github.com": "USER"}}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "USER"})

    def test_github_github_com_dict_new_and_scalar_old_is_conflict(self):
        """対称ケース (Codex R4 P1): new=dict {"github.com":"USER"} /
        old=scalar "USER" も conflict。どちらを残すかで照合対象のホストが
        変わるため、利用者が両側を見て選ぶ。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"github.com": "USER"}}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "USER"}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"github.com": "USER"}})

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

    def test_dict_dict_old_null_host_missing_in_new_is_conflict(self):
        """Codex R4 P2 の end-to-end: old に `{"ghe.example.com": null}` が
        あり new がその host を省略していると、キー存在を見ない比較では
        「非衝突」と判定され、旧ファイル削除の案内まで進んでしまう
        (host entry は黙って消える)。conflict にして手動解決へ落とす。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"github.com": "USER"}}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps(
                {"github": {"github.com": "USER", "ghe.example.com": None}}
            ),
            encoding="utf-8",
        )
        code, _out, err = self._run(["migrate", "--commit", "--show-values"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)
        self.assertIn("ghe.example.com", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": {"github.com": "USER"}})

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

    def test_non_scalar_key_host_dict_is_conflict_not_silently_collapsed(self):
        """Codex R1 P1 の end-to-end: new=scalar "USER" /
        old={"ghe.example.com":"USER"} は値が一致し old が単一キーでも、
        scalar が照合するのは github.com なので **GHE の制約が消える**。
        conflict のまま手動解決に落とし、書き込まない。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": "USER"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": {"ghe.example.com": "USER"}}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit", "--show-values"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)
        self.assertIn("ghe.example.com", err)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"github": "USER"})

    def test_dict_new_on_other_host_does_not_absorb_scalar_old(self):
        """逆方向 (S3 で「既知の制限」として残していた分岐)。new が
        github.com を持たなければ old scalar の制約は引き継がれない。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"github": {"ghe.example.com": "USER"}}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"github": "USER"}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)

    def test_firebase_scalar_and_alias_dict_is_conflict(self):
        """firebase は SCALAR_EQUIVALENT_DICT_KEY を宣言しないので、値が
        一致する scalar↔dict も conflict (alias 名を無警告で捨てない)。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"firebase": "proj-dev"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"firebase": {"default": "proj-dev"}}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("衝突", err)

    def test_gcloud_scalar_and_project_dict_is_not_conflict(self):
        """gcloud の SCALAR_EQUIVALENT_DICT_KEY = "project" — scalar 期待値は
        project だけを照合するので `{"project": <同値>}` とは等価。"""
        self.new_dir.mkdir(parents=True)
        self._new_path().write_text(
            json.dumps({"gcloud": "my-project"}), encoding="utf-8"
        )
        self._deprecated_path().write_text(
            json.dumps({"gcloud": {"project": "my-project"}}), encoding="utf-8"
        )
        code, _out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        data = json.loads(self._new_path().read_text(encoding="utf-8"))
        self.assertEqual(data, {"gcloud": "my-project"})

    def test_addition_with_truthy_non_str_on_read_key_is_rejected(self):
        """Codex R1 P2 の end-to-end: gcloud.verify() が reject する
        truthy 非 str の account を、migrate が旧パスから取り込めていた
        (書込は通り、次の gcloud 操作で初めて deny される形)。"""
        self._deprecated_path().write_text(
            json.dumps({"gcloud": {"project": "p", "account": 123}}),
            encoding="utf-8",
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("不正です", err)
        self.assertFalse(self._new_path().exists())

    def test_addition_with_empty_scalar_from_old_path_is_rejected(self):
        """旧パスに `"aws": ""` が残っていた場合も取り込まない
        (dispatcher がキー欠落と同じ扱いで恒久 deny する形)。"""
        self._deprecated_path().write_text(
            json.dumps({"aws": ""}), encoding="utf-8"
        )
        code, _out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 1)
        self.assertIn("空文字", err)
        self.assertFalse(self._new_path().exists())

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


class TestInheritedAncestorFile(BaseBuilder):
    """Codex R4 P2: 祖先の accounts.local.json を継承している階層での builder。

    fixture は入れ子ディレクトリ (親に accounts.local.json、その配下の
    `worktrees/feature-x` を cwd) — dispatcher が親遡及で親のファイルを読む
    のと同じ状況。修正前の builder は常に cwd 直下の新パスを対象にしていた
    ため、`set --commit` が**編集した service だけを含む子ファイル**を作り、
    dispatcher の遡及がその子ファイルで止まって、継承していた他 service が
    一斉に未設定 (deny) になっていた。`remove` も継承 entry を「存在しません」
    と報告していた。
    """

    ANCESTOR_ENTRIES = {"github": "USER", "aws": "111111111111"}

    def setUp(self):
        super().setUp()
        self.ancestor_path = self._new_path()
        self.ancestor_path.parent.mkdir(parents=True)
        self.ancestor_path.write_text(
            json.dumps(self.ANCESTOR_ENTRIES) + "\n", encoding="utf-8"
        )
        self.child_dir = self.project_dir / "worktrees" / "feature-x"
        self.child_dir.mkdir(parents=True)
        self._child_env = mock.patch.dict(
            os.environ, {"CLAUDE_PROJECT_DIR": str(self.child_dir)}
        )
        self._child_env.start()
        self.addCleanup(self._child_env.stop)

    def _child_new_path(self) -> Path:
        return (
            self.child_dir
            / ".claude"
            / "verify-cloud-account"
            / "accounts.local.json"
        )

    def _ancestor_data(self) -> dict:
        return json.loads(self.ancestor_path.read_text(encoding="utf-8"))

    def test_set_commit_updates_ancestor_and_creates_no_child_file(self):
        """(1) 継承中の `set --commit` は親ファイルを更新し、子ファイルを
        作らない (修正前は子ファイルを作り、継承していた aws が消えていた)。"""
        code, out, _err = self._run(
            ["set", "--service", "github", "--value", "new-user", "--commit"]
        )
        self.assertEqual(code, 0)
        self.assertIn("継承", out)
        self.assertIn(str(self.ancestor_path), out)
        self.assertFalse(
            self._child_new_path().exists(),
            "子ファイルを作ると dispatcher の遡及がそこで止まり継承分が消える",
        )
        self.assertEqual(
            self._ancestor_data(),
            {"github": "new-user", "aws": "111111111111"},
        )

    def test_remove_commit_deletes_entry_from_ancestor(self):
        """(2) 継承中の `remove --commit` は親ファイルの entry を削除する
        (修正前は子ファイルを見て「存在しません」と報告していた)。"""
        code, out, _err = self._run(["remove", "--service", "aws", "--commit"])
        self.assertEqual(code, 0)
        self.assertIn("継承", out)
        self.assertNotIn("存在しません", out)
        self.assertFalse(self._child_new_path().exists())
        self.assertEqual(self._ancestor_data(), {"github": "USER"})

    def test_init_refuses_to_shadow_inherited_file(self):
        """(3) 継承中の `init` は shadowing になるので exit 2 で拒否し、
        set / remove / --path を案内する。"""
        code, _out, err = self._run(
            ["init", "--service", "gcloud", "--value", "p", "--commit"]
        )
        self.assertEqual(code, 2)
        self.assertIn("継承", err)
        self.assertIn("set", err)
        self.assertIn("--path", err)
        self.assertFalse(self._child_new_path().exists())
        self.assertEqual(self._ancestor_data(), self.ANCESTOR_ENTRIES)

    def test_path_creates_child_specific_file_without_touching_ancestor(self):
        """(4) `--path` 明示時は解決を飛ばして子側に作れる (worktree 専用設定の
        escape hatch)。祖先は変更されない。"""
        code, out, _err = self._run(
            [
                "init", "--service", "gcloud", "--value", "p", "--commit",
                "--path", str(self._child_new_path()),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("--path", out)
        self.assertEqual(
            json.loads(self._child_new_path().read_text(encoding="utf-8")),
            {"gcloud": "p"},
        )
        self.assertEqual(self._ancestor_data(), self.ANCESTOR_ENTRIES)

    def test_path_warns_that_it_shadows_the_inherited_file(self):
        """`--path` は `init` が exit 2 で止めた shadowing を意図的に許す経路
        なので、**その代償をその場で必ず言う**。継承中のファイルを覆い隠すと、
        指定先に書いていない service (ここでは github / aws) が一斉に未設定
        (deny) になる。案内した先で黙って同じ事故を起こさせない。"""
        code, out, _err = self._run(
            [
                "init", "--service", "gcloud", "--value", "p",
                "--path", str(self._child_new_path()),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("警告", out)
        # symlink (macOS の /var → /private/var) を挟んでも一致するよう
        # 解決後のパスで照合する
        self.assertIn(str(self.ancestor_path.resolve()), out)
        self.assertIn("deny", out)

    def test_path_pointing_at_resolved_file_does_not_warn(self):
        """解決結果と同じファイルを --path で名指ししただけなら shadowing は
        起きないので警告しない (警告が常時出ると読まれなくなる)。"""
        code, out, _err = self._run(
            [
                "set", "--service", "github", "--value", "new-user",
                "--path", str(self.ancestor_path),
            ]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("警告", out)

    def test_migrate_path_targets_given_level(self):
        """migrate も `--path` で統合先の階層を明示できる。子側の旧パスだけを
        子側の新パスへ統合し、祖先には触らない。"""
        child_claude = self.child_dir / ".claude"
        child_claude.mkdir(parents=True)
        (child_claude / "accounts.json").write_text(
            json.dumps({"gcloud": "my-project"}), encoding="utf-8"
        )
        code, out, _err = self._run(
            ["migrate", "--commit", "--path", str(self._child_new_path())]
        )
        self.assertEqual(code, 0)
        self.assertIn("--path", out)
        self.assertEqual(
            json.loads(self._child_new_path().read_text(encoding="utf-8")),
            {"gcloud": "my-project"},
        )
        self.assertEqual(self._ancestor_data(), self.ANCESTOR_ENTRIES)

    def test_show_displays_inherited_file(self):
        """(5) `show` は継承元のファイルとその内容を表示する
        (修正前は「子側に無い」として no accounts.local.json と報告していた)。"""
        with mock.patch(
            "services.github.get_active_account",
            return_value={"github.com": "USER"},
        ), mock.patch(
            "services.aws.get_active_account", return_value="111111111111"
        ):
            code, out, _err = self._run(["show", "--show-values"])
        self.assertEqual(code, 0)
        self.assertIn("継承", out)
        self.assertIn(str(self.ancestor_path), out)
        self.assertIn("111111111111", out)
        self.assertNotIn("no accounts.local.json", out)

    def test_set_dry_run_shows_resolved_path_and_writes_nothing(self):
        """dry-run でも解決パスを表示する (書込前に対象を確認できないと、
        親 repo の設定を意図せず書き換える事故を止められない)。"""
        code, out, _err = self._run(
            ["set", "--service", "github", "--value", "new-user"]
        )
        self.assertEqual(code, 0)
        self.assertIn("継承", out)
        self.assertIn(str(self.ancestor_path), out)
        self.assertIn("(dry-run", out)
        self.assertFalse(self._child_new_path().exists())
        self.assertEqual(self._ancestor_data(), self.ANCESTOR_ENTRIES)

    def test_migrate_targets_ancestor_level(self):
        """継承中の migrate は**祖先階層**の旧パスを統合する。cwd 直下に
        統合すると祖先の設定を覆い隠す子ファイルを作ってしまう。"""
        deprecated = self.claude_dir / "accounts.local.json"
        deprecated.write_text(
            json.dumps({"gcloud": "my-project"}), encoding="utf-8"
        )
        code, out, _err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0)
        self.assertIn("継承", out)
        self.assertFalse(self._child_new_path().exists())
        self.assertEqual(
            self._ancestor_data(),
            {"github": "USER", "aws": "111111111111", "gcloud": "my-project"},
        )

    def test_set_refuses_when_ancestor_level_has_legacy_path(self):
        """継承先の階層に旧パスが同居していれば (D4 conflict)、cwd 側から
        操作しても同じ理由で拒否する — 書けてしまうと dispatcher は
        fail-closed deny のままになる。"""
        (self.claude_dir / "accounts.local.json").write_text(
            json.dumps({"gcloud": "my-project"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            ["set", "--service", "github", "--value", "new-user", "--commit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("旧パス", err)
        self.assertIn("migrate", err)
        self.assertEqual(self._ancestor_data(), self.ANCESTOR_ENTRIES)


class TestExplicitPathArgument(BaseBuilder):
    """`--path` の受理範囲 (D2 / D14)。

    builder は「dispatcher が読む配置」以外には書けないままにする — 任意
    パスへの書込を許すと、書けても hook が一生読まない設定ファイルを作れて
    しまい、利用者から見ると「設定したのに検証されない」になる。
    """

    def test_rejects_path_outside_known_tiers(self):
        code, _out, err = self._run(
            [
                "init", "--service", "github", "--value", "u", "--commit",
                "--path", str(self.project_dir / "accounts.local.json"),
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--path", err)
        self.assertFalse((self.project_dir / "accounts.local.json").exists())

    def test_rejects_write_to_deprecated_tier(self):
        """書込を伴うコマンドの --path は new パスのみ (旧パスへの新規書込は
        複数パス conflict を自作することになる)。"""
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--value", "u", "--commit",
                "--path", str(self.claude_dir / "accounts.local.json"),
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("新パス", err)
        self.assertFalse((self.claude_dir / "accounts.local.json").exists())

    def test_show_accepts_deprecated_tier_path(self):
        """show は読み取り専用なので旧パスも直接見られる。"""
        deprecated = self.claude_dir / "accounts.local.json"
        deprecated.write_text(json.dumps({"aws": "111"}), encoding="utf-8")
        code, out, _err = self._run(
            ["show", "--show-values", "--path", str(deprecated)]
        )
        self.assertEqual(code, 0)
        self.assertIn(str(deprecated), out)
        self.assertIn("111", out)

    def test_path_writes_only_to_given_file(self):
        """--path 指定時は 3-tier 解決を飛ばし、指定ファイルだけを書く。"""
        other = self.project_dir / "sub"
        target = other / ".claude" / "verify-cloud-account" / "accounts.local.json"
        code, out, _err = self._run(
            [
                "init", "--service", "aws", "--value", "111", "--commit",
                "--path", str(target),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("--path", out)
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")), {"aws": "111"}
        )
        self.assertFalse(self._new_path().exists())

    def test_path_still_refuses_same_level_conflict(self):
        """--path が飛ばすのは**探索**であって D4 の fail-closed ではない。
        指定先と同じ階層に旧パスが同居していれば拒否する (自作の conflict を
        書き込ませない)。"""
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        (self.claude_dir / "accounts.json").write_text(
            json.dumps({"aws": "111"}), encoding="utf-8"
        )
        code, _out, err = self._run(
            [
                "set", "--service", "github", "--value", "u", "--commit",
                "--path", str(self._new_path()),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("旧パス", err)
        self.assertFalse(self._new_path().exists())


if __name__ == "__main__":
    unittest.main()
