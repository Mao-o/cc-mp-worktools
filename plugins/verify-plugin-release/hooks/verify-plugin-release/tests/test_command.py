"""command.find_invocation の検出テスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from command import find_invocation


class FindInvocationTest(unittest.TestCase):
    def test_not_a_pr_command(self):
        for cmd in ("git status", "gh pr view 12", "gh pr list", "echo gh pr create", "ls pr"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(find_invocation(cmd))

    def test_plain_create(self):
        inv = find_invocation("gh pr create --title t --body b")
        self.assertEqual(inv.kind, "create")
        self.assertIsNone(inv.base)
        self.assertFalse(inv.draft)

    def test_base_forms(self):
        for cmd in ("gh pr create -B dev", "gh pr create --base dev", "gh pr create --base=dev"):
            with self.subTest(cmd=cmd):
                self.assertEqual(find_invocation(cmd).base, "dev")

    def test_draft(self):
        self.assertTrue(find_invocation("gh pr create --draft").draft)
        self.assertTrue(find_invocation("gh pr create -d -t x").draft)

    def test_compound_and_cd(self):
        inv = find_invocation("cd plugins/x && git push -u origin HEAD && gh pr create -t x")
        self.assertEqual(inv.kind, "create")
        self.assertEqual(inv.cd, "plugins/x")

    def test_multiline_body_with_substitution(self):
        cmd = 'gh pr create --title "t" --body "$(cat <<\'EOF\'\n## Summary\n- a; b && c\nEOF\n)"'
        inv = find_invocation(cmd)
        self.assertEqual(inv.kind, "create")
        self.assertTrue(inv.parsed)

    def test_newline_separated(self):
        inv = find_invocation("git push\ngh pr create -t x")
        self.assertEqual(inv.kind, "create")

    def test_env_prefix(self):
        self.assertEqual(find_invocation("GH_HOST=x gh pr create").kind, "create")
        self.assertEqual(find_invocation("env A=1 /usr/local/bin/gh pr create").kind, "create")

    def test_ready(self):
        inv = find_invocation("gh pr ready 42")
        self.assertEqual((inv.kind, inv.target), ("ready", "42"))
        self.assertIsNone(find_invocation("gh pr ready").target)
        self.assertEqual(find_invocation("gh pr ready -R o/r 7").target, "7")

    def test_ready_undo_is_ignored(self):
        self.assertIsNone(find_invocation("gh pr ready 42 --undo"))

    def test_unbalanced_quote_falls_back(self):
        inv = find_invocation('gh pr create --title "oops')
        self.assertEqual(inv.kind, "create")
        self.assertFalse(inv.parsed)

    def test_mention_inside_quoted_body_is_not_invocation(self):
        self.assertIsNone(find_invocation('git commit -m "run gh pr create later"'))


if __name__ == "__main__":
    unittest.main()
