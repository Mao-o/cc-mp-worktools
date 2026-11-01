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
            (f"x=`cat <<E `\n{_G}", {7}),
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
            f"git status  # it's (\n{_G}",
        ):
            with self.subTest(cmd=cmd):
                n = self._read(cmd)
                self.assertFalse(n.unclosed)
                self.assertEqual(n.open_heredocs, set())

    def test_ignored_heredoc_is_read_as_code(self):
        cmd = f"x=`cat <<E `\n{_G}"
        n = command._Normalizer(cmd, frozenset({7}))
        self.assertIn(_G, n.run())
        self.assertFalse(n.unclosed)


class UnionReadingTest(test_guard.GuardBase):
    def _decide(self, cmd: str):
        return decision(evaluate(bash(cmd, self.a)))

    def test_backtick_heredoc_closed_on_the_same_line(self):
        # backtick の中の here-document は backtick の中で終わる。bash / zsh とも次の行の git を実行する
        # (正規化の読みは次の行から後ろを本文として飛ばす。旧来の読みも同じ)
        m = self.main
        g = f'git -C "{m}" reset --hard'
        for head in ("x=`cat <<E `", "echo `cat <<E;` ; echo", "echo `cat <<'E' `", "echo `cat <<E|cat`",
                     "echo `cat <<E >/dev/null`"):
            with self.subTest(head=head):
                self.assertEqual(self._decide(f"{head}\n{g}"), "deny")

    def test_rereads_until_no_new_unterminated_heredoc(self):
        # 1 つ目を here-document と読まないと、2 つ目が新たに終わらないものとして現れる
        m = self.main
        g = f'git -C "{m}" reset --hard'
        self.assertEqual(self._decide(f"x=`cat <<E `\ny=`cat <<F `\n{g}"), "deny")
        self.assertEqual(self._decide(f"x=`cat <<E `\ny=`cat <<F `\nz=`cat <<G `\n{g}"), "deny")

    def test_legacy_reading_for_heredoc_inside_parameter_expansion(self):
        # `${...}` の中の `<(cat <<2E ...)`。bash は `}` の後ろの git を実行する
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

    def test_findings_from_several_readings_are_not_duplicated(self):
        m = self.main
        out = evaluate(bash(f'git -C "{m}" reset --hard\nx=`cat <<E `\ngit -C "{m}" reset --hard', self.a))
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason.count("`git reset` が別の checkout"), 1, reason)


if __name__ == "__main__":
    unittest.main()
