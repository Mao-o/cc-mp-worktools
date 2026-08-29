"""Claude Code 版数検出とモード自動選択のテスト (Codex R1 P1 対応)。

CLI 2.1.163 未満 (または版数不明) の環境で Stop の `additionalContext` が無視され、
`_run_review` が既に `state.complete_claim(...)` を呼んだ後のため、指摘が再試行されず
永久に失われる問題への対応。`EXTERNAL_AI_POST_REVIEW_MODE` 未設定 (auto) 時は実行中の
Claude Code の版数を自動検出し、対応していなければ `block` に fail-closed する。

ここでは版数検出そのもの (`_claude_code_version` 以下) を単体でテストする。モード解決
との結合 (auto → block/context の分岐、通知文への付記) は
`test_throttle_flow.py::TestOutputMode` を参照。

実機の `claude` は絶対に起動しない: `subprocess.run` は全テストで mock する
(`_common/tests/` 等、他 hook のテストと同じ方針)。
"""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from _testutil import load_entry


class VersionDetectTestCase(unittest.TestCase):
    """`CLAUDE_CODE_VERSION` / `CLAUDE_CODE_EXECPATH` を空にしてから各段を検証する。

    `HookTestCase` (git repo 込み) を使わない軽量な基底: 版数検出は git に依存しない
    純粋関数なので、TMPDIR や git repo のセットアップは不要。
    """

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ("CLAUDE_CODE_VERSION", "CLAUDE_CODE_EXECPATH"):
            os.environ.pop(key, None)
        self.entry = load_entry()

    def tearDown(self) -> None:
        self._env.stop()


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["claude", "--version"], returncode=returncode, stdout=stdout)


class TestVersionParsing(VersionDetectTestCase):
    """`_claude_code_version()` の検出順: (a) env var → (b) EXECPATH → (c) subprocess。"""

    def test_env_var_is_used_first_and_subprocess_is_not_invoked(self):
        os.environ["CLAUDE_CODE_VERSION"] = "2.1.200"
        os.environ["CLAUDE_CODE_EXECPATH"] = "/opt/claude/versions/9.9.9/claude"
        with mock.patch.object(self.entry.subprocess, "run") as run:
            self.assertEqual(self.entry._claude_code_version(), (2, 1, 200))
            run.assert_not_called()

    def test_env_var_with_trailing_text_is_parsed_from_prefix(self):
        """将来 hooks 向けに公開された場合の値の形が `claude --version` と同じ
        (`X.Y.Z (Claude Code)`) 可能性に備え、先頭一致で parse する。"""
        os.environ["CLAUDE_CODE_VERSION"] = "2.1.200 (Claude Code)"
        self.assertEqual(self.entry._claude_code_version(), (2, 1, 200))

    def test_invalid_env_var_falls_through_to_execpath(self):
        os.environ["CLAUDE_CODE_VERSION"] = "unknown"
        os.environ["CLAUDE_CODE_EXECPATH"] = (
            "/Users/x/.local/share/claude/versions/2.1.163/claude"
        )
        self.assertEqual(self.entry._claude_code_version(), (2, 1, 163))

    def test_execpath_version_component_is_detected(self):
        os.environ["CLAUDE_CODE_EXECPATH"] = (
            "/Users/x/.local/share/claude/versions/2.1.251/claude"
        )
        self.assertEqual(self.entry._claude_code_version(), (2, 1, 251))

    def test_execpath_component_must_match_exactly(self):
        """`2.1.251-beta` のような余計な文字付きは版数と認めない (パス要素の完全一致)。

        `_VERSION_FULL_RE` が `^...$` の完全一致であることの回帰。先頭一致にすると
        `2.1.251-beta` のような偽陽性を拾ってしまう。
        """
        os.environ["CLAUDE_CODE_EXECPATH"] = "/opt/claude/versions/2.1.251-beta/claude"
        with mock.patch.object(self.entry.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(self.entry._claude_code_version())

    def test_execpath_without_version_component_falls_through_to_subprocess(self):
        """npm 配布のパスには版数ディレクトリが無い。"""
        os.environ["CLAUDE_CODE_EXECPATH"] = (
            "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"
        )
        with mock.patch.object(
            self.entry.subprocess, "run", return_value=_completed("2.1.251 (Claude Code)\n")
        ):
            self.assertEqual(self.entry._claude_code_version(), (2, 1, 251))

    def test_subprocess_success_is_parsed(self):
        with mock.patch.object(
            self.entry.subprocess, "run", return_value=_completed("2.1.251 (Claude Code)\n")
        ):
            self.assertEqual(self.entry._claude_code_version(), (2, 1, 251))

    def test_subprocess_not_on_path_returns_none(self):
        """PATH に無い (`FileNotFoundError` は `OSError` のサブクラス)。"""
        with mock.patch.object(self.entry.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(self.entry._claude_code_version())

    def test_subprocess_timeout_returns_none(self):
        with mock.patch.object(
            self.entry.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude", "--version"], timeout=3),
        ):
            self.assertIsNone(self.entry._claude_code_version())

    def test_subprocess_unparseable_output_returns_none(self):
        with mock.patch.object(
            self.entry.subprocess, "run", return_value=_completed("command not found\n", 127)
        ):
            self.assertIsNone(self.entry._claude_code_version())

    def test_all_sources_fail_returns_none(self):
        """env var 未設定 / EXECPATH 未設定 / subprocess 失敗 → すべて素通りして None。"""
        with mock.patch.object(self.entry.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(self.entry._claude_code_version())

    def test_result_is_cached_within_process(self):
        """判定結果はプロセス内 (モジュール読み込み単位) で 1 回だけ計算する。"""
        with mock.patch.object(
            self.entry.subprocess, "run", return_value=_completed("2.1.251\n")
        ) as run:
            self.assertEqual(self.entry._claude_code_version(), (2, 1, 251))
            self.assertEqual(self.entry._claude_code_version(), (2, 1, 251))
        self.assertEqual(run.call_count, 1, "2 回目の呼び出しでキャッシュが効いていない")

    def test_subprocess_uses_short_timeout_and_devnull_stdin(self):
        """3 秒 timeout かつ hook 自身の stdin (payload の pipe) を継承させないこと。"""
        with mock.patch.object(
            self.entry.subprocess, "run", return_value=_completed("2.1.251\n")
        ) as run:
            self.entry._claude_code_version()
        _, kwargs = run.call_args
        self.assertEqual(kwargs["timeout"], self.entry._VERSION_SUBPROCESS_TIMEOUT_SEC)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)


class TestSupportsAdditionalContext(VersionDetectTestCase):
    """Stop の `additionalContext` が効く最低版数 (2.1.163) の判定。"""

    def test_threshold_table(self):
        cases = [
            ((2, 1, 162), False),
            ((2, 1, 163), True),
            ((2, 2, 0), True),
            (None, False),
        ]
        for version, expected in cases:
            with self.subTest(version=version):
                self.assertEqual(
                    self.entry._stop_supports_additional_context(version), expected
                )


class TestFormatVersion(VersionDetectTestCase):
    def test_known_version_is_dotted(self):
        self.assertEqual(self.entry._format_version((2, 1, 163)), "2.1.163")

    def test_none_is_unknown_label(self):
        self.assertEqual(self.entry._format_version(None), "不明")


if __name__ == "__main__":
    unittest.main()
