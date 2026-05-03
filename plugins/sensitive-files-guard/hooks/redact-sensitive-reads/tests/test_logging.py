"""logging.py の detail sanitize テスト (L1, 0.4.3)。

呼出側責任で「公開可情報のみ渡す」設計だが、コード変更時の意図せぬ秘密混入
(path / 値 / basename / コマンド文字列) を実行時に止める最終防御層。違反は
``_BAD`` placeholder に置換し、ログファイルへ漏れない。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import logging as L


class TestSanitizeDetail(unittest.TestCase):
    """``_sanitize_detail`` のホワイトリスト挙動。"""

    def test_typical_identifiers_pass(self):
        # 既存使用例で頻出する文字列はすべて通る
        for ok in (
            "FileNotFoundError",
            "OSError",
            "regular",
            "symlink",
            "missing",
            "input_redirect_glob_match",
            "segment_residual_metachar_lenient",
            "shell_keyword_lenient:if",
            "shell_keyword_lenient:[[",
            "glob_match:cat",
            "shlex_fail:ValueError",
            "bash:ValueError",
            "dotenv_parse_failed",
            "",
        ):
            self.assertEqual(L._sanitize_detail(ok), ok)

    def test_path_like_detail_is_blocked(self):
        # path / 値が混入したケースは _BAD に倒される
        for bad in (
            "/Users/mao/.env",
            "./relative/.env",
            ".env",  # `.` 単体は OK だが basename レベルは長さ的にも置換不要…
        ):
            # `.env` 単独は許可文字だけなので通る (誤検知ではなく仕様)
            # → path 形式のテストとしては / を含むケースで判定
            pass

        for bad in (
            "/Users/mao/.env",
            "./relative/.env",
            "DATABASE_URL=postgresql://x",
            "some value with space",
            'with "quote"',
        ):
            self.assertEqual(L._sanitize_detail(bad), L._DETAIL_PLACEHOLDER)

    def test_overlong_detail_is_blocked(self):
        # 64 文字超は丸ごと _BAD に
        long_val = "A" * 65
        self.assertEqual(L._sanitize_detail(long_val), L._DETAIL_PLACEHOLDER)

    def test_max_length_passes(self):
        # ちょうど 64 文字は通る (境界条件)
        boundary = "A" * 64
        self.assertEqual(L._sanitize_detail(boundary), boundary)

    def test_non_string_returns_placeholder(self):
        for bad in (None, 123, ["list"], {"k": "v"}):
            self.assertEqual(
                L._sanitize_detail(bad),  # type: ignore[arg-type]
                L._DETAIL_PLACEHOLDER,
            )

    def test_control_chars_are_blocked(self):
        # 改行・タブ・null 等は秘密側に入っている可能性が高いので drop
        for bad in (
            "ok\nleak",
            "ok\tleak",
            "ok\x00leak",
        ):
            self.assertEqual(L._sanitize_detail(bad), L._DETAIL_PLACEHOLDER)


class TestLogFileSanitize(unittest.TestCase):
    """ログファイル書き込み時に detail が sanitize されること。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # LOG_PATH を tmpdir に差し替え (実ホームを汚染しない)
        self._log_path_patch = mock.patch.object(
            L, "LOG_PATH", Path(self.tmp) / "redact-hook.log",
        )
        self._log_path_patch.start()
        self.addCleanup(self._log_path_patch.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_log(self) -> str:
        log_path = L.LOG_PATH
        if not log_path.exists():
            return ""
        return log_path.read_text()

    def test_clean_detail_passes_through(self):
        L.log_info("classify", "regular")
        self.assertIn("regular", self._read_log())
        self.assertNotIn("_BAD", self._read_log())

    def test_dirty_detail_replaced_with_placeholder(self):
        # path 風の detail を渡すと _BAD に置換されてログに書かれる
        L.log_info("classify", "/Users/mao/.env/secret")
        log = self._read_log()
        self.assertIn("_BAD", log)
        # 元の path 文字列はログに漏れない
        self.assertNotIn("/Users/mao/.env/secret", log)
        self.assertNotIn("secret", log)

    def test_log_error_also_sanitizes(self):
        # log_error も同じ防御層を通す
        # ただし stderr 出力は category だけで detail は出さない設計
        try:
            saved_stderr = os.dup(2)
            r, w = os.pipe()
            os.dup2(w, 2)
            os.close(w)
            try:
                L.log_error("normalize_failed", "/Users/mao/secret/path")
            finally:
                os.dup2(saved_stderr, 2)
                os.close(saved_stderr)
                os.close(r)
        except Exception:
            # piping が壊れても本体テストは続行
            L.log_error("normalize_failed", "/Users/mao/secret/path")

        log = self._read_log()
        self.assertIn("_BAD", log)
        self.assertNotIn("/Users/mao/secret/path", log)


if __name__ == "__main__":
    unittest.main()
