"""scripts/check_skill_allowed_tools.py の回帰テスト (CLI を subprocess で実行する)。

fixture の repo root を tempdir に作り、`--root` で指して終了コードと出力を見る。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check_skill_allowed_tools.py"
REPO_ROOT = SCRIPT.parent.parent

# スクリプトの ALLOWLIST に載っている skill の相対パス (例外が効く範囲の確認用)
ALLOWED_REL = "plugins/llms-docs/skills/researching-ai-sdk/SKILL.md"


def _run(root: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def skill(self, rel: str, frontmatter_body: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: x\n{frontmatter_body}---\n\n# body\n", encoding="utf-8")

    def ok_skill(self) -> None:
        self.skill("plugins/p/skills/ok/SKILL.md", "allowed-tools:\n  - Read\n")


class WildcardDetection(FixtureCase):
    def assert_fails(self, body: str, needle: str = "") -> None:
        self.ok_skill()
        self.skill("plugins/p/skills/bad/SKILL.md", body)
        r = _run(self.root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("plugins/p/skills/bad/SKILL.md", r.stderr)
        self.assertIn(needle, r.stderr)

    def assert_passes(self, body: str) -> None:
        self.ok_skill()
        self.skill("plugins/p/skills/good/SKILL.md", body)
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_cases_fail(self) -> None:
        cases = {
            "trailing_star_list": 'allowed-tools:\n  - Bash(python3 "a/b.py" *)\n',
            "star_in_middle": "allowed-tools:\n  - Bash(git * status)\n",
            "legacy_prefix_star": "allowed-tools:\n  - Bash(git:*)\n",
            "bare_bash_list": "allowed-tools:\n  - Read\n  - Bash\n",
            "bare_bash_scalar": "allowed-tools: Bash Read Write\n",
            "star_in_scalar": 'allowed-tools: Read Bash(gh pr view *) Write\n',
            "flow_list_bare": "allowed-tools: [Read, Bash]\n",
            "flow_list_star": "allowed-tools: [Read, 'Bash(ls *)']\n",
            "quoted_list_item": "allowed-tools:\n  - \"Bash(ls *)\"\n",
            "unindented_list": "allowed-tools:\n- Bash(ls *)\n",
            "star_after_good_entry": (
                "allowed-tools:\n  - Bash(gh auth status)\n  - Bash(gh pr *)\n"
            ),
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.setUp()
                self.assert_fails(body)

    def test_cases_pass(self) -> None:
        cases = {
            "exact_entries": (
                "allowed-tools:\n  - Bash(gh auth status)\n"
                '  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/s.py" show)\n  - AskUserQuestion\n'
            ),
            "star_in_non_bash_tool": "allowed-tools:\n  - Read(src/*)\n",
            "no_allowed_tools": "description: x\n",
            "next_key_is_not_a_continuation": (
                "allowed-tools:\n  - Read\nmetadata:\n  author: Bash\n"
            ),
            "scalar_exact":"allowed-tools: Read Bash(ls) Write\n",
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.setUp()
                self.assert_passes(body)

    def test_reason_names_the_entry(self) -> None:
        self.ok_skill()
        self.skill("plugins/p/skills/bad/SKILL.md", "allowed-tools:\n  - Bash(ls *)\n")
        r = _run(self.root)
        self.assertIn("`Bash(ls *)`", r.stderr)


class EmptyAndSkipped(FixtureCase):
    def test_no_skills_is_failure(self) -> None:
        (self.root / "plugins").mkdir()
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 件も見つかりません", r.stderr)

    def test_skill_without_frontmatter_is_skipped(self) -> None:
        self.ok_skill()
        path = self.root / "plugins/p/skills/nofm/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("allowed-tools:\n  - Bash\n", encoding="utf-8")
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)


class AllowlistScope(FixtureCase):
    def test_allowlisted_exact_entry_passes(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Read\n  - Bash\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_allowlist_does_not_cover_other_wildcard_in_same_skill(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n  - Bash(ls *)\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("`Bash(ls *)`", r.stderr)

    def test_allowlist_does_not_cover_same_entry_in_other_skill(self) -> None:
        self.skill("plugins/llms-docs/skills/other/SKILL.md", "allowed-tools:\n  - Bash\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("plugins/llms-docs/skills/other/SKILL.md", r.stderr)

    def test_stale_allowlist_entry_fails(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Read\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ALLOWLIST", r.stderr)


class RealRepo(unittest.TestCase):
    def test_repo_passes(self) -> None:
        r = _run(REPO_ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
