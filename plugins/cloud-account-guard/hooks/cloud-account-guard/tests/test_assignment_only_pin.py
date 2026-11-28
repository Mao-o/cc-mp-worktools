"""前段の代入だけのセグメントで identity env を変えたコマンドを、ディレクトリ単位の固定とみなさない (v0.36.8)。

期待値が未登録の aws / gcloud / firebase は、hook の env (`AWS_PROFILE` など) で固定されていれば止めない
(v0.19.0)。同じコマンドの前段で identity env を変えた後ろの CLI は固定とみなさないが、0.36.7 までは代入だけの
セグメントを次のように読み、変えた名前を落としていた:

- 候補の文字列 (`command_parser._parse_leading_env`) が最後の代入 (`AWS_PROFILE=prod X=1` の `X=1`) か、最初に値を
  飛ばした代入から後ろ (`AWS_PROFILE=prod TS=$(date +%s)` の `TS=$(date +%s)`) だけだった
- 名前の読み (`workdir._env_names_changed`) が shlex で分けた語が全部 `NAME=` で始まるときだけ数えたので、置換の
  中の空白で語が割れる形 (`AWS_PROFILE=$(echo q)`) と追記 (`AWS_PROFILE+=x` / `HOME+=/x`) を数えなかった。
  `HOME+=/x` は候補の時点で basename の `x` に揃えていた (`_strip_command_path`)
- `unset $V` は `$V` を名前と読み、`export HOME+=/x` は名前を `HOME+` と読んだ (完全一致の `HOME` に当たらない)

実シェル (起動時の env は AWS_PROFILE=dev / CLOUDSDK_CORE_ACCOUNT=a@example.com。cwd に `f` / `.p` (中身 prod)) で、
偽の aws / gcloud が受け取った値 (CLI は起動時の値ではなく代入した値で走る):

| コマンド | bash 3.2 | zsh -f |
|---|---|---|
| `AWS_PROFILE=$(echo q); aws s3 rm s3://b/k` | q | q |
| `AWS_PROFILE=$(cat .p)` + 改行 + `aws s3 rm s3://b/k` | prod | prod |
| `AWS_PROFILE=$(echo prod) X=1; aws s3 rm s3://b/sa` | prod | prod |
| `AWS_PROFILE=prod X=1; aws s3 rm s3://b/k` | prod | prod |
| `AWS_PROFILE=prod REGION=us-east-1; aws s3 rm s3://b/k` | prod | prod |
| `AWS_PROFILE=prod TS=$(date +%s); aws s3 rm s3://b/k` | prod | prod |
| `CLOUDSDK_CORE_ACCOUNT=b@example.com X=1; gcloud run deploy s` | b@example.com | b@example.com |
| `AWS_PROFILE=$(cat f); aws s3 rm s3://b/k` | prod | prod |
| `AWS_PROFILE+=x; aws s3 rm s3://b/k` | devx | devx |
| `AWS_PROFILE=$P X=1; aws s3 rm s3://b/k` | (空) | (空) |
| `AWS_PROFILE=prod<TAB>X=1; aws s3 rm s3://b/k` | prod | prod |
| `AWS_PROFILE=prod X=1>/dev/null; aws s3 rm s3://b/k` | prod | prod |
| `AWS_PROFILE[0]=prod; aws s3 rm s3://b/k` | (未設定。配列になり export されない) | (代入が失敗し aws は走らない) |
| `HOME+=/x; aws s3 rm s3://b/k` | HOME=<起動時>/x | HOME=<起動時>/x |
| `export HOME+=/x; aws s3 rm s3://b/k` | HOME=<起動時>/x | (export が失敗し aws は走らない) |
| `V=AWS_PROFILE; unset $V; aws s3 rm s3://b/k` | (未設定) | (未設定) |
| `unset $(env \\| grep -o '^AWS_PROFILE'); aws s3 rm s3://b/k` | (未設定) | (未設定) |
| `unset {AWS_PROFILE,X}; aws s3 rm s3://b/k` | (未設定) | (未設定) |

置換の中にパス付きのコマンドを書いた形 (`AWS_PROFILE=$(/bin/cat .p)`) は、実シェルの表にパス付きのコマンド語を
流さないので載せていない。代入の扱いは `AWS_PROFILE=$(cat .p)` の行と同じ (置換の出力が値になる)。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import command_parser, workdir  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


AWS = "aws s3 rm s3://b/k"
GCLOUD = "gcloud run deploy s"
ANY = workdir.ANY_ENV

# 固定のある世界 (AWS_PROFILE=dev / CLOUDSDK_CORE_ACCOUNT=a@example.com) で、固定とみなさなくなる形。どれも 0.36.7 は allow
OVERRIDES = [
    # チケットの 4 形
    f"AWS_PROFILE=$(echo q); {AWS}",
    f"AWS_PROFILE=$(cat .p)\n{AWS}",
    "AWS_PROFILE=$(echo prod) X=1; aws s3 rm s3://b/sa",
    f"AWS_PROFILE=prod X=1; {AWS}",
    # 普段書く形に近い例
    f"AWS_PROFILE=prod REGION=us-east-1; {AWS}",
    f"AWS_PROFILE=prod TS=$(date +%s); {AWS}",
    f"CLOUDSDK_CORE_ACCOUNT=b@example.com X=1; {GCLOUD}",
    f"AWS_PROFILE=$(cat f); {AWS}",
    # 追記・要素の代入
    f"AWS_PROFILE+=x; {AWS}",
    f"HOME+=/x; {AWS}",
    f"AWS_PROFILE[0]=prod; {AWS}",
    f"export HOME+=/x; {AWS}",
    # 名前を読めない unset
    f"V=AWS_PROFILE; unset $V; {AWS}",
    f"unset $(env | grep -o '^AWS_PROFILE'); {AWS}",
    f"unset {{AWS_PROFILE,X}}; {AWS}",
    f'unset "$V"; {AWS}',
    # 同じ形の変種 (区切り・値の形・並び)
    f"AWS_PROFILE=$P X=1; {AWS}",
    f"AWS_PROFILE=prod\tX=1; {AWS}",
    f"AWS_PROFILE=prod X=1>/dev/null; {AWS}",
    f"AWS_PROFILE=$(printf '%s' prod) X=1; {AWS}",
    f'AWS_PROFILE="a b" X=1; {AWS}',
    f"X=1 AWS_PROFILE=$(echo q) Y=2 && {AWS}",
    f"Y=$P AWS_PROFILE=$(echo q) X=1; {AWS}",
    f"AWS_PROFILE=`echo q` X=1; {AWS}",
    f"CLOUDSDK_CORE_ACCOUNT=$(echo b) X=1 && {GCLOUD}",
    f"X=1 CLOUDSDK_CORE_ACCOUNT+=b\n{GCLOUD}",
    f"{{ AWS_PROFILE=prod X=1; {AWS}; }}",
    # 先頭の代入の値の置換の中のパス (0.36.7 は basename に揃えた `cat .p` などを候補にして、名前を読めなかった)
    f"AWS_PROFILE=$(/bin/cat .p); {AWS}",
    f"X=$(/usr/bin/true) AWS_PROFILE=prod; {AWS}",
    f"X=`/usr/bin/true` AWS_PROFILE=prod; {AWS}",
    f"X=a/$(x)/b AWS_PROFILE=prod; {AWS}",
    f"X+=(`x/aws s3 ls`) AWS_PROFILE=$(echo q); {AWS}",
]
# 固定のまま通す形 (0.36.7 と同じ)。identity env でない代入だけのセグメントと、後ろの CLI に効かない代入
KEPT = [
    f"X=1 Y=2; {AWS}",
    f"X=$(echo a b) Y=1; {AWS}",
    f"X=1; {AWS}",
    f"X+=1 Y=2; {AWS}",
    f"unset X; {AWS}",
    f"unset -v X Y; {AWS}",
    f"export FOO+=x; {AWS}",
    f"declare arr[1]=x; {AWS}",
    f'X="$(printf "%s" "a b")" Y=1; {AWS}',
    f"X=(1 2) Y=1; {AWS}",
    f"{AWS}; AWS_PROFILE=prod X=1",
    f"X=1 Y=2; {GCLOUD}",
]


class _Case(unittest.TestCase):
    def call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")


class EnvNamesTests(_Case):
    """`workdir._env_names_changed` の表 (セグメント → 変えた env の名前)。"""

    def test_assignment_only_names(self):
        for segment, want in (
            ("AWS_PROFILE=$(echo q)", {"AWS_PROFILE"}),
            ("AWS_PROFILE=$(cat .p)", {"AWS_PROFILE"}),
            ("AWS_PROFILE=$(echo prod) X=1", {"AWS_PROFILE", "X"}),
            ("AWS_PROFILE=prod X=1", {"AWS_PROFILE", "X"}),
            ("AWS_PROFILE=prod TS=$(date +%s)", {"AWS_PROFILE", "TS"}),
            ("AWS_PROFILE=prod\tX=1", {"AWS_PROFILE", "X"}),
            ("AWS_PROFILE+=x", {"AWS_PROFILE"}),
            ("HOME+=/x", {"HOME"}),
            ("AWS_PROFILE[0]=prod", {"AWS_PROFILE"}),
            ("HOME[0]+=/x", {"HOME"}),
            ('AWS_PROFILE="$(printf "%s" "a b")"', {"AWS_PROFILE"}),
            ('AWS_PROFILE="a b" X=$(echo c d)', {"AWS_PROFILE", "X"}),
            ("X=(1 2) AWS_PROFILE=prod", {"X", "AWS_PROFILE"}),
            ("AWS_PROFILE=prod X=(1 2)", {"AWS_PROFILE", "X"}),
            ("AWS_PROFILE=${Y:- a b} X=1", {"AWS_PROFILE", "X"}),
            # 空白なしで続けたリダイレクトは値の続き、空白の後ろのリダイレクトの語は飛ばす
            ("AWS_PROFILE=prod X=1>/dev/null", {"AWS_PROFILE", "X"}),
            ("AWS_PROFILE=prod 2>/dev/null", {"AWS_PROFILE"}),
            ("AWS_PROFILE=prod > /dev/null X=1", {"AWS_PROFILE", "X"}),
            ("2>/dev/null AWS_PROFILE=prod", {"AWS_PROFILE"}),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(self.call(workdir._env_names_changed, segment), want)

    def test_segments_with_a_command_word_do_not_assign(self):
        """代入の後ろにコマンドの語がある形 (0.29.9 の読みの候補) は、代入をそのコマンドにだけ効かせる。"""
        for segment in (
            "AWS_PROFILE=$(z) aws s3 ls",
            "AWS_PROFILE=prod X=1 true",
            "AWS_PROFILE=prod $(cmd)",
            "true AWS_PROFILE=prod",
        ):
            with self.subTest(segment=segment):
                self.assertEqual(self.call(workdir._env_names_changed, segment), set())

    def test_the_0_36_7_reading_is_kept(self):
        """引用符・`\\` で始まる語 (シェルはコマンド名と読む) も、0.36.7 までの shlex の読みどおり名前に数える
        (多めに数える側。0.36.7 の読みとの和を取るので、0.36.7 が数えた名前は減らない)。"""
        for segment, want in (
            ('"AWS_PROFILE"=prod X=1', {"AWS_PROFILE", "X"}),
            ("AWS_PROFILE=prod \\X=1", {"AWS_PROFILE", "X"}),
            ("X=1 Y=$(echo AWS_PROFILE=prod)", {"X", "Y", "AWS_PROFILE"}),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(self.call(workdir._env_names_changed, segment), want)

    def test_unreadable_unset_names_are_any(self):
        for segment in (
            "unset $V",
            'unset "$V"',
            "unset $(env | grep -o '^AWS_PROFILE')",
            "unset `echo AWS_PROFILE`",
            "unset AWS_$N",
            "unset {AWS_PROFILE,X}",
            "unset AWS_PROFIL?",
            "unset -v $V",
            "unset X $V",
        ):
            with self.subTest(segment=segment):
                self.assertEqual(self.call(workdir._env_names_changed, segment), {ANY})

    def test_readable_unset_names_keep_the_0_36_7_reading(self):
        for segment, want in (
            ("unset AWS_PROFILE", {"AWS_PROFILE"}),
            ('unset "AWS_PROFILE"', {"AWS_PROFILE"}),
            ("unset -v AWS_PROFILE X", {"AWS_PROFILE", "X"}),
            ("unset HOME[0]", {"HOME[0]"}),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(self.call(workdir._env_names_changed, segment), want)

    def test_append_and_element_names_of_the_assign_builtins(self):
        """`export HOME+=/x` は 0.36.7 の名前 (`HOME+`) に本体 (`HOME`) を足す。名前の形でない語には足さない。"""
        for segment, want in (
            ("export HOME+=/x", {"HOME+", "HOME"}),
            ("export HOME[0]=/x", {"HOME[0]", "HOME"}),
            ("declare -x XDG_CONFIG_HOME+=/x", {"XDG_CONFIG_HOME+", "XDG_CONFIG_HOME"}),
            ("declare arr[1]+=x", {"arr[1]+", "arr"}),
            ("export AWS_PROFILE=$(cat f)", {"AWS_PROFILE", "f)"}),
            ("export HOME=/x", {"HOME"}),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(self.call(workdir._env_names_changed, segment), want)

    def test_long_segments_are_read_in_linear_time(self):
        """代入を 4,000 並べた形も 2 秒に届かない (手元で数 ms〜十数 ms)。"""
        for unit in ("A=$(b c) ", 'A="$(printf "%s %s" "$x")" ', "a=( ", "a=(1 2) ", "A+=x ", "A=1 > f "):
            segment = unit * 4000
            with self.subTest(unit=unit):
                started = time.perf_counter()
                self.call(workdir._env_names_changed, segment)
                self.assertLess(time.perf_counter() - started, 2.0)


class CandidateTests(_Case):
    """代入だけの行の候補の文字列 (先頭の代入から後ろ) と、置換の中のパス付き CLI の候補。"""

    def test_assignment_only_line_is_one_candidate(self):
        for command, want in (
            ("AWS_PROFILE=prod X=1", [("AWS_PROFILE=prod X=1", {})]),
            ("AWS_PROFILE=prod TS=$(date +%s)", [("AWS_PROFILE=prod TS=$(date +%s)", {})]),
            ("Y=$P AWS_PROFILE=$(echo q) X=1", [("Y=$P AWS_PROFILE=$(echo q) X=1", {})]),
            ("HOME+=/x", [("HOME+=/x", {})]),
            ("X=1 HOME+=/x/aws", [("X=1 HOME+=/x/aws", {})]),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.call(command_parser.extract_candidates, command), want)

    def test_no_unread_marks_on_assignment_only_candidates(self):
        """剥がした代入が無いので、値を読めない代入の印 (`unread_env_candidates`) は付かない。"""
        for command in (
            f"Y=$P AWS_PROFILE=$(echo q) X=1; {AWS}",
            f"AWS_PROFILE=$P X=1; {AWS}",
            f"env Y=$P AWS_PROFILE=$(echo q) X=1; {AWS}",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.call(command_parser.unread_env_candidates, command), {})

    def test_the_inner_cli_with_a_path_stays_a_candidate(self):
        """0.36.7 までの候補 (最初に値を飛ばした代入から後ろ) が置換の中のパス付き CLI を basename で拾っていた形は、
        0.36.7 までの読みで候補に残す (先頭の代入から後ろを候補にしても、firebase の照合を落とさない)。

        空白を含む `${...}` を前に置いた行も残す (0.29.9 の読みはそこで値を終えるので、代わりにならない)。
        """
        for command, inner, env in (
            # 末尾の対にならない `)` は `_strip_trailing_syntax` が落とす
            ("X=1 Y=$(/usr/bin/firebase deploy)", "firebase deploy", {"X": "1"}),
            ("X=1 Y=$(/usr/bin/firebase deploy) Z=2", "firebase deploy) Z=2", {"X": "1"}),
            ("AWS_PROFILE=prod Y=`./bin/gh pr create`", "gh pr create`", {"AWS_PROFILE": "prod"}),
            ("X=1 Y=a/$(x)/firebase", "firebase", {"X": "1"}),
            ("A=${B:- x y} C=$(/usr/bin/aws s3 rm s3://b/k)", "aws s3 rm s3://b/k", {}),
        ):
            with self.subTest(command=command):
                cands = self.call(command_parser.extract_candidates, command)
                self.assertIn((command, {}), cands)
                self.assertIn((inner, env), cands)

    def test_the_extra_reading_is_added_only_when_the_old_candidate_had_a_path(self):
        """0.36.7 までの読みを足すのは、0.36.7 までの候補の先頭の語を basename に揃えていた形だけ。

        basename に揃えない形 (`TS=$(date +%s)`) では足さない (足すと、ほかのセグメントまで 0.36.7 の候補で
        読み直した読みが増える)。値を飛ばした代入が先頭の代入のとき (`Y=a/$(x)/gh`) も足す (既定の読みは代入のまま
        返して basename に揃えない)。
        """
        for command, want in (
            ("Y=a/$(x)/gh; X=$(z) aws s3 ls", 2),
            ("Y=1; X=$(z) aws s3 ls", 1),
            ("AWS_PROFILE=prod TS=$(date +%s); X=$(z) aws s3 ls", 1),
            ("AWS_PROFILE=prod Y=a/$(x)/gh; X=$(z) aws s3 ls", 2),
        ):
            with self.subTest(command=command):
                self.assertEqual(len(self.call(command_parser.extract_candidate_readings, command)), want)
                # 読みの数に数えない (シェルの読み方ではない)
                self.assertEqual(len(self.call(command_parser.extract_candidate_readings, command, False)), 1)

    def test_the_extra_reading_follows_the_reading_that_found_it(self):
        """別の読み (ANSI-C) だけが代入だけの行と読む形も、その読みを 0.36.7 までの読みで分解し直す。

        既定の読みは `$'\\''` の 3 つ目の `'` から後ろを引用符の中と読み、`A=1 Y=...` を候補にしない。
        2 つ目は空白を含む `${...}` を前に置いた形 (0.29.9 の読みはそこで値を終えるので、`gh pr create` を
        拾うのは 0.36.7 までの読みだけ)。
        """
        for command, assignments, inner in (
            ("echo $'\\'' ; A=1 Y=$(/usr/bin/gh pr create)", "A=1 Y=$(/usr/bin/gh pr create)",
             ("gh pr create", {"A": "1"})),
            ("echo $'\\'' ; A=${B:- x y} C=$(/usr/bin/gh pr create)", "A=${B:- x y} C=$(/usr/bin/gh pr create)",
             ("gh pr create", {})),
        ):
            with self.subTest(command=command):
                cands = self.call(command_parser.extract_candidates, command)
                self.assertIn((assignments, {}), cands)
                self.assertIn(inner, cands)

    def test_unread_marks_of_the_extra_reading_are_the_0_36_7_ones(self):
        """0.36.7 までの読みの候補の印は、0.36.7 と同じく最初に値を飛ばした代入より前の代入だけ
        (後ろの `Z=$Q` は候補の中の代入で、剥がしていない)。"""
        for command, want in (
            ("X=$P Y=$(/usr/bin/aws s3 rm s3://b/k)", {"aws s3 rm s3://b/k": frozenset({"X"})}),
            ("X=$P Y=$(/usr/bin/aws s3 rm s3://b/k) Z=$Q W=1",
             {"aws s3 rm s3://b/k) Z=$Q W=1": frozenset({"X"})}),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.call(command_parser.unread_env_candidates, command), want)

    def test_assignment_only_candidates_are_not_basenamed(self):
        """既定の読みは代入だけの行を basename に揃えない (先頭の代入の置換の中のパスで名前を落とさない)。

        basename に揃えた 0.36.7 の候補は、0.36.7 までの読みで足す (最後の代入の配列の中の置換 `Y=(`...`)` は、
        閉じの後ろに空白が無いので配列として剥がさず、値を飛ばした代入として読む)。次のセグメントのパス付き
        コマンドは従来どおり揃える。
        """
        for command, want in (
            ("AWS_PROFILE=$(/bin/cat .p)", [("AWS_PROFILE=$(/bin/cat .p)", {}), ("cat .p", {})]),
            ("X=(`/usr/bin/firebase deploy`)", [("X=(`/usr/bin/firebase deploy`)", {}), ("firebase deploy`)", {})]),
            ("X=1 Y=(`/usr/bin/firebase deploy`)",
             [("X=1 Y=(`/usr/bin/firebase deploy`)", {}), ("firebase deploy`)", {"X": "1"})]),
            ("X=1; /usr/bin/aws s3 ls", [("X=1", {}), ("aws s3 ls", {})]),
            ("X=$(/bin/cat .p); /usr/bin/aws s3 ls", [("X=$(/bin/cat .p)", {}), ("aws s3 ls", {}), ("cat .p", {})]),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.call(command_parser.extract_candidates, command), want)

    def test_reading_switches_are_restored(self):
        self.call(command_parser.extract_candidates, "AWS_PROFILE=prod Y=a/$(x)/gh; " + AWS)
        self.assertFalse(command_parser._ASSIGNMENT_TAIL_READING)
        self.assertFalse(command_parser._ASSIGNMENT_TAIL_SEEN)

    def test_env_changes_reach_the_following_cli(self):
        for command, cli, name in (
            (f"AWS_PROFILE=prod X=1; {AWS}", AWS, "AWS_PROFILE"),
            (f"AWS_PROFILE=prod TS=$(date +%s); {AWS}", AWS, "AWS_PROFILE"),
            (f"X=1 Y=$(/usr/bin/true) AWS_PROFILE=prod; {AWS}", AWS, "AWS_PROFILE"),
            (f"HOME+=/x; {AWS}", AWS, "HOME"),
            (f"X=1 CLOUDSDK_CORE_ACCOUNT+=b\n{GCLOUD}", GCLOUD, "CLOUDSDK_CORE_ACCOUNT"),
            (f"AWS_PROFILE=$(/bin/cat .p); {AWS}", AWS, "AWS_PROFILE"),
            (f"X+=(`x/aws s3 ls`) AWS_PROFILE=$(echo q); {AWS}", AWS, "AWS_PROFILE"),
        ):
            with self.subTest(command=command):
                changes = self.call(workdir.segment_env_changes, command)
                self.assertIn(cli, changes)
                self.assertIn(name, changes[cli])


class _Base(_Case):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project_dir = self.tmp / "repo"
        (self.project_dir / ".git").mkdir(parents=True)
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.project_dir),
                "TMPDIR": str(cache_tmp),
                "AWS_PROFILE": "dev",
                "CLOUDSDK_CORE_ACCOUNT": "a@example.com",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_accounts(self, data: dict) -> None:
        d = self.project_dir / ".claude" / "cloud-account-guard"
        d.mkdir(parents=True, exist_ok=True)
        (d / "accounts.local.json").write_text(json.dumps(data), encoding="utf-8")

    def _decision(self, command):
        try:
            with mock.patch("subprocess.run", side_effect=AssertionError("CLI は起動しない")):
                result = dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        if result is None:
            return "allow", ""
        out = result["hookSpecificOutput"]
        if "permissionDecision" not in out:
            return "warn", out.get("additionalContext", "")
        return out["permissionDecision"], out.get("permissionDecisionReason", "")


class PinnedTests(_Base):
    def test_overrides_are_not_pinned(self):
        self._write_accounts({"github": "me"})
        for command in OVERRIDES:
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)
                self.assertIn("キーがありません", reason)

    def test_overrides_are_not_pinned_without_any_accounts_file(self):
        for command in OVERRIDES:
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)
                self.assertIn("未設定です", reason)

    def test_other_forms_stay_pinned(self):
        path = self.project_dir / ".claude" / "cloud-account-guard" / "accounts.local.json"
        for accounts in ({"github": "me"}, None):
            if accounts is None:
                path.unlink(missing_ok=True)
            else:
                self._write_accounts(accounts)
            for command in KEPT:
                with self.subTest(command=command, accounts=accounts):
                    self.assertEqual(self._decision(command), ("allow", ""))

    def test_query_tier_only_warns(self):
        self._write_accounts({"github": "me"})
        decision, reason = self._decision("AWS_PROFILE=prod X=1; aws s3 ls s3://b")
        self.assertEqual(decision, "warn", reason)
        self.assertIn("キーがありません", reason)

    def test_registered_value_is_still_checked_with_the_hook_env(self):
        """期待値を登録した service の照合は変えない (前段の代入は照合の env に重ねない)。"""
        self._write_accounts({"aws": "123456789012"})
        seen = []

        def fake(expected, project_dir, env=None, context=None):
            seen.append((os.environ if env is None else env).get("AWS_PROFILE"))
            return None

        with mock.patch("services.aws.verify", side_effect=fake):
            self.assertEqual(self._decision(f"AWS_PROFILE=prod X=1; {AWS}"), ("allow", ""))
        self.assertEqual(seen, ["dev"])


class FirebaseTests(_Base):
    """firebase の固定 (`firebase use` の記録) は configstore の場所 (`XDG_CONFIG_HOME`) で決まる。"""

    def setUp(self):
        super().setUp()
        self._write_accounts({"github": "me"})
        store = Path(os.environ["XDG_CONFIG_HOME"]) / "configstore"
        store.mkdir(parents=True, exist_ok=True)
        (store / "firebase-tools.json").write_text(
            json.dumps({"activeProjects": {str(self.project_dir.resolve()): "my-proj"}}),
            encoding="utf-8",
        )

    def test_pinned_by_the_record(self):
        self.assertEqual(self._decision("X=1 Y=2; firebase deploy"), ("allow", ""))

    def test_assignment_only_rewrite_is_not_pinned(self):
        for command in (
            "XDG_CONFIG_HOME+=/x; firebase deploy",
            "XDG_CONFIG_HOME=/x X=1; firebase deploy",
            "FIREBASE_TOKEN=$(cat t) X=1; firebase deploy",
            "X=1 HOME+=/x; firebase deploy",
        ):
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)


if __name__ == "__main__":
    unittest.main()
