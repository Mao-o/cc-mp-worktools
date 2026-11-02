"""cd が後ろの PR 操作の cwd を変えるかの 3 分類 (command._CdTracker)。

- 変えない: 元の cwd を検査する
- 変えるか確かでない: 止める
- 確かに変える: 合成して検査する

期待値は、PATH の先頭に偽の gh を置いた bash 3.2 / zsh -f / bash 5.2 で実際に走らせた $PWD。
ここの RealShellTest は、手元にあるシェルで同じことを確かめる。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401

import command
from command import find_invocations, unresolved_reason

_GH = "gh pr create --fill"

# gh は元の cwd で走る。止めずに元の cwd を検査する
NO_EFFECT = (
    f"(cd a) && {_GH}",
    f"(cd a); {_GH}",
    f"(cd a)\n{_GH}",
    f"(cd a && true) && {_GH}",
    f"(cd a && git push) && {_GH}",
    f"(cd a && make)\n{_GH}",
    f"X=$(cd a); {_GH}",
    f"X=$(cd a) && {_GH}",
    f"ROOT=$(cd .. && pwd); {_GH}",
    f"ROOT=$(cd .. && pwd)\n{_GH}",
    f"(cd a) && X=1 {_GH}",
    f"(cd a) && ({_GH})",
    f"(cd a);({_GH})",
    f"X=$(cd a);({_GH})",
    f"X=$(cd a) && \n({_GH})",
    f"cd a | cat; {_GH}",
    f"cd a | cat && {_GH}",
    f"cd a | {_GH}",
    f"true | cd a | cat; {_GH}",
    f"cd a & {_GH}",
    f"cd a &\n{_GH}",
    f"true && cd a & {_GH}",
    f"env cd a; {_GH}",
    f"env -- cd a; {_GH}",
    f"env -i cd a; {_GH}",
    f"env A=1 cd a; {_GH}",
    f"nohup cd a; {_GH}",
    f"nohup -- cd a; {_GH}",
    f"for i in 1 2; do (cd a && true); done; {_GH}",
    f"if true; then (cd a); fi; {_GH}",
    f"{{ (cd a); }}; {_GH}",
    # `)` + 区切り + `(` は 1 つの token になり、後ろの `(` が前置きに現れない
    f"(true);(cd a); {_GH}",
    f"(true)\n(cd a)\n{_GH}",
    f"X=$(true)\n(cd a; true)\n{_GH}",
)

# gh は確かな cd の先で走る (cd_steps)
SURE = (
    (f"cd a && {_GH}", ("a",)),
    (f"cd a; {_GH}", ("a",)),
    (f"cd a\n{_GH}", ("a",)),
    (f"cd a 2>/dev/null; {_GH}", ("a",)),
    (f"cd a >/dev/null && {_GH}", ("a",)),
    (f">/dev/null cd a; {_GH}", ("a",)),
    (f"builtin cd a; {_GH}", ("a",)),
    (f"A=1 cd a; {_GH}", ("a",)),
    (f"! cd a; {_GH}", ("a",)),
    (f"git push && cd a && {_GH}", ("a",)),
    (f"cd a && git push; {_GH}", ("a",)),
    (f"cd a && git add . && {_GH}", ("a",)),
    (f"cd a; git push; {_GH}", ("a",)),
    (f"cd a && cd b; {_GH}", ("a", "b")),
    (f"cd a && make && cd b && {_GH}", ("a", "b")),
    (f"if cd a; then {_GH}; fi", ("a",)),
    (f"if true; then cd a; {_GH}; fi", ("a",)),
    (f"{{ cd a; }} && {_GH}", ("a",)),
    (f"{{ cd a; }}; {_GH}", ("a",)),
    (f"cd a && {{ cd b; }} && {_GH}", ("a", "b")),
    (f"cd a || exit; {_GH}", ("a",)),
    (f"cd a || {{ echo no; exit 1; }}; {_GH}", ("a",)),
    (f"(cd a && {_GH})", ("a",)),
    (f"(cd a; {_GH})", ("a",)),
    (f"X=$(cd a; {_GH}) true", ("a",)),
    (f"cd a; ({_GH})", ("a",)),
    (f"cd a && ({_GH})", ("a",)),
    (f"cat <<'EOF'\nIt's\nEOF\ncd a; {_GH}", ("a",)),
    (f"git push && (cd a; {_GH})", ("a",)),
    (f"(true);(cd a; {_GH})", ("a",)),
    (f"make; cd a && cd b; {_GH}", ("a", "b")),
    (f"echo HOME; cd a; {_GH}", ("a",)),
    (f"echo $x; cd a; {_GH}", ("a",)),
    (f"cd a; \"$V\"/make; {_GH}", ("a",)),
    (f"cd a; $V/make; {_GH}", ("a",)),
)

# シェルや実行時の値で cwd が変わる。止める
MAYBE = (
    # パイプの右は zsh だけ移る
    f"true | cd a; {_GH}",
    f"true | cd a && {_GH}",
    f"cat <<EOF | cd a; {_GH}\nb\nEOF",
    # `||` の両側
    f"cd a || cd b; {_GH}",
    f"cd a || cd /tmp; {_GH}",
    f"cd a || true; {_GH}",
    f"false || cd a; {_GH}",
    f"true || cd a; {_GH}",
    f"true || \ncd a; {_GH}",
    # `&&` の前に別のコマンドがあり、後ろが `;` で続く (前が失敗すると cd は走らず gh は走る)
    f"false && cd a; {_GH}",
    f"[ -d a ] && cd a; {_GH}",
    f"false &&\ncd a; {_GH}",
    f"false && \ncd a; {_GH}",
    f"false && # c\ncd a; {_GH}",
    f"false && cd a || {_GH}",
    f"cd a && make && cd b; {_GH}",
    f"(false) && cd a; {_GH}",
    f"false && cd a && true || {_GH}",
    f"({{ cd a; }} | cat; {_GH})",
    f"cd \"$WT\"; cd a | cat; {_GH}",
    f"cd b; chdir a; {_GH}",
    f"cd b; $x cd a; {_GH}",
    # コマンド語が展開: c=cd なら移る。出力がコマンドになる形も読めない
    f"$c d; {_GH}",
    f"${{c}} d && {_GH}",
    f"$(echo cd a); {_GH}",
    f"`echo cd a`; {_GH}",
    f"`echo cd` d; {_GH}",
    f"X=$(a) $y cd a; {_GH}",
    f"X=$(a) `y` cd a; {_GH}",
    # 括弧を挟んでもループの中はループの中
    f"for i in 1; do (true); cd a && {_GH}; done",
    f"(while true; do cd a && {_GH}; exit; done)",
    f"$x; {_GH}",
    f"! cd b && cd a; {_GH}",
    # `&` の前のリスト
    f"cd a && true & {_GH}",
    f"{{ cd a; }} & {_GH}",
    f"{{ cd a; }} && true & {_GH}",
    f"{{ cd a; }} & (true); {_GH}",
    # 分岐・ループ・波括弧のサブシェル
    f"if false; then cd a; fi; {_GH}",
    f"if true; then cd a; fi; {_GH}",
    f"if false; then cd a; else {_GH}; fi",
    f"if cd a; then true; fi; {_GH}",
    f"for i in 1; do cd a; done; {_GH}",
    f"while false; do cd a; done; {_GH}",
    f"case a in a) cd a;; esac; {_GH}",
    f"case a in b) cd a;; esac; {_GH}",
    f"case a in a) cd a; {_GH};; esac",
    f"case a in a) chdir a;; esac; {_GH}",
    f"(case a in a) true;; esac; cd a); {_GH}",
    f"{{ cd a; }} | cat; {_GH}",
    f"false && {{ cd a; }}; {_GH}",
    f"false && {{ true; cd a; }}; {_GH}",
    # 関数の中の cd は呼ばれたときに走る
    f"f() {{ cd a; }}; f; {_GH}",
    f"f() {{\ncd a\n}}\nf\n{_GH}",
    f"f ( ) {{ cd a; }}; f; {_GH}",
    f"function f {{ cd a; }}; f; {_GH}",
    f"f() {{ chdir a; }}; f; {_GH}",
    # 語の並びでは読まないが移る、シェルで割れる
    f"X=$(a b) cd a; {_GH}",
    f"X=$(cd b) cd a; {_GH}",
    f"$x cd a; {_GH}",
    f"${{x}} cd a; {_GH}",
    f"$(true) cd a; {_GH}",
    f"`true` cd a; {_GH}",
    f"command cd a; {_GH}",
    f"command -- cd a; {_GH}",
    f"command -p cd a; {_GH}",
    f"builtin -- cd a; {_GH}",
    f"time cd a; {_GH}",
    f"time -p cd a; {_GH}",
    f"exec cd a; {_GH}",
    f"chdir a; {_GH}",
    f"cd a b; {_GH}",
    # 行き先が変わる
    f"CDPATH=/tmp cd a; {_GH}",
    f"CDPATH=/tmp; cd a; {_GH}",
    f"export CDPATH=/tmp; cd a; {_GH}",
    f"cdpath=/tmp; cd a; {_GH}",
    f"HOME=/tmp cd; {_GH}",
    f"HOME=/tmp cd ~/x; {_GH}",
    # 引数なしの cd は $HOME に移る
    f"cd; {_GH}",
    f"cd a && cd && {_GH}",
    # 変えない cd に続く相対の cd は起点を読まない (従来どおり)
    f"(cd a); cd b; {_GH}",
    f"cd a | cat; cd b; {_GH}",
    f"env cd a; cd b; {_GH}",
    f"nohup -- cd a; cd b; {_GH}",
)


def _reading(cmd: str):
    found = find_invocations(cmd)
    reason = unresolved_reason(cmd, found)
    return found, reason


class ClassificationTest(unittest.TestCase):
    def test_no_effect_inspects_the_original_cwd(self):
        for cmd in NO_EFFECT:
            with self.subTest(cmd=cmd):
                found, reason = _reading(cmd)
                self.assertIsNone(reason)
                self.assertEqual(len(found), 1)
                self.assertEqual((found[0].cd_steps, found[0].cd, found[0].cd_unresolved), ((), None, None))

    def test_sure_cd_is_composed(self):
        for cmd, steps in SURE:
            with self.subTest(cmd=cmd):
                found, reason = _reading(cmd)
                self.assertIsNone(reason)
                self.assertEqual(len(found), 1)
                self.assertEqual((found[0].cd_steps, found[0].cd_unresolved), (steps, None))

    def test_maybe_stops(self):
        for cmd in MAYBE:
            with self.subTest(cmd=cmd):
                found, reason = _reading(cmd)
                self.assertTrue(reason or any(inv.cd_unresolved for inv in found))

    def test_sure_absolute_cd_after_an_unsure_one_settles_it(self):
        for cmd in (f"true || cd a; cd /abs; {_GH}", f"(cd a); cd /abs; {_GH}", f"f() {{ :; }}; {_GH}"):
            with self.subTest(cmd=cmd):
                found, reason = _reading(cmd)
                self.assertIsNone(reason)
                self.assertIsNone(found[0].cd_unresolved)

    def test_each_invocation_gets_its_own_scope(self):
        found = find_invocations("(cd /x && gh pr create --base m1) && gh pr ready m2; cd /y; gh pr ready m3")
        self.assertEqual([i.cd_steps for i in found], [("/x",), (), ("/y",)])
        self.assertEqual([i.cd_unresolved for i in found], [None, None, None])

    def test_cd_in_a_loop_is_stopped_only_outside_a_subshell(self):
        found = find_invocations(f"for i in 1; do (cd a && {_GH}); done")
        self.assertEqual((found[0].cd_steps, found[0].cd_unresolved), (("a",), None))
        self.assertIsNotNone(find_invocations(f"for i in 1; do cd a && {_GH}; done")[0].cd_unresolved)

    def test_brace_in_a_list_opens_at_its_own_depth(self):
        # `{` の直後の括弧に入ってから block を積むと、括弧を閉じたときに一緒に消える
        self.assertIsNotNone(find_invocations(f"true | {{ (false); cd a; }}\n{_GH}")[0].cd_unresolved)
        self.assertIsNotNone(find_invocations(f"{{ (cd a); cd b; }} | cat; {_GH}")[0].cd_unresolved)

    def test_else_marks_the_branch_that_moved(self):
        self.assertIsNotNone(find_invocations(f"if true; then cd a; elif false; then {_GH}; fi")[0].cd_unresolved)
        # 別の分岐で動いていなければ止めない
        self.assertIsNone(find_invocations(f"if true; then true; else {_GH}; fi")[0].cd_unresolved)

    def test_stray_closing_words_are_ignored(self):
        # 対応する開きの無い fi / done / } は読みを変えない (引数でない位置に置かれた形を含む)
        for cmd in (f"cd a; fi; {_GH}", f"cd a; done; {_GH}", f"cd a; }}; {_GH}", f"cd a; else true; {_GH}"):
            with self.subTest(cmd=cmd):
                self.assertEqual(find_invocations(cmd)[0].cd_steps, ("a",))

    def test_case_ends_at_esac(self):
        inv = find_invocations(f"case a in a) true;; esac; cd a; {_GH}")[0]
        self.assertEqual((inv.cd_steps, inv.cd_unresolved), (("a",), None))


class SeparatorTest(unittest.TestCase):
    def test_newline_after_an_operator_keeps_the_operator(self):
        for text, kind in (
            ("a &&\nb", "and"),
            ("a && \nb", "and"),
            ("a &&  \n\n b", "and"),
            ("a || \nb", "or"),
            ("a | \nb", "pipe"),
            ("a & \nb", "bg"),
            ("a; \nb", "seq"),
            ("a\n\nb", "seq"),
        ):
            with self.subTest(text=text):
                segments = command._segments(command._tokens(text))
                self.assertEqual(segments[1].before, kind)

    def test_comment_after_an_operator_keeps_the_operator(self):
        stripped = command._strip_for_parsing("a && # it is\nb")
        self.assertEqual(command._segments(command._tokens(stripped))[1].before, "and")

    def test_end_depth_is_the_lowest_depth_of_the_separator(self):
        segments = command._segments(command._tokens("(a);(b)"))
        self.assertEqual(segments[0].end_depth, 0)
        self.assertEqual(segments[1].depths[0], 1)


class ExpansionCommandWordTest(unittest.TestCase):
    def test_pr_operation_after_an_expansion_stops(self):
        for prefix in (
            "$x ",
            "${x} ",
            '"$x" ',
            '"${x}" ',
            "${x}${y} ",
            "$x$y ",
            "x$y ",
            "$x/y ",
            "$(true) ",
            "$(p q) ",
            "`true` ",
            "`p q` ",
            "${x:-a b} ",
            "$x $y ",
            "A=1 $x ",
            "time ${x} ",
            "2>&1 $x ",
        ):
            for start in ("", "true && ", "true; ", "true\n", "(", "{ ", "if true; then "):
                cmd = f"{start}{prefix}gh pr create --fill"
                with self.subTest(cmd=cmd):
                    self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_expansion_in_an_argument_is_left_alone(self):
        for cmd in (
            "echo $x gh pr create",
            "echo ${x} gh pr create",
            "echo $(p) gh pr create",
            "gh pr view $x",
            "$x gh pr view",
            "$x gh pr list",
            "true && ${x} gh auth status",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_undo_after_an_expansion_is_not_counted(self):
        self.assertIsNone(unresolved_reason("$x gh pr ready --undo", find_invocations("$x gh pr ready --undo")))

    def test_many_expansions_take_linear_time(self):
        script = (
            "import time\n"
            "from command import find_invocations, unresolved_reason\n"
            "def run(k):\n"
            "    best = None\n"
            "    for _ in range(3):\n"
            "        t = time.perf_counter()\n"
            "        for tok in ('$x ', '${x} ', '$x$x', '`a` ', 'a$x', '$(a)', '$x;', '$x&', '\"$x\"'):\n"
            "            cmd = 'true; ' + tok * k + ' make gh pr'\n"
            "            unresolved_reason(cmd, find_invocations(cmd))\n"
            "        d = time.perf_counter() - t\n"
            "        best = d if best is None else min(best, d)\n"
            "    return best\n"
            "print(run(1000), run(4000))\n"
        )
        pkg = Path(__file__).resolve().parent.parent
        r = subprocess.run(["python3", "-c", script], cwd=pkg, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        small, large = map(float, r.stdout.split())
        self.assertLess(large, 2.0)
        self.assertLess(large / max(small, 1e-4), 8.0, (small, large))


class WrapperOptionTest(unittest.TestCase):
    def test_pr_operation_after_a_wrapper_with_options_stops(self):
        for cmd in (
            "env -i gh pr create",
            "env -u Q gh pr create",
            "env -- gh pr create",
            "command -- gh pr create",
            "command -p gh pr create",
            "nohup -- gh pr create",
            "time -p gh pr ready",
            "exec -- gh pr create",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_pr_operation_in_a_case_arm_stops(self):
        for cmd in ("case a in a) gh pr create;; esac", "case a in b) true;; c) gh pr ready 3;; esac"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(unresolved_reason(cmd, find_invocations(cmd)))
        # 分岐の中で行を分けた gh は語の並びで読める
        self.assertIsNone(unresolved_reason("case a in a)\ngh pr create;; esac", find_invocations("case a in a)\ngh pr create;; esac")))

    def test_other_gh_commands_after_a_wrapper_are_left_alone(self):
        for cmd in ("env -i gh pr view", "command -p gh auth status", "case a in a) gh pr view;; esac"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(unresolved_reason(cmd, find_invocations(cmd)))


class DirChangerWordTest(unittest.TestCase):
    def test_every_prefixed_form_stops(self):
        for cmd in (
            f"pushd a; {_GH}",
            f"if pushd a; then {_GH}; fi",
            f"command pushd a; {_GH}",
            f"Y=1 pushd a; {_GH}",
            f"'pushd' a; {_GH}",
            f'"pushd" a; {_GH}',
            f"p\\ushd a; {_GH}",
            f"! pushd a; {_GH}",
            f"time pushd a; {_GH}",
            f"`pushd a`; {_GH}",
            f"X=$(pushd a); {_GH}",
            f"{_GH}; pushd a",
            f"popd; {_GH}",
            f"if popd; then {_GH}; fi",
            f"command popd; {_GH}",
            f"`popd`; {_GH}",
        ):
            with self.subTest(cmd=cmd):
                self.assertIn("pushd / popd", unresolved_reason(cmd, find_invocations(cmd)) or "")

    def test_words_that_only_contain_it_are_left_alone(self):
        for cmd in (f"{_GH} --title 'fix pushd handling'", f"echo pushdown; {_GH}", "pushd a"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_unparsable_command_does_not_raise(self):
        try:
            self.assertFalse(command._names_dir_changer("echo 'a"))
        except ValueError as e:
            self.fail(f"例外が漏れた: {e}")


@unittest.skipUnless(shutil.which("bash"), "bash が無い")
class RealShellTest(unittest.TestCase):
    """読みが実際の $PWD と合うことを、手元のシェルで確かめる。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.top = Path(os.path.realpath(self._tmp.name))
        self.fake = self.top / "fake"
        self.fake.mkdir()
        for name, body in {"gh": 'echo "$PWD" >> "$GHLOG"', "git": ":", "make": ":"}.items():
            path = self.fake / name
            path.write_text(f"#!/bin/sh\n{body}\nexit 0\n")
            path.chmod(0o755)
        self.start = self.top / "w" / "start"
        (self.start / "a" / "b").mkdir(parents=True)
        (self.start / "b").mkdir()
        self.shells = [[s, *flags] for s, flags in (("bash", []), ("zsh", ["-f"])) if shutil.which(s)]

    def tearDown(self):
        self._tmp.cleanup()

    def ran(self, shell: list[str], cmd: str) -> list[str]:
        log = self.top / "gh.log"
        log.unlink(missing_ok=True)
        env = {"PATH": f"{self.fake}:/usr/bin:/bin", "GHLOG": str(log), "HOME": str(self.top)}
        subprocess.run(
            [*shell, "-c", cmd], cwd=self.start, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return log.read_text().split("\n")[:-1] if log.exists() else []

    def test_no_effect_runs_in_the_original_cwd(self):
        for cmd in NO_EFFECT:
            for shell in self.shells:
                with self.subTest(cmd=cmd, shell=shell[0]):
                    self.assertEqual(set(self.ran(shell, cmd)), {str(self.start)})

    def test_sure_runs_in_the_composed_dir(self):
        for cmd, steps in SURE:
            want = self.start
            for step in steps:
                want = want / step
            for shell in self.shells:
                with self.subTest(cmd=cmd, shell=shell[0]):
                    self.assertEqual(set(self.ran(shell, cmd)), {str(want)})


if __name__ == "__main__":
    unittest.main()
