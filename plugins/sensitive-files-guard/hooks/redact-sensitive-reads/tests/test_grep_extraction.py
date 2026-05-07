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
    def test_simple_positional_pattern(self):
        keys = extract_grep_keys(_tokens("grep DATABASE_URL .env"))
        self.assertEqual(keys, ["DATABASE_URL"])

    def test_dash_e_with_pattern(self):
        keys = extract_grep_keys(_tokens("grep -e DATABASE_URL .env"))
        self.assertEqual(keys, ["DATABASE_URL"])

    def test_dash_E_alternation(self):
        # `-E 'A|B|C'` の `|` 分割は regex.findall が境界処理する
        keys = extract_grep_keys(_tokens("grep -E 'JWT_SECRET|DATABASE_URL|API_KEY' .env"))
        self.assertEqual(set(keys), {"JWT_SECRET", "DATABASE_URL", "API_KEY"})
        # 出現順 dedup
        self.assertEqual(keys[0], "JWT_SECRET")

    def test_long_regex_option(self):
        keys = extract_grep_keys(_tokens("grep --regex=DATABASE_URL .env"))
        self.assertEqual(keys, ["DATABASE_URL"])

    def test_long_pattern_option(self):
        keys = extract_grep_keys(_tokens("grep --pattern=JWT_SECRET .env"))
        self.assertEqual(keys, ["JWT_SECRET"])

    def test_anchor_in_pattern(self):
        # `^DATABASE_URL=` から DATABASE_URL を抽出 (anchor / `=` は無視される)
        keys = extract_grep_keys(_tokens("grep ^DATABASE_URL= .env"))
        self.assertEqual(keys, ["DATABASE_URL"])

    def test_dotted_pattern_extracts_uppercase_part(self):
        # `DB.URL` のような中間ピリオド付きでも、左の `DB` は 2 文字なので
        # 3 文字以上要件 (`{2,}`) で落ちる。`URL` は 3 文字で抽出される
        keys = extract_grep_keys(_tokens("grep DB.URL .env"))
        self.assertEqual(keys, ["URL"])

    def test_dedup_in_order(self):
        keys = extract_grep_keys(_tokens(
            "grep -e DATABASE_URL -e DATABASE_URL -e JWT_SECRET .env"
        ))
        self.assertEqual(keys, ["DATABASE_URL", "JWT_SECRET"])

    def test_short_options_skipped(self):
        # `-i` / `-r` / `-v` は bool flag として skip
        keys = extract_grep_keys(_tokens("grep -i -r DATABASE_URL .env"))
        self.assertEqual(keys, ["DATABASE_URL"])

    def test_lowercase_pattern_ignored(self):
        # env-var 形式 (`^[A-Z][A-Z0-9_]{2,}$`) に合致しない
        keys = extract_grep_keys(_tokens("grep database_url .env"))
        self.assertEqual(keys, [])

    def test_short_uppercase_ignored(self):
        # 3 文字未満は抽出しない
        keys = extract_grep_keys(_tokens("grep AB .env"))
        self.assertEqual(keys, [])

    def test_dash_dash_separator_stops_pattern_scan(self):
        # `--` 以降は positional 扱いで pattern 抽出しない
        keys = extract_grep_keys(_tokens("grep DATABASE_URL -- IGNORED_KEY .env"))
        self.assertEqual(keys, ["DATABASE_URL"])

    def test_inline_long_option_value(self):
        # `--regex=A|B` 形式 (1 トークン)
        keys = extract_grep_keys(_tokens("grep --regex='A_KEY|B_KEY' .env"))
        self.assertEqual(set(keys), {"A_KEY", "B_KEY"})

    def test_empty_tokens(self):
        self.assertEqual(extract_grep_keys([]), [])

    def test_only_command_no_args(self):
        self.assertEqual(extract_grep_keys(["grep"]), [])

    def test_dash_e_at_end_no_value(self):
        # 値不足でも crash しない
        keys = extract_grep_keys(_tokens("grep -e"))
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()
