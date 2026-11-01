"""command.find_invocation の検出テスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from command import find_invocation, find_invocations, unresolved_reason


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


class CommentHandlingTest(unittest.TestCase):
    """行末コメントが次の行を巻き込まない / 単語の途中・引用符の中の `#` は切らない。"""

    def _kinds(self, cmd):
        found = find_invocations(cmd)
        self.assertIsNone(unresolved_reason(cmd, found), cmd)
        return [i.kind for i in found]

    def test_trailing_comment_then_next_line(self):
        cases = {
            "create after comment": ("git status # check\ngh pr create --fill", ["create"]),
            "ready after comment": ("git push  # c\ngh pr ready", ["ready"]),
            "comment right after separator": ("git status;# don't\ngh pr create", ["create"]),
            "comment only line": ("# don't\ngh pr create", ["create"]),
            "comment after pipe": ("git log |# don't\ngh pr create", ["create"]),
            "comment after ampersand": ("git log &&# don't\ngh pr create", ["create"]),
            "comment after open paren": ("(# don't\ngh pr create)", ["create"]),
            "comment after the invocation": ("gh pr create # c", ["create"]),
            "comment after redirect": ("git log >/dev/null # c\ngh pr ready", ["ready"]),
            "escaped single quote before comment": ("echo \\' # c\ngh pr create", ["create"]),
            "escaped double quote before comment": ('echo \\" # c\ngh pr create', ["create"]),
            "escaped backslash before comment": ("echo \\\\ # c\ngh pr create", ["create"]),
        }
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(self._kinds(cmd), want)

    def test_hash_inside_a_word_is_not_a_comment(self):
        cases = {
            "word with hash": "echo feature#1\ngh pr create",
            "parameter expansion": "echo ${x##*/}\ngh pr create",
            "string length": "echo ${#x} $#\ngh pr create",
            "escaped hash": "echo \\# x\ngh pr create",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                self.assertEqual(self._kinds(cmd), ["create"])

    def test_hash_inside_quotes_is_not_a_comment(self):
        cases = {
            "single": "echo '# x'\ngh pr create",
            "double": 'echo "a # b"\ngh pr create',
            "double with escaped quote": 'echo "a\\" # b"\ngh pr create',
            "single quote spanning lines": "echo '# a\n# b'\ngh pr create",
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                self.assertEqual(self._kinds(cmd), ["create"])

    def test_commented_out_invocation_is_not_found(self):
        self.assertEqual(find_invocations("git status # gh pr create\ngit diff"), [])

    def test_option_after_comment_line_is_not_swallowed(self):
        inv = find_invocation("git status # c\ngh pr create --base dev # more")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.base, "dev")

    def test_hash_inside_an_argument_is_kept(self):
        inv = find_invocation("gh pr create --base feature#1 --head x${y##*/}z --draft")
        self.assertIsNotNone(inv)
        self.assertEqual((inv.base, inv.head, inv.draft), ("feature#1", "x${y##*/}z", True))

    def test_backslash_newline_joins_lines(self):
        cases = {
            "between words": ("gh pr \\\ncreate --fill", ["create"]),
            "before option": ("gh pr create \\\n --draft", ["create"]),
            "hash after join is mid-word": ("echo a\\\n# x\ngh pr create", ["create"]),
            "comment after join with space": ("echo a \\\n# x\ngh pr create", ["create"]),
        }
        for label, (cmd, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(self._kinds(cmd), want)

    def test_continuation_option_is_parsed(self):
        inv = find_invocation("gh pr create \\\n --draft")
        self.assertIsNotNone(inv)
        self.assertTrue(inv.draft)


    def test_escaped_space_before_hash_is_not_a_comment(self):
        inv = find_invocation("gh pr create --title a\\ #b --base dev")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.base, "dev")

    def test_continuation_is_not_replaced_by_a_space(self):
        # `\`+改行は消えるだけ。空白に置き換えると `#1` が単語の先頭になりコメントと読まれる
        inv = find_invocation("gh pr create --title a\\\n#1 --base dev")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.base, "dev")

    def test_redirect_operator_is_a_word_start_for_comments(self):
        self.assertEqual(self._kinds("git log ># don't\ngh pr create"), ["create"])

    def test_safety_nets_see_joined_continuations(self):
        # shlex が ValueError になる (閉じていない ') 行の後でも、継続行をまたいだ gh pr create を検出する
        cmd = "echo it's done\ngh pr \\\n  create --body-file b.md"
        found = find_invocations(cmd)
        self.assertEqual([i.kind for i in found], ["create"])
        self.assertIsNotNone(unresolved_reason(cmd, found))

    def test_ready_undo_across_a_continuation_is_not_counted(self):
        cmd = "gh pr ready \\\n  --undo"
        self.assertIsNone(unresolved_reason(cmd, find_invocations(cmd)))

    def test_fallback_joins_only_real_continuations(self):
        # 閉じていない引用符 -> 安全網。`\\`+改行は継続行ではない (バックスラッシュ自体のエスケープ)
        found = find_invocations("echo it's \\\\\ngh pr create")
        self.assertEqual([i.kind for i in found], ["create"])
        # 継続行でつないだ結果 `t--draft` は --draft ではない
        found = find_invocations("echo it's\ngh pr create --title t\\\n--draft")
        self.assertEqual([(i.kind, i.draft) for i in found], [("create", False)])


if __name__ == "__main__":
    unittest.main()
