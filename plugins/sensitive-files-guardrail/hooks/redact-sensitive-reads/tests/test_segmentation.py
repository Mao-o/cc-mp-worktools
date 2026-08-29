"""``handlers/bash/segmentation.py`` の pure helper テスト (0.22.0 新設)。

まず heredoc (``<<`` / ``<<-``) の本文を segment 分割から外す挙動を固定する。
0.21.x までは ``_split_command_on_operators`` が ``\\n`` で分割するだけだったため
heredoc 本文の各行が「コマンド」として解析され、本文の ``*`` や ``.env`` 様の
文字列が deny / ask を起こしていた (実ログの hard_stop_quoted 193 件のほぼ全部)。
``docs/DESIGN.md`` は以前から「``<<`` heredoc, ``<<-`` | delimiter/body は read
対象外」と宣言しており、実装を docs に合わせる修正。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import output
from handlers.bash.segmentation import (
    _EXPANSION_PLACEHOLDER,
    _has_hard_stop,
    _replace_simple_expansions,
    _split_command_on_operators,
)
from handlers.bash_handler import handle


class TestHeredocBodyIsNotASegment(unittest.TestCase):
    def test_quoted_delimiter_body_dropped(self):
        cmd = "cat > x.py <<'PY'\nn = kb * 1024\nPY"
        self.assertEqual(_split_command_on_operators(cmd), ["cat > x.py <<'PY'"])

    def test_unquoted_delimiter_body_dropped(self):
        self.assertEqual(
            _split_command_on_operators("cat <<EOF\nhello\nEOF"), ["cat <<EOF"],
        )

    def test_double_quoted_and_backslash_delimiters(self):
        self.assertEqual(
            _split_command_on_operators('cat <<"EOF"\nbody\nEOF'), ['cat <<"EOF"'],
        )
        self.assertEqual(
            _split_command_on_operators("cat <<\\EOF\nbody\nEOF"), ["cat <<\\EOF"],
        )
        self.assertEqual(
            _split_command_on_operators("cat << EOF\nbody\nEOF"), ["cat << EOF"],
        )

    def test_dash_form_strips_leading_tabs(self):
        cmd = "cat <<-EOF\n\tbody\n\tEOF\necho done"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<-EOF", "echo done"])

    def test_body_with_sensitive_looking_lines_dropped(self):
        cmd = "cat <<'EOF' > notes.txt\ncat .env\n*\n.env\nEOF"
        self.assertEqual(
            _split_command_on_operators(cmd), ["cat <<'EOF' > notes.txt"],
        )

    def test_commands_on_the_operator_line_are_still_split(self):
        cmd = "cat <<EOF && cat .env\nbody\nEOF"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<EOF", "cat .env"])
        cmd = "cat <<'EOF' | grep x\nfoo\nEOF"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<'EOF'", "grep x"])

    def test_commands_after_the_terminator_are_analyzed(self):
        cmd = "cat <<EOF\nbody\nEOF\ncat .env"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<EOF", "cat .env"])

    def test_multiple_heredocs_consumed_in_order(self):
        cmd = "cat <<A <<B\n1\nA\n2\nB\necho x"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<A <<B", "echo x"])

    def test_terminator_must_match_exactly(self):
        # 先頭空白付きの行は terminator ではない (``<<-`` でも tab 以外は剥がさない)
        cmd = "cat <<EOF\n EOF\nEOF\necho x"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<EOF", "echo x"])
        cmd = "cat <<-EOF\n  EOF\n\tEOF\necho x"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<-EOF", "echo x"])

    def test_here_string_is_not_a_heredoc(self):
        cmd = "cat <<< 'x'\ncat .env"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<< 'x'", "cat .env"])

    def test_unterminated_heredoc_falls_back_to_line_split(self):
        # ``$((1<<2))`` の ``<<`` を heredoc と誤認して以降を丸ごと本文扱いすると、
        # 後続の ``cat .env`` が解析されずに auto で素通りする。terminator が
        # 見つからない ``<<`` は heredoc として扱わない (0.21.x と同じ行分割)
        cmd = "echo $((1<<2))\ncat .env"
        self.assertEqual(_split_command_on_operators(cmd), ["echo $((1<<2))", "cat .env"])
        cmd = "cat <<EOF\nline1\nline2"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<EOF", "line1", "line2"])

    def test_arithmetic_shift_is_not_a_heredoc(self):
        # Codex R1 P1: ``$((1<<2))`` の ``<<`` はシフト演算子。terminator 未検出の
        # fallback だけに頼ると、後続に右オペランドと同じ行 (``2``) があったとき
        # ``cat .env`` が本文扱いで消える。算術式 (``$((`` / ``((``) の中では
        # heredoc を検出しない
        cases = {
            "echo $((1<<2))\ncat .env\n2": ["echo $((1<<2))", "cat .env", "2"],
            "(( x = 1<<2 ))\ncat .env\n2": ["(( x = 1<<2 ))", "cat .env", "2"],
            "echo $(( (1<<2) + 1 ))\ncat .env\n2": ["echo $(( (1<<2) + 1 ))", "cat .env", "2"],
            "x=$((a<<b))\ncat .env\nb": ["x=$((a<<b))", "cat .env", "b"],
            # 算術式を抜けた後の本物の heredoc は従来どおり
            "echo $((1<<2)); cat <<EOF\nbody\nEOF\necho x": ["echo $((1<<2))", "cat <<EOF", "echo x"],
            # Codex R2 P1: legacy の ``$[...]`` 算術展開も同じ (bash 3.2 実測:
            # ``echo $[1<<2]`` の後の ``cat`` は実行され、最終行 ``2]`` で失敗)
            "echo $[1<<2]\ncat .env\n2]": ["echo $[1<<2]", "cat .env", "2]"],
            "x=$[a<<b]\ncat .env\nb]": ["x=$[a<<b]", "cat .env", "b]"],
            "echo $[ $[1<<2] + a[1] ]\ncat .env\n2]": ["echo $[ $[1<<2] + a[1] ]", "cat .env", "2]"],
            "echo $[1<<2]; cat <<EOF\nbody\nEOF\necho x": ["echo $[1<<2]", "cat <<EOF", "echo x"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_split_command_on_operators(cmd), expected)

    def test_quoted_operator_is_not_a_heredoc(self):
        cmd = "echo '<<EOF'\ncat .env"
        self.assertEqual(_split_command_on_operators(cmd), ["echo '<<EOF'", "cat .env"])
        cmd = 'echo "a <<EOF"\ncat .env'
        self.assertEqual(_split_command_on_operators(cmd), ['echo "a <<EOF"', "cat .env"])

    def test_comment_after_operator_line(self):
        cmd = "cat <<EOF # note\nbody\nEOF\necho x"
        self.assertEqual(_split_command_on_operators(cmd), ["cat <<EOF", "echo x"])

    def test_operator_line_keeps_hard_stop(self):
        # ``<`` は hard-stop のまま (heredoc の行自体は ask_or_allow)
        for seg in _split_command_on_operators("cat <<EOF\nbody\nEOF"):
            self.assertTrue(_has_hard_stop(seg))


class TestHeredocVerdict(unittest.TestCase):
    """heredoc 込みのコマンドの verdict (default / auto)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.home = os.path.join(self.tmp, "home")
        self.xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(self.home)
        os.makedirs(self.xdg)
        patcher = mock.patch.dict(
            os.environ, {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, cmd: str, mode: str) -> dict:
        return handle({
            "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "test"},
            "cwd": self.tmp,
            "permission_mode": mode,
        })

    @staticmethod
    def _decision(resp: dict) -> str | None:
        return (resp.get("hookSpecificOutput") or {}).get("permissionDecision")

    def test_body_does_not_deny(self):
        # 0.21.x: 本文の ``n = kb * 1024`` が segment になり裸の ``*`` で deny
        # (reason は matched_operand: * / first_token: n でユーザーには意味不明)
        for cmd in (
            "cat > x.py <<'PY'\nn = kb * 1024\nPY",
            "cat <<'EOF' > notes.txt\n.env\n*\nEOF",
            "cat <<EOF\ncat .env\nEOF",
            "tee -a notes.md <<'EOF'\nsee .env.example\nEOF",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._decision(self._run(cmd, "default")), "ask")
                self.assertTrue(output.is_allow(self._run(cmd, "auto")))

    def test_real_segments_around_heredoc_still_deny(self):
        for cmd in (
            "cat <<EOF && cat .env\nbody\nEOF",
            "cat <<EOF\nbody\nEOF\ncat .env",
            "echo $((1<<2))\ncat .env",
            "echo $((1<<2))\ncat .env\n2",
            "(( x = 1<<2 ))\ncat .env\n2",
            "echo $[1<<2]\ncat .env\n2]",
            "cat <<EOF | tee .env\nbody\nEOF",
        ):
            for mode in ("default", "auto"):
                with self.subTest(cmd=cmd, mode=mode):
                    self.assertEqual(self._decision(self._run(cmd, mode)), "deny")

    def test_interpreter_heredoc_is_opaque_like_dash_c(self):
        # ``bash <<EOF`` / ``python3 - <<PY`` は ``bash -c '…'`` と同じ opaque
        # wrapper (ask_or_allow)。0.21.x は本文の ``cat .env`` が偶然 segment に
        # なって deny していたが、それは設計された guard ではない (DESIGN.md
        # 「対応外: heredoc」「autonomous モードでの opaque 緩和」)
        for cmd in (
            "bash <<EOF\ncat .env\nEOF",
            "python3 - <<'PY'\nprint(open('.env').read())\nPY",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._decision(self._run(cmd, "default")), "ask")
                self.assertTrue(output.is_allow(self._run(cmd, "auto")))


class TestReplaceSimpleExpansions(unittest.TestCase):
    """0.25.0: hard-stop 救済 scan の前処理 (単純変数展開の placeholder 置換)。"""

    P = _EXPANSION_PLACEHOLDER

    def test_plain_and_braced_names(self):
        self.assertEqual(
            _replace_simple_expansions("cat $PWD/.env"), f"cat {self.P}/.env"
        )
        self.assertEqual(
            _replace_simple_expansions("cat ${PWD}/.env"), f"cat {self.P}/.env"
        )

    def test_double_quoted_expansion_is_live(self):
        self.assertEqual(
            _replace_simple_expansions('cat "$PWD/.env"'), f'cat "{self.P}/.env"'
        )

    def test_single_quoted_and_escaped_are_literal(self):
        self.assertIsNone(_replace_simple_expansions("echo '$PWD/.env'"))
        self.assertIsNone(_replace_simple_expansions("cat \\$PWD/.env"))
        # ダブルクォート内の \$ も literal
        self.assertIsNone(_replace_simple_expansions('echo "\\$PWD"'))

    def test_complex_forms_are_not_replaced(self):
        # 置換対象ゼロ → None
        self.assertIsNone(_replace_simple_expansions("cat $(pwd)/.env"))
        self.assertIsNone(_replace_simple_expansions("cat ${X:-fallback}/.env"))
        self.assertIsNone(_replace_simple_expansions("echo $? $1 $@"))
        # 単純形と複合形が同居する場合は単純形だけ置換し、複合形は残す
        out = _replace_simple_expansions("cat $(x) $PWD/.env")
        self.assertEqual(out, f"cat $(x) {self.P}/.env")
        self.assertTrue(_has_hard_stop(out))  # 呼び出し側はここで救済を断念する

    def test_longest_match_name(self):
        self.assertEqual(
            _replace_simple_expansions("cat $PWDx/.env"), f"cat {self.P}/.env"
        )

    def test_adjacent_expansions(self):
        self.assertEqual(
            _replace_simple_expansions("cat $A$B/.env"),
            f"cat {self.P}{self.P}/.env",
        )

    def test_no_expansion_returns_none(self):
        self.assertIsNone(_replace_simple_expansions("cat .env"))
        self.assertIsNone(_replace_simple_expansions("echo cost$"))

    def test_placeholder_survives_hard_stop_check(self):
        out = _replace_simple_expansions("cat $PWD/.env")
        self.assertIsNotNone(out)
        self.assertFalse(_has_hard_stop(out))


if __name__ == "__main__":
    unittest.main()
