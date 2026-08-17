"""``redaction/file_render.py`` の単体テスト (E3, 0.10.0)。

``render_for_bash(operand, cwd)`` が:

- dotenv ファイルから ``<DATA>`` 包装込みの reason 文字列と info dict 両方を返す
- 非 dotenv (json / toml / yaml / opaque) からは reason のみ返し info は None
- 失敗ケース (file 不在 / symlink / 空 operand / 非通常ファイル) では
  ``(None, None, <failure_kind>)`` を返す (0.16.0 で kind を追加)

ことを確認する。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from redaction.file_render import render_for_bash


class TestRenderForBashDotenv(unittest.TestCase):
    def test_dotenv_returns_reason_and_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "DATABASE_URL=postgresql://u:p@h/d\n"
                "JWT_SECRET=eyJhbGc.eyJzdWI.aaaaaaaaaaaaaa\n"
                "EMPTY_KEY=\n",
                encoding="utf-8",
            )
            reason, info, kind, base = render_for_bash(".env", tmp)
        self.assertIsNotNone(reason)
        self.assertIn('<DATA untrusted="true"', reason)
        self.assertIn("file: .env", reason)
        self.assertIn("DATABASE_URL", reason)
        self.assertIn("JWT_SECRET", reason)
        self.assertIn("EMPTY_KEY", reason)
        # info dict が dotenv format で 3 件のキーを持つ
        self.assertIsNotNone(info)
        self.assertEqual(info["format"], "dotenv")
        self.assertEqual(info["entries"], 3)
        names = [k["name"] for k in info["keys"]]
        self.assertEqual(names, ["DATABASE_URL", "JWT_SECRET", "EMPTY_KEY"])

    def test_envrc_uses_dotenv_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".envrc"
            path.write_text("export FOO=bar\nexport BAZ=qux\n", encoding="utf-8")
            reason, info, kind, base = render_for_bash(".envrc", tmp)
        self.assertIsNotNone(reason)
        self.assertIsNotNone(info)
        self.assertEqual(info["format"], "dotenv")
        names = [k["name"] for k in info["keys"]]
        self.assertEqual(names, ["FOO", "BAZ"])

    def test_dotenv_with_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("KEY=value\n", encoding="utf-8")
            reason, info, kind, base = render_for_bash(str(path), cwd="/")
        self.assertIsNotNone(reason)
        self.assertIsNotNone(info)
        self.assertEqual(info["entries"], 1)


class TestRenderForBashOtherFormats(unittest.TestCase):
    def test_json_returns_reason_no_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            path.write_text('{"client_id": "abc", "secret": "xyz"}', encoding="utf-8")
            reason, info, kind, base = render_for_bash("credentials.json", tmp)
        self.assertIsNotNone(reason)
        self.assertIn('<DATA untrusted="true"', reason)
        self.assertIn("client_id", reason)
        # dotenv 以外は info dict を返さない
        self.assertIsNone(info)

    def test_toml_returns_reason_no_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('foo = "bar"\nbaz = 42\n', encoding="utf-8")
            reason, info, kind, base = render_for_bash("config.toml", tmp)
        self.assertIsNotNone(reason)
        self.assertIsNone(info)

    def test_yaml_falls_back_to_opaque(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.yaml"
            path.write_text("foo: bar\nbaz: qux\n", encoding="utf-8")
            reason, info, kind, base = render_for_bash("secrets.yaml", tmp)
        self.assertIsNotNone(reason)
        self.assertIsNone(info)


class TestRenderForBashFailures(unittest.TestCase):
    def setUp(self):
        # project root fallback (0.16.0) が発火すると結果が変わるため、
        # 素の失敗経路を見るテストでは明示的に環境変数を落としておく。
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.addCleanup(patcher.stop)

    def test_empty_operand(self):
        reason, info, kind, base = render_for_bash("", "/tmp")
        self.assertIsNone(reason)
        self.assertIsNone(info)
        self.assertEqual(kind, "no_operand")

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            reason, info, kind, base = render_for_bash(".env", tmp)
        self.assertIsNone(reason)
        self.assertIsNone(info)
        self.assertEqual(kind, "unresolved")

    def test_relative_operand_unresolvable_from_cwd(self):
        """先行 `cd` で cwd がずれたケース (2026-08-17 実観測の再現)。

        ファイル自体は存在するが hook の cwd からは見えないため
        ``unresolved`` になる。
        """
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj" / "poc"
            proj.mkdir(parents=True)
            (proj / ".env.local").write_text("KEY=value\n", encoding="utf-8")
            other = Path(tmp) / "other"
            other.mkdir()
            reason, info, kind, base = render_for_bash("poc/.env.local", str(other))
        self.assertIsNone(reason)
        self.assertEqual(kind, "unresolved")

    def test_symlink_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.env"
            target.write_text("KEY=value\n", encoding="utf-8")
            link = Path(tmp) / ".env"
            link.symlink_to(target)
            reason, info, kind, base = render_for_bash(".env", tmp)
        # classify が "symlink" になり、render はスキップされる
        self.assertIsNone(reason)
        self.assertIsNone(info)
        self.assertEqual(kind, "not_regular")

    def test_directory_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "envdir").mkdir()
            reason, info, kind, base = render_for_bash("envdir", tmp)
        self.assertIsNone(reason)
        self.assertIsNone(info)
        self.assertEqual(kind, "not_regular")

    def test_normalize_failure_does_not_raise(self):
        # NUL byte を含むパス → ValueError / stat 例外 → (None, None, kind)
        reason, info, kind, base = render_for_bash("\x00.env", "/tmp")
        self.assertIsNone(reason)
        self.assertIsNone(info)
        self.assertIn(kind, {"normalize_failed", "stat_failed"})

    def test_success_kind_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("KEY=value\n", encoding="utf-8")
            reason, info, kind, base = render_for_bash(".env", tmp)
        self.assertIsNotNone(reason)
        self.assertEqual(kind, "")

    def test_failure_kinds_are_log_detail_safe(self):
        """全 kind が core.logging の detail ホワイトリストを通ること。

        kind は ``bash_handler`` からログに渡るため、path / 値が混ざる形
        (``_BAD`` 置換される形) になっていないことを保証する。
        """
        from core.logging import _sanitize_detail
        from redaction.file_render import _CLASSIFY_FAILURE_KIND

        kinds = set(_CLASSIFY_FAILURE_KIND.values()) | {
            "no_operand", "normalize_failed", "stat_failed",
            "open_failed", "redact_failed",
        }
        for k in kinds:
            with self.subTest(kind=k):
                self.assertEqual(_sanitize_detail(f"render_failed:{k}"),
                                 f"render_failed:{k}")


class TestProjectRootFallback(unittest.TestCase):
    """相対 operand が cwd で missing のとき project root 基準で 1 回再解決する。

    先行 ``cd`` で ``cwd`` がずれるケース (2026-08-17 実観測) の救済経路。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp) / "proj"
        (self.root / "poc").mkdir(parents=True)
        (self.root / "poc" / ".env.local").write_text(
            "ANTHROPIC_API_KEY=sk-aaaaaaaaaaaa\nDEBUG=true\n", encoding="utf-8"
        )
        self.other = Path(self.tmp) / "other"
        self.other.mkdir()

    def _with_project_dir(self, value):
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(value)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resolves_from_project_root_and_flags_candidate(self):
        self._with_project_dir(self.root)
        reason, info, status, base = render_for_bash("poc/.env.local", str(self.other))
        self.assertEqual(status, "project_root")
        self.assertIsNotNone(reason)
        self.assertIn("ANTHROPIC_API_KEY", reason)
        self.assertIsNotNone(info)
        # 判別材料として project root の basename を 1 要素だけ返す
        self.assertEqual(base, "proj")

    def test_resolved_base_identifies_which_of_two_candidates_was_read(self):
        """同じ相対 path の機密ファイルが 2 つあるとき、どちらを読んだか判る。

        「候補です」とだけ書いても、読み手は **実際に取り違えたのか** を確認
        できない。resolved_base があれば意図したディレクトリ名と突き合わせて
        判定できる。
        """
        # project root 側 (hook が実際に読む方)
        repo_a = Path(self.tmp) / "repo_a"
        (repo_a / "poc").mkdir(parents=True)
        (repo_a / "poc" / ".env.local").write_text(
            "KEY_FROM_REPO_A=x\n", encoding="utf-8"
        )
        # モデルが `cd` して意図していた方 (hook からは見えない)
        repo_b = Path(self.tmp) / "repo_b"
        (repo_b / "poc").mkdir(parents=True)
        (repo_b / "poc" / ".env.local").write_text(
            "KEY_FROM_REPO_B=y\n", encoding="utf-8"
        )
        self._with_project_dir(repo_a)
        reason, info, status, base = render_for_bash(
            "poc/.env.local", str(self.other)
        )
        self.assertEqual(status, "project_root")
        # 読んだのは repo_a 側。basename でそれが判別できること
        self.assertIn("KEY_FROM_REPO_A", reason)
        self.assertNotIn("KEY_FROM_REPO_B", reason)
        self.assertEqual(base, "repo_a")

    def test_resolved_base_is_single_component_not_full_path(self):
        """絶対 path を漏らさない (matched_operand と同水準に収める)。"""
        self._with_project_dir(self.root)
        _, _, status, base = render_for_bash("poc/.env.local", str(self.other))
        self.assertEqual(status, "project_root")
        self.assertNotIn("/", base)
        self.assertNotIn(self.tmp, base)

    def test_no_fallback_when_project_root_equals_cwd(self):
        self._with_project_dir(self.other)
        reason, info, status, base = render_for_bash("poc/.env.local", str(self.other))
        self.assertIsNone(reason)
        self.assertEqual(status, "unresolved")

    def test_no_fallback_for_absolute_operand(self):
        """絶対 path は基準を変えても同じなので再解決しない。"""
        self._with_project_dir(self.root)
        missing = str(self.other / ".env")
        reason, info, status, base = render_for_bash(missing, str(self.other))
        self.assertIsNone(reason)
        self.assertEqual(status, "unresolved")

    def test_no_fallback_when_cwd_hit_is_not_missing(self):
        """cwd 側にファイルが実在するなら (symlink でも) 別基準を試さない。"""
        self._with_project_dir(self.root)
        (self.other / "poc").mkdir()
        link = self.other / "poc" / ".env.local"
        link.symlink_to(self.root / "poc" / ".env.local")
        reason, info, status, base = render_for_bash("poc/.env.local", str(self.other))
        self.assertIsNone(reason)
        self.assertEqual(status, "not_regular")

    def test_falls_back_to_unresolved_when_project_root_also_misses(self):
        self._with_project_dir(self.root)
        reason, info, status, base = render_for_bash("nope/.env", str(self.other))
        self.assertIsNone(reason)
        self.assertEqual(status, "unresolved")

    def test_git_dir_used_when_env_unset(self):
        """$CLAUDE_PROJECT_DIR 未設定でも .git 上方探索で救える。"""
        (self.root / ".git").mkdir()
        sub = self.root / "sub"
        sub.mkdir()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.addCleanup(patcher.stop)
        reason, info, status, base = render_for_bash("poc/.env.local", str(sub))
        self.assertEqual(status, "project_root")
        self.assertIn("ANTHROPIC_API_KEY", reason)


if __name__ == "__main__":
    unittest.main()
