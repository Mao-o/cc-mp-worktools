"""command_parser: split / env strip / wrapper strip / extract_candidates のテスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from core.command_parser import (  # noqa: E402
    extract_candidates,
    split_on_operators,
    strip_leading_env,
    strip_transparent_wrappers,
)


class TestSplitOnOperators(unittest.TestCase):
    def test_single_command(self):
        self.assertEqual(split_on_operators("gh pr list"), ["gh pr list"])

    def test_and_operator(self):
        self.assertEqual(split_on_operators("a && b"), ["a", "b"])

    def test_or_operator(self):
        self.assertEqual(split_on_operators("a || b"), ["a", "b"])

    def test_semicolon(self):
        self.assertEqual(split_on_operators("a ; b"), ["a", "b"])

    def test_pipe(self):
        self.assertEqual(split_on_operators("a | b"), ["a", "b"])

    def test_newline(self):
        self.assertEqual(split_on_operators("a\nb"), ["a", "b"])

    def test_mixed(self):
        self.assertEqual(
            split_on_operators("a && b ; c | d || e"),
            ["a", "b", "c", "d", "e"],
        )

    def test_double_quotes_protect(self):
        self.assertEqual(
            split_on_operators('echo "a && b"'),
            ['echo "a && b"'],
        )

    def test_single_quotes_protect(self):
        self.assertEqual(
            split_on_operators("echo 'a ; b'"),
            ["echo 'a ; b'"],
        )

    def test_subshell_protect(self):
        self.assertEqual(
            split_on_operators("cmd $(date && echo x)"),
            ["cmd $(date && echo x)"],
        )

    def test_backtick_protect(self):
        self.assertEqual(
            split_on_operators("cmd `date; echo x`"),
            ["cmd `date; echo x`"],
        )

    def test_trim_and_filter_empty(self):
        self.assertEqual(split_on_operators("  ;;  "), [])
        self.assertEqual(split_on_operators(""), [])

    def test_escape_ampersand(self):
        self.assertEqual(
            split_on_operators(r"echo a\&\&b && echo c"),
            [r"echo a\&\&b", "echo c"],
        )


class TestStripLeadingEnv(unittest.TestCase):
    def test_single_assignment(self):
        self.assertEqual(strip_leading_env("FOO=bar gh pr list"), "gh pr list")

    def test_multiple_assignments(self):
        self.assertEqual(
            strip_leading_env("FOO=bar BAZ=qux gh pr list"),
            "gh pr list",
        )

    def test_quoted_value(self):
        self.assertEqual(
            strip_leading_env('FOO="a b c" gh pr list'),
            "gh pr list",
        )

    def test_no_assignment(self):
        self.assertEqual(strip_leading_env("gh pr list"), "gh pr list")

    def test_assignment_only_kept(self):
        """`FOO=bar` だけのケースは剥がすと空コマンドになるので保持。"""
        self.assertEqual(strip_leading_env("FOO=bar"), "FOO=bar")

    def test_subshell_value_kept(self):
        """値に $() を含む場合は保守的に剥がさない。"""
        self.assertEqual(
            strip_leading_env("FOO=$(date) gh pr list"),
            "FOO=$(date) gh pr list",
        )

    def test_backtick_value_kept(self):
        self.assertEqual(
            strip_leading_env("FOO=`date` gh pr list"),
            "FOO=`date` gh pr list",
        )

    def test_empty_value(self):
        self.assertEqual(strip_leading_env("FOO= gh pr list"), "gh pr list")


class TestStripTransparentWrappers(unittest.TestCase):
    def test_sudo(self):
        self.assertEqual(strip_transparent_wrappers("sudo gh pr list"), "gh pr list")

    def test_time(self):
        self.assertEqual(strip_transparent_wrappers("time gh pr list"), "gh pr list")

    def test_nohup(self):
        self.assertEqual(strip_transparent_wrappers("nohup gh pr list"), "gh pr list")

    def test_command_builtin(self):
        self.assertEqual(
            strip_transparent_wrappers("command gh pr list"), "gh pr list"
        )
        self.assertEqual(
            strip_transparent_wrappers("builtin gh pr list"), "gh pr list"
        )

    def test_exec(self):
        self.assertEqual(strip_transparent_wrappers("exec gh pr list"), "gh pr list")

    def test_env_simple(self):
        self.assertEqual(
            strip_transparent_wrappers("env FOO=bar gh pr list"),
            "gh pr list",
        )

    def test_env_with_option_not_stripped(self):
        """env -i / env --  など option 付きは挙動が変わるため剥がさない。"""
        self.assertEqual(
            strip_transparent_wrappers("env -i gh pr list"),
            "env -i gh pr list",
        )
        self.assertEqual(
            strip_transparent_wrappers("env -- gh pr list"),
            "env -- gh pr list",
        )

    def test_npx(self):
        self.assertEqual(
            strip_transparent_wrappers("npx firebase deploy"),
            "firebase deploy",
        )

    def test_pnpm_exec(self):
        self.assertEqual(
            strip_transparent_wrappers("pnpm exec firebase deploy"),
            "firebase deploy",
        )

    def test_pnpm_dlx(self):
        self.assertEqual(
            strip_transparent_wrappers("pnpm dlx firebase deploy"),
            "firebase deploy",
        )

    def test_mise_exec(self):
        self.assertEqual(
            strip_transparent_wrappers("mise exec -- firebase deploy"),
            "firebase deploy",
        )

    def test_bun_x(self):
        self.assertEqual(
            strip_transparent_wrappers("bun x firebase deploy"),
            "firebase deploy",
        )

    def test_stacked_wrappers(self):
        self.assertEqual(
            strip_transparent_wrappers("sudo time gh pr list"),
            "gh pr list",
        )

    def test_env_assign_mixed_with_wrapper(self):
        self.assertEqual(
            strip_transparent_wrappers("FOO=bar sudo gh pr list"),
            "gh pr list",
        )

    def test_no_wrapper(self):
        self.assertEqual(strip_transparent_wrappers("gh pr list"), "gh pr list")

    def test_bash_c_not_stripped(self):
        """`bash -c` は script 実行なので透過剥がし対象外 (= 検証対象外)。"""
        self.assertEqual(
            strip_transparent_wrappers("bash -c 'gh pr list'"),
            "bash -c 'gh pr list'",
        )


class TestExtractCandidates(unittest.TestCase):
    def test_chain_with_cd(self):
        self.assertEqual(
            extract_candidates("cd /tmp && gh pr create"),
            ["cd /tmp", "gh pr create"],
        )

    def test_env_prefix_stripped(self):
        self.assertEqual(
            extract_candidates("FOO=bar gh pr create"),
            ["gh pr create"],
        )

    def test_sudo_stripped(self):
        self.assertEqual(
            extract_candidates("sudo gh pr create"),
            ["gh pr create"],
        )

    def test_readonly_and_mutating_both_surfaced(self):
        self.assertEqual(
            extract_candidates("gh auth status && gh pr list"),
            ["gh auth status", "gh pr list"],
        )

    def test_nested_wrappers(self):
        self.assertEqual(
            extract_candidates("sudo time mise exec -- firebase deploy"),
            ["firebase deploy"],
        )

    def test_quoted_command_not_decomposed(self):
        self.assertEqual(
            extract_candidates('echo "gh auth status"'),
            ['echo "gh auth status"'],
        )

    def test_empty_command(self):
        self.assertEqual(extract_candidates(""), [])
        self.assertEqual(extract_candidates("   "), [])


if __name__ == "__main__":
    unittest.main()
