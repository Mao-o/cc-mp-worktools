"""上限を超えるコマンドは読まずに分ける: 語が揃えば止め、揃わなければ通す。"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

import _testutil  # noqa: F401

import command
from command import _READ_LIMIT, find_invocations, unresolved_reason

_PKG = Path(__file__).resolve().parent.parent
_REASON = "コマンドが長すぎて"


def _sized(head: str, size: int, tail: str = "") -> str:
    """単一引用符の中の文字で大きさを合わせる (語の読みに触らない)。"""
    filler = size - len(head) - len(tail) - len("echo ''; ")
    return f"{head}echo '{'x' * filler}'; {tail}"


def _verdict(cmd: str) -> str | None:
    return unresolved_reason(cmd, find_invocations(cmd))


class BoundaryTest(unittest.TestCase):
    def test_stops_only_above_the_limit(self):
        for size, stops in ((_READ_LIMIT - 1, False), (_READ_LIMIT, False), (_READ_LIMIT + 1, True)):
            cmd = _sized("", size, "gh pr create --fill")
            self.assertEqual(len(cmd), size)
            with self.subTest(size=size):
                if stops:
                    self.assertIn(_REASON, _verdict(cmd) or "")
                else:
                    self.assertIsNone(_verdict(cmd))
                    self.assertEqual(len(find_invocations(cmd)), 1)
                    self.assertTrue(find_invocations(cmd)[0].parsed)

    def test_reason_points_to_body_file(self):
        self.assertIn("--body-file", _verdict(_sized("", _READ_LIMIT + 1, "gh pr create --fill")) or "")

    def test_found_invocation_is_not_parsed_so_no_caller_reads_it_as_checked(self):
        found = find_invocations(_sized("", _READ_LIMIT + 1, "gh pr create"))
        self.assertEqual([inv.parsed for inv in found], [False])


class PrOperationFormsTest(unittest.TestCase):
    def test_every_way_of_writing_a_pr_operation_stops(self):
        for op in (
            "gh pr create",
            "gh pr new",
            "gh pr ready 3",
            "gh.exe pr create",
            "/opt/bin/gh pr create",
            'gh "pr" create',
            "gh p''r cre'a'te",
            "gh\\\n pr\\\n create",
            "g\\\nh pr cre\\\nate",
            "gh\tpr  create",
            "gh\npr create",
            "gh -R a/b pr create",
            "gh --repo=a/b pr new",
            "gh pr -R a/b create",
            'gh -R "a b" pr create',
            'gh pr -R "a b" ready',
            "(gh pr create)",
            'x="$(gh pr create)"',
            "`gh pr create`",
            "bash -c 'gh pr create'",
        ):
            with self.subTest(op=op):
                self.assertIn(_REASON, _verdict(_sized("", _READ_LIMIT + 1, op)) or "")
                self.assertIn(_REASON, _verdict(_sized(op + "; ", _READ_LIMIT + 1)) or "")

    def test_quoted_heredoc_body_counts_towards_the_size(self):
        # 本文を取り除くには読む必要があるので、上限は読む前の大きさで見る
        cmd = "cat > f <<'E'\ngh pr create\n" + "x\n" * (_READ_LIMIT // 2) + "E\n"
        self.assertIn(_REASON, _verdict(cmd) or "")

    def test_long_body_in_substitution_stops(self):
        body = "x\n" * (_READ_LIMIT // 2)
        self.assertIn(_REASON, _verdict(f"gh pr create --body \"$(cat <<'EOF'\n{body}EOF\n)\"") or "")


class WithoutPrOperationTest(unittest.TestCase):
    def test_large_commands_without_the_words_do_not_stop(self):
        for cmd in (
            _sized("", _READ_LIMIT + 1),
            _sized("", _READ_LIMIT + 1, "gh pr view 3"),
            _sized("", _READ_LIMIT + 1, "gh pr list"),
            _sized("", _READ_LIMIT + 1, "git push origin new-branch"),
            _sized("", _READ_LIMIT + 1, "gh" + " -R a/b" * 8 + " pr view"),
            _sized("", _READ_LIMIT + 1, "cp" + " -R a/b" * 9 + " x"),
            "cat > f.py <<'E'\n" + "print('right', 'new')\n" * 2000 + "E\n",
        ):
            with self.subTest(cmd=cmd[-30:]):
                self.assertGreater(len(cmd), _READ_LIMIT)
                self.assertEqual(command._oversized(cmd), "pass")
                self.assertEqual(find_invocations(cmd), [])
                self.assertIsNone(_verdict(cmd))

    def test_a_word_that_ends_in_gh_is_not_the_gh_word(self):
        for word in ("through", "high", "ugh", "x-gh", "x.gh"):
            cmd = _sized("", _READ_LIMIT + 1, f"echo {word} pr create")
            with self.subTest(word=word):
                self.assertIsNone(_verdict(cmd))

    def test_recursive_copy_flag_and_english_text_do_not_stop(self):
        para = "We went through the new design and the pr looks ready. " * 700
        cmd = "cp -R a b\ncat > notes.md <<'EOF'\n" + para + "\nEOF\n"
        self.assertGreater(len(cmd), _READ_LIMIT)
        self.assertIsNone(_verdict(cmd))

    def test_pr_word_and_operation_word_are_required(self):
        self.assertIsNone(_verdict(_sized("", _READ_LIMIT + 1, "gh spr create")))
        self.assertIsNone(_verdict(_sized("", _READ_LIMIT + 1, "gh pr created")))


class WordsTogetherStopTest(unittest.TestCase):
    """語が揃えば、並びは見ずに止める。"""

    def test_words_in_any_order_stop(self):
        for tail in (
            "f() { gh\n#c\npr create; }",
            "gh -R a/b pr view; git switch -c new",
            "gh pr view 3; git switch -c new",
            "gh pr ready --undo",
            "gh" + " -R a/b" * 9 + " pr view; echo new",
            "bash -c 'gh pr new\\\nx'",
            "g\\\nh pr cre\\\nate",
        ):
            with self.subTest(tail=tail):
                self.assertIn(_REASON, _verdict(_sized("", _READ_LIMIT + 1, tail)) or "")

    def test_word_glued_by_quote_removal_still_stops(self):
        # 外した後は `createx` になるが、シェルでは `create\x` 等の語。外す前の文字列でも見る
        for tail in ("gh pr create\\x", "gh pr create'x'", 'gh pr create"x"', "g''h pr new\\;"):
            with self.subTest(tail=tail):
                self.assertIn(_REASON, _verdict(_sized("", _READ_LIMIT + 1, tail)) or "")

    def test_long_repo_run_stops_even_when_the_words_are_hidden(self):
        # シェルでは gh pr create が走るが、語の途中の展開で語としては見えない
        for tail in ("pr view", "pr cre$()ate", "pr cr{e,}ate", "${x:-pr} create", "{pr,} create"):
            for run in (" -R a/b" * 9, " --repo=a/b" * 9):
                with self.subTest(tail=tail, run=run[:8]):
                    self.assertIn(_REASON, _verdict(_sized("", _READ_LIMIT + 1, f"gh{run} {tail}")) or "")

    def test_repo_run_is_not_quadratic_to_look_for(self):
        for unit in ("--repo=", "-R a ", "-R ", " -R a/b"):
            script = (
                "import command\n"
                f"command._oversized('gh pr ' + {unit!r} * {1024 * 1024 // len(unit)})\n"
            )
            with self.subTest(unit=unit):
                try:
                    r = subprocess.run([sys.executable, "-c", script], cwd=_PKG, capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    self.fail(f"1 MB で 30 秒を超えた: {unit!r}")
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_dollar_quotes_are_quotes(self):
        for tail in ("gh $'pr' create", 'gh $"pr" new', "$'gh' pr ready", "gh pr cre$''ate", 'gh pr $"create"'):
            with self.subTest(tail=tail):
                self.assertIn(_REASON, _verdict(_sized("", _READ_LIMIT + 1, tail)) or "")

    def test_path_and_quoted_gh_stop(self):
        for gh in ("/usr/bin/gh", "./gh", "~/gh", "$x/gh", "\\gh", "'gh'", '"gh"', "$'gh'", "GH", "gh.exe", "g\\h"):
            with self.subTest(gh=gh):
                self.assertIn(_REASON, _verdict(_sized("", _READ_LIMIT + 1, f"{gh} pr create")) or "")


class NestedShellTest(unittest.TestCase):
    """`bash -c` の検出は、`c` を含むオプションだけを入れ子と読む。"""

    def test_option_with_c_is_nested(self):
        for opt in ("-c", "-lc", "-cl", "-xcl"):
            with self.subTest(opt=opt):
                self.assertIn("別の shell", _verdict(f"bash {opt} 'gh pr create'") or "")

    def test_option_without_c_is_not_nested(self):
        for opt in ("-l", "-x", "-lx"):
            with self.subTest(opt=opt):
                self.assertNotIn("別の shell", _verdict(f"bash {opt} 'gh pr create'") or "")


class TimeTest(unittest.TestCase):
    def _run(self, expr: str) -> float:
        script = (
            "import time\n"
            "from command import find_invocations, unresolved_reason\n"
            f"s = {expr}\n"
            "t = time.perf_counter()\n"
            "r = unresolved_reason(s, find_invocations(s))\n"
            "print(time.perf_counter() - t)\n"
            "print(r)\n"
        )
        # 子は理由 (日本語) を stdout に書く。Windows の pipe の既定は cp1252 で書けずに落ちるので、子も親も UTF-8 に固定する
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            r = subprocess.run(
                [sys.executable, "-c", script], cwd=_PKG, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60
            )
        except subprocess.TimeoutExpired:
            self.fail(f"60 秒を超えた: {expr}")
        self.assertEqual(r.returncode, 0, r.stderr)
        seconds, reason = r.stdout.splitlines()[:2]
        self.assertIn(_REASON, reason)
        return float(seconds)

    def test_child_output_does_not_depend_on_the_locale_encoding(self):
        # 環境が子の stdout を cp1252 にしていても (Windows の既定)、日本語の理由を読める
        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}):
            self.assertLess(self._run("'gh pr create; ' + 'a=$(' * 262144"), 10)

    def test_forms_that_are_quadratic_to_read_stop_without_reading(self):
        # 読むと 1 MB で hook の時間切れをはるかに超える形 (`sh -c` の並びは 32 KB で 2〜3 秒で、2 乗で伸びるので 1 MB ならその約 1000 倍)
        size = 1024 * 1024
        for unit in ("sh -c", "--repo=", "(a=' '", "\n", "a=$("):
            with self.subTest(unit=unit):
                self.assertLess(self._run(f"'gh pr create; ' + {unit!r} * {size // len(unit)}"), 10)


if __name__ == "__main__":
    unittest.main()
