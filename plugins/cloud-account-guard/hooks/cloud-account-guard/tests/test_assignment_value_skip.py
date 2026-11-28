"""先頭の代入の値に置換・展開があっても、後ろのコマンドを候補にする (v0.29.10)。

0.29.9 までは、先頭の代入の値にコマンド置換 `$(...)`・backtick があると代入を剥がすのをやめ、
`X=$(z) aws s3 rm ...` の aws を候補にしていなかった。算術展開 `$((1 + 2))` と、空白を含む
パラメータ展開 `${Y:-a b}` は値の途中で値を終え、残りを読み違えていた。値は読まずに飛ばし
(値も集めない)、後ろのコマンドを普段どおり照合する。

0.29.9 の読みがコマンドの候補を作っていた形 (置換の中のパス付き CLI を basename で拾う
`X=$(/usr/bin/firebase deploy) aws ...`、空白を含む `${...}` の中の語から読み直す
`X=${Y:- aws s3 rm x} gh ...`) は、0.29.9 の読みも足して候補を落とさない。

右の注記は、その CLI が走るシェル (bash 3.2 / zsh -f で実測。CHANGELOG の 0.29.10 の節)。
"""
from __future__ import annotations

import unittest
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401

from core import command_parser, workdir  # noqa: E402
from core.command_parser import (  # noqa: E402
    extract_candidate_readings,
    extract_candidates,
    strip_leading_env,
)
from core.dispatcher import dispatch  # noqa: E402

AWS = "aws s3 rm s3://b/x"


class _Case(unittest.TestCase):
    """分解の例外を assertion の失敗にする (変異で例外が漏れても FAIL として数える)。"""

    def call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def cands(self, command):
        return [c for c, _e in self.call(extract_candidates, command)]

    def readings(self, command):
        return self.call(extract_candidate_readings, command)


# 値を飛ばして後ろのコマンドを候補にする形 (どれも bash / zsh とも aws が走る)。
SKIPPED_VALUE_FORMS = [
    "X=$(z) {a}",
    "AWS_PROFILE=$(echo prod) {a}",
    "X=$((1+2)) {a}",
    "X=$((1 + 2)) {a}",
    "X=${Y:-a b} {a}",
    "X=${Y:-a\tb} {a}",
    "X=${Y:-/a b} {a}",
    "X=`z` {a}",
    "X=`z w` {a}",
    "X=a$(z)b {a}",
    'X=$(z)"q r" {a}',
    "X=$(z)x {a}",
    "X=$(z)# {a}",
    "X=$(z)\t{a}",
    "X=`z`\t{a}",
    "X=$(z) \t{a}",
    "X=$(z ')' w) {a}",
    'X=$(z ")" w) {a}',
    "X=$(z # )\n) {a}",
    "X=$(z $(z w)) {a}",
    "X=$(z `z`) {a}",
    "X=`z $(z)` {a}",
    "X=${Y:-$(z w)} {a}",
    "X=${Y:-`z w`} {a}",
    "X=$(\nz\n) {a}",
    "X=$(cat <<EOF\nbody\nEOF\n) {a}",
    "X+=$(z) {a}",
    "X+=`z` {a}",
    "X[0]=$(z) {a}",  # bash (zsh は代入の形として読まない)
    "a=(x)$(z) {a}",
    "a=(x)$(z w) {a}",
    "X=$(z) Y=1 {a}",
    "Y=1 X=$(z) {a}",
    "X=$(z w) Y=$(z v) {a}",
    "(X=$(z) {a})",
    "{ X=$(z) {a}; }",
    "! X=$(z) {a}",
    "if X=$(z) {a}; then true; fi",
    "X=$(z) command {a}",
    "X=$(z) \\{a}",
    "X=$(z) /usr/local/bin/{a}",
    "X=$$(z) {a}",  # zsh
]

# 値の中の空白・タブをエスケープした形と、引用符の中の CLI は、どちらのシェルでも aws を走らせない。
NOT_RUN_FORMS = [
    "X=$(z)\\ {a}",
    "X=$(z)\\\t{a}",
    "X=$(z)' {a}'",
    'X=$(z)" {a}"',
    "X=((1 + 2)) {a}",  # `((` を値の中の算術評価として読まない (0.29.9 と同じ)
]


class SkippedValueTests(_Case):
    def test_command_after_a_skipped_value_is_a_candidate(self):
        for form in SKIPPED_VALUE_FORMS:
            command = form.replace("{a}", AWS)
            with self.subTest(command=command):
                self.assertIn(AWS, self.cands(command))

    def test_forms_that_do_not_run_the_cli(self):
        for form in NOT_RUN_FORMS:
            command = form.replace("{a}", AWS)
            with self.subTest(command=command):
                cands = self.cands(command)
                self.assertFalse([c for c in cands if c.startswith(("aws", "gh"))], cands)

    def test_escaped_space_keeps_the_word_in_the_value(self):
        """`X=$(z)\\ aws s3 rm` の値は `$(z) aws` で、走るのは `s3` (bash / zsh とも aws は走らない)。"""
        self.assertEqual(self.cands("X=$(z)\\ aws s3 rm"), ["s3 rm"])

    def test_path_after_the_substitution_is_part_of_the_value(self):
        """`X=$(z)/gh pr create` の gh は値の一部 (bash 3.2 / zsh -f とも走らない。走るのは `pr`)。

        0.29.9 は代入の語を basename に揃えて gh と読み、誤 deny にしていた (deny → allow に変わる形)。
        """
        cases = {
            "X=$(z)/gh pr create": ["pr create"],
            "X=`z`/gh pr create": ["pr create"],
            "X=$(z)/usr/bin/firebase deploy": ["deploy"],
            "Y=1 X=$(z)/gh pr create": ["pr create"],
            "sudo X=$(z)/gh pr create": ["pr create"],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.cands(command), expected)

    def test_values_with_substitution_are_not_collected(self):
        """値は読まない: 検証の env に入れない (hook の env で照合する)。静的な値は従来どおり集める。"""
        cases = {
            "AWS_PROFILE=$(echo prod) " + AWS: {},
            "AWS_PROFILE=`echo prod` " + AWS: {},
            "X=`z` " + AWS: {},
            "X=a`z`b " + AWS: {},
            "AWS_PROFILE=$((1 + 2)) " + AWS: {},
            "AWS_PROFILE=${P:-a b} " + AWS: {},
            "KUBECONFIG=~/$(z) " + AWS: {},
            "X=$(z) Y=1 " + AWS: {"Y": "1"},
            "Y=1 X=`z` " + AWS: {"Y": "1"},
            "X+=`z` Y=1 " + AWS: {"Y": "1"},
        }
        for command, env in cases.items():
            with self.subTest(command=command):
                # 空白を含む `${...}` は 0.29.9 の読みの候補 (`b} aws ...`) も足すので、aws の候補だけを見る
                got = [(c, e) for c, e in self.call(extract_candidates, command) if c == AWS]
                self.assertEqual(got, [(AWS, env)])

    def test_assignment_without_a_command_is_kept(self):
        """後ろにコマンドが無い・値が閉じない代入は剥がさない (0.29.9 と同じ)。"""
        for command in (
            "X=$(z)",
            "X=`z`",
            "X=$(z " + AWS,  # 閉じない `$(` (どちらのシェルでも構文エラー)
            "X=`z " + AWS,
            'X="$(z ' + AWS,
        ):
            with self.subTest(command=command):
                self.assertEqual(self.cands(command), [command])

    def test_strip_leading_env_skips_the_value(self):
        self.assertEqual(self.call(strip_leading_env, "FOO=$(date) gh pr list"), "gh pr list")
        self.assertEqual(self.call(strip_leading_env, "FOO=`date` gh pr list"), "gh pr list")
        self.assertEqual(self.call(strip_leading_env, "FOO=$(date)"), "FOO=$(date)")


class LegacyValueReadingTests(_Case):
    """0.29.9 の読みがコマンドの候補を作っていた形は、その候補も残す。"""

    def test_inner_path_cli_is_kept_with_the_outer_command(self):
        cases = {
            # bash / zsh とも firebase と aws の両方が走る
            "X=$(/usr/bin/firebase deploy) aws s3 ls": ["aws s3 ls", "firebase deploy) aws s3 ls"],
            "X=`/usr/bin/firebase deploy` aws s3 ls": ["aws s3 ls", "firebase deploy` aws s3 ls"],
            "X=$(./bin/gh auth token) gh pr create": ["gh pr create", "gh auth token) gh pr create"],
            "Y=1 X=$(/usr/bin/gh pr create) " + AWS: [AWS, "gh pr create) " + AWS],
            "X=a$(/usr/bin/gh pr create) " + AWS: [AWS, "gh pr create) " + AWS],
            "sudo X=$(/usr/bin/gh pr create) " + AWS: [AWS, "gh pr create) " + AWS],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(sorted(self.cands(command)), sorted(expected))

    def test_inner_path_cli_in_an_alternative_reading(self):
        """別の読み (ANSI-C) の分解で見つけた形も、その読みを 0.29.9 の読みで分解し直す。"""
        cands = self.cands("echo $'\\'' ; X=$(/usr/bin/firebase deploy) aws s3 ls")
        self.assertIn("firebase deploy) aws s3 ls", cands)
        self.assertIn("aws s3 ls", cands)

    def test_legacy_reading_is_the_previous_decomposition(self):
        """足す読みは 0.29.9 の分解そのもの (空白を含む `${...}` も 0.29.9 と同じく途中で値を終える)。"""
        readings = self.readings("X=${Y:-a b} Z=$(/x/firebase deploy) aws s3 ls")
        self.assertEqual(len(readings), 2)
        self.assertEqual([c for c, _e, _s in readings[0]], ["aws s3 ls"])
        self.assertEqual([c for c, _e, _s in readings[1]], ["b} Z=$(/x/firebase deploy) aws s3 ls"])

    def test_no_extra_reading_without_the_path_form(self):
        """0.29.9 の basename が置換の中のコマンド名にならない代入では、0.29.9 の読みを足さない (読みは 1 つ)。

        語にパスが無い・引用符がある・`/` が置換の外にある・置換が語の中で閉じる・basename に
        コマンド名でない文字がある形。
        """
        for command in (
            "X=$(z) " + AWS,
            "X=`z` " + AWS,
            "X=$(cat ./f) " + AWS,
            'X="a"$(/usr/bin/gh pr create) ' + AWS,
            "X='a'$(/usr/bin/gh pr create) " + AWS,
            "X=${Y:-/ab} gh pr create",
            "X=/usr/bin/x " + AWS,
            "KUBECONFIG=$(pwd)/k.yaml kubectl apply -f x.yaml",
            "KUBECONFIG=~/$(z) kubectl apply -f x.yaml",
            "X=`z`/usr/bin/gh " + AWS,
            "X=$((1))/usr/bin/gh " + AWS,
            "TOKEN=$(./get-token.sh) gh pr create",
            "X=$(/usr/bin/gh) " + AWS,
            "X=$(/x/$(z)) " + AWS,
        ):
            with self.subTest(command=command):
                self.assertEqual(len(self.readings(command)), 1)

    def test_extra_reading_for_the_path_form(self):
        """置換の中のパス付きコマンドの形と、空白を含む `${...}` では 0.29.9 の読みを足す (読みは 2 つ)。"""
        for command in (
            "X=$(/usr/bin/gh pr create) " + AWS,
            "X=`/usr/bin/gh pr create` " + AWS,
            "X=$(a)$(/usr/bin/gh pr create) " + AWS,
            "X=$(a$(/usr/bin/gh pr create)) " + AWS,
            "X=${Y:-$(/usr/bin/gh pr create)} " + AWS,
            "X=$(./bin/firebase-tools@13 deploy) " + AWS,
            "X=${Y:-a b} " + AWS,
            "X=${Y:-a\tb} " + AWS,
        ):
            with self.subTest(command=command):
                self.assertEqual(len(self.readings(command)), 2)

    def test_words_inside_a_spaced_parameter_expansion_are_kept(self):
        """空白を含む `${...}` の中の語は、0.29.9 と同じく候補に残す。

        wrapper の後ろの代入の形の引数は、bash が展開の結果を語に分ける (`env X=${Y:- aws s3 rm x} gh pr create`
        は bash で `aws s3 rm x gh pr create` が走る。zsh は分けず gh が走る)。
        """
        for prefix in ("", "env ", "sudo "):
            command = prefix + "X=${Y:- aws s3 rm x} gh pr create"
            with self.subTest(command=command):
                cands = self.cands(command)
                self.assertIn("aws s3 rm x} gh pr create", cands)
                self.assertIn("gh pr create", cands)

    def test_extra_reading_is_added_only_for_the_reading_that_found_it(self):
        """既定の読みだけが見つけた形では、別の読みを 0.29.9 の読みで分解し直さない。

        既定の読みは `$'\\''` の 3 つ目の `'` から次の `'` までを引用符の中と読み、後ろの X の代入を
        見つける。ANSI-C の読みは Y の代入 (パスの無い語) を飛ばし、X の代入は引用符の中にある。
        読みは 既定 / ANSI-C / 既定を 0.29.9 で読んだもの の 3 つ。
        """
        command = "echo $'\\'' ; Y=$(z) true '; X=$(/usr/bin/gh pr create) aws s3 ls"
        self.assertEqual(len(self.readings(command)), 3)

    def test_reading_switches_are_restored(self):
        self.call(extract_candidates, "X=$(/usr/bin/firebase deploy) aws s3 ls ; echo $'\\'' ; " + AWS)
        self.assertFalse(command_parser._LEGACY_VALUE_READING)
        self.assertFalse(command_parser._LEGACY_VALUE_SEEN)
        self.assertFalse(command_parser._ANSI_C_READING)
        # 既定の読みに戻っている: 値を飛ばす
        self.assertEqual(self.call(strip_leading_env, "X=$(/usr/bin/gh x) " + AWS), AWS)


class WorkdirAfterSkippedValueTests(_Case):
    def test_cd_after_a_skipped_value_is_tracked(self):
        """`X=$(z) cd sub && ...` の cd は走る (bash / zsh とも)。0.29.9 は cd と読まなかった。"""
        dirs = self.call(workdir.segment_dirs, "X=$(z) cd sub && " + AWS, "/p")
        self.assertEqual(dirs.get(AWS), {"/p/sub"})

    def test_env_change_after_a_skipped_value_is_tracked(self):
        changes = self.call(workdir.segment_env_changes, "X=$(z) export AWS_PROFILE=q; " + AWS)
        self.assertIn("AWS_PROFILE", changes.get(AWS, set()))

    def test_assignment_only_segment_keeps_the_skipped_names(self):
        """値を飛ばした代入の後ろが代入だけのセグメントは、先頭の代入から後ろを候補にする (v0.36.8。0.36.7 までは
        最初に値を飛ばした代入から後ろ)。

        `AWS_PROFILE=$(z) X=1; aws ...` の AWS_PROFILE は、export 済みなら後ろの aws に効く (bash 3.2 /
        zsh -f で実測)。`X=1` だけを候補にすると、変えた env の名前から AWS_PROFILE が消え、hook の env の
        AWS_PROFILE をディレクトリ単位の固定として通してしまう。
        """
        for value in ("$(z)", "`z`", "$((1))", "$(<p)", "a$(z)b", "$$(z)"):
            for name, cli in (("AWS_PROFILE", AWS), ("CLOUDSDK_CORE_ACCOUNT", "gcloud run deploy")):
                for rest in ("X=1", "X=$(y)", "X=1 Y=`y`"):
                    command = f"{name}={value} {rest}; {cli}"
                    with self.subTest(command=command):
                        changes = self.call(workdir.segment_env_changes, command)
                        self.assertIn(cli, changes)
                        self.assertLessEqual({name, "X"}, changes[cli])

    def test_assignment_only_candidate_starts_at_the_first_assignment(self):
        """候補は先頭の代入から後ろで、env は空 (v0.36.8。剥がした代入が無い)。

        0.36.7 までは最初に値を飛ばした代入から後ろを候補にし、それより前の静的な代入 (Y=1) を env に入れていた
        (0.29.9 と同じ)。前の代入の名前が候補の文字列から消え、`_env_names_changed` が数えなかった。
        """
        cases = {
            "AWS_PROFILE=$(z) X=1": [("AWS_PROFILE=$(z) X=1", {})],
            "Y=1 AWS_PROFILE=$(z) X=1": [("Y=1 AWS_PROFILE=$(z) X=1", {})],
            "Y=1 AWS_PROFILE=$(z) X=$(y) W=2": [("Y=1 AWS_PROFILE=$(z) X=$(y) W=2", {})],
            "Y=1 AWS_PROFILE=$(z) W=2 X=1": [("Y=1 AWS_PROFILE=$(z) W=2 X=1", {})],
            # 値を読める代入だけの行も同じ (0.36.7 までは最後の代入 (X=1) だけが候補で、前の代入は env に入れていた)
            "AWS_PROFILE=prod X=1": [("AWS_PROFILE=prod X=1", {})],
            "AWS_PROFILE=prod": [("AWS_PROFILE=prod", {})],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.call(extract_candidates, command), expected)

    def test_legacy_reading_does_not_unsure_the_cd(self):
        """0.29.9 の読みを足しても、`cd` を確実とみなす判定 (`sure_chdirs`) は変わらない。

        足すのはシェルの読み方ではなく 0.29.9 の候補を落とさないための読みなので、読みの数に数えない。
        """
        for command in (
            "cd sub && X=$(/bin/date +%s) firebase deploy",
            "cd sub && X=`/bin/date +%s` firebase deploy",
            "cd sub && X=${Y:-a b} firebase deploy",
        ):
            with self.subTest(command=command):
                self.assertEqual(len(self.readings(command)), 2)
                self.assertEqual(len(self.call(extract_candidate_readings, command, False)), 1)
                self.assertEqual(self.call(workdir.sure_chdirs, command), {"cd sub"})


class GhIdentityEnvTests(_Case):
    """gh の token / host の env に 0.29.9 が読めなかった値を代入したセグメントの gh は、0.29.9 と同じく読む。

    gh が書き込むアカウントは代入したトークンで決まり、hook からは決められない (決められない形は deny
    しない)。起動時の env のアカウントで照合すると、一致しないときに止め、自動切替まで走らせてしまう。
    """

    VALUES = ("$(gh auth token --user x)", "`gh auth token`", "$((1))", "${T:-a b}", "a$(z)")
    FORMS = (
        "{n}={v} gh pr create",
        "sudo -E {n}={v} gh pr create",
        "{n}={v} sudo -E gh pr create",
        "env {n}={v} gh pr create",
        "X=$(z) {n}={v} gh pr create",
        "{n}={v} X=$(z) gh pr create",
        "{n}+={v} gh pr create",
        "{n}={v} watch 'true && gh pr create'",
        "(cd sub && {n}={v} gh pr create)",
    )

    def test_names_match_the_github_service(self):
        from services import github

        self.assertEqual(command_parser._GH_IDENTITY_ENV, {*github._TOKEN_ENV_VARS, github._HOST_ENV_VAR})

    def test_gh_after_an_unreadable_value_is_read_as_before(self):
        for name in sorted(command_parser._GH_IDENTITY_ENV):
            for value in self.VALUES:
                for form in self.FORMS:
                    command = form.format(n=name, v=value)
                    with self.subTest(command=command):
                        cands = self.cands(command)
                        self.assertFalse([c for c in cands if c.startswith("gh pr")], cands)
                        with command_parser._legacy_value_reading(None):
                            before = self.call(extract_candidates, command)
                        self.assertEqual(self.call(extract_candidates, command), before)

    def test_variable_value_with_another_skipped_value(self):
        """値が変数参照の形でも、同じセグメントに値を飛ばした代入があれば 0.29.9 は gh を候補にしていなかった。

        `GH_TOKEN=$T X=$(z) gh pr create` は 0.29.9 が X で読むのをやめていた (照合も自動切替もしない)。
        """
        for command in (
            "GH_TOKEN=$T X=$(z) gh pr create",
            'X=`z` GH_TOKEN="$(t)" gh pr create',
            "GH_HOST=${H} X=$((1)) sudo -E gh pr create",
            "GITHUB_TOKEN+=abc X=$(z) gh pr create",
        ):
            with self.subTest(command=command):
                cands = self.cands(command)
                self.assertFalse([c for c in cands if c.startswith("gh pr")], cands)
                with command_parser._legacy_value_reading(None):
                    before = self.call(extract_candidates, command)
                self.assertEqual(self.call(extract_candidates, command), before)

    def test_other_commands_after_the_token_are_checked(self):
        """gh の無いセグメントは、ほかの代入と同じく値を飛ばして後ろを照合する。"""
        cases = {
            "GH_TOKEN=$(z) " + AWS: AWS,
            "GITHUB_TOKEN=`gh auth token` firebase deploy": "firebase deploy",
            "GH_HOST=$(z) kubectl apply -f x.yaml": "kubectl apply -f x.yaml",
            "GH_TOKEN=$(z) cd sub && gh pr create": "gh pr create",
            # 印はセグメントごと (前のセグメントの印で、後ろのセグメントの gh を 0.29.9 の読みにしない)
            "GH_TOKEN=$(z) aws s3 ls; X=$(z) gh pr create": "gh pr create",
            "GH_TOKEN=$T X=$(z) " + AWS: AWS,
        }
        for command, cli in cases.items():
            with self.subTest(command=command):
                self.assertIn(cli, self.cands(command))
        dirs = self.call(workdir.segment_dirs, "GH_TOKEN=$(z) cd sub && gh pr create", "/p")
        self.assertEqual(dirs.get("gh pr create"), {"/p/sub"})

    def test_readable_values_are_unchanged(self):
        """0.29.9 も gh を候補にしていた値 (`$VAR` / 静的な値 / 二重引用符の中の置換) は変えない。"""
        cases = {
            "GH_TOKEN=$T gh pr create": [("gh pr create", {})],
            "GH_TOKEN=${T} gh pr create": [("gh pr create", {})],
            'GH_TOKEN="$(z)" gh pr create': [("gh pr create", {})],
            "GH_TOKEN=abc gh pr create": [("gh pr create", {"GH_TOKEN": "abc"})],
            # 静的な値は検証の env に入り、gh はそのトークンで照合する (自動切替はトークンの条件で止まる)。
            # ほかの代入の値を飛ばしても同じ
            "GH_TOKEN=abc X=$(z) gh pr create": [("gh pr create", {"GH_TOKEN": "abc"})],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.call(extract_candidates, command), expected)


class DispatchTests(BaseWithTmpProject):
    def setUp(self):
        super().setUp()
        self._write_accounts({"aws": "123456789012", "github": "example-user"})

    def _decide(self, command, target, result):
        with self.isolated_cache(), mock.patch(target, return_value=result) as v:
            try:
                out = dispatch(command, str(self.project_dir))
            except Exception as e:  # noqa: BLE001
                self.fail(f"例外: {e!r}")
        decision = "allow" if out is None else out["hookSpecificOutput"].get("permissionDecision", "allow")
        return decision, v

    def test_mismatch_is_denied_and_match_is_allowed(self):
        cases = [
            ("X=$(z) aws s3 rm s3://b/k", "services.aws.verify"),
            ("AWS_PROFILE=$(echo prod) aws s3 rm s3://b/k", "services.aws.verify"),
            ("X=$((1+2)) gh pr create", "services.github.verify"),
            ("X=${Y:-a b} gh pr create", "services.github.verify"),
            ("X=`z` gh pr create", "services.github.verify"),
        ]
        for command, target in cases:
            with self.subTest(command=command):
                decision, v = self._decide(command, target, "不一致")
                self.assertTrue(v.called, f"検証されていない: {command!r}")
                self.assertEqual(decision, "deny")
                decision, v = self._decide(command, target, None)
                self.assertTrue(v.called)
                self.assertEqual(decision, "allow")

    def test_skipped_value_is_not_passed_to_verify(self):
        _decision, v = self._decide("AWS_PROFILE=$(echo prod) aws s3 rm s3://b/k", "services.aws.verify", None)
        self.assertTrue(v.called)
        self.assertIsNone(v.call_args.kwargs.get("env"))
        _decision, v = self._decide("X=$(z) AWS_PROFILE=p aws s3 rm s3://b/k", "services.aws.verify", None)
        self.assertTrue(v.called)
        self.assertEqual((v.call_args.kwargs.get("env") or {}).get("AWS_PROFILE"), "p")


if __name__ == "__main__":
    unittest.main()
