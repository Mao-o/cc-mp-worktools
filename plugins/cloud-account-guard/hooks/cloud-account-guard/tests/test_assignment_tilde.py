"""行頭の代入の値の `~` を、シェルと同じく HOME に展開して照合の env に渡す (v0.29.5)。

0.29.4 までは `KUBECONFIG=~/.kube/prod.yaml kubectl apply` の値を `~/.kube/prod.yaml` のまま
照合の env に渡していた。シェルは代入の値の先頭と `:` の後の `~` を展開するので、kubectl は
HOME の下のファイルを読むが、照合は作業ディレクトリの `./~/.kube/prod.yaml` を探して誤 deny に
なっていた。

展開するのは bash 3.2 と zsh -f で読みが一致する形だけ。表の各行は、両方のシェルで実際に
`printenv` して確かめる (`ShellAgreementTests`)。展開しない形は 0.29.4 までと同じ (生の値)。

予約語の `time` の後ろの代入と、シェル経由 wrapper を先頭に置いた heredoc の行の後ろの段の代入も展開する
(v0.30.4。TIME_* / WRAPPER_HEREDOC_* の表。`ReservedTimeAndWrapperHeredocShellTests` が両方のシェルで確かめる)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401
from test_kubectl_workdir import _FakeKubectl

from core import command_parser  # noqa: E402
from core.command_parser import extract_candidates  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

_HOME = "/H"
_SHELLS = [s for s in ("/bin/bash", shutil.which("zsh")) if s and os.path.exists(s)]

# (代入の値の書き方, 照合の env に入る値)。None は値を集めない (0.29.4 と同じ)。
# 右の注記は bash 3.2 / zsh -f の `K=<値> printenv K` の実測。
EXPANDED = [
    ("~/a", "/H/a"),
    ("~", "/H"),
    ("~/", "/H/"),
    ("~:", "/H:"),
    ("~:x", "/H:x"),
    ("~/a:~/b", "/H/a:/H/b"),
    ("x:~/b", "x:/H/b"),
    ("x:~", "x:/H"),
    ("x::~/b", "x::/H/b"),
    ("/x:~/b:~", "/x:/H/b:/H"),
    ("~/a:'~/b'", "/H/a:~/b"),  # 引用符の中の 2 つ目は両方とも展開しない
    ("~/a#", "/H/a#"),
    ("~/a'b c'", "/H/ab c"),
    ('~/a"b"', "/H/ab"),
]
KEPT = [
    # 両方のシェルが展開しない
    ("'~/a'", "~/a"),
    ('"~/a"', "~/a"),
    ("\\~/a", "~/a"),
    ("a~/b", "a~/b"),
    ("a/~/b", "a/~/b"),
    ("~#", "~#"),
    ("~=", "~="),
    ("~~", "~~"),
    ("x=~/b", "x=~/b"),
    # zsh だけが展開する (bash は展開しない)。照合は 0.29.4 までと同じ生の値
    ("x':'~/b", "x:~/b"),
    ("x\\:~/b", "x:~/b"),
    ('x":"~/b', "x:~/b"),
    ('~"/a"', "~/a"),
    ("~\\/a", "~/a"),
    ("~''/a", "~/a"),
    # HOME 以外の行き先 (`~user` / `~+` / `~-`)
    ("~root/a", "~root/a"),
    ("~+", "~+"),
    ("~-", "~-"),
]

# heredoc を置いた行の後ろの段の行頭の代入 (v0.30.2)。本文を落とした行を分割し直した段は Bash のシェルが
# 行う代入なので、行頭の代入と同じく展開する。どの形も bash 3.2 / zsh -f で `printenv` して、両方が
# HOME に展開することを確かめる (`ShellAgreementTests.test_heredoc_forms_expand_in_both_shells`)。
HEREDOC_EXPANDED = [
    "cat <<EOF && KUBECONFIG=~/a kubectl apply\nbody\nEOF",
    "cat <<'EOF' && KUBECONFIG=~/a kubectl apply\ndon't (x) # y\nEOF",
    'cat <<"EOF"; KUBECONFIG=~/a kubectl apply\nx\nEOF',
    "cat <<\\EOF | KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "false <<'END MSG' || KUBECONFIG=~/a kubectl apply\nx\nEND MSG",
    "cat <<'2EOF' && KUBECONFIG=~/a kubectl apply\nx\n2EOF",
    "cat <<-EOF && KUBECONFIG=~/a kubectl apply\n\tx\n\tEOF",
    "cat <<A <<B && KUBECONFIG=~/a kubectl apply\na\nA\nb\nB",
    "cat > f <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "X=1 cat <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "cat <<EOF && X=1 KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "cat <<EOF && cd sub && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "nohup cat <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "cat <<EOF && (KUBECONFIG=~/a kubectl apply)\nx\nEOF",
    "cat <<EOF && { KUBECONFIG=~/a kubectl apply; }\nx\nEOF",
]
# 展開しない (0.30.1 と同じ生の値): wrapper の後ろの代入の形の引数、シェル経由の wrapper の中身、
# HOME を変えうるコマンド (heredoc の本文にある `HOME` の語も数える)。
HEREDOC_KEPT = [
    "cat <<EOF && env KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "cat <<EOF && sudo KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "cat <<EOF && watch 'KUBECONFIG=~/a kubectl apply'\nx\nEOF",
    "cat <<EOF && npx -c 'KUBECONFIG=~/a kubectl apply'\nx\nEOF",
    "cat <<EOF && HOME=/y KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "cat <<EOF && . ./env.sh && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "cat <<EOF && KUBECONFIG=~/a kubectl apply\nHOME=/y\nEOF",
]

# 予約語の `time` の後ろの代入 (v0.30.4)。option の無い `time` を、代入・wrapper・パスの正規化を挟まずに
# 置いた形は、bash 3.2 と zsh -f の両方が予約語と読み、後ろの代入をシェル自身が行う (HOME に展開する)。
# 0.30.3 までは wrapper として剥がした後ろの代入を展開していなかった。
TIME_EXPANDED = [
    "time KUBECONFIG=~/a kubectl apply",
    "time\tKUBECONFIG=~/a kubectl apply",
    "time X=1 KUBECONFIG=~/a kubectl apply",
    "time a=(1 2) KUBECONFIG=~/a kubectl apply",
    "time ! KUBECONFIG=~/a kubectl apply",
    "time (KUBECONFIG=~/a kubectl apply)",
    "time { KUBECONFIG=~/a kubectl apply; }",
    "time 2>/dev/null KUBECONFIG=~/a kubectl apply",
    "( time KUBECONFIG=~/a kubectl apply )",
    "{ time KUBECONFIG=~/a kubectl apply; }",
    "true && time KUBECONFIG=~/a kubectl apply",
    "true; time KUBECONFIG=~/a kubectl apply",
    "true & time KUBECONFIG=~/a kubectl apply",
    "if true; then time KUBECONFIG=~/a kubectl apply; fi",
    "for i in 1; do time KUBECONFIG=~/a kubectl apply; done",
    "cat <<EOF && time KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "time KUBECONFIG=~/a kubectl apply <<EOF\nx\nEOF",
]
# zsh -f だけが予約語と読む位置 (bash 3.2 は外部の time を起動し、代入の形の語をコマンド名として起動しようとして
# 失敗するので CLI は走らない)。CLI が走るシェルでは HOME に展開されるので、展開する側に含める。
TIME_ZSH_ONLY = [
    "! time KUBECONFIG=~/a kubectl apply",
    "if time KUBECONFIG=~/a kubectl apply; then :; fi",
    "while time KUBECONFIG=~/a kubectl apply; do break; done",
    "true | time KUBECONFIG=~/a kubectl apply",
    "2>/dev/null time KUBECONFIG=~/a kubectl apply",
]
# 展開しない (0.30.3 と同じ生の値)。予約語として読まない位置の `time` は外部の time で、どちらのシェルでも
# 後ろの CLI は走らない (`X=1 time` は zsh では構文エラー)。option 付きの `time -p` は zsh が `-p` を
# コマンド名として起動する。`time` の後ろの wrapper の後ろの代入は、ほかの wrapper と同じく bash だけが展開する。
TIME_KEPT = [
    "time -p KUBECONFIG=~/a kubectl apply",
    "time -- KUBECONFIG=~/a kubectl apply",
    "time time KUBECONFIG=~/a kubectl apply",
    "X=1 time KUBECONFIG=~/a kubectl apply",
    "\\time KUBECONFIG=~/a kubectl apply",
    "nohup time KUBECONFIG=~/a kubectl apply",
    "command time KUBECONFIG=~/a kubectl apply",
    "time nohup KUBECONFIG=~/a kubectl apply",
    "time env KUBECONFIG=~/a kubectl apply",
]
# heredoc を置いた行の先頭の段がシェル経由の wrapper で、区切りが wrapper の引用符の外にある行の後ろの段
# (v0.30.4)。後ろの段は外のシェルが走らせるので、ほかの heredoc の行と同じく HOME に展開する。0.30.3 までは
# 行の全段を wrapper の中身と読み、展開していなかった。
WRAPPER_HEREDOC_EXPANDED = [
    "watch 'cat' <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "watch cat <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "watch -n 1 'cat' <<'EOF'; KUBECONFIG=~/a kubectl apply\ndon't (x) # y\nEOF",
    "watch 'cat' <<EOF | KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "watch 'cat' <<EOF & KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "watch 'true && cat' <<\\EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "watch 'cat' <<-EOF && true && KUBECONFIG=~/a kubectl apply\n\tx\n\tEOF",
    "watch 'cat' <<'END MSG' && (KUBECONFIG=~/a kubectl apply)\nx\nEND MSG",
    "watch 'cat' <<'2EOF' && { KUBECONFIG=~/a kubectl apply; }\nx\n2EOF",
    "sudo -i cat <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "sudo -s 'cat' <<\"EOF\" ; KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "npx -c 'cat' <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "npx -c 'cat' pkg <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "K0=1 watch 'cat' <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "time watch 'cat' <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "watch 'cat' <<EOF && time KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "watch 'cat' <<EOF && KUBECONFIG=~/a sudo -E kubectl apply\nx\nEOF",
]
# 展開しない: 後ろの段自身が wrapper の後ろの代入の形 (bash 3.2 と zsh -f で読みが割れる)
WRAPPER_HEREDOC_SPLIT = [
    "watch 'cat' <<EOF && env KUBECONFIG=~/a kubectl apply\nx\nEOF",
    "sudo -i cat <<EOF && sudo -i KUBECONFIG=~/a kubectl apply\nx\nEOF",
]
# 展開しない (上に加えて): 後ろの段自身がシェル経由の wrapper の中身 (wrapper が起動するシェルが読み直す)、
# 本文に HOME の語がある行 (`tilde_home`)。どちらも展開しない側に倒した読みで、0.30.3 と同じ
WRAPPER_HEREDOC_KEPT = WRAPPER_HEREDOC_SPLIT + [
    "watch 'cat' <<EOF && watch 'KUBECONFIG=~/a kubectl apply'\nx\nEOF",
    "watch 'cat' <<EOF && KUBECONFIG=~/a kubectl apply\nHOME=/y\nEOF",
]


def _env(command, home=_HOME):
    with mock.patch.dict(os.environ, {"HOME": home}):
        try:
            cands = extract_candidates(command)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"例外: {e!r}") from e
    return cands


class ExpansionTableTests(unittest.TestCase):
    def test_expanded_forms(self):
        for raw, want in EXPANDED:
            with self.subTest(raw=raw):
                cands = _env(f"KUBECONFIG={raw} kubectl apply -f x.yaml")
                self.assertEqual(cands, [("kubectl apply -f x.yaml", {"KUBECONFIG": want})])

    def test_kept_forms(self):
        for raw, want in KEPT:
            with self.subTest(raw=raw):
                cands = _env(f"KUBECONFIG={raw} kubectl apply -f x.yaml")
                self.assertEqual(cands, [("kubectl apply -f x.yaml", {"KUBECONFIG": want})])

    def test_other_variables_take_the_same_path(self):
        for command, key, svc in (
            ("AWS_CONFIG_FILE=~/c aws s3 rm s3://b/k", "AWS_CONFIG_FILE", "aws s3 rm s3://b/k"),
            ("CLOUDSDK_CONFIG=~/g gcloud run deploy", "CLOUDSDK_CONFIG", "gcloud run deploy"),
            ("GH_CONFIG_DIR=~/gh gh pr create", "GH_CONFIG_DIR", "gh pr create"),
            ("XDG_CONFIG_HOME=~/x gh pr create", "XDG_CONFIG_HOME", "gh pr create"),
        ):
            with self.subTest(command=command):
                self.assertEqual(_env(command), [(svc, {key: f"/H/{command.split('~/')[1].split()[0]}"})])

    def test_shell_level_positions_expand(self):
        for command in (
            "KUBECONFIG=~/a kubectl apply",
            "true && KUBECONFIG=~/a kubectl apply",
            "true | KUBECONFIG=~/a kubectl apply",
            "true; KUBECONFIG=~/a kubectl apply",
            "( KUBECONFIG=~/a kubectl apply )",
            "{ KUBECONFIG=~/a kubectl apply; }",
            "X=1 KUBECONFIG=~/a kubectl apply",
            "KUBECONFIG=~/a sudo -E kubectl apply",
            "cd .. && KUBECONFIG=~/a kubectl apply",
            # 語の途中の `source` / `eval` / `.` は HOME を変えるコマンドではない
            "KUBECONFIG=~/a kubectl apply -l tier=retrieval",
            "KUBECONFIG=~/a kubectl apply -l tier=opensource",
            "KUBECONFIG=~/a kubectl apply -f a.",
            # 引数の `.` はファイルを読まない (bash 3.2 / zsh -f とも kubectl は $HOME/a を読む)
            "KUBECONFIG=~/a kubectl apply -k .",
            "KUBECONFIG=~/a kubectl apply -f .",
            "KUBECONFIG=~/a kubectl apply -f . && echo ok",
            "KUBECONFIG=~/a kubectl apply -f .|cat",
            "KUBECONFIG=~/a kubectl get po; cd .",
            "KUBECONFIG=~/a kubectl get po; cd . # x",
            "git add . && KUBECONFIG=~/a kubectl get po",
            "git add .; KUBECONFIG=~/a kubectl get po",
            "find . -name x; KUBECONFIG=~/a kubectl get po",
            "docker build . -t x && KUBECONFIG=~/a kubectl get po",
            "(cd .) && KUBECONFIG=~/a kubectl get po",
            "cd ..; KUBECONFIG=~/a kubectl get po",
            # 名前の一部の HOME と、読むだけの `$HOME` / `${HOME}` は HOME を変えない
            "XDG_CONFIG_HOME=~/c KUBECONFIG=~/a kubectl apply",
            "FOO_HOME=x KUBECONFIG=~/a kubectl apply",
            "HOMEBREW_X=1 KUBECONFIG=~/a kubectl apply",
            "echo $HOME && KUBECONFIG=~/a kubectl apply",
            "KUBECONFIG=~/a kubectl apply -f ${HOME}/x.yaml",
            'KUBECONFIG=~/a kubectl apply -f "${HOME}/x.yaml"',
            "KUBECONFIG=~/a kubectl apply -f ${HOME:-/x}/y.yaml",
            *HEREDOC_EXPANDED,
            *TIME_EXPANDED,
            *TIME_ZSH_ONLY,
            *WRAPPER_HEREDOC_EXPANDED,
        ):
            with self.subTest(command=command):
                envs = [e for c, e in _env(command) if c.startswith("kubectl")]
                self.assertEqual(len(envs), 1, command)
                self.assertEqual(envs[0].get("KUBECONFIG"), "/H/a")

    def test_after_a_heredoc_on_the_same_line(self):
        """heredoc を置いた行の後ろの段の行頭の代入も展開する (v0.30.2。0.30.1 までは生の `~/a`)。

        heredoc の本文を落とした行を分割し直した段は、Bash のシェルが行う代入 (`ShellAgreementTests`)。
        HEREDOC_EXPANDED は `test_shell_level_positions_expand` が流す。ここは候補と env の全体を見る。
        """
        # 前の段が CLI の形、代入の後ろに wrapper が続く形、ほかの変数
        for command, want in (
            ("cat <<EOF && KUBECONFIG=~/a kubectl apply\nbody\nEOF",
             [("cat", {}), ("kubectl apply", {"KUBECONFIG": "/H/a"})]),
            ("kubectl get po -o yaml <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
             [("kubectl get po -o yaml", {}), ("kubectl apply", {"KUBECONFIG": "/H/a"})]),
            ("cat <<EOF && KUBECONFIG=~/a sudo -E kubectl apply\nx\nEOF",
             [("cat", {}), ("kubectl apply", {"KUBECONFIG": "/H/a"})]),
            ("cat <<EOF && AWS_CONFIG_FILE=~/c aws s3 rm s3://b/k\nx\nEOF",
             [("cat", {}), ("aws s3 rm s3://b/k", {"AWS_CONFIG_FILE": "/H/c"})]),
            ("cat <<EOF | CLOUDSDK_CONFIG=~/g gcloud run deploy\nx\nEOF",
             [("cat", {}), ("gcloud run deploy", {"CLOUDSDK_CONFIG": "/H/g"})]),
            ("cat <<EOF && GH_CONFIG_DIR=~/gh:~ gh pr create\nx\nEOF",
             [("cat", {}), ("gh pr create", {"GH_CONFIG_DIR": "/H/gh:/H"})]),
        ):
            with self.subTest(command=command):
                self.assertEqual(_env(command), want)

    def test_reserved_time(self):
        """予約語の `time` の後ろの代入は展開する (v0.30.4)。候補と env の全体を見る。"""
        for command, want in (
            ("time KUBECONFIG=~/a kubectl apply", [("kubectl apply", {"KUBECONFIG": "/H/a"})]),
            ("time X=1 KUBECONFIG=~/a:~/b kubectl apply",
             [("kubectl apply", {"X": "1", "KUBECONFIG": "/H/a:/H/b"})]),
            ("time AWS_CONFIG_FILE=~/c aws s3 rm s3://b/k", [("aws s3 rm s3://b/k", {"AWS_CONFIG_FILE": "/H/c"})]),
            ("time GH_CONFIG_DIR=~/g gh pr create", [("gh pr create", {"GH_CONFIG_DIR": "/H/g"})]),
            # 予約語として読まない位置と、option 付きの `time` は 0.30.3 と同じ生の値
            ("X=1 time KUBECONFIG=~/a kubectl apply", [("kubectl apply", {"X": "1", "KUBECONFIG": "~/a"})]),
            ("time time KUBECONFIG=~/a kubectl apply", [("kubectl apply", {"KUBECONFIG": "~/a"})]),
            ("time -p KUBECONFIG=~/a kubectl apply", [("kubectl apply", {"KUBECONFIG": "~/a"})]),
            ("\\time KUBECONFIG=~/a kubectl apply", [("kubectl apply", {"KUBECONFIG": "~/a"})]),
            # sudo の env scrub は従来どおり (予約語の time の後ろの代入も sudo の前の代入)
            ("time KUBECONFIG=~/a sudo kubectl apply", [("kubectl apply", {})]),
            ("time KUBECONFIG=~/a sudo -E kubectl apply", [("kubectl apply", {"KUBECONFIG": "/H/a"})]),
        ):
            with self.subTest(command=command):
                self.assertEqual(_env(command), want)

    def test_heredoc_line_led_by_a_shell_wrapper(self):
        """先頭の段がシェル経由 wrapper の heredoc の行 (v0.30.4)。引用符の外の区切りの後ろの段は展開し、
        wrapper の中身の段は展開しない (wrapper が起動するシェルが読み直す)。"""
        for command, want in (
            ("watch 'cat' <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
             [("'cat'", {}), ("kubectl apply", {"KUBECONFIG": "/H/a"})]),
            ("npx -c 'true && KUBECONFIG=~/b kubectl get po' <<EOF ; KUBECONFIG=~/a kubectl apply\nx\nEOF",
             [("true", {}), ("kubectl get po", {"KUBECONFIG": "~/b"}), ("kubectl apply", {"KUBECONFIG": "/H/a"})]),
            ("sudo -i cat <<EOF | AWS_CONFIG_FILE=~/c aws s3 cp - s3://b/k\nx\nEOF",
             [("cat", {}), ("aws s3 cp - s3://b/k", {"AWS_CONFIG_FILE": "/H/c"})]),
            ("K0=1 watch 'cat' <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
             [("'cat'", {"K0": "1"}), ("kubectl apply", {"KUBECONFIG": "/H/a"})]),
            # heredoc の無い行の wrapper の中身は従来どおり展開しない
            ("watch 'true && KUBECONFIG=~/a kubectl apply'",
             [("true", {}), ("kubectl apply", {"KUBECONFIG": "~/a"})]),
        ):
            with self.subTest(command=command):
                self.assertEqual(_env(command), want)

    def test_unsure_commands_keep_the_raw_value(self):
        """HOME を変えうるコマンド・wrapper の引数の代入は、0.29.4 までと同じ生の値。"""
        for command in (
            # 同じ行頭の HOME: bash は元の HOME、zsh は新しい HOME で展開する
            "HOME=/y KUBECONFIG=~/a kubectl apply",
            "KUBECONFIG=~/a HOME=/y kubectl apply",
            "HOME=/y; KUBECONFIG=~/a kubectl apply",
            "export HOME=/y && KUBECONFIG=~/a kubectl apply",
            "read HOME < f; KUBECONFIG=~/a kubectl apply",
            ": ${HOME:=/y}; KUBECONFIG=~/a kubectl apply",
            ": ${HOME=/y}; KUBECONFIG=~/a kubectl apply",
            ": ${HOME::=/y}; KUBECONFIG=~/a kubectl apply",
            "unset HOME; KUBECONFIG=~/a kubectl apply",
            # ファイルを読む `.` (bash 3.2 / zsh -f で読んだファイルの HOME が効くことを実測)
            ". ./env.sh && KUBECONFIG=~/a kubectl apply",
            ". env; KUBECONFIG=~/a kubectl apply",
            "(. x) ; KUBECONFIG=~/a kubectl apply",
            ". -- ./env.sh; KUBECONFIG=~/a kubectl apply",
            '. "./env.sh"; KUBECONFIG=~/a kubectl apply',
            ".\t./env.sh; KUBECONFIG=~/a kubectl apply",
            ". ~/env.sh; KUBECONFIG=~/a kubectl apply",
            "A=1 . ./env.sh; KUBECONFIG=~/a kubectl apply",
            "A=1 . -- ./env.sh; KUBECONFIG=~/a kubectl apply",
            "if . ./env.sh; then KUBECONFIG=~/a kubectl apply; fi",
            "true && . ./env.sh && KUBECONFIG=~/a kubectl apply",
            "! . ./env.sh; KUBECONFIG=~/a kubectl apply",
            "{ . ./env.sh; }; KUBECONFIG=~/a kubectl apply",
            "builtin . ./env.sh; KUBECONFIG=~/a kubectl apply",
            # コマンドの位置の `. -x` (zsh は `-x` という名前のファイルを読む)
            "true; . -x; KUBECONFIG=~/a kubectl apply",
            "if . -x; then KUBECONFIG=~/a kubectl apply; fi",
            "! . -x; KUBECONFIG=~/a kubectl apply",
            "source env.sh; KUBECONFIG=~/a kubectl apply",
            "eval x; KUBECONFIG=~/a kubectl apply",
            # wrapper の後ろの代入の形の引数: bash は展開し zsh は展開しない (予約語の `time` は wrapper ではない。
            # TIME_EXPANDED)
            "env KUBECONFIG=~/a kubectl apply",
            "sudo KUBECONFIG=~/a kubectl apply",
            "nohup env KUBECONFIG=~/a kubectl apply",
            "/usr/bin/time KUBECONFIG=~/a kubectl apply",
            "watch 'KUBECONFIG=~/a kubectl apply && true'",
            # wrapper の中身を分割し直した後ろの段も展開しない (0.29.4 と同じ)
            "watch 'true && KUBECONFIG=~/a kubectl apply'",
            *HEREDOC_KEPT,
            *TIME_KEPT,
            *WRAPPER_HEREDOC_KEPT,
        ):
            with self.subTest(command=command):
                envs = [e for c, e in _env(command) if c.startswith("kubectl")]
                self.assertTrue(envs, command)
                for env in envs:
                    self.assertEqual(env.get("KUBECONFIG"), "~/a")

    def test_no_usable_home_keeps_the_raw_value(self):
        for home in ("", "/a$b"):
            with self.subTest(home=home):
                self.assertEqual(
                    _env("KUBECONFIG=~/a kubectl apply", home=home),
                    [("kubectl apply", {"KUBECONFIG": "~/a"})],
                )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HOME", None)
            self.assertEqual(
                extract_candidates("KUBECONFIG=~/a kubectl apply"),
                [("kubectl apply", {"KUBECONFIG": "~/a"})],
            )

    def test_home_with_shell_characters_is_inserted_literally(self):
        for home in ("/h h", "/h'q", '/h"d', "/h;x", "/h\\b"):
            with self.subTest(home=home):
                self.assertEqual(
                    _env("KUBECONFIG=~/a:~ kubectl apply", home=home),
                    [("kubectl apply", {"KUBECONFIG": f"{home}/a:{home}"})],
                )

    def test_candidates_do_not_change(self):
        """展開は env の値だけを変え、候補 (コマンドの断片) は変えない。"""
        commands = [f"KUBECONFIG={raw} kubectl apply && aws s3 rm s3://b/k" for raw, _want in EXPANDED + KEPT]
        for command in (
            commands + HEREDOC_EXPANDED + HEREDOC_KEPT + TIME_EXPANDED + TIME_ZSH_ONLY + TIME_KEPT
            + WRAPPER_HEREDOC_EXPANDED + WRAPPER_HEREDOC_KEPT
        ):
            with self.subTest(command=command):
                with_home = [c for c, _e in _env(command)]
                with mock.patch.object(command_parser, "tilde_home", return_value=None):
                    without = [c for c, _e in _env(command)]
                self.assertEqual(with_home, without)

    def test_unclosed_quote_is_left_alone(self):
        for raw in ("~/a'b", '~/a"b', '~/a"b\\"'):
            with self.subTest(raw=raw):
                self.assertEqual(command_parser._expand_assignment_tilde(raw, "/H"), raw)


@unittest.skipUnless(len(_SHELLS) == 2, "bash と zsh の両方が要る")
class ShellAgreementTests(unittest.TestCase):
    """表の各行を bash 3.2 と zsh -f で実際に走らせ、展開した値が両方のシェルの値と一致し、
    展開しない値はどちらかのシェルの値と一致する (0.29.4 までの値のまま) ことを確かめる。"""

    def _shell_values(self, raw):
        return self._run_shells(f"K={raw} printenv K", "/")

    def _run_shells(self, script, cwd):
        values = {}
        for shell in _SHELLS:
            args = [shell, "-f", "-c"] if shell.endswith("zsh") else [shell, "-c"]
            done = subprocess.run(
                [*args, script],
                env={"PATH": "/usr/bin:/bin", "HOME": _HOME},
                capture_output=True,
                text=True,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
            )
            values[shell] = done.stdout.rstrip("\n")
        return values

    def _heredoc_values(self, command):
        """`kubectl apply` を `printenv KUBECONFIG` に替えて走らせた、各シェルの最後の出力行。"""
        self.assertEqual(command.count("kubectl apply"), 1, command)
        script = command.replace("kubectl apply", "printenv KUBECONFIG")
        with tempfile.TemporaryDirectory() as cwd:
            os.mkdir(os.path.join(cwd, "sub"))
            values = self._run_shells(script, cwd)
        return {shell: (out.splitlines() or [""])[-1] for shell, out in values.items()}

    def test_heredoc_forms_expand_in_both_shells(self):
        """HEREDOC_EXPANDED の代入は、bash 3.2 と zsh -f の両方が HOME に展開する。"""
        for command in HEREDOC_EXPANDED:
            with self.subTest(command=command):
                values = self._heredoc_values(command)
                self.assertEqual(set(values.values()), {"/H/a"}, values)

    def test_wrapper_argument_after_a_heredoc_splits_the_shells(self):
        """heredoc の後ろの段でも、wrapper の後ろの代入の形の引数は bash だけが展開する (展開しない側に倒す)。"""
        values = self._heredoc_values("cat <<EOF && env KUBECONFIG=~/a kubectl apply\nx\nEOF")
        self.assertEqual(len(set(values.values())), 2, values)
        self.assertIn("~/a", values.values())

    def test_expanded_values_match_both_shells(self):
        for raw, want in EXPANDED:
            with self.subTest(raw=raw):
                values = self._shell_values(raw)
                self.assertEqual(set(values.values()), {want}, values)

    def test_kept_values_are_not_expanded_by_both_shells(self):
        for raw, kept in KEPT:
            with self.subTest(raw=raw):
                values = self._shell_values(raw)
                # 両方のシェルが同じ HOME の展開をする形は表に置かない (置くなら EXPANDED)
                home_both = all(v.startswith(_HOME) for v in values.values()) and len(set(values.values())) == 1
                self.assertFalse(home_both, values)


# 偽のシェル経由 wrapper (中身を /bin/sh -c で走らせる)。偽の sudo は `-i` / `-s` のとき HOME を対象ユーザーの
# HOME の代わり (/R) にして走らせる (wrapper が起動するシェルは別の HOME で `~` を読む)。
_FAKE_WRAPPERS = {
    "watch": '#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; [ "$1" -eq "$1" ] 2>/dev/null && shift; done\n'
             'exec /bin/sh -c "$*"\n',
    "npx": '#!/bin/sh\n[ "$1" = "-c" ] || exit 2\nexec /bin/sh -c "$2"\n',
    "sudo": '#!/bin/sh\nshell=\nwhile [ "${1#-}" != "$1" ]; do case "$1" in -i|-s) shell=1;; esac; shift; done\n'
            'if [ -n "$shell" ]; then HOME=/R exec /bin/sh -c "$*"; fi\nexec "$@"\n',
    # `kubectl apply` の代わり。受け取った KUBECONFIG を印付きで出す (本文を出す段と混ざらない)
    "kcprobe": '#!/bin/sh\nprintf "KC=%s\\n" "$KUBECONFIG"\n',
}


@unittest.skipUnless(len(_SHELLS) == 2, "bash と zsh の両方が要る")
class ReservedTimeAndWrapperHeredocShellTests(unittest.TestCase):
    """TIME_* と WRAPPER_HEREDOC_* の各行を bash 3.2 と zsh -f で走らせ、`kubectl apply` が受け取る
    KUBECONFIG を取る (偽の `kcprobe` に替える。CLI が走らなければ空)。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "fakebin"
        self.bin.mkdir()
        for name, text in _FAKE_WRAPPERS.items():
            p = self.bin / name
            p.write_text(text, encoding="utf-8")
            p.chmod(0o755)
        (self.tmp / "sub").mkdir()

    def _values(self, command):
        self.assertEqual(command.count("kubectl apply"), 1, command)
        script = command.replace("kubectl apply", "kcprobe")
        values = {}
        for shell in _SHELLS:
            args = [shell, "-f", "-c"] if shell.endswith("zsh") else [shell, "-c"]
            done = subprocess.run(
                [*args, script],
                env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": _HOME},
                capture_output=True, text=True, cwd=self.tmp, stdin=subprocess.DEVNULL, timeout=30,
            )
            got = [line[3:] for line in done.stdout.splitlines() if line.startswith("KC=")]
            self.assertLessEqual(len(got), 1, (shell, done.stdout))
            values["zsh" if shell.endswith("zsh") else "bash"] = got[0] if got else ""
        return values

    def test_expanded_forms_match_both_shells(self):
        for command in TIME_EXPANDED + WRAPPER_HEREDOC_EXPANDED:
            with self.subTest(command=command):
                self.assertEqual(self._values(command), {"bash": "/H/a", "zsh": "/H/a"})

    def test_zsh_only_reserved_positions(self):
        """zsh だけが予約語と読む位置: zsh は HOME に展開して走らせ、bash 3.2 では CLI が走らない。"""
        for command in TIME_ZSH_ONLY:
            with self.subTest(command=command):
                self.assertEqual(self._values(command), {"bash": "", "zsh": "/H/a"})

    def test_kept_forms_are_not_home_in_zsh(self):
        """展開しない形は、両方のシェルが HOME に展開して走らせる形ではなく、zsh (Bash tool のシェル) でも
        HOME の展開で走らない (CLI が走らないか、生の `~/a` を受け取る)。"""
        for command in TIME_KEPT + WRAPPER_HEREDOC_SPLIT:
            with self.subTest(command=command):
                values = self._values(command)
                self.assertNotEqual(values, {"bash": "/H/a", "zsh": "/H/a"})
                self.assertNotEqual(values["zsh"], "/H/a", values)


class KubectlHeredocAssignmentDispatchTests(BaseWithTmpProject):
    """heredoc の後ろの段の `KUBECONFIG=~/k.yaml kubectl ...` を、HOME の下の kubeconfig で照合する (v0.30.2)。

    0.30.1 は作業ディレクトリの `./~/k.yaml` を読んでいた (無ければ current-context を読めずに deny。
    締めすぎ側)。HOME の下と `./~/` の下の両方に kubeconfig を置き、どちらを読んだかで判定が分かれるようにする。
    """

    EXPANDED_FORMS = HEREDOC_EXPANDED
    KEPT_FORMS = HEREDOC_KEPT

    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp) / "home"
        (self.home / ".kube").mkdir(parents=True)
        (self.project_dir / "~").mkdir()
        (self.project_dir / "sub" / "~").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("KUBECONFIG", None)
        (self.home / ".kube" / "config").write_text("default-ctx\n", encoding="utf-8")
        self._write_accounts({"kubectl": "ctx"})
        self.fake = _FakeKubectl()

    def _layout(self, home_ctx, literal_ctx):
        (self.home / "k.yaml").write_text(home_ctx + "\n", encoding="utf-8")
        for d in (self.project_dir / "~", self.project_dir / "sub" / "~"):
            (d / "k.yaml").write_text(literal_ctx + "\n", encoding="utf-8")

    def _run(self, command):
        try:
            with self.isolated_cache(), mock.patch("subprocess.run", side_effect=self.fake):
                result = dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        if result is None:
            return "allow"
        return result["hookSpecificOutput"].get("permissionDecision", "allow")

    @staticmethod
    def _commands(forms):
        out = []
        for form in forms:
            command = form.replace("KUBECONFIG=~/a", "KUBECONFIG=~/k.yaml")
            assert command.count("KUBECONFIG=~/k.yaml") == 1, form
            out.append(command)
        return out

    def test_dod_home_kubeconfig_is_read(self):
        self._layout("ctx", "other")
        for command in self._commands(self.EXPANDED_FORMS):
            with self.subTest(command=command):
                self.assertEqual(self._run(command), "allow")
        envs = {kw["env"].get("KUBECONFIG") for _argv, kw in self.fake.calls}
        self.assertEqual(envs, {str(self.home / "k.yaml")})

    def test_literal_tilde_directory_is_not_used_for_expanded_forms(self):
        """`./~/k.yaml` が期待値でも、kubectl が読む HOME の下が違えば止める (0.30.1 は allow)。"""
        self._layout("other", "ctx")
        for command in self._commands(self.EXPANDED_FORMS):
            with self.subTest(command=command):
                self.assertEqual(self._run(command), "deny")

    def test_kept_forms_read_the_literal_path(self):
        """展開しない形は 0.30.1 と同じ読み (作業ディレクトリの `./~/k.yaml`)。"""
        self._layout("other", "ctx")
        for command in self._commands(self.KEPT_FORMS):
            with self.subTest(command=command):
                self.assertEqual(self._run(command), "allow")


class ReservedTimeAndWrapperHeredocDispatchTests(KubectlHeredocAssignmentDispatchTests):
    """予約語の `time` の後ろと、シェル経由 wrapper を先頭に置いた heredoc の行の後ろの段 (v0.30.4)。

    0.30.3 は作業ディレクトリの `./~/k.yaml` を読んでいた。`./~/k.yaml` が無ければ誤 deny、期待値どおりの
    `./~/k.yaml` があれば、kubectl が読む HOME の下が違っても通していた (誤 allow)。
    """

    EXPANDED_FORMS = [
        c for c in TIME_EXPANDED + TIME_ZSH_ONLY + WRAPPER_HEREDOC_EXPANDED if "~/b" not in c
    ]
    # `sudo -i ... KUBECONFIG=~/a kubectl` は sudo が代入を捨てる (env scrub) ので表に置かない
    KEPT_FORMS = TIME_KEPT + [c for c in WRAPPER_HEREDOC_SPLIT if "sudo" not in c]


if __name__ == "__main__":
    unittest.main()
