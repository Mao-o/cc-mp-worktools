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
import unittest
from unittest import mock

import _testutil  # noqa: F401

from core import command_parser  # noqa: E402
from core.command_parser import extract_candidates  # noqa: E402

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
            # 名前の一部の HOME と、読むだけの `$HOME` は HOME を変えない
            "XDG_CONFIG_HOME=~/c KUBECONFIG=~/a kubectl apply",
            "FOO_HOME=x KUBECONFIG=~/a kubectl apply",
            "HOMEBREW_X=1 KUBECONFIG=~/a kubectl apply",
            "echo $HOME && KUBECONFIG=~/a kubectl apply",
        ):
            with self.subTest(command=command):
                envs = [e for c, e in _env(command) if c.startswith("kubectl")]
                self.assertEqual(len(envs), 1, command)
                self.assertEqual(envs[0].get("KUBECONFIG"), "/H/a")

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
            "unset HOME; KUBECONFIG=~/a kubectl apply",
            ". ./env.sh && KUBECONFIG=~/a kubectl apply",
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
        for raw, _want in EXPANDED + KEPT:
            command = f"KUBECONFIG={raw} kubectl apply && aws s3 rm s3://b/k"
            with self.subTest(raw=raw):
                with_home = [c for c, _e in _env(command)]
                with mock.patch.object(command_parser, "tilde_home", return_value=None):
                    without = [c for c, _e in _env(command)]
                self.assertEqual(with_home, without)

    def test_unclosed_quote_is_left_alone(self):
        for raw in ("~/a'b", '~/a"b', '~/a"b\\"'):
            with self.subTest(raw=raw):
                self.assertEqual(command_parser._expand_assignment_tilde(raw, "/H"), raw)


@unittest.skipUnless(_SHELLS, "bash / zsh が無い")
class ShellAgreementTests(unittest.TestCase):
    """表の各行を bash 3.2 と zsh -f で実際に走らせ、展開した値が両方のシェルの値と一致し、
    展開しない値はどちらかのシェルの値と一致する (0.29.4 までの値のまま) ことを確かめる。"""

    def _shell_values(self, raw):
        values = {}
        for shell in _SHELLS:
            args = [shell, "-f", "-c"] if shell.endswith("zsh") else [shell, "-c"]
            done = subprocess.run(
                [*args, f"K={raw} printenv K"],
                env={"PATH": "/usr/bin:/bin", "HOME": _HOME},
                capture_output=True,
                text=True,
                cwd="/",
            )
            values[shell] = done.stdout.rstrip("\n")
        return values

    def test_expanded_values_match_both_shells(self):
        self.assertEqual(len(_SHELLS), 2, "bash と zsh の両方が要る")
        for raw, want in EXPANDED:
            with self.subTest(raw=raw):
                values = self._shell_values(raw)
                self.assertEqual(set(values.values()), {want}, values)

    def test_kept_values_are_not_expanded_by_both_shells(self):
        self.assertEqual(len(_SHELLS), 2, "bash と zsh の両方が要る")
        for raw, kept in KEPT:
            with self.subTest(raw=raw):
                values = self._shell_values(raw)
                # 両方のシェルが同じ HOME の展開をする形は表に置かない (置くなら EXPANDED)
                home_both = all(v.startswith(_HOME) for v in values.values()) and len(set(values.values())) == 1
                self.assertFalse(home_both, values)


if __name__ == "__main__":
    unittest.main()
