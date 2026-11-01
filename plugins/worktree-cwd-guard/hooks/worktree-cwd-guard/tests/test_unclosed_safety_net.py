"""字句の正規化が閉じない frame / 終わらない here-document を残したときの安全網 (読みの和集合)。

正規化の読みが shell とずれると、here-document の本文や閉じない frame として後ろの行を読み落とす。
そのときは旧来の読み (行ごとの here-document の除去と shlex の comment) と、終わらない here-document を
here-document と読まない読みでも判定し、どれかで見つかったら止める。
"""
from __future__ import annotations

import unittest

import command
import test_guard
from test_guard import bash, decision, evaluate

_G = 'git -C "/m" reset --hard'


class UnclosedDetectionTest(unittest.TestCase):
    """run() が「閉じない frame / 終わらない here-document が残った」ことを返す。"""

    def _read(self, cmd: str) -> command._Normalizer:
        n = command._Normalizer(cmd)
        n.run()
        return n

    def test_unclosed_frames_and_heredocs_are_detected(self):
        for cmd, opened in (
            # 閉じない frame (here-document なし)
            (f'git commit -m "$(echo x\n" && {_G}', set()),
            (f"( echo a\n{_G}", set()),
            (f"echo `pwd\n{_G}", set()),
            (f"{_G}\necho 'abc", set()),
            # 本文が最後まで続いた here-document (閉じない frame の中を含む)
            (f"x=`cat <<E\n{_G}`", {7}),  # backtick の中の改行で本文が始まり、終端行が無い
            (f"cat > a.sh <<'EOF'\n{_G}\n", {11}),
            (f"echo $(cat <<E\n{_G}", {11}),
            (f"git commit -m \"$(cat <<'EOF'\nmsg\n)\" && {_G}", {21}),
            (f"echo ${{x:-<(cat <<2E\nbody\n)}}\n{_G}", {16}),
            # 本文が始まらなかった here-document (最後の行)
            (f"{_G}; cat <<E", {30}),
        ):
            with self.subTest(cmd=cmd):
                n = self._read(cmd)
                self.assertTrue(n.unclosed)
                self.assertEqual(n.open_heredocs, opened)

    def test_closed_input_is_not_flagged(self):
        for cmd in (
            _G,
            "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"",
            f"cat > x.sh <<'EOF'\n{_G}\nEOF\ngit status",
            f"cat <<-EOF\n\tbody\n\tEOF\n{_G}",
            f"cat <<E\nbody\nE",  # 終端行が最後の行 (改行なし)
            f"echo $(cat <<E) && git status\n{_G}",  # 同じ行で閉じるコマンド置換の中
            f"(( x = 1 << 2 ))\n{_G}",
            # 同じ行で閉じる backtick の中の here-document は、外の行を本文にしない (後ろに区切り語と同じ行があっても)
            f"x=`cat <<E `\n{_G}",
            f"x=`cat <<E `\n{_G}\nE",
            f"x=`echo \\`cat <<E \\``\n{_G}\nE",
            f"git status  # it's (\n{_G}",
        ):
            with self.subTest(cmd=cmd):
                n = self._read(cmd)
                self.assertFalse(n.unclosed)
                self.assertEqual(n.open_heredocs, set())

    def test_ignored_heredoc_is_read_as_code(self):
        cmd = f"cat <<E\n{_G}"
        n = command._Normalizer(cmd, frozenset({4}))
        self.assertIn(_G, n.run())
        self.assertFalse(n.unclosed)


class UnionReadingTest(test_guard.GuardBase):
    def _decide(self, cmd: str):
        return decision(evaluate(bash(cmd, self.a)))

    def test_backtick_heredoc_closed_on_the_same_line(self):
        # backtick の中の here-document は backtick の中で終わる。bash / zsh とも次の行の git を実行する
        # (0.2.0 の正規化は次の行から後ろを本文として飛ばしていた。旧来の読みは今も飛ばす)
        m = self.main
        g = f'git -C "{m}" reset --hard'
        for head in ("x=`cat <<E `", "echo `cat <<E;` ; echo", "echo `cat <<'E' `", "echo `cat <<E|cat`",
                     "echo `cat <<E >/dev/null`"):
            with self.subTest(head=head):
                self.assertEqual(self._decide(f"{head}\n{g}"), "deny")

    def test_backtick_heredoc_with_a_later_delimiter_line(self):
        # backtick の中の here-document は backtick の閉じで終わる。後ろに区切り語と同じ行があっても、
        # bash / zsh は次の行の git を実行する (閉じで here-document を終えないと、区切り語の行を終端行と読み、
        # 閉じた入力として安全網も働かなかった)
        g = f'git -C "{self.main}" reset --hard'
        for cmd in (
            f"x=`cat <<E `\n{g}\nE",
            f"v=`cat <<EOF `\n{g}\ngit commit -F - <<'EOF'\nmsg\nEOF",
            f"echo `cat <<E #c`\n{g}\nE",
            f"x=`echo \\`cat <<E \\``\n{g}\nE",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._decide(cmd), "deny")

    @staticmethod
    def _param_chain(k: int, g: str) -> str:
        # `${...}` の中の識別子の区切り語の here-document を k 個並べる。正規化はどれも終わらない here-document と読み、
        # 旧来の読みは 1 つ目の本文を最後まで飛ばす。bash 3.2 は `)}` で本文を打ち切って最後の git を実行する
        return "".join(f"echo ${{v{i}:-<(cat <<E{i}\nb\n)}}\n" for i in range(k)) + g

    def test_rereads_until_no_new_unterminated_heredoc(self):
        # 1 つ目を here-document と読まないと、2 つ目が新たに終わらないものとして現れる (k 回の読み直しで git に届く)
        g = f'git -C "{self.main}" reset --hard'
        for k in (1, 2, 3):
            with self.subTest(k=k):
                self.assertEqual(self._decide(self._param_chain(k, g)), "deny")

    def test_reread_limit_reaches_eight_chained_heredocs(self):
        # 読み直しの上限 (8 回) の境目
        g = f'git -C "{self.main}" reset --hard'
        self.assertEqual(self._decide(self._param_chain(8, g)), "deny")

    def test_heredoc_inside_parameter_expansion(self):
        # `${...}` の中の `<(cat <<2E ...)`。bash は `}` の後ろの git を実行する。旧来の読み (区切り語が識別子でないので
        # here-document と読まない) と読み直しのどちらでも止まる。閉じない frame が無く終わらない here-document だけが
        # 残る形で、旧来の読みだけが拾い、かつシェルが実際に後ろを実行する入力は見つかっていない (旧来の読みだけが
        # 拾うのは、どちらのシェルも何も実行しない形だけだった)
        m = self.main
        g = f'git -C "{m}" reset --hard'
        self.assertEqual(self._decide(f"echo ${{x:-<(cat <<2E\nbody\n)}}\n{g}"), "deny")

    def test_legacy_reading_for_unclosed_command_substitution(self):
        # 閉じない `"$(`: 旧来の読みは `"..."` を 1 語として読み、後ろの git を判定する
        m = self.main
        g = f'git -C "{m}" reset --hard'
        # 終わらない here-document: bash 3.2 は `)` で本文を打ち切って後ろの git を実行する
        self.assertEqual(self._decide(f"git commit -m \"$(cat <<'EOF'\nmsg\n)\" && {g}"), "deny")
        # here-document なし: bash / zsh とも構文エラーで何も実行しないが、読みが割れた入力は止める側に倒す
        self.assertEqual(self._decide(f'git commit -m "$(echo x\n" && {g}'), "deny")

    def test_legacy_reading_drops_comments(self):
        # 旧来の読みは shlex の comment を使う。comment の `'` で分解に失敗しない (入力はどちらのシェルでも
        # 構文エラー。上と同じく止める側に倒す)
        m = self.main
        g = f'git -C "{m}" reset --hard'
        self.assertEqual(self._decide(f'git commit -m "$(echo x\n" && {g}  # don\'t'), "deny")

    def test_legacy_reading_skips_heredoc_bodies(self):
        # 旧来の読みも here-document の本文は読まず、終端行の次から読む (1 つ目はどちらのシェルでも構文エラーで
        # 何も実行しない)
        m = self.main
        g = f'git -C "{m}" reset --hard'
        self.assertIsNone(evaluate(bash(f"cat > x.sh <<'EOF'\n{g}\nEOF\n( echo a", self.a)))
        self.assertEqual(self._decide(f'cat <<EOF\nbody\nEOF\ngit commit -m "$(echo x\n" && {g}'), "deny")

    def test_closed_input_does_not_use_the_other_readings(self):
        # 閉じた入力では正規化の読みだけで判定する (旧来の読みは `<<\\EOF` を here-document と読まず、本文の
        # git を止めてしまう)
        m = self.main
        g = f'git -C "{m}" reset --hard'
        self.assertIsNone(evaluate(bash(f"cat > x.sh <<\\EOF\n{g}\nEOF", self.a)))
        self.assertIsNone(evaluate(bash(f"cat > x.sh <<'END MSG'\n{g}\nEND MSG\ngit status", self.a)))

    def test_backtick_closed_after_a_heredoc_keeps_the_heredoc_body(self):
        # backtick の閉じで捨てるのは backtick の中で始めた here-document だけ。同じ行の手前 (外の深さ) で始めた
        # here-document の本文は次の行から始まり、本文の git はどちらのシェルでも実行されない
        g = f'git -C "{self.main}" reset --hard'
        for cmd in (f"cat > x.sh <<'EOF' && echo `pwd`\n{g}\nEOF", f"cat <<E `echo x`\n{g}\nE"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_findings_from_several_readings_are_not_duplicated(self):
        g = f'git -C "{self.main}" reset --hard'
        out = evaluate(bash(f"{g}\n" + self._param_chain(1, g), self.a))
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason.count("`git reset` が別の checkout"), 1, reason)


if __name__ == "__main__":
    unittest.main()
