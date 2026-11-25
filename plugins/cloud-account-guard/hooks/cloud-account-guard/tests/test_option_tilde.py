"""context option の値の `~` を、シェルと同じく HOME に展開して照合する (v0.29.6)。

0.29.5 までは `kubectl --kubeconfig ~/.kube/prod.yaml apply` / `firebase deploy -c ~/x/firebase.json` の
`~/...` を生のまま読み、コマンドの走るディレクトリからの相対パス (`./~/.kube/prod.yaml`) として
照合していた。`~/...` が独立した語なら、シェルは HOME に展開して CLI に渡す。照合は存在しない
ファイルを見て誤 deny し、`./~/...` が実在すれば CLI が読まないファイルで照合していた。

展開するのは bash 3.2 と zsh -f で読みが一致する形だけ (`ShellAgreementTests` が両方のシェルで
実際に `printf` して確かめる)。展開しない形は 0.29.5 までと同じ (生の値):

- 引用符の中・語の途中 (`--kubeconfig=~/a` / `-c~/a`) の `~` (両方のシェルが展開しない)
- `~"/a"` / `~:x` / `~-` のように bash と zsh で読みが割れる形、`~user` / `~+`
- HOME を変えうるコマンド (`HOME=/y kubectl ...` / `source` / `eval`。`tilde_home`)
- シェル経由の wrapper の中身 (`watch '...'` / `sudo -i ...` / `npx -c '...'`) と、それを分割し直した段
  (引用符で包んだ中身は wrapper が起動するシェルが読み直す。包まずに並べた形は Bash のシェルが先に
  展開するが、剥がした後の文字列から区別できないので展開しない側に倒す)。heredoc を置いた行で、区切りが
  wrapper の引用符の外にある後ろの段 (`watch 'cat' <<EOF && kubectl ...`) は外のシェルが読むので展開する
  (v0.30.4。WRAPPER_HEREDOC_EXPANDED)
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import (  # noqa: F401
    BaseWithTmpProject,
    _FirebaseConfigFixture,
    setUpModule,
    tearDownModule,
)
from test_kubectl_workdir import _FakeKubectl

from core import command_parser, dispatcher  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from services import firebase, kubectl  # noqa: E402

_HOME = "/H"
_SHELLS = [s for s in ("/bin/bash", shutil.which("zsh")) if s and os.path.exists(s)]

# (option の値の書き方, 照合に使う値)。右の注記は bash 3.2 / zsh -f の `printf '<%s>' --kubeconfig <値>`。
EXPANDED = [
    ("~/a", "/H/a"),
    ("~", "/H"),
    ("~/", "/H/"),
    ("~/a:~/b", "/H/a:~/b"),  # 語の先頭だけ (代入と違い `:` の後は展開しない)
    ("~/a#x", "/H/a#x"),
    ("~/a'b c'", "/H/ab c"),
    ('~/a"b"', "/H/ab"),
    ("~/a\\ b", "/H/a b"),
    ("~//a", "/H//a"),
]
KEPT = [
    # 両方のシェルが展開しない
    ("'~/a'", "~/a"),
    ('"~/a"', "~/a"),
    ("\\~/a", "~/a"),
    ("'a ~/b'", "a ~/b"),  # 引用符の中の空白の後ろは語の先頭ではない
    ('"a ~/b"', "a ~/b"),
    ('"a\\" ~/b"', 'a" ~/b'),
    ("\\ ~/a", " ~/a"),  # エスケープした空白も語を切らない
    ("a~/b", "a~/b"),
    ("./~/a", "./~/a"),
    ("~#", "~#"),
    ("~=", "~="),
    ("~~", "~~"),
    ("x=~/a", "x=~/a"),
    # bash と zsh で読みが割れる
    ('~"/a"', "~/a"),
    ("~''/a", "~/a"),
    ("~\\/a", "~/a"),
    ("~:x", "~:x"),
    ("~-", "~-"),
    # HOME 以外の行き先
    ("~root/a", "~root/a"),
    ("~+", "~+"),
]
# `=` で値を付けた形と短い option の連結形。どちらのシェルも `~` を展開しない
JOINED = [
    ("kubectl --kubeconfig=~/a apply", kubectl, {"kubeconfig": "~/a"}),
    ("firebase deploy --config=~/a", firebase, {"config": "~/a"}),
    ("firebase deploy -c~/a", firebase, {"config": "~/a"}),
    ("firebase deploy -c=~/a", firebase, {"config": "~/a"}),
]


# heredoc を置いた行の先頭の段がシェル経由の wrapper で、区切りが wrapper の引用符の外にある行の後ろの段
# (v0.30.4)。後ろの段は外のシェルが読む語なので展開する (0.30.3 までは wrapper の中身と読み展開しなかった)。
# 各行は `WrapperHeredocShellTests` が bash 3.2 と zsh -f で走らせて確かめる。
WRAPPER_HEREDOC_EXPANDED = [
    "watch 'cat' <<EOF && kubectl --kubeconfig ~/a apply\nx\nEOF",
    "watch cat <<'EOF' | kubectl --kubeconfig ~/a apply -f -\ndon't (x) # y\nEOF",
    "watch -n 1 'cat' <<\\EOF; kubectl --kubeconfig ~/a apply\nx\nEOF",
    "sudo -i cat <<EOF && kubectl --kubeconfig ~/a apply\nx\nEOF",
    "sudo -s 'cat' <<-EOF & kubectl --kubeconfig ~/a apply\n\tx\n\tEOF",
    "npx -c 'cat' <<EOF && kubectl --kubeconfig ~/a apply\nx\nEOF",
    "npx -c 'true && cat' <<'END MSG' ; kubectl --kubeconfig ~/a apply\nx\nEND MSG",
    "watch 'cat' <<EOF && time kubectl --kubeconfig ~/a apply\nx\nEOF",
    "watch 'cat' <<EOF && (kubectl --kubeconfig ~/a apply)\nx\nEOF",
]
# 後ろの段自身がシェル経由の wrapper の形は従来どおり展開しない (引用符で包んだ中身は wrapper が起動するシェルが
# 読み直す。包まずに並べた形は剥がした後の文字列から区別できないので、展開しない側に倒す)
WRAPPER_HEREDOC_KEPT = [
    "watch 'cat' <<EOF && watch 'kubectl --kubeconfig ~/a apply'\nx\nEOF",
    "sudo -i cat <<EOF && sudo -i 'kubectl --kubeconfig ~/a apply'\nx\nEOF",
    "sudo -i cat <<EOF && sudo -i kubectl --kubeconfig ~/a apply\nx\nEOF",
]


def _ctx(candidate, svc=kubectl, home=_HOME):
    try:
        return dispatcher._context_override(candidate, svc, home)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"例外: {e!r}") from e


class ExpansionTableTests(unittest.TestCase):
    def test_expanded_forms(self):
        for raw, want in EXPANDED:
            for shape in ("kubectl --kubeconfig {} apply", "kubectl apply -k . --kubeconfig {}"):
                with self.subTest(raw=raw, shape=shape):
                    self.assertEqual(_ctx(shape.format(raw)), {"kubeconfig": want})
            for shape in ("firebase deploy --config {}", "firebase -c {} deploy"):
                with self.subTest(raw=raw, shape=shape):
                    self.assertEqual(_ctx(shape.format(raw), firebase), {"config": want})

    def test_kept_forms(self):
        for raw, want in KEPT:
            with self.subTest(raw=raw):
                self.assertEqual(_ctx(f"kubectl --kubeconfig {raw} apply"), {"kubeconfig": want})
                self.assertEqual(_ctx(f"firebase -c {raw} deploy", firebase), {"config": want})

    def test_joined_forms_are_kept(self):
        for command, svc, want in JOINED:
            with self.subTest(command=command):
                self.assertEqual(_ctx(command, svc), want)

    def test_without_home_nothing_is_expanded(self):
        for raw, _want in EXPANDED:
            with self.subTest(raw=raw):
                self.assertEqual(
                    _ctx(f"kubectl --kubeconfig {raw} apply", home=None),
                    {"kubeconfig": shlex.split(raw)[0]},
                )

    def test_whitespace_kinds_start_a_word(self):
        for command in ("kubectl --kubeconfig\t~/a apply", "kubectl --kubeconfig\n~/a apply",
                        "kubectl --kubeconfig  ~/a apply", "kubectl --kubeconfig ~/a\tapply"):
            with self.subTest(command=command):
                self.assertEqual(_ctx(command), {"kubeconfig": "/H/a"})

    def test_quoted_words_before_the_value_keep_the_word_boundaries(self):
        """引用符の中の空白の後ろは語の先頭ではない。閉じた引用符の後ろの語は展開する。"""
        self.assertEqual(_ctx("kubectl -l 'a ~/b' --kubeconfig ~/a apply"), {"kubeconfig": "/H/a"})
        self.assertEqual(_ctx('kubectl -l "a ~/b" --kubeconfig ~/a apply'), {"kubeconfig": "/H/a"})
        self.assertEqual(_ctx('kubectl -l "a\\" ~/b" --kubeconfig ~/a apply'), {"kubeconfig": "/H/a"})
        self.assertEqual(_ctx("kubectl -l a\\ b --kubeconfig ~/a apply"), {"kubeconfig": "/H/a"})
        # エスケープした空白は語を切らない (`a --kubeconfig` が 1 語。0.29.5 と同じ)
        self.assertEqual(_ctx("kubectl -l a\\ --kubeconfig ~/a apply"), {})
        self.assertEqual(_ctx("kubectl --kubeconfig \\ ~/a apply"), {"kubeconfig": " ~/a"})
        # `--kubeconfig` が引用符の中なら option ではない (0.29.5 と同じ)
        self.assertEqual(_ctx("kubectl -l 'x --kubeconfig ~/a' apply"), {})

    def test_ansi_c_quote_is_left_alone(self):
        """`$'...'` は分割器と読みが割れるので、候補ごと展開しない (0.29.5 と同じ値)。"""
        for command in ("kubectl -l $'x' --kubeconfig ~/a apply", "kubectl --kubeconfig ~/a -l $'x' apply"):
            with self.subTest(command=command):
                self.assertEqual(_ctx(command), {"kubeconfig": "~/a"})

    def test_unclosed_quote_is_left_alone(self):
        for candidate in ("kubectl --kubeconfig ~/a 'x", 'kubectl --kubeconfig ~/a "x', 'kubectl --kubeconfig ~/a "x\\"'):
            with self.subTest(candidate=candidate):
                self.assertEqual(command_parser.expand_word_tilde(candidate, "/H"), candidate)

    def test_home_with_shell_characters_is_inserted_literally(self):
        for home in ("/h h", "/h'q", '/h"d', "/h;x", "/h\\b", "/h#c"):
            with self.subTest(home=home):
                self.assertEqual(
                    _ctx("kubectl --kubeconfig ~/a apply -f x", home=home), {"kubeconfig": f"{home}/a"}
                )
                expanded = command_parser.expand_word_tilde("kubectl --kubeconfig ~/a apply -f x", home)
                self.assertEqual(len(shlex.split(expanded)), 6)

    def test_other_context_options_take_the_same_path(self):
        """値の解釈はしない (シェルが渡す値で照合する)。"""
        from services import aws, gcloud

        self.assertEqual(_ctx("aws --profile ~ s3 rm s3://b/k", aws), {"profile": "/H"})
        self.assertEqual(_ctx("gcloud run deploy --project ~/p", gcloud), {"project": "/H/p"})


@unittest.skipUnless(len(_SHELLS) == 2, "bash と zsh の両方が要る")
class ShellAgreementTests(unittest.TestCase):
    """表の各行を bash 3.2 と zsh -f で走らせ、展開した値は両方のシェルが渡す値と一致し、
    展開しない値は両方のシェルが同じ HOME の展開をする形ではないことを確かめる。"""

    def _shell_values(self, words):
        values = {}
        for shell in _SHELLS:
            args = [shell, "-f", "-c"] if shell.endswith("zsh") else [shell, "-c"]
            done = subprocess.run(
                [*args, f"printf '%s\\n' {words}"],
                env={"PATH": "/usr/bin:/bin", "HOME": _HOME},
                capture_output=True,
                text=True,
                cwd="/",
            )
            values[shell] = done.stdout.rstrip("\n").split("\n")[-1]  # 最後の語
        return values

    def test_expanded_values_match_both_shells(self):
        for raw, want in EXPANDED:
            with self.subTest(raw=raw):
                values = self._shell_values(f"--kubeconfig {raw}")
                self.assertEqual(set(values.values()), {want}, values)

    def test_kept_values_are_not_expanded_by_both_shells(self):
        for raw, _kept in KEPT:
            with self.subTest(raw=raw):
                values = self._shell_values(f"--kubeconfig {raw}")
                home_both = all(v.startswith(_HOME) for v in values.values()) and len(set(values.values())) == 1
                self.assertFalse(home_both, values)

    def test_joined_values_are_literal_in_both_shells(self):
        for command, _svc, _want in JOINED:
            word = command.split()[-2] if command.endswith("apply") else command.split()[-1]
            with self.subTest(word=word):
                values = self._shell_values(word)
                self.assertEqual(set(values.values()), {word}, values)


@unittest.skipUnless(len(_SHELLS) == 2, "bash と zsh の両方が要る")
class WrapperHeredocShellTests(unittest.TestCase):
    """WRAPPER_HEREDOC_* の各行を bash 3.2 と zsh -f で走らせ、`kubectl` が受け取る `--kubeconfig` の値を取る。

    偽の kubectl は引数を印付きで出す。偽の watch / npx は中身を /bin/sh -c で走らせ、偽の sudo は `-i` / `-s`
    のとき HOME を対象ユーザーの HOME の代わり (/R) にして走らせる。
    """

    _FAKES = {
        "kubectl": '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = --kubeconfig ] && printf "KC=%s\\n" "$2"; shift; done\n',
        "watch": '#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; [ "$1" -eq "$1" ] 2>/dev/null && shift; done\n'
                 'exec /bin/sh -c "$*"\n',
        "npx": '#!/bin/sh\n[ "$1" = "-c" ] || exit 2\nexec /bin/sh -c "$2"\n',
        "sudo": '#!/bin/sh\nshell=\nwhile [ "${1#-}" != "$1" ]; do case "$1" in -i|-s) shell=1;; esac; shift; done\n'
                'if [ -n "$shell" ]; then HOME=/R exec /bin/sh -c "$*"; fi\nexec "$@"\n',
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "fakebin"
        self.bin.mkdir()
        for name, text in self._FAKES.items():
            p = self.bin / name
            p.write_text(text, encoding="utf-8")
            p.chmod(0o755)

    def _values(self, command):
        values = {}
        for shell in _SHELLS:
            args = [shell, "-f", "-c"] if shell.endswith("zsh") else [shell, "-c"]
            done = subprocess.run(
                [*args, command],
                env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": _HOME},
                capture_output=True, text=True, cwd=self.tmp, stdin=subprocess.DEVNULL, timeout=30,
            )
            got = [line[3:] for line in done.stdout.splitlines() if line.startswith("KC=")]
            self.assertLessEqual(len(got), 1, (shell, done.stdout))
            values["zsh" if shell.endswith("zsh") else "bash"] = got[0] if got else ""
        return values

    def test_expanded_forms_match_both_shells(self):
        for command in WRAPPER_HEREDOC_EXPANDED:
            with self.subTest(command=command):
                self.assertEqual(self._values(command), {"bash": "/H/a", "zsh": "/H/a"})

    def test_kept_forms_are_read_by_another_shell(self):
        """後ろの段自身がシェル経由 wrapper の形は、wrapper が起動するシェルが読む (`sudo -i` は別の HOME で読む)。"""
        self.assertEqual(self._values(WRAPPER_HEREDOC_KEPT[1]), {"bash": "/R/a", "zsh": "/R/a"})


def _contexts(command, home=_HOME):
    """dispatcher の分解 (`_analyze_command`) が target ごとに読んだ context の一覧。"""
    with mock.patch.dict(os.environ, {"HOME": home}):
        try:
            targets, _switching, _segments = dispatcher._analyze_command(command, None)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"例外: {e!r}") from e
    return [ctx for _svc, _cands, _env, ctx, _tier in targets]


class CommandLevelTests(unittest.TestCase):
    def test_shell_level_positions_expand(self):
        for command in (
            "kubectl --kubeconfig ~/a apply",
            "sudo kubectl --kubeconfig ~/a apply",
            "sudo -H kubectl --kubeconfig ~/a apply",
            "sudo -u root kubectl --kubeconfig ~/a apply",
            "sudo -- kubectl --kubeconfig ~/a apply",
            "time kubectl --kubeconfig ~/a apply",
            "nohup kubectl --kubeconfig ~/a apply",
            "env kubectl --kubeconfig ~/a apply",
            "command -- kubectl --kubeconfig ~/a apply",
            "timeout 5 kubectl --kubeconfig ~/a apply",
            "xargs kubectl --kubeconfig ~/a apply",
            "watch -x kubectl --kubeconfig ~/a apply",
            "mise exec -- kubectl --kubeconfig ~/a apply",
            "cd sub && kubectl --kubeconfig ~/a apply",
            "true | kubectl --kubeconfig ~/a apply",
            "( kubectl --kubeconfig ~/a apply )",
            "X=1 kubectl --kubeconfig ~/a apply",
            "KUBECONFIG=~/b kubectl --kubeconfig ~/a apply",
            "kubectl --kubeconfig ~/a apply -k .",
            "kubectl --kubeconfig ~/a apply -f ${HOME}/x.yaml",
            "kubectl --kubeconfig ~/a apply # c",
            # heredoc を置いた行の後ろの段も Bash のシェルが読む語 (本文を落とした行を分割し直した段)
            "cat <<EOF && kubectl --kubeconfig ~/a apply\nbody\nEOF",
            "cat <<EOF; kubectl --kubeconfig ~/a apply\nbody\nEOF",
            "cat <<'EOF' | kubectl --kubeconfig ~/a apply -f -\nx\nEOF",
            "cat <<EOF | kubectl --kubeconfig ~/a apply -f -\nx\nEOF",
            "X=1 cat <<EOF && kubectl --kubeconfig ~/a apply\nx\nEOF",
            "cat <<EOF && sudo kubectl --kubeconfig ~/a apply\nx\nEOF",
            "cat <<EOF && cd sub && kubectl --kubeconfig ~/a apply\nx\nEOF",
            *WRAPPER_HEREDOC_EXPANDED,
        ):
            with self.subTest(command=command):
                self.assertEqual(_contexts(command), [{"kubeconfig": "/H/a"}])

    def test_heredoc_line_led_by_a_shell_wrapper_mixes_both_readings(self):
        """wrapper の中身の段 (展開しない) と、引用符の外の区切りの後ろの段 (展開する) が同じ行にある (v0.30.4)。"""
        command = (
            "npx -c 'true && kubectl --kubeconfig ~/b get po' <<EOF ; kubectl --kubeconfig ~/a apply\nx\nEOF"
        )
        self.assertEqual(_contexts(command), [{"kubeconfig": "~/b"}, {"kubeconfig": "/H/a"}])
        self.assertEqual(
            command_parser.shell_string_candidates(command),
            frozenset({"true", "kubectl --kubeconfig ~/b get po"}),
        )

    def test_after_a_heredoc_on_the_same_line(self):
        """heredoc の宣言を持つ段の後ろの CLI (`kubectl apply -f - <<EOF && kubectl ...`) も展開する。"""
        self.assertEqual(
            _contexts("kubectl apply -f - <<EOF && kubectl --kubeconfig ~/a get po\nx\nEOF"),
            [{}, {"kubeconfig": "/H/a"}],
        )
        self.assertEqual(
            _contexts("kubectl apply -f - <<EOF && kubectl --kubeconfig ~/a delete po x\nx\nEOF"),
            [{}, {"kubeconfig": "/H/a"}],
        )
        self.assertEqual(
            _contexts("kubectl --kubeconfig ~/a apply -f - <<EOF && true\nx\nEOF"),
            [{"kubeconfig": "/H/a"}],
        )

    def test_unsure_commands_keep_the_raw_value(self):
        for command in (
            "HOME=/y kubectl --kubeconfig ~/a apply",
            "HOME=/y; kubectl --kubeconfig ~/a apply",
            "export HOME=/y && kubectl --kubeconfig ~/a apply",
            ". ./env.sh; kubectl --kubeconfig ~/a apply",
            "source env.sh; kubectl --kubeconfig ~/a apply",
            "eval x; kubectl --kubeconfig ~/a apply",
            # シェル経由の wrapper の中身。引用符で包んだ中身は wrapper が起動するシェルが読み直す。
            # 包まずに並べた形 (`watch kubectl ...` / `sudo -i kubectl ...`) は Bash のシェルが先に展開するが、
            # 剥がした後の文字列から包んだ形と区別できないので展開しない側に倒す (0.29.5 と同じ読み)
            "watch 'kubectl --kubeconfig ~/a apply'",
            "watch -n 1 'kubectl --kubeconfig ~/a apply'",
            "watch kubectl --kubeconfig ~/a apply",
            "sudo -i 'kubectl --kubeconfig ~/a apply'",
            "sudo -i kubectl --kubeconfig ~/a apply",
            "sudo -s kubectl --kubeconfig ~/a apply",
            "npx -c 'kubectl --kubeconfig ~/a apply'",
            "watch 'true && kubectl --kubeconfig ~/a apply'",
            # 同じ文字列の候補が wrapper の中身にもある
            "watch 'kubectl --kubeconfig ~/a apply'; kubectl --kubeconfig ~/a apply",
            # heredoc の後ろの段でも、シェル経由の wrapper の中身と HOME を変えうるコマンドは展開しない
            "cat <<EOF && watch 'kubectl --kubeconfig ~/a apply'\nx\nEOF",
            "cat <<EOF && sudo -i kubectl --kubeconfig ~/a apply\nx\nEOF",
            "cat <<EOF && sudo -s 'kubectl --kubeconfig ~/a apply'\nx\nEOF",
            "cat <<EOF && npx -c 'kubectl --kubeconfig ~/a apply'\nx\nEOF",
            "cat <<EOF && HOME=/y kubectl --kubeconfig ~/a apply\nx\nEOF",
            "cat <<EOF\nHOME=/y\nEOF\nkubectl --kubeconfig ~/a apply",
            # wrapper の中身の複合コマンド (外側が heredoc を含んでも見送り)
            "sudo -s 'true; kubectl --kubeconfig ~/a apply'",
            "sudo -i 'true; kubectl --kubeconfig ~/a apply'",
            "npx -c 'true && kubectl --kubeconfig ~/a apply'",
            *WRAPPER_HEREDOC_KEPT,
        ):
            with self.subTest(command=command):
                self.assertEqual(_contexts(command), [{"kubeconfig": "~/a"}])

    def test_no_usable_home_keeps_the_raw_value(self):
        for home in ("", "/a$b"):
            with self.subTest(home=home):
                self.assertEqual(
                    _contexts("kubectl --kubeconfig ~/a apply", home=home), [{"kubeconfig": "~/a"}]
                )

    def test_shell_string_candidates(self):
        self.assertEqual(
            command_parser.shell_string_candidates("watch 'kubectl get po' && kubectl apply"),
            frozenset({"kubectl get po"}),
        )
        self.assertEqual(
            command_parser.shell_string_candidates("watch 'true && kubectl apply'"),
            frozenset({"true", "kubectl apply"}),
        )
        self.assertEqual(command_parser.shell_string_candidates("sudo kubectl apply"), frozenset())
        # heredoc の本文を落とした行を分割し直した段は Bash のシェルの語 (段自身が wrapper の形なら含める)
        self.assertEqual(
            command_parser.shell_string_candidates(
                "kubectl apply -f - <<EOF && kubectl --kubeconfig ~/a get po\nx\nEOF"
            ),
            frozenset(),
        )
        self.assertEqual(
            command_parser.shell_string_candidates("cat <<EOF && watch 'kubectl get po'\nx\nEOF"),
            frozenset({"kubectl get po"}),
        )
        # 先頭の段がシェル経由 wrapper の行の、引用符の外の区切りの後ろの段は Bash のシェルの語 (v0.30.4)
        self.assertEqual(
            command_parser.shell_string_candidates("watch 'cat' <<EOF && true && kubectl get po\nx\nEOF"),
            frozenset({"'cat'"}),
        )
        self.assertEqual(
            command_parser.shell_string_candidates("sudo -i cat <<EOF | kubectl apply -f -\nx\nEOF"),
            frozenset({"cat"}),
        )
        self.assertIsNone(command_parser._SHELL_STRING_SEEN)

    def test_candidates_do_not_change(self):
        """展開は context の値だけを変え、候補 (コマンドの断片) は変えない。"""
        for raw, _want in EXPANDED + KEPT:
            command = f"kubectl --kubeconfig {raw} apply && firebase -c {raw} deploy"
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"HOME": _HOME}):
                    with_home = command_parser.extract_candidates(command)
                    os.environ.pop("HOME")
                    without = command_parser.extract_candidates(command)
                self.assertEqual(with_home, without)


# -- dispatcher を通した照合 ---------------------------------------------------------
def _decision(result) -> tuple[str, str]:
    if result is None:
        return "allow", ""
    out = result["hookSpecificOutput"]
    return out.get("permissionDecision", "allow"), (
        out.get("permissionDecisionReason") or out.get("additionalContext") or ""
    )


class KubectlTildeDispatchTests(BaseWithTmpProject):
    """HOME の下と、作業ディレクトリの `./~/` の下の両方に同名の kubeconfig を置く。"""

    _EXPANDED = (
        "kubectl --kubeconfig ~/k.yaml apply -f x.yaml",
        "kubectl apply -f x.yaml --kubeconfig ~/k.yaml",
        "sudo kubectl --kubeconfig ~/k.yaml apply -f x.yaml",
        "cd sub && kubectl --kubeconfig ~/k.yaml apply -f x.yaml",
    )
    _KEPT = (
        "kubectl --kubeconfig '~/k.yaml' apply -f x.yaml",
        "kubectl --kubeconfig=~/k.yaml apply -f x.yaml",
        "HOME=/y kubectl --kubeconfig ~/k.yaml apply -f x.yaml",
        "watch 'kubectl --kubeconfig ~/k.yaml apply -f x.yaml'",
        "sudo -i kubectl --kubeconfig ~/k.yaml apply -f x.yaml",
    )

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
                return _decision(dispatch(command, str(self.project_dir)))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_dod_home_kubeconfig_is_read(self):
        """DoD: `--kubeconfig ~/k.yaml` は HOME の下のファイルで照合する (0.29.5 は `./~/k.yaml` で deny)。"""
        self._layout("ctx", "other")
        for command in self._EXPANDED:
            with self.subTest(command=command):
                decision, reason = self._run(command)
                self.assertEqual(decision, "allow", f"{command}\n{reason}")
        # 照合の kubectl に渡したのは HOME の下の絶対パス
        paths = {argv[argv.index("--kubeconfig") + 1] for argv, _kw in self.fake.calls}
        self.assertEqual(paths, {str(self.home / "k.yaml")})

    def test_literal_tilde_directory_is_not_used_for_expanded_forms(self):
        """`./~/k.yaml` が期待値でも、CLI が読む HOME の下が違えば止める (0.29.5 は allow)。"""
        self._layout("other", "ctx")
        for command in self._EXPANDED:
            with self.subTest(command=command):
                decision, reason = self._run(command)
                self.assertEqual(decision, "deny", command)
                self.assertIn("other", reason)

    def test_kept_forms_read_the_literal_path(self):
        """展開しない形は 0.29.5 と同じ読み (作業ディレクトリの `./~/k.yaml` か、決められず止める)。"""
        self._layout("ctx", "other")
        for command in self._KEPT:
            with self.subTest(command=command):
                decision, _reason = self._run(command)
                self.assertEqual(decision, "deny", command)

    def test_guide_is_the_home_path_and_round_trips(self):
        """deny の案内は HOME の下の絶対パス。加工せずに通し、書込を再現すると元のコマンドが通る。"""
        self._layout("other", "other")
        command = "cd sub && kubectl --kubeconfig ~/k.yaml apply -f x.yaml"
        decision, reason = self._run(command)
        self.assertEqual(decision, "deny")
        want = f"kubectl config use-context ctx --kubeconfig {shlex.quote(str(self.home / 'k.yaml'))}"
        self.assertIn(want, reason)
        tokens = shlex.split(want, comments=True)
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(dispatch(want, str(self.project_dir)))
        run.assert_not_called()
        Path(tokens[tokens.index("--kubeconfig") + 1]).write_text("ctx\n", encoding="utf-8")
        self.assertEqual(self._run(command)[0], "allow")
        # `./~/` の下は書き換えていない
        self.assertEqual((self.project_dir / "sub" / "~" / "k.yaml").read_text(encoding="utf-8"), "other\n")


class FirebaseTildeDispatchTests(_FirebaseConfigFixture):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp) / "home"
        (self.home / "x").mkdir(parents=True)
        (self.home / "x" / "firebase.json").write_text("{}", encoding="utf-8")
        self.literal = self.project_dir / "~" / "x"
        self.literal.mkdir(parents=True)
        (self.literal / "firebase.json").write_text("{}", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._write_accounts({"firebase": "right-project"})

    _EXPANDED = (
        "firebase deploy --config ~/x/firebase.json",
        "firebase --config ~/x/firebase.json deploy",
        "firebase deploy -c ~/x/firebase.json",
        "cd sub && firebase -c ~/x/firebase.json deploy",
    )
    _KEPT = (
        "firebase deploy --config '~/x/firebase.json'",
        "firebase deploy --config=~/x/firebase.json",
        "firebase deploy -c~/x/firebase.json",
        "npx -c 'firebase deploy -c ~/x/firebase.json'",
    )

    def _check(self, with_cli, command, want):
        with self.subTest(command=command, with_cli=with_cli):
            try:
                decision, reason = _decision(self._dispatch(with_cli, command))
            except Exception as e:  # noqa: BLE001
                self.fail(f"例外: {e!r}")
            self.assertEqual(decision, want, f"{command}\n{reason}")

    def test_dod_home_config_is_read(self):
        self._record({self.home / "x": "right-project", self.literal: "wrong-project",
                      self.project_dir: "wrong-project"})
        for command in self._EXPANDED:
            self._check(True, command, "allow")
        for command in self._KEPT:
            self._check(True, command, "deny")

    def test_literal_tilde_directory_is_not_used_for_expanded_forms(self):
        self._record({self.home / "x": "wrong-project", self.literal: "right-project",
                      self.project_dir: "right-project"})
        for command in self._EXPANDED:
            self._check(True, command, "deny")


if __name__ == "__main__":
    unittest.main()
