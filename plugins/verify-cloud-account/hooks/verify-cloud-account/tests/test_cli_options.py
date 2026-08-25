"""core.cli_options: global option の剥がし (正規化) と context option の抽出。"""
from __future__ import annotations

import shlex
import unittest

import _testutil  # noqa: F401

from core.cli_options import (  # noqa: E402
    find_context_options,
    strip_leading_options,
)

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


class TestShortOptionAttachedValue(unittest.TestCase):
    """短縮 option の連結形 (`-Pprod`) も剥がす。

    `--flag=value` / 分離形だけを扱っていると `firebase -Pprod use other` が
    未知 option 扱いで正規化されず、anchored な STATE_CHANGING をすり抜ける
    (R5 P1-A と同じ失敗形の短縮版)。分解規則は `_option_name_value` に一本化。
    """

    def test_attached_short_value(self):
        self.assertEqual(
            strip_leading_options("firebase -Pprod use other", WITH_VALUE, FLAGS),
            ("firebase use other", {"-P": "prod"}),
        )

    def test_attached_short_value_with_equals(self):
        self.assertEqual(
            strip_leading_options("firebase -P=prod use other", WITH_VALUE, FLAGS),
            ("firebase use other", {"-P": "prod"}),
        )


CONTEXT = {"--profile": "profile"}


class TestFindContextOptions(unittest.TestCase):
    """行全体から「照合先を決める option」を拾う。

    `strip_leading_options` は CLI 名直後しか見ないが、これらは global option
    なのでサブコマンドの後ろにも書ける (`aws s3 ls --profile prod`)。
    """

    def _find(self, cmd, context=None, with_value=None):
        return find_context_options(
            cmd, context if context is not None else CONTEXT,
            with_value if with_value is not None else WITH_VALUE,
        )

    def test_all_written_forms(self):
        for cmd in (
            "aws s3 ls --profile prod",
            "aws s3 ls --profile=prod",
            "aws --profile prod s3 ls",
            "aws --profile=prod s3 ls",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._find(cmd), {"profile": "prod"})

    def test_short_forms(self):
        ctx = {"-P": "project", "--project": "project"}
        for cmd in (
            "firebase deploy -P prod",
            "firebase deploy -P=prod",
            "firebase -Pprod deploy",
            "firebase deploy --project=prod",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._find(cmd, ctx), {"project": "prod"})

    def test_absent_returns_empty(self):
        self.assertEqual(self._find("aws s3 ls"), {})

    def test_last_occurrence_wins(self):
        # argparse / pflag / commander いずれも last-wins。
        self.assertEqual(
            self._find("aws s3 ls --profile a --profile b"), {"profile": "b"}
        )

    def test_unresolvable_value_falls_back_to_default_context(self):
        # 変数展開は hook からは解決できない。既定コンテキストでの照合に戻す
        # (最後の指定が解決不能なら、前の解決可能な値も採用しない)。
        for cmd in (
            "aws s3 ls --profile $PROF",
            'aws s3 ls --profile "${PROF}"',
            "aws s3 ls --profile a --profile $PROF",
            "aws s3 ls --profile=",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._find(cmd), {})

    def test_missing_value_at_end(self):
        self.assertEqual(self._find("aws s3 ls --profile"), {})

    def test_double_dash_terminates_scan(self):
        # `--` 以降は後続コマンドの引数。`kubectl exec pod -- cmd --context x` の
        # `--context` は内側コマンドのものなので採用しない。
        ctx = {"--context": "context"}
        self.assertEqual(
            self._find("kubectl exec pod -- cmd --context inner", ctx, frozenset()), {}
        )

    def test_unknown_options_do_not_stop_the_scan(self):
        """未知 option で打ち切らない — `strip_leading_options` との意図的な差。

        正規化 (`strip_leading_options`) は「未知 option = 値を取るか不明」なので
        打ち切って保守的に何もしないのが正しい。一方こちらはサブコマンド固有の
        未知 option が並ぶ**後方**にある global option を拾うのが目的なので、
        打ち切ると本来の用途を果たせない。代償 (未知の値付き option の値が
        context option に見える誤検出) は README の既知の制限に明記してある。
        """
        self.assertEqual(
            self._find("aws s3 sync . s3://b --delete --quiet --profile prod"),
            {"profile": "prod"},
        )

    def test_known_value_option_consumes_its_value(self):
        # 値トークンを消費しないと `--region --profile` のような並びで値を
        # option と誤読する。消費規則は正規化側と共有。
        self.assertEqual(
            self._find("aws --region us-east-1 s3 ls --profile prod"),
            {"profile": "prod"},
        )

    def test_unbalanced_quote_returns_empty(self):
        self.assertEqual(self._find('aws s3 cp "a --profile prod'), {})


class TestContextValueRejection(unittest.TestCase):
    """コマンドラインの「値に見えるが値ではないもの」を採用しない。

    採用すると照合先が実行時と食い違う。両方向に実害がある:
    誤って別 profile で検証して allow する (false-allow) / 実在しない
    コンテキスト名で deny する (false-deny)。
    """

    def _find(self, cmd, context, with_value=frozenset()):
        return find_context_options(cmd, context, with_value)

    def test_next_option_or_stdin_dash_is_not_a_value(self):
        # heredoc / `-f -` で YAML を流し込む形。`-` は stdin であって context 名ではない。
        self.assertEqual(
            self._find("kubectl apply -f - --context -", {"--context": "context"}), {}
        )
        self.assertEqual(
            self._find("aws s3 ls --profile --debug", {"--profile": "profile"}), {}
        )

    def test_xargs_replacement_placeholder_is_not_a_value(self):
        # `xargs -I{}` の placeholder。実行時に別の文字列へ置換される。
        self.assertEqual(
            self._find("aws --profile {} s3 rm s3://b", {"--profile": "profile"}), {}
        )

    def test_ordinary_values_are_still_accepted(self):
        for value in ("prod", "my-proj-123", "me@example.com", "/tmp/kubeconfig"):
            with self.subTest(value=value):
                self.assertEqual(
                    self._find(
                        f"aws s3 ls --profile {value}", {"--profile": "profile"}
                    ),
                    {"profile": value},
                )


if __name__ == "__main__":
    unittest.main()
