"""``redaction/file_render.py`` の単体テスト (E3, 0.10.0)。

``render_for_bash(operand, cwd)`` が:

- dotenv ファイルから ``<DATA>`` 包装込みの reason 文字列と info dict 両方を返す
- 非 dotenv (json / toml / yaml / opaque) からは reason のみ返し info は None
- 失敗ケース (file 不在 / symlink / 空 operand / 非通常ファイル) では
  ``(None, None)`` を返す

ことを確認する。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

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
            reason, info = render_for_bash(".env", tmp)
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
            reason, info = render_for_bash(".envrc", tmp)
        self.assertIsNotNone(reason)
        self.assertIsNotNone(info)
        self.assertEqual(info["format"], "dotenv")
        names = [k["name"] for k in info["keys"]]
        self.assertEqual(names, ["FOO", "BAZ"])

    def test_dotenv_with_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("KEY=value\n", encoding="utf-8")
            reason, info = render_for_bash(str(path), cwd="/")
        self.assertIsNotNone(reason)
        self.assertIsNotNone(info)
        self.assertEqual(info["entries"], 1)


class TestRenderForBashOtherFormats(unittest.TestCase):
    def test_json_returns_reason_no_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            path.write_text('{"client_id": "abc", "secret": "xyz"}', encoding="utf-8")
            reason, info = render_for_bash("credentials.json", tmp)
        self.assertIsNotNone(reason)
        self.assertIn('<DATA untrusted="true"', reason)
        self.assertIn("client_id", reason)
        # dotenv 以外は info dict を返さない
        self.assertIsNone(info)

    def test_toml_returns_reason_no_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('foo = "bar"\nbaz = 42\n', encoding="utf-8")
            reason, info = render_for_bash("config.toml", tmp)
        self.assertIsNotNone(reason)
        self.assertIsNone(info)

    def test_yaml_falls_back_to_opaque(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.yaml"
            path.write_text("foo: bar\nbaz: qux\n", encoding="utf-8")
            reason, info = render_for_bash("secrets.yaml", tmp)
        self.assertIsNotNone(reason)
        self.assertIsNone(info)


class TestRenderForBashFailures(unittest.TestCase):
    def test_empty_operand(self):
        reason, info = render_for_bash("", "/tmp")
        self.assertIsNone(reason)
        self.assertIsNone(info)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            reason, info = render_for_bash(".env", tmp)
        self.assertIsNone(reason)
        self.assertIsNone(info)

    def test_symlink_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.env"
            target.write_text("KEY=value\n", encoding="utf-8")
            link = Path(tmp) / ".env"
            link.symlink_to(target)
            reason, info = render_for_bash(".env", tmp)
        # classify が "symlink" になり、render はスキップされる
        self.assertIsNone(reason)
        self.assertIsNone(info)

    def test_directory_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "envdir").mkdir()
            reason, info = render_for_bash("envdir", tmp)
        self.assertIsNone(reason)
        self.assertIsNone(info)

    def test_normalize_failure_does_not_raise(self):
        # NUL byte を含むパス → ValueError → (None, None)
        reason, info = render_for_bash("\x00.env", "/tmp")
        self.assertIsNone(reason)
        self.assertIsNone(info)


if __name__ == "__main__":
    unittest.main()
