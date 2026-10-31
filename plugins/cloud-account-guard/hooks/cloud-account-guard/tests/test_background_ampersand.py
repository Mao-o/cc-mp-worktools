"""単独の `&` (バックグラウンド実行) をトップレベルの区切りとして扱う (v0.29.0)。

0.28.0 までの分解は `&&` / `||` / `;` / `|` / 改行でしか切らず、`true & kubectl apply ...` や
`sleep 1 & aws s3 rm ...` は先頭の `true` / `sleep` のセグメントに畳まれて、後ろのクラウド CLI が
どれも検証されなかった (全 service)。

あわせて、`&` で終わるリストはサブシェルで走るので、その中の `cd` は `&` の後ろに効かない
(`core/workdir.py` の `segment_dirs`)。`&&` / `2>&1` / `>&` / `&>` / `|&` / 引用符・置換の中の
`&` は区切りではない。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401
from test_kubectl_workdir import _FakeKubectl

from core import workdir  # noqa: E402
from core.command_parser import (  # noqa: E402
    extract_candidates,
    extract_candidates_with_separators,
    split_on_operators,
    split_with_separators,
)
from core.dispatcher import dispatch  # noqa: E402

# service ごとの書込 (WRITE tier) の形。
WRITES = {
    "github": "gh pr create --fill",
    "aws": "aws s3 rm s3://b/x",
    "gcloud": "gcloud compute instances delete vm-1",
    "kubectl": "kubectl apply -f x.yaml",
}

# 単独の `&` で区切った形。`{w}` が書込。どれも後ろの書込が候補になる。
SPLIT_FORMS = [
    "true & {w}",
    "true&{w}",
    "sleep 1 & {w}",
    "true &\n{w}",
    "true & {w} &",
    'echo "a & b" & {w}',
    "echo a\\&b & {w}",
    # エスケープした `>` の後の `&` は fd の複製ではない (`\>` は文字の `>`)
    "echo \\>& {w}",
    # リダイレクトの対象の後の `&` は背景実行 (`>` の直後だけが fd の複製)
    "true >out & {w}",
    'true >"out"& {w}',
    # `|&` は `2>&1 |` の略記。0.28.0 は `|` で切った残りが `& {w}` になり見失っていた
    "true |& {w}",
    "curl http://h/?a=1&b=2 & {w}",
    # heredoc の宣言の後の `&`。本文の除去後に再分割される
    "cat <<EOF & {w}\nbody & more\nEOF",
    # シェル経由 wrapper の中身も同じ規則で分割する
    "watch 'true & {w}'",
]

# 区切りではない `&`。候補はこの通り (分割しない)。
NOT_SPLIT = [
    ("a && b", ["a", "b"]),
    ("kubectl apply -f x 2>&1", ["kubectl apply -f x 2>&1"]),
    ("kubectl apply -f x >&2", ["kubectl apply -f x >&2"]),
    ("kubectl apply -f x <&3", ["kubectl apply -f x <&3"]),
    ("kubectl apply -f x &>/dev/null", ["kubectl apply -f x &>/dev/null"]),
    ("kubectl apply -f x &>>log", ["kubectl apply -f x &>>log"]),
    ("kubectl apply -f x 2>&1 | cat", ["kubectl apply -f x 2>&1", "cat"]),
    ("echo 'a & b' && kubectl apply -f x", ["echo 'a & b'", "kubectl apply -f x"]),
    ('kubectl apply -f "a & b"', ['kubectl apply -f "a & b"']),
    ("kubectl apply -f a\\&b", ["kubectl apply -f a\\&b"]),
    ("echo $(true & false) && kubectl apply -f x", ["echo $(true & false)", "kubectl apply -f x"]),
    ("echo `true & false`; kubectl apply -f x", ["echo `true & false`", "kubectl apply -f x"]),
    ("(( a & b )) && kubectl apply -f x", ["(( a & b ))", "kubectl apply -f x"]),
    ("echo $(( a & b )) && kubectl apply -f x", ["echo $(( a & b ))", "kubectl apply -f x"]),
    # パラメータ展開の中の `&` で切ると、後ろの `--profile p` が別セグメントに移り、
    # aws が profile 無しで照合される
    ("aws s3 rm ${B:-a&b} --profile p", ["aws s3 rm ${B:-a&b} --profile p"]),
    ("echo ${x:-${y:-a&b}} && kubectl apply -f x", ["echo ${x:-${y:-a&b}}", "kubectl apply -f x"]),
    # 入れ子の `${` の `}` は外側を閉じない (bash / zsh とも `a&b` を 1 語に展開する)
    ("aws s3 rm ${B:-${C:-a}&b} --profile p", ["aws s3 rm ${B:-${C:-a}&b} --profile p"]),
    # 展開の中の `$${` は 2 つ目の `$` からの入れ子の `${` (bash / zsh とも最後の `}` で閉じる)
    ("aws s3 rm ${B:-$${}&b} --profile p", ["aws s3 rm ${B:-$${}&b} --profile p"]),
    # 展開の中の引用符・エスケープの `}` は展開を閉じない
    ('aws s3 rm ${B:-"a}&b"} --profile p', ['aws s3 rm ${B:-"a}&b"} --profile p']),
    ("aws s3 rm ${B:-a\\}&b} --profile p", ["aws s3 rm ${B:-a\\}&b} --profile p"]),
    ("aws s3 rm ${B:-$(echo })&b} --profile p", ["aws s3 rm ${B:-$(echo })&b} --profile p"]),
    ("cat <<EOF\nkubectl apply -f x & y\nEOF\naws s3 ls", ["cat", "aws s3 ls"]),
]


class SplitTests(unittest.TestCase):
    def test_lone_ampersand_splits(self):
        for form in SPLIT_FORMS:
            for svc, write in WRITES.items():
                command = form.format(w=write)
                with self.subTest(command=command):
                    cands = [c for c, _e in extract_candidates(command)]
                    self.assertIn(write, cands, command)

    def test_other_ampersands_do_not_split(self):
        for command, want in NOT_SPLIT:
            with self.subTest(command=command):
                self.assertEqual([c for c, _e in extract_candidates(command)], want)

    def test_separators(self):
        self.assertEqual(
            split_with_separators("a & b && c | d |& e || f; g\nh"),
            [
                ("a", "&"), ("b", "&&"), ("c", "|"), ("d", "|&"),
                ("e", "||"), ("f", ";"), ("g", "\n"), ("h", ""),
            ],
        )
        self.assertEqual(split_on_operators("a & b"), ["a", "b"])

    def test_unterminated_parameter_expansion_does_not_swallow(self):
        """閉じない `${` を 1 領域にすると残りを飲み込み、後ろの CLI が消える。"""
        cands = [c for c, _e in extract_candidates("echo ${x & aws s3 rm s3://b/x")]
        self.assertIn("aws s3 rm s3://b/x", cands)

    def test_deeply_nested_parameter_expansion(self):
        """深い入れ子の `${` で再帰の上限に当たらない (例外 = hook の失敗)。"""
        command = "echo " + "${" * 3000 + " & aws s3 rm s3://b/x"
        try:
            cands = [c for c, _e in extract_candidates(command)]
        except RecursionError as e:
            self.fail(f"例外: {e!r}")
        self.assertIn("aws s3 rm s3://b/x", cands)

    def test_bare_brace_in_parameter_expansion_is_not_nesting(self):
        """展開の中の素の `{` は入れ子に数えない (bash 3.2 / 5.2 は最初の `}` で閉じる)。

        zsh は数えるが、閉じる位置が早い側に合わせれば zsh で展開の中にある部分は分割が増える
        側に出るだけで、どちらのシェルでも後ろの CLI は候補に残る。
        """
        for sep in ("; ", " && ", "\n", " & "):
            command = f"echo ${{x:-{{}}{sep}aws s3 rm s3://b/x{sep}echo }}"
            with self.subTest(command=command):
                cands = [c for c, _e in extract_candidates(command)]
                self.assertIn("aws s3 rm s3://b/x", cands)
        # 素の `{` の後ろの `#` は展開の中 (comment ではない)。`cmd` は bash で走る
        cands = [c for c, _e in extract_candidates("echo ${x:-{ #}; aws s3 rm s3://b/x")]
        self.assertIn("aws s3 rm s3://b/x", cands)
        # 素の `{` があっても、最初の `}` までの `&` は展開の中 (bash / zsh とも 1 語)
        cands = [c for c, _e in extract_candidates("echo ${x:-{a&b}} && kubectl apply -f x")]
        self.assertIn("kubectl apply -f x", cands)
        self.assertNotIn("b}}", cands)

    def test_pid_before_brace_is_not_parameter_expansion(self):
        """`$$` (PID) の後ろの `{` は `${` の始まりではない (bash / zsh とも `cmd` が走る)。"""
        for sep in ("; ", " && ", " & ", "\n"):
            command = f"echo $${{{sep}aws s3 rm s3://b/x{sep}echo }}"
            with self.subTest(command=command):
                cands = [c for c, _e in extract_candidates(command)]
                self.assertIn("aws s3 rm s3://b/x", cands)

    def _within(self, seconds, fn):
        """`fn()` を `seconds` 秒で打ち切る。時間切れと例外は FAIL にする (止まる変異も検出する)。"""
        import signal

        if not hasattr(signal, "setitimer"):
            self.skipTest("setitimer が無い")

        class _Timeout(Exception):
            pass

        def _raise(_sig, _frame):
            raise _Timeout()

        old = signal.signal(signal.SIGALRM, _raise)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        except _Timeout:
            self.fail(f"{seconds} 秒で終わらない")
        except RecursionError as e:
            self.fail(f"例外: {e!r}")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def _cands_within(self, seconds, command):
        return self._within(seconds, lambda: [c for c, _e in extract_candidates(command)])

    def test_unclosed_command_substitution_inside_expansion_is_linear(self):
        """閉じない `$(` を含む `${` の連なりで、入れ子 1 段ごとに走査が倍にならない。"""
        command = "aws s3 rm s3://b/x\necho " + "${x:-$(echo " * 40
        self.assertIn("aws s3 rm s3://b/x", self._cands_within(2, command))
        # `$(` は閉じていて `${` だけが閉じない形も同じ
        command = "aws s3 rm s3://b/x\necho " + "${x $(echo " * 40 + ")" * 40
        self.assertIn("aws s3 rm s3://b/x", self._cands_within(2, command))

    def test_many_unclosed_parameter_expansions(self):
        command = "aws s3 rm s3://b/x\necho " + "${" * 20000
        self.assertIn("aws s3 rm s3://b/x", self._cands_within(1, command))

    def test_deep_mutual_nesting_of_expansion_and_substitution(self):
        """`${x:-$(echo ` の入れ子 350 段で再帰の上限に当たらない (0.28.0 は通っていた)。"""
        command = "aws s3 rm s3://b/x; echo " + "${x:-$(echo " * 350 + ")}" * 350
        self.assertIn("aws s3 rm s3://b/x", self._cands_within(5, command))

    def test_nesting_depth_does_not_leak_across_calls(self):
        """深さの数え上げは呼び出しごとに戻る (残ると以後の `${` が全部閉じない扱いになる)。"""
        for _ in range(3):
            extract_candidates("echo " + "${x:-$(echo " * 100 + ")}" * 100)
        for _ in range(100):
            extract_candidates("echo ${a} $(b)")
        self.assertEqual(
            [c for c, _e in extract_candidates("aws s3 rm ${B:-a&b} --profile p")],
            ["aws s3 rm ${B:-a&b} --profile p"],
        )

    def test_memo_is_per_text(self):
        """終端の memo を別の text に使い回さない (同じ位置の `${` の終端は text ごとに違う)。"""
        self.assertEqual(
            [c for c, _e in extract_candidates("x ${a&b} & aws s3 ls")], ["x ${a&b}", "aws s3 ls"]
        )
        self.assertEqual(
            [c for c, _e in extract_candidates("x ${abcde&g} & aws s3 ls")],
            ["x ${abcde&g}", "aws s3 ls"],
        )

    def test_resplit_parts_keep_outer_separator_last(self):
        """再分割した候補は中の区切りを使い、最後の 1 つにだけ外側の区切りが付く。"""
        self.assertEqual(
            [(c, s) for c, _e, s in extract_candidates_with_separators(
                "watch 'cd a; kubectl get x' & aws s3 ls"
            )],
            [("cd a", ";"), ("kubectl get x", "&"), ("aws s3 ls", "")],
        )


class SegmentDirsTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("bg cd", "cd sub & kubectl apply -f x", {"kubectl apply -f x": {"/w"}}),
            ("bg cd, newline", "cd sub &\nkubectl apply -f x", {"kubectl apply -f x": {"/w"}}),
            (
                "cd inside bg list",
                "cd sub && kubectl get pods & kubectl apply -f x",
                {"kubectl get pods": {"/w/sub"}, "kubectl apply -f x": {"/w"}},
            ),
            ("fg cd before bg cd", "cd a; cd b & kubectl apply -f x", {"kubectl apply -f x": {"/w/a"}}),
            ("fg cd, newline", "cd a\ncd b & kubectl apply -f x", {"kubectl apply -f x": {"/w/a"}}),
            (
                "fg cd after bg command",
                "kubectl apply -f x & cd sub; kubectl delete -f x",
                {"kubectl apply -f x": {"/w"}, "kubectl delete -f x": {"/w/sub"}},
            ),
            (
                "conditional bg list",
                "false && cd sub & kubectl apply -f x",
                {"false": {"/w"}, "kubectl apply -f x": {"/w"}},
            ),
            (
                "redirect is not bg",
                "cd sub && kubectl get x 2>&1; kubectl apply -f x &>/dev/null",
                {"kubectl get x 2>&1": {"/w/sub"}, "kubectl apply -f x &>/dev/null": {"/w/sub"}},
            ),
            ("unchanged", "cd sub && kubectl apply -f x", {"kubectl apply -f x": {"/w/sub"}}),
        ]
        for name, command, want in cases:
            with self.subTest(name):
                self.assertEqual(workdir.segment_dirs(command, "/w"), want, command)

    def test_chdir_is_sequential_stays_conservative(self):
        """`&` と `cd` が同じコマンドにあれば、相対パスの kubeconfig は決めない (0.28.0 と同じ)。

        0.28.0 は `kubectl apply & cd sub; kubectl delete` の `cd` を 1 セグメントに畳んで見落とし、
        `delete` を起動ディレクトリで走るものとして True を返していた。
        """
        for command, want in (
            ("kubectl apply -f x & cd sub; kubectl delete -f x", False),
            ("cd sub & kubectl apply -f x", False),
            ("true & kubectl apply -f x", True),
        ):
            with self.subTest(command=command):
                self.assertEqual(workdir.chdir_is_sequential(command), want)


class DispatchTests(BaseWithTmpProject):
    ACCOUNTS = {
        "github": "Mao-o",
        "aws": "123456789012",
        "gcloud": "my-proj",
        "kubectl": "ctx",
    }

    def setUp(self):
        super().setUp()
        self._write_accounts(self.ACCOUNTS)

    def _decide(self, command, svc, verify_value):
        with self.isolated_cache(), mock.patch(
            f"services.{svc}.verify", return_value=verify_value
        ) as v:
            try:
                result = dispatch(command, str(self.project_dir))
            except Exception as e:  # noqa: BLE001
                self.fail(f"例外: {e!r}")
        if result is None:
            return "allow", "", v
        out = result["hookSpecificOutput"]
        return out.get("permissionDecision", "allow"), out.get("permissionDecisionReason", ""), v

    def test_mismatch_after_lone_ampersand_is_denied(self):
        for form in SPLIT_FORMS:
            for svc, write in WRITES.items():
                command = form.format(w=write)
                with self.subTest(command=command):
                    decision, _reason, v = self._decide(command, svc, "不一致")
                    self.assertTrue(v.called, f"検証されていない: {command}")
                    self.assertEqual(decision, "deny", command)

    def test_cli_after_brace_forms_is_verified_and_denied(self):
        """`${x:-{}` (bash は最初の `}` で閉じる) と `$${` (PID) の後ろの CLI も検証する。"""
        for sep in ("; ", " && ", " & ", "\n"):
            for head in ("echo ${{x:-{{}}", "echo $${{"):
                command = f"{head}{sep}aws s3 rm s3://b/x{sep}echo }}"
                with self.subTest(command=command):
                    decision, _reason, v = self._decide(command, "aws", "不一致")
                    self.assertTrue(v.called, f"検証されていない: {command!r}")
                    self.assertEqual(decision, "deny", command)

    def test_background_cd_does_not_move_the_following_command(self):
        """`cd .. & aws ...` の aws は起動ディレクトリで走る (外での実行として止めない)。"""
        decision, reason, v = self._decide("cd .. & aws s3 rm s3://b/x", "aws", None)
        self.assertTrue(v.called)
        self.assertEqual(decision, "allow", reason)

    def test_cd_inside_background_list_is_tracked(self):
        """`cd .. && aws ... &` の aws は `..` で走る (リストの中の cd は効く)。"""
        decision, reason, v = self._decide("cd .. && aws s3 rm s3://b/x & true", "aws", None)
        self.assertEqual(decision, "deny")
        self.assertIn("の外で実行されるコマンドです", reason)
        self.assertFalse(v.called)


class KubeconfigAfterAmpersandTests(BaseWithTmpProject):
    """偽の kubectl が実ファイルを読む形で、`&` の後ろの kubectl を照合する。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("KUBECONFIG", None)
        self.sub = self.project_dir / "sub"
        self.sub.mkdir()
        self._write_accounts({"kubectl": "ctx"})
        self.fake = _FakeKubectl()

    @staticmethod
    def _write(path, ctx):
        path.write_text(ctx + "\n", encoding="utf-8")

    def _decision(self, command):
        with self.isolated_cache(), mock.patch("subprocess.run", side_effect=self.fake):
            try:
                result = dispatch(command, str(self.project_dir))
            except Exception as e:  # noqa: BLE001
                self.fail(f"例外: {e!r}")
        if result is None:
            return "allow"
        return result["hookSpecificOutput"].get("permissionDecision", "allow")

    def _assert_deny(self, command):
        self.assertEqual(self._decision(command), "deny", command)

    def _assert_allow(self, command):
        self.assertEqual(self._decision(command), "allow", command)

    def test_kubectl_after_lone_ampersand_reads_the_file(self):
        self._write(self.project_dir / "k.yaml", "wrong")
        command = "true & kubectl apply -f x.yaml --kubeconfig k.yaml"
        self._assert_deny(command)
        self.assertEqual(len(self.fake.calls), 1, self.fake.calls)
        self._write(self.project_dir / "k.yaml", "ctx")
        self.fake.calls.clear()
        self._assert_allow(command)
        self.assertEqual(len(self.fake.calls), 1, self.fake.calls)

    def test_background_cd_is_not_used_for_the_file(self):
        """`cd sub & kubectl --kubeconfig k.yaml` は `./k.yaml` を開く。`sub/k.yaml` で照合しない。"""
        self._write(self.sub / "k.yaml", "ctx")
        self._write(self.project_dir / "k.yaml", "wrong")
        self._assert_deny("cd sub & kubectl apply -f x.yaml --kubeconfig k.yaml")


if __name__ == "__main__":
    unittest.main()
