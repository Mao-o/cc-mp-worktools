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
        # 値を取るか分からない option に当たったら保守的に無変更 (通常検証に落ちる)
        for cmd in (
            "aws --unknown sso login",
            "aws --profile prod --unknown sso login",
            "aws --debug=true sso login",
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
