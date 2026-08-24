"""core.cli_options.strip_leading_options: CLI 名直後の global option 剥がし。"""
from __future__ import annotations

import shlex
import unittest

import _testutil  # noqa: F401

from core.cli_options import strip_leading_options  # noqa: E402

WITH_VALUE = frozenset({"--profile", "--region", "-P"})
FLAGS = frozenset({"--debug", "--no-verify-ssl"})


class TestStripLeadingOptions(unittest.TestCase):
    def _strip(self, cmd: str):
        return strip_leading_options(cmd, WITH_VALUE, FLAGS)

    def test_no_options_returns_same_string(self):
        for cmd in ("aws sso login", "aws", "aws s3 cp 'a b' c", ""):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._strip(cmd), (cmd, {}))

    def test_value_option_space_and_equals_forms(self):
        self.assertEqual(
            self._strip("aws --profile prod sso login"),
            ("aws sso login", {"--profile": "prod"}),
        )
        self.assertEqual(
            self._strip("aws --profile=prod sso login"),
            ("aws sso login", {"--profile": "prod"}),
        )

    def test_multiple_options_and_flags(self):
        norm, opts = self._strip(
            "aws --debug --region us-east-1 --no-verify-ssl --profile prod configure sso"
        )
        self.assertEqual(norm, "aws configure sso")
        self.assertEqual(
            opts,
            {"--debug": True, "--region": "us-east-1", "--no-verify-ssl": True, "--profile": "prod"},
        )

    def test_boolean_flag_with_explicit_value_is_stripped(self):
        """boolean option の `--flag=<bool>` 形も剥がす (Codex R5 P1-A)。

        Go の pflag / cobra は bool に分離形 `--flag value` を許さず `=` 形のみ
        受け付けるため、`--flag=true` は正当な呼び出し形。値の真偽で剥がすかどうかを
        変えない (剥がす目的は後続の subcommand を見つけることなので値は無関係)。
        """
        for cmd, want_value in (
            ("aws --debug=true configure sso", True),
            ("aws --debug=false configure sso", False),
            ("aws --debug=TRUE configure sso", True),
            ("aws --debug=1 configure sso", True),
            ("aws --debug=0 configure sso", False),
            ("aws --debug=yes configure sso", True),
            ("aws --debug=no configure sso", False),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    self._strip(cmd), ("aws configure sso", {"--debug": want_value})
                )

    def test_boolean_flag_with_unparsable_value_is_still_stripped(self):
        """bool として読めない値でも剥がす (値は生文字列で保持)。

        剥がさないと候補が正規化されず、anchored な READONLY / STATE_CHANGING を
        すり抜ける — R5 P1-A の失敗形そのもの。
        """
        self.assertEqual(
            self._strip("aws --debug=maybe configure sso"),
            ("aws configure sso", {"--debug": "maybe"}),
        )

    def test_boolean_flag_equals_form_mixed_with_value_options(self):
        norm, opts = self._strip(
            "aws --debug=false --profile prod --no-verify-ssl=true configure sso"
        )
        self.assertEqual(norm, "aws configure sso")
        self.assertEqual(
            opts, {"--debug": False, "--profile": "prod", "--no-verify-ssl": True}
        )

    def test_short_option(self):
        self.assertEqual(
            self._strip("firebase -P prod use other"),
            ("firebase use other", {"-P": "prod"}),
        )

    def test_double_dash_terminates_options(self):
        self.assertEqual(
            self._strip("aws --profile prod -- sso login"),
            ("aws sso login", {"--profile": "prod"}),
        )

    def test_unknown_option_leaves_candidate_unchanged(self):
        # 値を取るか分からない option に当たったら保守的に無変更 (通常検証に落ちる)。
        # **宣言済みの flag に `=value` が付いた形はここには入らない** — 以前は
        # `aws --debug=true ...` を「未知 option」として無変更にしており、それが
        # Codex R5 P1-A (`kubectl --insecure-skip-tls-verify=true config use-context`
        # が STATE_CHANGING をすり抜ける) の作り込み地点だった。
        # 宣言済み flag の値付き形は test_boolean_flag_with_explicit_value_is_stripped。
        for cmd in (
            "aws --unknown sso login",
            "aws --profile prod --unknown sso login",
            "aws --unknown=true sso login",
            "aws -x sso login",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._strip(cmd), (cmd, {}))

    def test_missing_value_leaves_candidate_unchanged(self):
        self.assertEqual(self._strip("aws --profile"), ("aws --profile", {}))

    def test_only_options_leaves_cli_name(self):
        self.assertEqual(self._strip("aws --profile prod"), ("aws", {"--profile": "prod"}))

    def test_quoted_arguments_preserved(self):
        norm, _ = self._strip("aws --profile prod s3 cp 'a b' c")
        self.assertEqual(norm, "aws s3 cp 'a b' c")
        self.assertEqual(shlex.split(norm), ["aws", "s3", "cp", "a b", "c"])

    def test_unbalanced_quote_leaves_candidate_unchanged(self):
        cmd = 'aws --profile prod s3 cp "a'
        self.assertEqual(self._strip(cmd), (cmd, {}))

    def test_help_and_version_are_not_stripped(self):
        # --help / --version は宣言しない前提 → 未知 option として無変更
        # (剥がすと `aws --version` が `aws` になり readonly 判定から外れる)
        self.assertEqual(self._strip("aws --version"), ("aws --version", {}))
        self.assertEqual(self._strip("aws --help"), ("aws --help", {}))


if __name__ == "__main__":
    unittest.main()
