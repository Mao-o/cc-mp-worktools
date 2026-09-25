"""command.find_invocation の検出テスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from command import find_invocation, find_invocations


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

    def test_global_repo_flag_before_subcommand(self):
        for cmd in ("gh -R o/r pr create -t x", "gh --repo o/r pr create", "gh --repo=o/r pr ready 3"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(find_invocation(cmd))

    def test_repo_value_is_kept(self):
        self.assertEqual(find_invocation("gh -R o/r pr create").repo, "o/r")
        self.assertEqual(find_invocation("gh pr create --repo=o/r").repo, "o/r")
        self.assertEqual(find_invocation("gh pr ready 3 -R o/r").repo, "o/r")
        self.assertIsNone(find_invocation("gh pr create").repo)

    def test_all_invocations_are_returned(self):
        found = find_invocations("gh pr create --draft -t x && gh pr ready")
        self.assertEqual([(i.kind, i.draft) for i in found], [("create", True), ("ready", False)])

    def test_head_forms(self):
        for cmd in ("gh pr create -H feat", "gh pr create --head feat", "gh pr create --head=feat"):
            with self.subTest(cmd=cmd):
                self.assertEqual(find_invocation(cmd).head, "feat")
        self.assertIsNone(find_invocation("gh pr create").head)

    def test_redirections_are_not_separators(self):
        inv = find_invocation("gh pr create -t x 2>&1 | tee log")
        self.assertEqual(inv.kind, "create")
        self.assertIsNone(find_invocation("echo x &> gh pr create"))

    def test_unresolved_reason(self):
        from command import unresolved_reason

        def reason(cmd):
            return unresolved_reason(cmd, find_invocations(cmd))

        self.assertIsNone(reason("gh pr create -t x"))
        self.assertIsNone(reason("git push && gh pr create -t x && gh pr ready"))
        self.assertIsNone(reason("gh pr ready 3 --undo"))
        self.assertIsNone(reason('git commit -m "mention gh pr create here"'))
        self.assertIsNotNone(reason('u="$(gh pr create -t x)"'))
        self.assertIsNotNone(reason("u=`gh pr new`"))
        self.assertIsNotNone(reason("pushd x && gh pr create"))
        self.assertIsNotNone(reason('gh pr create --title "oops'))
        self.assertIsNotNone(reason("bash -c 'gh pr create'"))
        self.assertIsNone(reason("bash -c 'echo hi'"))
        self.assertIsNone(reason("gh pr -R o/r create"))

    def test_repo_flag_between_pr_and_action(self):
        inv = find_invocation("gh pr -R o/r create -t x")
        self.assertEqual((inv.kind, inv.repo), ("create", "o/r"))
        self.assertEqual(find_invocation("gh pr --repo=o/r ready 3").kind, "ready")

    def test_gh_repo_env_prefix(self):
        self.assertEqual(find_invocation("GH_REPO=o/r gh pr create").repo, "o/r")

    def test_new_alias(self):
        self.assertEqual(find_invocation("gh pr new -t x").kind, "create")
        self.assertTrue(find_invocation("gh pr new --draft").draft)

    def test_inside_shell_control_flow(self):
        for cmd in (
            "if true; then gh pr create -t x; fi",
            "if false; then :; else gh pr create; fi",
            "while true; do gh pr ready; break; done",
            "{ gh pr create; }",
            "! gh pr create",
            "(true); gh pr create",
            "(cd x)&& gh pr create",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(find_invocation(cmd))

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
