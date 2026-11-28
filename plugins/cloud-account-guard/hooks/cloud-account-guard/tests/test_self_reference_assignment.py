"""同じキーの素の参照だけの代入は、値が hook の env の値のままなら固定の上書きに数えない (v0.36.5)。

0.30.3 は、前置きの代入の値を読めない形 (`AWS_PROFILE=$(cmd) aws ...`) に印を付け、ディレクトリ単位の固定の判定で
上書きとして数える。同じキーの自己参照 (`AWS_PROFILE=${AWS_PROFILE:-dev}` / `AWS_PROFILE=${AWS_PROFILE}` /
`AWS_PROFILE="$AWS_PROFILE"`) にも印が付き、固定のある世界と期待値ファイルの無い世界で deny になっていた。
シェルはその時点の値を代入し直すだけなので、値が hook の env の値のままと決められる形には印を付けない。

期待値は bash 3.2 (`/bin/bash`) と `zsh -f` で、偽の aws が受け取った `AWS_PROFILE` を記録した実測から取った
(`MEASURED`)。印が要るのは、実測の値が起動時の値と違うセル (起動時に無い変数は「無い」として比べる)。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core.command_parser import unread_env_candidates  # noqa: E402
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
K = "AWS_PROFILE"
UNSET = None
# 起動時の AWS_PROFILE の 3 状態
LAUNCHES = {"set": "launch", "empty": "", "unset": UNSET}

# 値の書き方 → 起動時の状態 (set / empty / unset) ごとに aws が受け取った AWS_PROFILE (bash 3.2 と zsh -f で
# 同じ値。None は比べないセル)。`OTHER=otherval` は起動時の env にある。
# 自己参照の形: 印が付くのは、実測の値が起動時の値と違うときちょうど
SELF_MEASURED = {
    "$AWS_PROFILE": ("launch", "", ""),
    "${AWS_PROFILE}": ("launch", "", ""),
    '"$AWS_PROFILE"': ("launch", "", ""),
    '"${AWS_PROFILE}"': ("launch", "", ""),
    "${AWS_PROFILE:-dev}": ("launch", "dev", "dev"),
    '"${AWS_PROFILE:-dev}"': ("launch", "dev", "dev"),
    "${AWS_PROFILE-dev}": ("launch", "", "dev"),
    '"${AWS_PROFILE-dev}"': ("launch", "", "dev"),
    "${AWS_PROFILE:-$OTHER}": ("launch", "otherval", "otherval"),
    "${AWS_PROFILE:-a:~/x}": ("launch", None, None),  # 空・無いときの値は `~` の展開で割れる
    "${AWS_PROFILE-}": ("launch", "", ""),
    # 空の既定の語の `:-` は、起動時が空のときも値は "" のままだが印を付ける (保守側。`:-` は空でないことを条件にする)
    "${AWS_PROFILE:-}": ("launch", None, ""),
}
# ほかの形: 実測の値が起動時の値と違えば印が付く (同じでも付くことがある。起動時が空なら `${AWS_PROFILE#l}` も "")
OTHER_MEASURED = {
    "${OTHER}": ("otherval", "otherval", "otherval"),
    "${AWS_PROFILE}-x": ("launch-x", "-x", "-x"),
    "$AWS_PROFILE-x": ("launch-x", "-x", "-x"),
    "${AWS_PROFILE}x": ("launchx", "x", "x"),
    "$AWS_PROFILEx": ("", "", ""),
    "${AWS_PROFILEx}": ("", "", ""),
    "'$AWS_PROFILE'": ("$AWS_PROFILE", "$AWS_PROFILE", "$AWS_PROFILE"),
    "\\$AWS_PROFILE": ("$AWS_PROFILE", "$AWS_PROFILE", "$AWS_PROFILE"),
    "${AWS_PROFILE:+x}": ("x", "", ""),
    "${AWS_PROFILE+x}": ("x", "x", ""),
    "${AWS_PROFILE#l}": ("aunch", "", ""),
    "${AWS_PROFILE%h}": ("launc", "", ""),
    "${AWS_PROFILE/l/L}": ("Launch", "", ""),
    "${AWS_PROFILE:0:3}": ("lau", "", ""),
    "${#AWS_PROFILE}": ("6", "0", "0"),
    "${AWS_PROFILE:-x}y": ("launchy", "xy", "xy"),
    "${AWS_PROFILE:-}}": ("launch}", "}", "}"),
    '"$AWS_PROFILE"x': ("launchx", "x", "x"),
    '"$AWS_PROFILE""$AWS_PROFILE"': ("launchlaunch", "", ""),
    "${AWS_PROFILE}${AWS_PROFILE}": ("launchlaunch", "", ""),
    "${AWS_PROFILE:-dev}-${AWS_PROFILE}": ("launch-launch", "dev-", "dev-"),
}
# 値は起動時の値のままでも、印を付けたままにする形 (保守側。読みを足さない):
# 既定の語に置換・空白・入れ子の展開・引用符がある (展開の閉じの読みが bash と zsh で割れうる)、代入し直す展開
# (`:=` / `=`)、`:?`、空の既定の語の `:-` (起動時が空のときの値は同じ "")
CONSERVATIVE = {
    "${AWS_PROFILE:-$(echo dev)}": ("launch", "dev", "dev"),
    "${AWS_PROFILE:-`echo dev`}": ("launch", "dev", "dev"),
    "${AWS_PROFILE:-a b}": ("launch", "a b", "a b"),
    '${AWS_PROFILE:-"a b"}': ("launch", "a b", "a b"),
    "${AWS_PROFILE:-${OTHER}}": ("launch", "otherval", "otherval"),
    "${AWS_PROFILE:=dev}": ("launch", "dev", "dev"),
    "${AWS_PROFILE=dev}": ("launch", "", "dev"),
    "$AWS_PROFILE\"\"": ("launch", "", ""),
    "''$AWS_PROFILE": ("launch", "", ""),
}


def _launch_env(state):
    value = LAUNCHES[state]
    env = {"OTHER": "otherval"}
    if value is not None:
        env[K] = value
    return env


class _EnvMixin:
    def _unread(self, command, state="set"):
        patcher = mock.patch.dict(os.environ, _launch_env(state))
        patcher.start()
        try:
            if state == "unset":
                os.environ.pop(K, None)
            try:
                return {c: set(k) for c, k in unread_env_candidates(command).items()}
            except Exception as e:  # noqa: BLE001
                self.fail(f"例外: {e!r}")
        finally:
            patcher.stop()

    def _marked(self, command, candidate=AWS, state="set"):
        unread = self._unread(command, state)
        return K in unread.get(candidate, set())


class MeasuredTableTests(_EnvMixin, unittest.TestCase):
    """実測の値が起動時の値と違えば印が付く。自己参照の形は、同じなら付かない。"""

    def _cells(self, table):
        for value, measured in table.items():
            self.assertEqual(len(measured), len(LAUNCHES), value)
            for (state, launch), got in zip(LAUNCHES.items(), measured):
                if got is not None:
                    yield f"{K}={value} {AWS}", state, got != launch

    def test_self_references_are_marked_exactly_when_the_value_changes(self):
        for command, state, changed in self._cells(SELF_MEASURED):
            with self.subTest(command=command, launch=state):
                self.assertEqual(self._marked(command, state=state), changed)

    def test_other_values_are_marked_when_the_value_changes(self):
        for command, state, changed in self._cells(OTHER_MEASURED):
            if changed:
                with self.subTest(command=command, launch=state):
                    self.assertTrue(self._marked(command, state=state))

    def test_empty_default_word_with_an_empty_launch_value_is_marked(self):
        self.assertTrue(self._marked(f"{K}=${{{K}:-}} {AWS}", state="empty"))

    def test_conservative_forms_stay_marked(self):
        for value in CONSERVATIVE:
            command = f"{K}={value} {AWS}"
            for state in LAUNCHES:
                with self.subTest(command=command, launch=state):
                    self.assertTrue(self._marked(command, state=state))

    def test_tilde_in_the_default_word_is_read_before_expansion(self):
        """行頭の代入の `~` は展開してから値を読む (v0.29.5) が、自己参照の判定は書いたままの値で見る。

        展開した値は HOME を引用符で包んで差し込む (`_expand_assignment_tilde`) ので、空白を含む HOME では
        既定の語に引用符が入る。既定の語は K が空でないときは使われないので、書いたままの値で判定する。
        """
        command = f"{K}=${{{K}:-a:~/x}} {AWS}"
        with mock.patch.dict(os.environ, {"HOME": "/tmp/a home"}):
            self.assertFalse(self._marked(command, state="set"))
            self.assertTrue(self._marked(command, state="unset"))


class AssignmentShapeTests(_EnvMixin, unittest.TestCase):
    """自己参照と読めるのは、ふつうの代入 (`NAME=値`) が同じ呼び出しで初めてそのキーに代入するときだけ。"""

    def test_append_and_element_assignments_stay_marked(self):
        # `AWS_PROFILE+=$AWS_PROFILE` は bash 3.2 で launch、zsh -f で launchlaunch。`[0]=` は zsh で構文エラー
        for command in (f"{K}+=${K} {AWS}", f"{K}[0]=${K} {AWS}", f"{K}+=${{{K}:-dev}} {AWS}"):
            with self.subTest(command=command):
                self.assertTrue(self._marked(command))

    def test_an_earlier_assignment_of_the_same_key_keeps_the_mark(self):
        # bash 3.2 は前の代入を見ずに起動時の値、zsh -f は前の代入の値を使う (実測)
        for command in (
            f"{K}=dev {K}=${K} {AWS}",
            f"{K}=dev {K}=${{{K}:-x}} {AWS}",
            f"{K}= {K}=${{{K}:-x}} {AWS}",
            f"{K}=$(echo q) {K}=${K} {AWS}",
        ):
            with self.subTest(command=command):
                self.assertTrue(self._marked(command))
        # 前の代入も自己参照なら、どちらのシェルでも起動時の値 (実測)
        self.assertFalse(self._marked(f"{K}=${K} {K}=${K} {AWS}"))

    def test_other_keys_before_and_after_do_not_matter(self):
        for command in (f"X=1 {K}=${K} {AWS}", f"{K}=${K} X=1 {AWS}", f"X=$OTHER {K}=${{{K}}} {AWS}"):
            with self.subTest(command=command):
                self.assertFalse(self._marked(command))

    def test_the_name_used_anywhere_else_keeps_the_mark(self):
        """コマンドのほかの場所にキーの名前があれば、値を変えうるので印を付ける (`_self_reference_env`)。

        `AWS_PROFILE=$(echo q); AWS_PROFILE=$AWS_PROFILE aws ...` の aws は q で走る (前の代入だけのセグメントの
        `$(echo q)` は env_changes が数えていない形)。読むだけの使い方 (`X=$AWS_PROFILE` / `echo`) も数える (保守側)。
        """
        for command in (
            f"{K}=$(echo q); {K}=${K} {AWS}",
            f"{K}=$(echo q) X=1; {K}=${{{K}:-dev}} {AWS}",
            f"read {K}; {K}=${K} {AWS}",
            f"for {K} in a; do {K}=${K} {AWS}; done",
            f"printf -v {K} q; {K}=${K} {AWS}",
            f"X=${K} {K}=${{{K}}} {AWS}",
            f"echo ${K}; {K}=${K} {AWS}",
            f"{K}=$(echo q) aws s3 ls s3://b && {K}=${K} {AWS}",
            # 自己参照で始まって後ろに文字が続く語は、自己参照の語に数えない (`_SELF_ASSIGNMENT` の後ろの境界)。
            # 前の代入だけのセグメントで値を変え、`X=$(echo q)` が並ぶと env_changes が数えない
            # (bash 3.2 / zsh -f で aws は launch-x / launchx / launchotherval で走る)
            f"{K}=${{{K}}}-x X=$(echo q); {K}=${K} {AWS}",
            f"{K}=${K}-x X=$(echo q); {K}=${{{K}:-dev}} {AWS}",
            f'{K}="${K}"x X=$(echo q); {K}="${K}" {AWS}',
            f"{K}=${{{K}}}${{OTHER}} X=$(date); {K}=${K} {AWS}",
        ):
            with self.subTest(command=command):
                self.assertTrue(self._marked(command))
        # 名前の一部 (`X_AWS_PROFILE`) と、自己参照の語どうしは数えない
        for command in (
            f"X_{K}=1 {K}=${K} {AWS}",
            f"{K}=${K} aws s3 ls s3://b && {K}=${{{K}:-dev}} {AWS}",
        ):
            with self.subTest(command=command):
                self.assertFalse(self._marked(command))

    # 自己参照で始まり、後ろに語の続きがある頭 (値は起動時の値に文字を足したもの。bash 3.2 / zsh -f で実測。
    # `(` は zsh -f が語の続きと読み、`launch(x)` になる。CR・VT・NBSP は両方のシェルで語の続き)
    LONGER_HEADS = (
        f"{K}=${{{K}}}-x", f"{K}=${K}-x", f'{K}="${K}"x', f'{K}="${{{K}}}"-x', f"{K}=${{{K}}}${{OTHER}}",
        f"{K}=${K}$OTHER", f"{K}=${{{K}:-w}}x", f'{K}="${{{K}:-w}}"x', f"{K}=${{{K}-w}}x", f"{K}=${K}(x)",
        f"{K}=${{{K}}}'x'", f'{K}=${K}"x"', f"{K}=${K}\\ x", f"{K}=${K}#x", f"{K}=${K}}}x", f"{K}=${K}=x",
        f"{K}=${K}\rx", f"{K}=${K}\vx", f"{K}=${K}\u00a0x",
    )

    def test_a_word_that_only_starts_with_a_self_reference_keeps_the_mark(self):
        tails = (f"{K}=${K} {AWS}", f"{K}=${{{K}:-dev}} {AWS}", f'{K}="${K}" {AWS}', f"{K}=${{{K}}} {AWS}")
        for head in self.LONGER_HEADS:
            for sep in ("; ", "\n", " && "):
                for tail in tails:
                    command = f"{head} X=$(echo q){sep}{tail}"
                    with self.subTest(command=command):
                        self.assertTrue(self._marked(command))

    def test_a_self_reference_word_ends_at_a_blank_a_separator_or_the_end(self):
        """対照: 語が空白・区切り・末尾で終わる自己参照の語は数える (値は起動時の値のまま。印を付けない)。"""
        for sep in ("; ", ";", "\n", "\t;", " && ", "&&", " || ", "|", "&", ">/dev/null; ", "</dev/null; "):
            command = f"{K}=${K}{sep}{K}=${K} {AWS}"
            with self.subTest(command=command):
                self.assertFalse(self._marked(command))
        for command in (
            f"({K}=${K}) && {K}=${K} {AWS}",
            f'{K}="${K}"; {K}="${K}" {AWS}',
            f"{K}=${{{K}:-w}}; {K}=${K} {AWS}",
            f"{K}=${K} {AWS}; {K}=${K}",
            f"{K}=${K} {AWS} && {K}=${{{K}:-dev}}",
        ):
            with self.subTest(command=command):
                self.assertFalse(self._marked(command))

    def test_a_later_static_assignment_still_wins(self):
        self.assertEqual(self._unread(f"{K}=${K} {K}=dev {AWS}"), {})

    def test_wrapper_arguments_are_expanded_by_the_bash_shell(self):
        """wrapper の引数の代入も Bash のシェルが展開する (`env` / `sudo` / シェル経由 wrapper の外の段)。"""
        for command in (
            f"env {K}=${K} {AWS}",
            f"env {K}=${{{K}:-dev}} {AWS}",
            f"nohup env {K}=${K} {AWS}",
            f"{K}=${K} nohup {AWS}",
            f"time {K}=${K} {AWS}",
            f"sudo {K}=${K} {AWS}",
            f"sudo {K}=${{{K}:-dev}} {AWS}",
            f"watch '{K}=${K} {AWS}'",
            f"watch 'sudo {K}=${K} {AWS}'",
            f"sudo -E watch '{K}=${{{K}:-dev}} {AWS}'",
        ):
            with self.subTest(command=command):
                self.assertFalse(self._marked(command))

    def test_an_outer_assignment_before_a_wrapper_keeps_its_mark(self):
        # 外側の値を読めない代入の印は、wrapper の引数の自己参照で外さない (実際の値は起動時の値だが、保守側)
        for command in (f"{K}=$(echo q) env {K}=${K} {AWS}",):
            with self.subTest(command=command):
                self.assertTrue(self._marked(command))


class HelperTests(unittest.TestCase):
    """`_keeps_launch_value` / `_parse_leading_env` は、渡された env だけで判定する。

    `unread_env_candidates` が渡す env (`_self_reference_env`) は、コマンドのほかの場所でキーの名前を使う形を先に
    外すので、違うキーの参照・`+=`・ほかの演算子は env の段階でも外れる。ここでは env を直接渡し、代入 1 つの
    判定だけでも外れることを確かめる (env の作り方を変えたときの守り)。
    """

    ENV = {K: "launch", "OTHER": "otherval"}

    def test_the_value_must_reference_the_same_key(self):
        from core.command_parser import _keeps_launch_value

        for raw, want in (
            ("${OTHER}", False), ("$OTHER", False), ('"$OTHER"', False), ("${OTHER:-x}", False),
            (f"${{{K}}}", True), (f"${K}", True),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_keeps_launch_value(K, raw, self.ENV), want)

    def test_only_the_default_operators(self):
        from core.command_parser import _keeps_launch_value

        for op, want in ((":-", True), ("-", True), (":=", False), ("=", False), (":+", False), ("+", False),
                         (":?", False), ("?", False)):
            with self.subTest(op=op):
                self.assertEqual(_keeps_launch_value(K, f"${{{K}{op}x}}", self.ENV), want)

    def test_only_plain_assignments(self):
        from core.command_parser import _parse_leading_env

        for command, want in (
            (f"{K}=${K} {AWS}", set()),
            (f"{K}+=${K} {AWS}", {K}),
            (f"{K}[0]=${K} {AWS}", {K}),
            (f"{K}=(${K}) {AWS}", {K}),
        ):
            with self.subTest(command=command):
                unread: set[str] = set()
                rest, _env = _parse_leading_env(command, None, unread, self.ENV)
                self.assertEqual(rest, AWS)
                self.assertEqual(unread, want)


class LinearTimeTests(_EnvMixin, unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RUN_TIMING_TESTS") == "1", "時間を測るテスト (RUN_TIMING_TESTS=1 で走る)")
    def test_unclosed_default_words_do_not_backtrack(self):
        """既定の語の `$NAME` を 1 つの選択肢にすると、閉じの無い `$AA$AA...` で分け方の数 (2 の個数乗) だけ後戻りする。

        1 文字ずつどちらか一方の選択肢にしか当たらない形 (`_SELF_DEFAULT_WORD`) なら線形。
        """
        import time

        for command in (f"{K}=${{{K}:-" + "$AA" * 26 + f" {AWS}", f'{K}="${{{K}:-' + "$AA" * 26 + f'" {AWS}'):
            with self.subTest(command=command):
                started = time.perf_counter()
                self.assertTrue(self._marked(command))
                self.assertLess(time.perf_counter() - started, 2.0)

    @unittest.skipUnless(os.environ.get("RUN_TIMING_TESTS") == "1", "時間を測るテスト (RUN_TIMING_TESTS=1 で走る)")
    def test_counting_names_is_one_pass(self):
        """コマンド全体の自己参照の語と名前を数えるのは 1 回の走査 (1 MB ほどで 2 秒に届かない)。"""
        import time

        from core import command_parser

        for command, want in (
            (f"{K}=${{{K}:-" * 40000 + f" {AWS}", {}),
            (" ".join(f"{K}=${K}" for _ in range(40000)) + f" {AWS}", {K: "launch"}),
        ):
            with self.subTest(size=len(command)), mock.patch.dict(os.environ, _launch_env("set")):
                started = time.perf_counter()
                got = command_parser._self_reference_env(command)
                self.assertLess(time.perf_counter() - started, 2.0)
                self.assertEqual(got, want)


class ScrubbedShellTests(_EnvMixin, unittest.TestCase):
    """env を scrub する sudo の後ろのシェル経由 wrapper の中身は、scrub した env のシェルが代入を展開する。

    `sudo watch 'AWS_PROFILE=${AWS_PROFILE:-dev} aws ...'` の aws は dev で走る (偽の sudo が env を空にして実測)。
    """

    def test_contents_of_a_shell_wrapper_after_a_scrubbing_sudo_stay_marked(self):
        for command in (
            f"sudo watch '{K}=${{{K}:-dev}} {AWS}'",
            f"sudo watch '{K}=${K} {AWS}'",
            f"sudo -u u watch '{K}=${K} {AWS}'",
            f"sudo -s '{K}=${K} {AWS}'",
            # 引用符の無い `sudo -i` の引数は Bash のシェルが先に展開する (起動時の値になる) が、剥がした後の
            # 文字列からは引用符の中身と区別できないので印を付ける (保守側)
            f"sudo -i {K}=${K} {AWS}",
            f"sudo npx -c '{K}=${K} {AWS}'",
            # scrub した sudo とシェル経由 wrapper の間に、env を素通しする wrapper を挟んだ形 (実測で dev / 空)
            f"sudo nohup watch '{K}=${{{K}:-dev}} {AWS}'",
            f"sudo env npx -c '{K}=${K} {AWS}'",
        ):
            with self.subTest(command=command):
                self.assertTrue(self._marked(command))

    def test_a_preserve_env_list_still_scrubs_for_the_self_reference(self):
        """`--preserve-env=LIST` は LIST の外の変数を scrub する (sudo(8))。中のシェルの `${K:-dev}` は dev。

        LIST の中身は読まない (`--preserve-env=AWS_PROFILE` も scrub に数える。保守側)。
        """
        for command in (
            f"sudo --preserve-env=OTHER watch '{K}=${{{K}:-dev}} {AWS}'",
            f"sudo --preserve-env=OTHER npx -c '{K}=${K} {AWS}'",
            f"sudo -u u --preserve-env=OTHER watch '{K}=${K} {AWS}'",
            f"sudo --preserve-env=OTHER nohup watch '{K}=${K} {AWS}'",
            f"sudo --preserve-env=OTHER watch 'true && {K}=${{{K}:-dev}} {AWS}'",
        ):
            with self.subTest(command=command):
                self.assertTrue(self._marked(command))
        # 対照: `-E` / `--preserve-env` (LIST なし) は env を保つ。sudo の引数の代入は Bash のシェルが展開する
        for command in (
            f"sudo --preserve-env=OTHER -E watch '{K}=${{{K}:-dev}} {AWS}'",
            f"sudo -E --preserve-env=OTHER watch '{K}=${{{K}:-dev}} {AWS}'",
            f"sudo --preserve-env watch '{K}=${{{K}:-dev}} {AWS}'",
            f"sudo --preserve-env=OTHER {K}=${K} {AWS}",
        ):
            with self.subTest(command=command):
                self.assertFalse(self._marked(command))

    def test_a_preserve_env_list_keeps_the_marks_before_the_sudo(self):
        # 収集 env と前置きの印の向きは変えない (`--preserve-env=LIST` は保持に倒す。誤 deny を増やすだけの側)
        self.assertTrue(self._marked(f"{K}=$(echo q) sudo --preserve-env=OTHER {AWS}"))
        self.assertFalse(self._marked(f"{K}=$(echo q) sudo {AWS}"))

    def test_split_stages_inside_the_wrapper_stay_marked(self):
        command = f"sudo watch 'true && {K}=${{{K}:-dev}} {AWS}'"
        self.assertTrue(self._marked(command))
        # 対照: scrub しない wrapper の中身の段は印を付けない
        self.assertFalse(self._marked(f"watch 'true && {K}=${{{K}:-dev}} {AWS}'"))
        self.assertFalse(self._marked(f"sudo -E watch 'true && {K}=${{{K}:-dev}} {AWS}'"))

    def test_stages_after_the_wrapper_quotes_run_in_the_bash_shell(self):
        # heredoc の行の、引用符の外の区切りの後ろの段は外のシェルが走らせる (実測で launch)
        command = f"sudo watch 'cat' <<EOF && {K}=${{{K}:-dev}} {AWS}\nx\nEOF"
        self.assertFalse(self._marked(command))


class _DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project_dir = self.tmp / "repo"
        (self.project_dir / ".git").mkdir(parents=True)
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.project_dir),
                "TMPDIR": str(cache_tmp),
                "AWS_PROFILE": "dev",
                "CLOUDSDK_CORE_ACCOUNT": "a@example.com",
                "OTHER": "otherval",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_accounts(self, data):
        d = self.project_dir / ".claude" / "cloud-account-guard"
        d.mkdir(parents=True, exist_ok=True)
        path = d / "accounts.local.json"
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(data), encoding="utf-8")

    def _decision(self, command):
        try:
            with mock.patch("subprocess.run", side_effect=AssertionError("CLI は起動しない")):
                result = dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        if result is None:
            return "allow"
        out = result["hookSpecificOutput"]
        return out.get("permissionDecision", "warn")


# 期待値が未登録の aws を hook の env (AWS_PROFILE=dev) で固定している世界
SELF_FORMS = [
    f"{K}=${{{K}:-dev}} {AWS}",
    f"{K}=${{{K}:-prod}} {AWS}",
    f"{K}=${{{K}}} {AWS}",
    f'{K}="${K}" {AWS}',
    f"{K}=${K} {AWS}",
    f"{K}=${{{K}-prod}} {AWS}",
    f"X=1 {K}=${K} {AWS}",
    f"env {K}=${{{K}:-prod}} {AWS}",
    f"cd sub && {K}=${{{K}:-prod}} {AWS}",
    f"sudo --preserve-env watch '{K}=${{{K}:-prod}} {AWS}'",
]
STILL_DENIED = [
    f"{K}=${{OTHER}} {AWS}",
    f"{K}=${{{K}}}-x {AWS}",
    f"{K}='${K}' {AWS}",
    f"{K}=${{{K}:+x}} {AWS}",
    f"{K}=dev {K}=${K} {AWS}",
    f"sudo watch '{K}=${{{K}:-prod}} {AWS}'",
    f"sudo --preserve-env=OTHER watch '{K}=${{{K}:-prod}} {AWS}'",
    # 前の代入だけのセグメントで、自己参照で始まる別の値を代入する形 (aws は dev-x / devx / devotherval で走る。
    # `(x)` の形は zsh -f で dev(x)、bash 3.2 は構文エラー)
    f"{K}=${{{K}}}-x X=$(echo q); {K}=${K} {AWS}",
    f"{K}=${K}-x X=$(echo q); {K}=${{{K}:-dev}} {AWS}",
    f'{K}="${K}"x X=$(echo q); {K}="${K}" {AWS}',
    f"{K}=${{{K}}}${{OTHER}} X=$(date); {K}=${K} {AWS}",
    f"{K}=${K}(x) X=$(echo q); {K}=${K} {AWS}",
    # 前段で値を変えた形は env_changes が数える (代入の無い aws と同じ)
    f"export {K}=q; {K}=${K} {AWS}",
    f"unset {K}; {K}=${{{K}:-dev}} {AWS}",
    f"{K}=q; {K}=${K} {AWS}",
]


class PinnedSelfReferenceTests(_DispatchBase):
    def test_self_reference_stays_pinned(self):
        (self.project_dir / "sub").mkdir()
        for accounts in ({"github": "me"}, None):
            self._write_accounts(accounts)
            for command in SELF_FORMS:
                with self.subTest(command=command, accounts=accounts):
                    self.assertEqual(self._decision(command), "allow")

    def test_other_values_are_still_overrides(self):
        for accounts in ({"github": "me"}, None):
            self._write_accounts(accounts)
            for command in STILL_DENIED:
                with self.subTest(command=command, accounts=accounts):
                    self.assertEqual(self._decision(command), "deny")

    def test_a_self_referencing_query_and_a_write_are_one_pinned_target(self):
        """印が付かないので、0.36.3 の警告 (印の付いた read と印の無い write が別の target) にならない。"""
        self._write_accounts({"github": "me"})
        self.assertEqual(self._decision(f"{K}=${{{K}:-dev}} aws s3 ls s3://b && {AWS}"), "allow")
        self.assertEqual(self._decision(f"{K}=${K} aws s3 ls s3://b && {K}=${{{K}:-dev}} {AWS}"), "allow")
        # キーの名前をほかの代入で使う形は 0.36.3 と同じ (後ろの aws は起動時の値で走るが、保守側)
        self.assertEqual(self._decision(f"{K}=$(echo q) aws s3 ls s3://b && {K}=${K} {AWS}"), "deny")
        # 前の代入だけのセグメントで値を変える形は止める (aws は q で走る)
        self.assertEqual(self._decision(f"{K}=$(echo q); {K}=${K} {AWS}"), "deny")


class UnsetIdentityEnvTests(_DispatchBase):
    """固定が identity env 以外で決まる世界では、hook の env に無い変数の自己参照は既定の語で上書きする。"""

    def test_gcloud_pinned_by_the_active_configuration(self):
        gc = self.tmp / "gc"
        (gc / "configurations").mkdir(parents=True)
        (gc / "configurations" / "config_work").write_text("[core]\naccount = a@example.com\n")
        os.environ.pop("CLOUDSDK_CORE_ACCOUNT", None)
        os.environ.update(CLOUDSDK_CONFIG=str(gc), CLOUDSDK_ACTIVE_CONFIG_NAME="work", HOME=str(self.home))
        self._write_accounts({"github": "me"})
        self.assertEqual(self._decision("gcloud run deploy s"), "allow")
        for command in (
            "CLOUDSDK_CORE_ACCOUNT=${CLOUDSDK_CORE_ACCOUNT:-b@example.com} gcloud run deploy s",
            "CLOUDSDK_CORE_ACCOUNT=${CLOUDSDK_CORE_ACCOUNT-b@example.com} gcloud run deploy s",
            "CLOUDSDK_CORE_ACCOUNT=$CLOUDSDK_CORE_ACCOUNT gcloud run deploy s",
            "CLOUDSDK_CORE_PROJECT=${CLOUDSDK_CORE_PROJECT:-other} gcloud run deploy s",
        ):
            with self.subTest(command=command):
                self.assertEqual(self._decision(command), "deny")
        self.assertEqual(
            self._decision("CLOUDSDK_ACTIVE_CONFIG_NAME=${CLOUDSDK_ACTIVE_CONFIG_NAME:-x} gcloud run deploy s"),
            "allow",
        )

    def test_firebase_pinned_by_the_record_under_home(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ["HOME"] = str(self.home)
        store = self.home / ".config" / "configstore"
        store.mkdir(parents=True)
        (store / "firebase-tools.json").write_text(
            json.dumps({"activeProjects": {str(self.project_dir.resolve()): "my-proj"}}), encoding="utf-8"
        )
        (self.project_dir / "firebase.json").write_text("{}", encoding="utf-8")
        self._write_accounts({"github": "me"})
        self.assertEqual(self._decision("firebase deploy"), "allow")
        self.assertEqual(self._decision("HOME=${HOME:-/h} firebase deploy"), "allow")
        for command in (
            "XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-/elsewhere} firebase deploy",
            "XDG_CONFIG_HOME=$XDG_CONFIG_HOME firebase deploy",
        ):
            with self.subTest(command=command):
                self.assertEqual(self._decision(command), "deny")


if __name__ == "__main__":
    unittest.main()
