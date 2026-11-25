"""heredoc を置いた行の前置きの代入は、行の先頭のコマンドにだけ効く (v0.30.3)。

0.30.2 までは、heredoc の本文を落とした行を分割し直した段に、先頭のコマンドの前置きの代入
(env と、値を読めなかった代入の印) を全部の段に継いでいた。シェルでは前置きの代入はその段の
コマンドにだけ効くので、後ろの段は起動時の値で走る:

    KUBECONFIG=~/k.yaml kubectl apply -f - <<EOF && kubectl delete po x
    AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/k

の後ろの kubectl / aws を、hook は `~/k.yaml` / `prod` で照合して allow にしていた (heredoc の無い
同じ形は起動時の値で照合して deny)。

分割し直す段の由来ごとの扱い (`core/command_parser.py` の `_segment_candidates`):

| 段の由来 | 外側の前置きの代入 |
|---|---|
| heredoc の本文を落とした行 (先頭の段がシェル経由 wrapper でも、区切りが引用符の外なら) | 先頭の段だけ |
| シェル経由 wrapper の中身 (`watch '...'` / `npx -c '...'`) | 全段 (wrapper が起動するシェルに渡る) |
| 前置きの代入を剥がした残りが `;` / `&&` / `||` で始まる (コマンド語の無い代入) | 全段 (0.30.2 と同じ) |
| 前置きの代入を剥がした残りが `|` / `&` で始まる heredoc の行 | どの段にも継がない (サブシェル) |

表の各行は `ShellAgreementTests` が bash 3.2 と zsh -f で全段の env を実際に取って確かめる。
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

from core.command_parser import extract_candidates, unread_env_candidates  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from services import aws, gcloud, github  # noqa: E402

_HOME = "/H"
_SHELLS = [s for s in ("/bin/bash", shutil.which("zsh")) if s and os.path.exists(s)]
_TRACKED = ("AWS_PROFILE", "KUBECONFIG", "GH_CONFIG_DIR", "CLOUDSDK_CONFIG")
# 起動時の値 (シェルの env と、hook が前置きの代入の無い段を照合する env)
_LAUNCH = {
    "AWS_PROFILE": "launch", "KUBECONFIG": "/launch/kc", "GH_CONFIG_DIR": "/launch/gh",
    "CLOUDSDK_CONFIG": "/launch/gc", "P": "fromvar",
}

# 全段の env を両方のシェルで取る行。CLI の出現はどれも引数が違う (シェルの記録を候補に対応させる)。
# 1 つ目のグループは heredoc の本文を落とした行 (先頭の段だけ)、2 つ目は全段に継ぐ由来、3 つ目は
# 値を読めない代入。
SHELL_TABLE = [
    # heredoc の行: 区切り (`|` / `&&` / `;` / `||` / `&`) と区切り語の形
    "AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
    "AWS_PROFILE=prod cat <<EOF && aws s3 rm s3://b/k\nx\nEOF",
    "KUBECONFIG=~/k.yaml kubectl apply -f - <<EOF && kubectl delete po x\nkind: x\nEOF",
    "KUBECONFIG=/k/prod.yaml cat <<'EOF' | kubectl apply -f -\nkind: x\nEOF",
    'GH_CONFIG_DIR=/g gh issue create -F - <<"EOF"; gh pr create --title t\nx\nEOF',
    "CLOUDSDK_CONFIG=~/gc cat <<\\EOF | gcloud run deploy s\nx\nEOF",
    "AWS_PROFILE=prod false <<'END MSG' || aws s3 rm s3://b/or\ndon't (x) # y\nEND MSG",
    "AWS_PROFILE=prod cat <<-EOF & aws s3 rm s3://b/bg\n\tx\n\tEOF\nwait",
    "AWS_PROFILE=prod aws s3 cp - s3://b/first <<'2EOF' && aws s3 rm s3://b/second\nx\n2EOF",
    # 複数の前置き・wrapper を挟んだ先頭の段、後ろの段の自分の代入、3 段
    "X=1 AWS_PROFILE=prod nohup cat <<EOF && KUBECONFIG=~/k2.yaml kubectl get po && aws s3 ls\nx\nEOF",
    "env AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/e\nx\nEOF",
    "AWS_PROFILE=prod KUBECONFIG=~/k.yaml GH_CONFIG_DIR=/g CLOUDSDK_CONFIG=~/gc cat <<EOF"
    " && aws s3 rm s3://b/all && kubectl delete po all && gh pr create --title all"
    " && gcloud run deploy all\nx\nEOF",
    # 行の前置きの構文
    "( AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/p )\nx\nEOF",
    "{ AWS_PROFILE=prod cat <<EOF && aws s3 rm s3://b/b; }\nx\nEOF",
    "if AWS_PROFILE=prod cat <<EOF && aws s3 rm s3://b/i; then true; fi\nx\nEOF",
    "cd sub && AWS_PROFILE=prod cat <<EOF && aws s3 rm s3://b/c\nx\nEOF",
    "! AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/n\nx\nEOF",
    "AWS_PROFILE=prod cat <<A && KUBECONFIG=~/k.yaml cat <<B && aws s3 rm s3://b/two\na\nA\nb\nB",
    # 先頭の段がシェル経由 wrapper でも、区切りが wrapper の引用符の外なら後ろの段は外のシェルで走る
    "AWS_PROFILE=prod watch 'cat' <<EOF && aws s3 rm s3://b/wc\nx\nEOF",
    "AWS_PROFILE=prod npx -c 'cat' <<EOF && aws s3 rm s3://b/nc\nx\nEOF",
    "KUBECONFIG=~/k.yaml watch 'cat' <<EOF | kubectl apply -f -\nkind: x\nEOF",
    "GH_CONFIG_DIR=/g npx -c 'cat' <<'EOF'; gh pr create --title wc\nx\nEOF",
    "AWS_PROFILE=prod watch 'true' <<EOF && aws s3 rm s3://b/f3a\nx\nEOF",
    "AWS_PROFILE=prod npx -c 'true' <<EOF ; aws s3 rm s3://b/f3b\nx\nEOF",
    "KUBECONFIG=~/k.yaml watch 'true' <<EOF && kubectl delete po f3c\nx\nEOF",
    # 中身に区切りがある wrapper では、中身の段は全部が先頭の段 (前置きの値で走る)
    "AWS_PROFILE=prod npx -c 'true && aws s3 rm s3://b/ni' <<EOF ; aws s3 rm s3://b/no\nx\nEOF",
    "KUBECONFIG=~/k.yaml npx -c 'true | kubectl delete po ni' <<EOF && kubectl delete po no\nx\nEOF",
    # 後ろの段自身の代入の `~` も外のシェルが HOME に展開する (v0.30.4。0.30.3 は wrapper の中身と読み展開しなかった)
    "watch 'cat' <<EOF && KUBECONFIG=~/k2.yaml kubectl get po wt\nx\nEOF",
    "npx -c 'cat' <<EOF ; KUBECONFIG=~/k2.yaml kubectl get po nt\nx\nEOF",
    "watch cat <<'EOF' | KUBECONFIG=~/k2.yaml kubectl apply -f -\nkind: x\nEOF",
    "AWS_PROFILE=prod watch 'true' <<EOF && KUBECONFIG=~/k2.yaml aws s3 rm s3://b/wk\nx\nEOF",
    "watch 'cat' <<EOF && time KUBECONFIG=~/k2.yaml kubectl get po wtt\nx\nEOF",
    # 予約語の `time` の後ろの代入もシェル自身が行う (v0.30.4)
    "time KUBECONFIG=~/k2.yaml kubectl get po t1",
    "{ time KUBECONFIG=~/k2.yaml kubectl get po t2; }",
    "cat <<EOF && time KUBECONFIG=~/k2.yaml kubectl get po t3\nx\nEOF",
    "true && time AWS_PROFILE=prod KUBECONFIG=~/k2.yaml aws s3 rm s3://b/t4",
    # コマンド語の無い代入の後ろにリダイレクトを挟む形 (`;` / `&&` が続けば全段、`|` / `&` が続けば
    # サブシェルなのでどの段にも効かない)
    "AWS_PROFILE=prod 2>/dev/null <<EOF ; aws s3 rm s3://b/nwr\nx\nEOF",
    "AWS_PROFILE=prod <<EOF >/dev/null && aws s3 rm s3://b/f1b\nx\nEOF",
    "KUBECONFIG=~/k.yaml </dev/null <<EOF ; kubectl delete po f1c\nx\nEOF",
    "AWS_PROFILE=prod 2>./err.txt <<EOF ; aws s3 rm s3://b/f1d\nx\nEOF",
    "AWS_PROFILE=prod <<EOF 2>/dev/null | aws s3 cp - s3://b/nwp\nx\nEOF",
    "AWS_PROFILE=prod <<EOF | aws s3 cp - s3://b/nwq\nx\nEOF",
    # 全段に継ぐ由来: シェル経由 wrapper の中身 (heredoc の本文があっても)
    "AWS_PROFILE=prod watch 'true && aws s3 rm s3://b/w && kubectl delete po w'",
    "AWS_PROFILE=prod npx -c 'true && aws s3 rm s3://b/npx'",
    "AWS_PROFILE=prod watch 'true && aws s3 rm s3://b/wh' <<EOF\nx\nEOF",
    # heredoc の無い対照と、本文の後ろの行の段 (別のセグメント)
    "AWS_PROFILE=prod cat x | aws s3 cp - s3://b/ctl",
    "KUBECONFIG=~/k.yaml kubectl apply -f x && kubectl delete po ctl",
    "AWS_PROFILE=prod cat <<EOF\nx\nEOF\naws s3 rm s3://b/next",
    # 値を読めない代入 (印も先頭の段だけ / wrapper の中身なら全段)
    "AWS_PROFILE=$(echo prod) aws s3 cp - s3://b/u1 <<EOF && aws s3 rm s3://b/u2\nx\nEOF",
    "AWS_PROFILE=$(echo prod) cat <<EOF | aws s3 cp - s3://b/u3\nx\nEOF",
    "AWS_PROFILE=$(echo prod) watch 'true && aws s3 rm s3://b/uw'",
    "AWS_PROFILE=$(echo prod) watch 'true && AWS_PROFILE=dev aws s3 rm s3://b/uwd'",
    "AWS_PROFILE=prod <<EOF ; aws s3 ls nw1 ; aws s3 rm s3://b/nw2\nx\nEOF",
    "AWS_PROFILE=$P aws s3 rm s3://b/v",
    "AWS_PROFILE=`echo prod` aws s3 rm s3://b/bt",
    "AWS_PROFILE=dev AWS_PROFILE=$(echo q) aws s3 rm s3://b/dq",
    "AWS_PROFILE=$(echo q) AWS_PROFILE=dev aws s3 rm s3://b/qd",
    "AWS_PROFILE=$(echo q) env AWS_PROFILE=dev aws s3 rm s3://b/qe",
    "cat <<EOF && AWS_PROFILE=$(echo own) aws s3 rm s3://b/own\nx\nEOF",
    # 追記の代入 (`AWS_PROFILE+=x aws ...`) は bash 3.2 が `x`、zsh -f が `launchx` で走る (読みが割れる)
    # ので表に置かない。どちらも起動時の値と違うので印を付ける (`unread_env_candidates` の表)。
]

# 偽の CLI: 名前と引数と追う env を 1 行にまとめ、1 回の書き込みで記録する (パイプの段は並行に走る)。
_RECORDER = "#!/bin/sh\nline=\"${0##*/}\"\nfor a in \"$@\"; do line=\"$line $a\"; done\n" + "".join(
    f'if [ -n "${{{v}+x}}" ]; then line="$line\t{v}=${v}"; else line="$line\t{v}=<unset>"; fi\n'
    for v in _TRACKED
) + 'printf \'%s\\n\' "$line" >> "$CAG_RECORD"\n'
_WATCH = '#!/bin/sh\nexec /bin/sh -c "$*"\n'
_NPX = '#!/bin/sh\n[ "$1" = "-c" ] || exit 2\nexec /bin/sh -c "$2"\n'


def _candidates(command):
    with mock.patch.dict(os.environ, {"HOME": _HOME}):
        try:
            return extract_candidates(command), unread_env_candidates(command)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"例外: {e!r}") from e


class CandidateEnvTests(unittest.TestCase):
    """段の由来ごとの env と、値を読めなかった代入の印。"""

    def _envs(self, command):
        cands, unread = _candidates(command)
        return [(c, e, sorted(unread.get(c, ()))) for c, e in cands]

    def test_heredoc_line_keeps_the_leading_env_on_the_first_command(self):
        for command, want in (
            ("AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
             [("cat", {"AWS_PROFILE": "prod"}, []), ("aws s3 cp - s3://b/k", {}, [])]),
            ("AWS_PROFILE=prod cat <<EOF && aws s3 rm s3://b/k\nx\nEOF",
             [("cat", {"AWS_PROFILE": "prod"}, []), ("aws s3 rm s3://b/k", {}, [])]),
            ("KUBECONFIG=~/k.yaml kubectl apply -f - <<EOF && kubectl delete po x\nb\nEOF",
             [("kubectl apply -f -", {"KUBECONFIG": "/H/k.yaml"}, []),
              ("kubectl delete po x", {}, [])]),
            ("GH_CONFIG_DIR=/g cat <<'EOF'; gh pr create\nx\nEOF",
             [("cat", {"GH_CONFIG_DIR": "/g"}, []), ("gh pr create", {}, [])]),
            ("CLOUDSDK_CONFIG=/c cat <<\\EOF & gcloud run deploy\nx\nEOF",
             [("cat", {"CLOUDSDK_CONFIG": "/c"}, []), ("gcloud run deploy", {}, [])]),
            # 後ろの段の自分の代入は残る。3 段目にも継がない
            ("X=1 cat <<EOF && Y=2 aws s3 ls && aws s3 rm s3://b/k\nb\nEOF",
             [("cat", {"X": "1"}, []), ("aws s3 ls", {"Y": "2"}, []),
              ("aws s3 rm s3://b/k", {}, [])]),
            # 先頭の段の wrapper (外部プログラム) も先頭の段だけ
            ("AWS_PROFILE=prod nohup cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
             [("cat", {"AWS_PROFILE": "prod"}, []), ("aws s3 cp - s3://b/k", {}, [])]),
            ("env AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
             [("cat", {"AWS_PROFILE": "prod"}, []), ("aws s3 cp - s3://b/k", {}, [])]),
            # 値を読めない代入の印も先頭の段だけ
            ("AWS_PROFILE=$(echo prod) cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
             [("cat", {}, ["AWS_PROFILE"]), ("aws s3 cp - s3://b/k", {}, [])]),
            ("AWS_PROFILE=$P aws s3 cp - s3://b/a <<EOF && aws s3 rm s3://b/b\nx\nEOF",
             [("aws s3 cp - s3://b/a", {}, ["AWS_PROFILE"]), ("aws s3 rm s3://b/b", {}, [])]),
            # 後ろの段の自分の読めない代入には印が付く
            ("cat <<EOF && AWS_PROFILE=$(echo own) aws s3 rm s3://b/k\nx\nEOF",
             [("cat", {}, []), ("aws s3 rm s3://b/k", {}, ["AWS_PROFILE"])]),
        ):
            with self.subTest(command=command):
                self.assertEqual(self._envs(command), want)

    def test_shell_wrapper_contents_get_the_env_on_every_stage(self):
        """`watch` / `npx -c` の中身は wrapper が起動するシェルが走らせるので、前置きの代入は全段に効く。"""
        for command, want in (
            ("AWS_PROFILE=prod watch 'true && aws s3 rm s3://b/k'",
             [("true", {"AWS_PROFILE": "prod"}, []), ("aws s3 rm s3://b/k", {"AWS_PROFILE": "prod"}, [])]),
            ("AWS_PROFILE=prod npx -c 'true && aws s3 rm s3://b/k'",
             [("true", {"AWS_PROFILE": "prod"}, []), ("aws s3 rm s3://b/k", {"AWS_PROFILE": "prod"}, [])]),
            # heredoc の本文があっても、分割し直した段は wrapper の中身
            ("AWS_PROFILE=prod watch 'true && aws s3 rm s3://b/k' <<EOF\nx\nEOF",
             [("true", {"AWS_PROFILE": "prod"}, []), ("aws s3 rm s3://b/k", {"AWS_PROFILE": "prod"}, [])]),
            ("AWS_PROFILE=$(echo prod) watch 'true && aws s3 rm s3://b/k'",
             [("true", {}, ["AWS_PROFILE"]), ("aws s3 rm s3://b/k", {}, ["AWS_PROFILE"])]),
            # 中身の段の静的な代入が外側の読めない代入に勝つ (印を外す)
            ("AWS_PROFILE=$(echo prod) watch 'true && AWS_PROFILE=dev aws s3 rm s3://b/k'",
             [("true", {}, ["AWS_PROFILE"]), ("aws s3 rm s3://b/k", {"AWS_PROFILE": "dev"}, [])]),
        ):
            with self.subTest(command=command):
                self.assertEqual(self._envs(command), want)

    def test_assignments_without_a_command_word_still_reach_every_stage(self):
        """前置きの代入を剥がした残りが区切りで始まる (代入の後ろにコマンド語が無い) 行は 0.30.2 と同じ。

        代入は export 済みの変数なら後ろのコマンドに効くので、全段に継ぐ (照合する側)。
        """
        for command in (
            "AWS_PROFILE=prod a=(<<E \\\na+=(x)y ; aws s3 ls",
            "AWS_PROFILE=prod <<EOF ; aws s3 ls\nx\nEOF",
        ):
            with self.subTest(command=command):
                envs = [e for c, e, _u in self._envs(command) if c == "aws s3 ls"]
                self.assertGreaterEqual(len(envs), 1, command)
                self.assertTrue(all(e == {"AWS_PROFILE": "prod"} for e in envs), envs)
        # 区切りの後ろの 2 段目にも継ぐ (heredoc の本文を落とした行でも、代入の後ろにコマンド語が無い)
        for command, want in (
            ("AWS_PROFILE=prod <<EOF ; aws s3 ls ; aws s3 rm s3://b/k\nx\nEOF",
             [("aws s3 ls", {"AWS_PROFILE": "prod"}, []),
              ("aws s3 rm s3://b/k", {"AWS_PROFILE": "prod"}, [])]),
            ("AWS_PROFILE=$(echo prod) <<EOF ; aws s3 ls ; aws s3 rm s3://b/k\nx\nEOF",
             [("aws s3 ls", {}, ["AWS_PROFILE"]), ("aws s3 rm s3://b/k", {}, ["AWS_PROFILE"])]),
        ):
            with self.subTest(command=command):
                self.assertEqual(self._envs(command), want)

    def test_other_segments_do_not_change(self):
        """heredoc の無い行と、本文の後ろの行の段は 0.30.2 と同じ (別のセグメント)。"""
        for command, want in (
            ("AWS_PROFILE=prod cat x | aws s3 cp - s3://b/k",
             [("cat x", {"AWS_PROFILE": "prod"}, []), ("aws s3 cp - s3://b/k", {}, [])]),
            ("AWS_PROFILE=prod cat <<EOF\nx\nEOF\naws s3 rm s3://b/k",
             [("cat", {"AWS_PROFILE": "prod"}, []), ("aws s3 rm s3://b/k", {}, [])]),
            ("cat <<EOF && KUBECONFIG=~/a kubectl apply\nx\nEOF",
             [("cat", {}, []), ("kubectl apply", {"KUBECONFIG": "/H/a"}, [])]),
        ):
            with self.subTest(command=command):
                self.assertEqual(self._envs(command), want)


@unittest.skipUnless(len(_SHELLS) == 2, "bash と zsh の両方が要る")
class ShellAgreementTests(unittest.TestCase):
    """SHELL_TABLE の各行を bash 3.2 と zsh -f で走らせ、行の**全段**の CLI の env が hook の照合の env と
    一致することを確かめる。

    hook の照合の env は、候補の env (前置きの代入) に無い変数なら起動時の値。値を読めなかった代入の印が
    付いた変数は hook の env で照合しないので一致は求めず、代わりに起動時の値と違う値で走ることを求める
    (印の付けすぎも捉える)。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "fakebin"
        self.bin.mkdir()
        for name, text in (("aws", _RECORDER), ("kubectl", _RECORDER), ("gh", _RECORDER),
                           ("gcloud", _RECORDER), ("watch", _WATCH), ("npx", _NPX)):
            p = self.bin / name
            p.write_text(text, encoding="utf-8")
            p.chmod(0o755)
        self.work = self.tmp / "work"
        (self.work / "sub").mkdir(parents=True)
        self.record = self.tmp / "record.txt"

    def _runs(self, command):
        """各シェルで走った CLI の (名前と引数, env) の列。"""
        runs = {}
        for shell in _SHELLS:
            if self.record.exists():
                self.record.unlink()
            args = [shell, "-f", "-c"] if shell.endswith("zsh") else [shell, "-c"]
            subprocess.run(
                [*args, command],
                env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": _HOME,
                     "CAG_RECORD": str(self.record), **_LAUNCH},
                capture_output=True, text=True, cwd=self.work, stdin=subprocess.DEVNULL, timeout=30,
            )
            lines = self.record.read_text(encoding="utf-8").splitlines() if self.record.exists() else []
            out = []
            for line in lines:
                head, *cols = line.split("\t")
                self.assertEqual(len(cols), len(_TRACKED), line)
                out.append((head, dict(c.split("=", 1) for c in cols)))
            runs[shell] = sorted(out)
        return runs

    def test_every_stage_matches_both_shells(self):
        for command in SHELL_TABLE:
            with self.subTest(command=command):
                cands, unread = _candidates(command)
                runs = self._runs(command)
                # 両方のシェルで同じ CLI が同じ env で走る (表に置くのは読みの割れない形だけ)
                self.assertEqual(runs[_SHELLS[0]], runs[_SHELLS[1]], runs)
                self.assertGreaterEqual(len(runs[_SHELLS[0]]), 1, runs)
                for cli, shell_env in runs[_SHELLS[0]]:
                    envs = [e for c, e in cands if c == cli]
                    self.assertEqual(len(envs), 1, (cli, cands))
                    marked = unread.get(cli, frozenset())
                    for name in _TRACKED:
                        if name in marked:
                            self.assertNotEqual(shell_env[name], _LAUNCH[name], (cli, name))
                        else:
                            want = envs[0].get(name, _LAUNCH[name])
                            self.assertEqual(shell_env[name], want, (cli, name))


_GOOD = {"AWS_PROFILE": "prod", "GH_CONFIG_DIR": "/g", "CLOUDSDK_CONFIG": "/gc"}


class DispatchTests(BaseWithTmpProject):
    """チケットの例と、aws / gh / gcloud の同じ形の判定。heredoc の無い対照と同じになる。"""

    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp) / "home"
        (self.home / ".kube").mkdir(parents=True)
        self.launch_kc = Path(self.tmp) / "launch.yaml"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "KUBECONFIG": str(self.launch_kc)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._write_accounts({
            "kubectl": "ctx", "aws": "123456789012", "github": "me",
            "gcloud": {"account": "a@example.com"},
        })
        self.fake = _FakeKubectl()

    def _fake_verify(self, name, good):
        def verify(expected, project_dir, env=None, context=None):
            value = (os.environ if env is None else env).get(name)
            return None if value == good else f"mismatch: {name}={value}"
        return verify

    def _run(self, command):
        verifies = [
            mock.patch.object(svc, "verify", side_effect=self._fake_verify(name, good))
            for svc, name, good in (
                (aws, "AWS_PROFILE", _GOOD["AWS_PROFILE"]),
                (github, "GH_CONFIG_DIR", _GOOD["GH_CONFIG_DIR"]),
                (gcloud, "CLOUDSDK_CONFIG", _GOOD["CLOUDSDK_CONFIG"]),
            )
        ]
        try:
            with self.isolated_cache(), mock.patch("subprocess.run", side_effect=self.fake):
                for p in verifies:
                    p.start()
                try:
                    result = dispatch(command, str(self.project_dir))
                finally:
                    for p in verifies:
                        p.stop()
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        if result is None:
            return "allow"
        return result["hookSpecificOutput"].get("permissionDecision", "allow")

    def test_dod_kubectl_after_a_heredoc_is_checked_with_the_launch_kubeconfig(self):
        """`KUBECONFIG=~/k.yaml kubectl apply -f - <<EOF && kubectl delete po x` の delete は起動時の
        kubeconfig (期待外) で走る。0.30.2 は `~/k.yaml` で照合して allow にしていた。"""
        (self.home / "k.yaml").write_text("ctx\n", encoding="utf-8")
        self.launch_kc.write_text("other\n", encoding="utf-8")
        for command in (
            "KUBECONFIG=~/k.yaml kubectl apply -f - <<EOF && kubectl delete po x\nkind: x\nEOF",
            "KUBECONFIG=~/k.yaml cat <<'EOF' | kubectl apply -f -\nkind: x\nEOF",
            # heredoc の無い対照
            "KUBECONFIG=~/k.yaml kubectl apply -f x && kubectl delete po x",
            "KUBECONFIG=~/k.yaml cat x | kubectl apply -f -",
        ):
            with self.subTest(command=command):
                self.assertEqual(self._run(command), "deny")
        # 先頭の段だけなら `~/k.yaml` で照合する (前置きの代入は先頭の段には効く)
        self.assertEqual(
            self._run("KUBECONFIG=~/k.yaml kubectl apply -f - <<EOF\nkind: x\nEOF"), "allow"
        )

    def test_dod_other_clis_are_checked_with_the_launch_env(self):
        """起動時の値が期待外で前置きの値が期待どおりの世界: heredoc の行の後ろの段は deny (対照と同じ)。"""
        launch = {"AWS_PROFILE": "launch", "GH_CONFIG_DIR": "/launch/gh", "CLOUDSDK_CONFIG": "/launch/gc"}
        with mock.patch.dict(os.environ, launch):
            for heredoc, control in (
                ("AWS_PROFILE=prod cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
                 "AWS_PROFILE=prod cat x | aws s3 cp - s3://b/k"),
                ("AWS_PROFILE=prod cat <<EOF && aws s3 rm s3://b/k\nx\nEOF",
                 "AWS_PROFILE=prod cat x && aws s3 rm s3://b/k"),
                ("GH_CONFIG_DIR=/g cat <<'EOF' && gh pr create --title t\nx\nEOF",
                 "GH_CONFIG_DIR=/g cat x && gh pr create --title t"),
                ('CLOUDSDK_CONFIG=/gc cat <<"EOF" | gcloud run deploy s\nx\nEOF',
                 "CLOUDSDK_CONFIG=/gc cat x | gcloud run deploy s"),
            ):
                with self.subTest(command=heredoc):
                    self.assertEqual(self._run(control), "deny")
                    self.assertEqual(self._run(heredoc), "deny")
            # 前置きの代入が先頭の CLI にだけ効く形は、先頭の CLI を前置きの値で照合する
            self.assertEqual(self._run("AWS_PROFILE=prod aws s3 cp - s3://b/k <<EOF\nx\nEOF"), "allow")

    def test_launch_env_decides_the_later_stage_in_the_other_world(self):
        """起動時の値が期待どおりで前置きの値が期待外の世界: 後ろの段は起動時の値で走るので allow
        (0.30.2 は前置きの値で照合して deny。bash 3.2 / zsh -f の値は `ShellAgreementTests`)。"""
        with mock.patch.dict(os.environ, {"AWS_PROFILE": "prod", "GH_CONFIG_DIR": "/g", "CLOUDSDK_CONFIG": "/gc"}):
            for command in (
                "AWS_PROFILE=other cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
                "GH_CONFIG_DIR=/other cat <<'EOF' && gh pr create --title t\nx\nEOF",
                "CLOUDSDK_CONFIG=/other cat <<\\EOF; gcloud run deploy s\nx\nEOF",
            ):
                with self.subTest(command=command):
                    self.assertEqual(self._run(command), "allow")
            # シェル経由 wrapper の中身は前置きの値で走るので、そのまま照合する (deny)
            self.assertEqual(self._run("AWS_PROFILE=other watch 'true && aws s3 rm s3://b/k'"), "deny")

    def test_redirects_and_wrappers_on_the_heredoc_line(self):
        """起動時の値が期待どおりで前置きの値が期待外の世界。シェルが前置きの値で走らせる後ろの CLI は deny、
        起動時の値で走らせる後ろの CLI は allow (値は `ShellAgreementTests` の表の同じ形)。"""
        with mock.patch.dict(os.environ, {"AWS_PROFILE": "prod", "GH_CONFIG_DIR": "/g", "CLOUDSDK_CONFIG": "/gc"}):
            for command, want in (
                # コマンド語の無い代入の後ろの `/` を含むリダイレクト: `;` / `&&` の後ろは前置きの値で走る
                ("AWS_PROFILE=other 2>/dev/null <<EOF ; aws s3 rm s3://b/k\nx\nEOF", "deny"),
                ("AWS_PROFILE=other <<EOF >/dev/null && aws s3 rm s3://b/k\nx\nEOF", "deny"),
                ("AWS_PROFILE=other 2>./err.txt <<EOF ; aws s3 rm s3://b/k\nx\nEOF", "deny"),
                ("AWS_PROFILE=other <<EOF ; aws s3 rm s3://b/k\nx\nEOF", "deny"),
                # `|` の後ろはサブシェルなので起動時の値で走る
                ("AWS_PROFILE=other <<EOF | aws s3 cp - s3://b/k\nx\nEOF", "allow"),
                ("AWS_PROFILE=other <<EOF 2>/dev/null | aws s3 cp - s3://b/k\nx\nEOF", "allow"),
                # 先頭の段のシェル経由 wrapper の引用符の外の後ろの段は、外のシェルで起動時の値で走る
                ("AWS_PROFILE=other watch 'true' <<EOF && aws s3 rm s3://b/k\nx\nEOF", "allow"),
                ("AWS_PROFILE=other npx -c 'true' <<EOF ; aws s3 rm s3://b/k\nx\nEOF", "allow"),
                # heredoc の無い行の前置きの代入の後ろのリダイレクトも、後ろの CLI を前置きの値で照合する
                ("AWS_PROFILE=other 2>/dev/null aws s3 rm s3://b/k", "deny"),
                ("AWS_PROFILE=other </dev/null aws s3 rm s3://b/k", "deny"),
            ):
                with self.subTest(command=command):
                    self.assertEqual(self._run(command), want)


if __name__ == "__main__":
    unittest.main()
