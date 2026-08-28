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
    _has_hard_stop,
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


if __name__ == "__main__":
    unittest.main()
