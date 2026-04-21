"""``_normalize_segment_prefix`` (限定版) の単体テスト (0.3.2)。

剥がす対象:
- ``FOO=1`` 形式の env prefix (任意個)
- ``env`` (option 無し)
- ``command`` (option 無し)
- ``builtin`` / ``nohup``
- 上記の連鎖
- 絶対/相対パスの basename が上記 4 つに該当するもの

opaque (None 返却):
- ``bash``/``eval``/``sudo``/``awk``/``python3``/``time``/``exec``/``!`` 等の wrapper
- ``env`` / ``command`` のオプション付き呼び出し
- 任意 path exec (basename が transparent でない)
"""
from __future__ import annotations

import unittest

from _testutil import FIXTURES  # noqa: F401

from handlers.bash_handler import _normalize_segment_prefix


class TestPassThrough(unittest.TestCase):
    """剥がし対象なし (通常コマンド)。"""

    def test_plain_cat(self):
        self.assertEqual(
            _normalize_segment_prefix(["cat", ".env"]),
            ["cat", ".env"],
        )


class TestEnvPrefixStripping(unittest.TestCase):
    """``FOO=1 cmd`` 形式の env prefix を任意個剥がす。"""

    def test_single_assignment(self):
        self.assertEqual(
            _normalize_segment_prefix(["FOO=1", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_multiple_assignments(self):
        self.assertEqual(
            _normalize_segment_prefix(["FOO=1", "BAR=2", "cat", ".env"]),
            ["cat", ".env"],
        )


class TestEnvCommand(unittest.TestCase):
    """``env`` コマンド: option 無しのみ剥がす。"""

    def test_env_alone(self):
        self.assertEqual(
            _normalize_segment_prefix(["env", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_env_with_assignment(self):
        self.assertEqual(
            _normalize_segment_prefix(["env", "FOO=1", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_env_dash_i(self):
        self.assertIsNone(
            _normalize_segment_prefix(["env", "-i", "cat", ".env"]),
        )

    def test_env_dash_u(self):
        self.assertIsNone(
            _normalize_segment_prefix(["env", "-u", "HOME", "cat", ".env"]),
        )

    def test_env_double_dash(self):
        # `--` も "-" 始まりとして opaque 扱い (semantics 変動を避ける)
        self.assertIsNone(
            _normalize_segment_prefix(["env", "--", "cat", ".env"]),
        )


class TestCommandBuiltin(unittest.TestCase):
    """``command`` / ``builtin`` / ``nohup`` の剥がし。"""

    def test_command_alone(self):
        self.assertEqual(
            _normalize_segment_prefix(["command", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_command_double_dash(self):
        self.assertIsNone(
            _normalize_segment_prefix(["command", "--", "cat", ".env"]),
        )

    def test_command_dash_p(self):
        self.assertIsNone(
            _normalize_segment_prefix(["command", "-p", "cat", ".env"]),
        )

    def test_builtin(self):
        self.assertEqual(
            _normalize_segment_prefix(["builtin", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_nohup(self):
        self.assertEqual(
            _normalize_segment_prefix(["nohup", "cat", ".env"]),
            ["cat", ".env"],
        )


class TestAbsolutePathBasename(unittest.TestCase):
    """絶対/相対パス: basename が transparent (env/command/builtin/nohup) なら剥がす。"""

    def test_usr_bin_env_with_assignment(self):
        self.assertEqual(
            _normalize_segment_prefix(["/usr/bin/env", "FOO=1", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_bin_command(self):
        self.assertEqual(
            _normalize_segment_prefix(["/bin/command", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_bin_cat_opaque(self):
        # basename "cat" は transparent ではない → opaque
        self.assertIsNone(
            _normalize_segment_prefix(["/bin/cat", ".env"]),
        )

    def test_relative_script(self):
        self.assertIsNone(_normalize_segment_prefix(["./myscript"]))

    def test_dotdot_relative(self):
        self.assertIsNone(_normalize_segment_prefix(["../foo"]))

    def test_bin_bash_opaque(self):
        # basename "bash" は opaque wrapper でもあり transparent でもない → opaque
        self.assertIsNone(
            _normalize_segment_prefix(["/bin/bash", "-lc", "cat .env"]),
        )


class TestOpaqueWrappers(unittest.TestCase):
    """``_OPAQUE_WRAPPERS`` (bash / eval / python / sudo / awk / sed / time / exec / !) は None。"""

    def test_bash_c(self):
        self.assertIsNone(
            _normalize_segment_prefix(["bash", "-c", "cat .env"]),
        )

    def test_eval(self):
        self.assertIsNone(
            _normalize_segment_prefix(["eval", "cat", ".env"]),
        )

    def test_python_c(self):
        self.assertIsNone(
            _normalize_segment_prefix(["python3", "-c", "print(1)"]),
        )

    def test_sudo(self):
        self.assertIsNone(
            _normalize_segment_prefix(["sudo", "cat", ".env"]),
        )

    def test_xargs(self):
        self.assertIsNone(_normalize_segment_prefix(["xargs", "cat"]))

    def test_awk(self):
        self.assertIsNone(
            _normalize_segment_prefix(["awk", "{print}", "file"]),
        )

    def test_exec_with_options(self):
        # `exec -a name cmd` は opaque (exec 自体が _OPAQUE_WRAPPERS)
        self.assertIsNone(
            _normalize_segment_prefix(["exec", "-a", "name", "cat", ".env"]),
        )

    def test_exec_without_options(self):
        self.assertIsNone(
            _normalize_segment_prefix(["exec", "cat", ".env"]),
        )

    def test_time(self):
        self.assertIsNone(
            _normalize_segment_prefix(["time", "cat", ".env"]),
        )


class TestChaining(unittest.TestCase):
    """連鎖 prefix 剥がし。"""

    def test_nohup_command_chain(self):
        self.assertEqual(
            _normalize_segment_prefix(["nohup", "command", "cat", ".env"]),
            ["cat", ".env"],
        )

    def test_command_env_chain(self):
        self.assertEqual(
            _normalize_segment_prefix(
                ["command", "env", "FOO=1", "cat", ".env"]
            ),
            ["cat", ".env"],
        )


if __name__ == "__main__":
    unittest.main()
