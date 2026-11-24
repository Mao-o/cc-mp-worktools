"""``handlers/bash/grep_extract.py`` の単体テスト (E4, 0.10.0)。

``extract_grep_keys`` が grep family の token 列から env-var 名候補を
正しく抽出することを確認する。
"""
from __future__ import annotations

import shlex
import unittest

from _testutil import FIXTURES  # noqa: F401

from handlers.bash.grep_extract import extract_grep_keys, is_grep_command


def _tokens(command: str) -> list[str]:
    return shlex.split(command, comments=False, posix=True)


class TestIsGrepCommand(unittest.TestCase):
    def test_grep_family_recognized(self):
        for tok in ("grep", "rg", "ag", "ack", "egrep", "fgrep"):
            self.assertTrue(is_grep_command(tok), msg=f"{tok!r} should be grep family")

    def test_non_grep_not_recognized(self):
        for tok in ("cat", "head", "tail", "awk", "sed", "find", ""):
            self.assertFalse(is_grep_command(tok), msg=f"{tok!r} should not be grep")


class TestExtractGrepKeys(unittest.TestCase):
    # (ラベル = 畳む前のテスト名, grep のコマンド, 期待する鍵名の列)。
    # コマンドは ``_tokens`` (shlex) で token 列にしてから渡す
    KEYS_CASES = (
        ("test_simple_positional_pattern", "grep DATABASE_URL .env", ["DATABASE_URL"]),
        ("test_dash_e_with_pattern", "grep -e DATABASE_URL .env", ["DATABASE_URL"]),
        ("test_long_regex_option", "grep --regex=DATABASE_URL .env", ["DATABASE_URL"]),
        ("test_long_pattern_option", "grep --pattern=JWT_SECRET .env", ["JWT_SECRET"]),
        # `^DATABASE_URL=` から DATABASE_URL を抽出 (anchor / `=` は無視される)
        ("test_anchor_in_pattern", "grep ^DATABASE_URL= .env", ["DATABASE_URL"]),
        # `DB.URL` のような中間ピリオド付きでも、左の `DB` は 2 文字なので
        # 3 文字以上要件 (`{2,}`) で落ちる。`URL` は 3 文字で抽出される
        ("test_dotted_pattern_extracts_uppercase_part", "grep DB.URL .env", ["URL"]),
        (
            "test_dedup_in_order",
            "grep -e DATABASE_URL -e DATABASE_URL -e JWT_SECRET .env",
            ["DATABASE_URL", "JWT_SECRET"],
        ),
        # `-i` / `-r` / `-v` は bool flag として skip
        ("test_short_options_skipped", "grep -i -r DATABASE_URL .env", ["DATABASE_URL"]),
        # env-var 形式 (`^[A-Z][A-Z0-9_]{2,}$`) に合致しない
        ("test_lowercase_pattern_ignored", "grep database_url .env", []),
        # 3 文字未満は抽出しない
        ("test_short_uppercase_ignored", "grep AB .env", []),
        # `--` 以降は positional 扱いで pattern 抽出しない
        (
            "test_dash_dash_separator_stops_pattern_scan",
            "grep DATABASE_URL -- IGNORED_KEY .env",
            ["DATABASE_URL"],
        ),
        # 値不足でも crash しない
        ("test_dash_e_at_end_no_value", "grep -e", []),
    )

    def test_extracted_keys(self):
        self.assertTrue(self.KEYS_CASES)
        for case, command, expected in self.KEYS_CASES:
            with self.subTest(case=case, command=command):
                keys = extract_grep_keys(_tokens(command))
                self.assertEqual(keys, expected)

    def test_dash_E_alternation(self):
        # `-E 'A|B|C'` の `|` 分割は regex.findall が境界処理する
        keys = extract_grep_keys(_tokens("grep -E 'JWT_SECRET|DATABASE_URL|API_KEY' .env"))
        self.assertEqual(set(keys), {"JWT_SECRET", "DATABASE_URL", "API_KEY"})
        # 出現順 dedup
        self.assertEqual(keys[0], "JWT_SECRET")

    def test_inline_long_option_value(self):
        # `--regex=A|B` 形式 (1 トークン)
        keys = extract_grep_keys(_tokens("grep --regex='A_KEY|B_KEY' .env"))
        self.assertEqual(set(keys), {"A_KEY", "B_KEY"})

    def test_empty_tokens(self):
        self.assertEqual(extract_grep_keys([]), [])

    def test_only_command_no_args(self):
        self.assertEqual(extract_grep_keys(["grep"]), [])


if __name__ == "__main__":
    unittest.main()
