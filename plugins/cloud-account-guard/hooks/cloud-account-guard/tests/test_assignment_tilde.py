"""行頭の代入の値の `~` を、シェルと同じく HOME に展開して照合の env に渡す (v0.29.5)。

0.29.4 までは `KUBECONFIG=~/.kube/prod.yaml kubectl apply` の値を `~/.kube/prod.yaml` のまま
照合の env に渡していた。シェルは代入の値の先頭と `:` の後の `~` を展開するので、kubectl は
HOME の下のファイルを読むが、照合は作業ディレクトリの `./~/.kube/prod.yaml` を探して誤 deny に
なっていた。

展開するのは bash 3.2 と zsh -f で読みが一致する形だけ。表の各行は、両方のシェルで実際に
`printenv` して確かめる (`ShellAgreementTests`)。展開しない形は 0.29.4 までと同じ (生の値)。
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
            # wrapper の後ろの代入の形の引数: bash は展開し zsh は展開しない
            "env KUBECONFIG=~/a kubectl apply",
            "sudo KUBECONFIG=~/a kubectl apply",
            "nohup env KUBECONFIG=~/a kubectl apply",
            "time KUBECONFIG=~/a kubectl apply",
            "watch 'KUBECONFIG=~/a kubectl apply && true'",
            # wrapper の中身を分割し直した後ろの段も展開しない (0.29.4 と同じ)
            "watch 'true && KUBECONFIG=~/a kubectl apply'",
            *HEREDOC_KEPT,
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
        for command in commands + HEREDOC_EXPANDED + HEREDOC_KEPT:
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


class KubectlHeredocAssignmentDispatchTests(BaseWithTmpProject):
    """heredoc の後ろの段の `KUBECONFIG=~/k.yaml kubectl ...` を、HOME の下の kubeconfig で照合する (v0.30.2)。

    0.30.1 は作業ディレクトリの `./~/k.yaml` を読んでいた (無ければ current-context を読めずに deny。
    締めすぎ側)。HOME の下と `./~/` の下の両方に kubeconfig を置き、どちらを読んだかで判定が分かれるようにする。
    """

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
        for command in self._commands(HEREDOC_EXPANDED):
            with self.subTest(command=command):
                self.assertEqual(self._run(command), "allow")
        envs = {kw["env"].get("KUBECONFIG") for _argv, kw in self.fake.calls}
        self.assertEqual(envs, {str(self.home / "k.yaml")})

    def test_literal_tilde_directory_is_not_used_for_expanded_forms(self):
        """`./~/k.yaml` が期待値でも、kubectl が読む HOME の下が違えば止める (0.30.1 は allow)。"""
        self._layout("other", "ctx")
        for command in self._commands(HEREDOC_EXPANDED):
            with self.subTest(command=command):
                self.assertEqual(self._run(command), "deny")

    def test_kept_forms_read_the_literal_path(self):
        """展開しない形は 0.30.1 と同じ読み (作業ディレクトリの `./~/k.yaml`)。"""
        self._layout("other", "ctx")
        for command in self._commands(HEREDOC_KEPT):
            with self.subTest(command=command):
                self.assertEqual(self._run(command), "allow")


if __name__ == "__main__":
    unittest.main()
