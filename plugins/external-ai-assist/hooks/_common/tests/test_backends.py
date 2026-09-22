"""`_common/backends` の registry: 絞り込み規則 / 結果の分類 / 各 backend の起動形。

外部 AI CLI は**起動しない**。PATH 先頭に置いた偽 CLI か `subproc` のモックで確認する。
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

import _testutil
from _testutil import write_script

from _common import backends, cursorcli, subproc

# argv は NUL 区切りで記録する (プロンプト本文に改行や `---` が含まれるため)
_RECORD_ARGV = "for a in \"$@\"; do printf '%s\\0' \"$a\"; done > {argv_file}\n"


class TestRegistry(unittest.TestCase):
    """名前の解決と `select()` の絞り込み規則。"""

    def test_names_are_unique_lowercase_identifiers(self):
        names = backends.names()
        self.assertEqual(len(names), len(set(names)), "backend 名が重複している")
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(name, name.lower())
                self.assertTrue(name.isidentifier(), "env に書ける名前であること")

    def test_get_resolves_known_and_rejects_unknown(self):
        self.assertIs(backends.get("cursor"), backends.cursor)
        self.assertIs(backends.get("codex"), backends.codex)
        self.assertIsNone(backends.get("cursr"))

    def test_every_backend_satisfies_the_interface(self):
        for module in backends.ALL:
            with self.subTest(backend=module.NAME):
                self.assertTrue(callable(module.is_available))
                self.assertTrue(callable(module.run))

    def test_select_none_means_all_in_declaration_order(self):
        chosen, unknown = backends.select(backends.ALL, None)
        self.assertEqual([m.NAME for m in chosen], list(backends.names()))
        self.assertEqual(unknown, [])

    def test_select_keeps_request_order_not_declaration_order(self):
        """`fixed` の「列挙順そのまま」は select の並びに依存する (マージ前レビューの指摘)。"""
        chosen, _ = backends.select(backends.ALL, ["codex", "cursor"])
        self.assertEqual([m.NAME for m in chosen], ["codex", "cursor"])

    def test_select_collapses_duplicates_to_the_first_position(self):
        chosen, unknown = backends.select(backends.ALL, ["codex", "cursor", "codex"])
        self.assertEqual([m.NAME for m in chosen], ["codex", "cursor"])
        self.assertEqual(unknown, [])

    def test_select_narrows_and_reports_unknown(self):
        chosen, unknown = backends.select(backends.ALL, ["cursor", "gemini"])
        self.assertEqual([m.NAME for m in chosen], ["cursor"])
        self.assertEqual(unknown, ["gemini"])

    def test_select_unknown_only_is_empty_not_all(self):
        """タイプミスで既定の全件へ fallback しない (外したはずの送信先が黙って戻る)。"""
        chosen, unknown = backends.select(backends.ALL, ["cursr"])
        self.assertEqual(chosen, [])
        self.assertEqual(unknown, ["cursr"])


class TestResultClassification(unittest.TestCase):
    """`from_captured()` の 3 値分類。**`limit` は推測で作らない**。"""

    def _completed(self, returncode: int, stdout: str):
        return subprocess.CompletedProcess(["fake"], returncode, stdout, "")

    def test_zero_exit_with_output_is_ok(self):
        result = backends.from_captured(self._completed(0, "  FINDINGS  \n"))
        self.assertTrue(result.is_ok)
        self.assertEqual(result.text, "FINDINGS")

    def test_none_is_failed(self):
        """timeout / 起動失敗 (コマンド不在) はここに来る。"""
        self.assertTrue(backends.from_captured(None).is_failed)

    def test_nonzero_exit_is_failed(self):
        self.assertTrue(backends.from_captured(self._completed(2, "boom")).is_failed)

    def test_empty_output_is_failed(self):
        self.assertTrue(backends.from_captured(self._completed(0, "  \n")).is_failed)

    def test_limit_is_never_guessed_from_output(self):
        """上限の文言を実機で確認できていないので、**推測で `limit` を作らない**。

        推測パターンを入れると、正常なレビュー本文に「limit」「quota」等が含まれる
        だけで取得済みのレビューを捨てて別の backend へ送り直す (送信量が倍になる)。
        分からないものは `failed` に倒し、上限文言を確認できた backend から
        `run()` 側で `limit()` を返すようにする。
        """
        for stdout, returncode in (
            ("You have reached your usage limit for this month.", 1),
            ("rate limit exceeded", 1),
            ("1. **直接影響** — quota を超えると limit に達する実装になっている", 0),
        ):
            with self.subTest(stdout=stdout):
                result = backends.from_captured(self._completed(returncode, stdout))
                self.assertFalse(result.is_limit, "出力から limit を推測してはいけない")

    def test_truncated_only_touches_ok_results(self):
        ok = backends.ok("x" * 100).truncated(10)
        self.assertEqual(ok.text, "x" * 10)
        failed = backends.failed().truncated(10)
        self.assertTrue(failed.is_failed)
        self.assertIsNone(failed.text)
        self.assertEqual(backends.ok("abc").truncated(None).text, "abc")

    def test_status_values_are_distinct(self):
        self.assertTrue(backends.ok("x").is_ok)
        self.assertTrue(backends.failed().is_failed)
        self.assertTrue(backends.limit().is_limit)
        self.assertFalse(backends.limit().is_failed)


class FakeCliTestCase(unittest.TestCase):
    """PATH 先頭に偽の `cursor` / `codex` を置く。実機の CLI は絶対に起動しない。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self._tmp.name, "bin")
        os.makedirs(self.bin)
        # **開発機の PATH は引き継がない** (実機の cursor / codex を掴んで本当に起動して
        # しまう)。偽 CLI の bash script が使う基本コマンド (`cat` 等) のために
        # システムの標準ディレクトリだけを足す — ここに cursor / codex は入らない。
        self._env = mock.patch.dict(
            os.environ,
            {
                "PATH": os.pathsep.join([self.bin, "/usr/bin", "/bin"]),
                "EXTERNAL_AI_CURSOR_COMMAND": "cursor",
                "TMPDIR": self._tmp.name,
            },
        )
        self._env.start()
        cursorcli.reset()  # プロセス内に残った検出結果を捨てる
        self._grace = mock.patch.object(subproc, "KILL_GRACE_SEC", 0.5)
        self._grace.start()

    def tearDown(self) -> None:
        self._grace.stop()
        self._env.stop()
        cursorcli.reset()
        self._tmp.cleanup()

    def fake(self, name: str, body: str) -> str:
        return write_script(self.bin, name, body)

    def read_argv(self, argv_file: str) -> list[str]:
        with open(argv_file, encoding="utf-8") as f:
            return f.read().split("\0")[:-1]


class TestCursorBackend(FakeCliTestCase):
    def test_run_uses_read_only_print_mode_with_the_prompt_as_one_arg(self):
        argv_file = os.path.join(self._tmp.name, "cursor.argv")
        self.fake(
            "cursor",
            _RECORD_ARGV.format(argv_file=shlex.quote(argv_file)) + "printf 'BODY\\n'\n",
        )
        result = backends.cursor.run("PROMPT-BODY", cwd=None, timeout=5)

        self.assertTrue(result.is_ok)
        self.assertEqual(result.text, "BODY")
        args = self.read_argv(argv_file)
        self.assertEqual(args[:5], ["agent", "--trust", "--print", "--mode", "plan"])
        self.assertEqual(len(args), 6, "プロンプトは最後の 1 引数")
        self.assertEqual(args[5], "PROMPT-BODY")

    def test_run_classifies_failures(self):
        self.fake("cursor", "printf 'partial'\nexit 3\n")
        self.assertTrue(backends.cursor.run("p", cwd=None, timeout=5).is_failed)

    def test_missing_cli_is_failed_not_an_exception(self):
        self.assertTrue(backends.cursor.run("p", cwd=None, timeout=5).is_failed)

    def test_is_available_delegates_to_the_shared_detection(self):
        """検出ロジックは `cursorcli` の 1 か所に保つ (候補順・IDE ランチャーの扱い)。"""
        with mock.patch.object(cursorcli, "is_available", return_value=True) as spy:
            self.assertTrue(backends.cursor.is_available(12.5))
        spy.assert_called_once_with(12.5)

    def test_agent_is_not_a_candidate(self):
        """汎用名 `agent` を候補に戻さない (無関係な実体に diff が渡る)。"""
        self.assertNotIn("agent", cursorcli.CANDIDATES)


class TestCodexBackend(FakeCliTestCase):
    def test_run_sends_prompt_on_stdin_with_read_only_exec(self):
        argv_file = os.path.join(self._tmp.name, "codex.argv")
        stdin_file = os.path.join(self._tmp.name, "codex.stdin")
        self.fake(
            "codex",
            _RECORD_ARGV.format(argv_file=shlex.quote(argv_file))
            + f"cat > {shlex.quote(stdin_file)}\n"
            + "printf 'BODY\\n'\n",
        )
        result = backends.codex.run("PROMPT-BODY", cwd=None, timeout=5)

        self.assertTrue(result.is_ok)
        self.assertEqual(
            self.read_argv(argv_file), ["exec", "-s", "read-only", "--ephemeral", "-"]
        )
        with open(stdin_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "PROMPT-BODY")

    def test_prompt_never_lands_in_argv(self):
        """長い diff を argv に載せない (`ps` から見える / 引数長の上限に当たる)。"""
        argv_file = os.path.join(self._tmp.name, "codex.argv")
        self.fake(
            "codex",
            _RECORD_ARGV.format(argv_file=shlex.quote(argv_file))
            + "cat > /dev/null\nprintf 'BODY\\n'\n",
        )
        backends.codex.run("SECRET-DIFF-BODY", cwd=None, timeout=5)
        self.assertNotIn("SECRET-DIFF-BODY", " ".join(self.read_argv(argv_file)))

    def test_missing_cli_is_failed(self):
        self.assertTrue(backends.codex.run("p", cwd=None, timeout=5).is_failed)

    def test_is_available_follows_path(self):
        self.assertFalse(backends.codex.is_available())
        self.fake("codex", "exit 0\n")
        self.assertTrue(backends.codex.is_available())


class TestReadOnlyLaunch(unittest.TestCase):
    """どの backend も外部 AI に作業ツリーを書き換えさせない (0.9 の設計原則)。"""

    def test_cursor_argv_keeps_plan_mode(self):
        self.assertEqual(
            cursorcli.READONLY_FLAGS, ("--trust", "--print", "--mode", "plan")
        )

    def test_codex_argv_keeps_read_only_sandbox(self):
        self.assertIn("-s", backends.codex.EXEC_ARGS)
        self.assertIn("read-only", backends.codex.EXEC_ARGS)
        self.assertEqual(backends.codex.EXEC_ARGS[-1], "-", "プロンプトは stdin 一本")


if __name__ == "__main__":
    unittest.main()
