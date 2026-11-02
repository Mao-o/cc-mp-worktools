"""scripts/check_skill_allowed_tools.py の回帰テスト (CLI を subprocess で実行する)。

fixture の repo root を tempdir に作り、`--root` で指して終了コードと出力を見る。
"""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check_skill_allowed_tools.py"
REPO_ROOT = SCRIPT.parent.parent

# スクリプトの ALLOWLIST の範囲に入る skill の相対パス (例外が効く範囲の確認用)
ALLOWED_REL = "plugins/llms-docs/skills/researching-ai-sdk/SKILL.md"
ALLOWED_SCOPE = "plugins/llms-docs/skills/"
CODEX_REL = "plugins/codex-pr-review/skills/iterating-pr-codex-reviews/SKILL.md"

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # CI には無い。手元にあるときだけ突き合わせる
    yaml = None

# FAIL になるべき frontmatter の本文 → stderr に出るべき文字列
FAIL_CASES: dict[str, tuple[str, str]] = {
    "trailing_star_list": ('allowed-tools:\n  - Bash(python3 "a/b.py" *)\n', "ワイルドカード"),
    "star_in_middle": ("allowed-tools:\n  - Bash(git * status)\n", "`Bash(git * status)`"),
    "legacy_prefix_star": ("allowed-tools:\n  - Bash(git:*)\n", "`Bash(git:*)`"),
    "bare_bash_list": ("allowed-tools:\n  - Read\n  - Bash\n", "`Bash` — Bash 単独"),
    "bare_bash_scalar": ("allowed-tools: Bash Read Write\n", "`Bash` — Bash 単独"),
    "star_in_scalar": ("allowed-tools: Read Bash(gh pr view *) Write\n", "`Bash(gh pr view *)`"),
    "flow_list_bare": ("allowed-tools: [Read, Bash]\n", "`Bash` — Bash 単独"),
    "flow_list_star": ("allowed-tools: [Read, 'Bash(ls *)']\n", "`Bash(ls *)`"),
    "quoted_list_item": ('allowed-tools:\n  - "Bash(ls *)"\n', "`Bash(ls *)`"),
    "unindented_list": ("allowed-tools:\n- Bash(ls *)\n", "`Bash(ls *)`"),
    "star_after_good_entry": (
        "allowed-tools:\n  - Bash(gh auth status)\n  - Bash(gh pr *)\n",
        "`Bash(gh pr *)`",
    ),
    # 以下、自前 parser が読み違えていた形 (マージ前レビューの指摘)
    "block_item_comment": ("allowed-tools:\n  - Bash(ls *)  # read only\n", "`Bash(ls *)` —"),
    "quoted_item_comment": ('allowed-tools:\n  - "Bash(ls *)" # c\n', "`Bash(ls *)` —"),
    "flow_bare_comment": ("allowed-tools: [Read, Bash]  # c\n", "`Bash` — Bash 単独"),
    "flow_star_comment": ("allowed-tools: [Read, Bash(git *)]  # note\n", "`Bash(git *)` —"),
    "col0_comment_between_items": ("allowed-tools:\n  - Read\n# note\n  - Bash\n", "`Bash` —"),
    "col0_comment_after_key": ("allowed-tools:\n# c\n  - Bash\n", "`Bash` —"),
    "multiline_flow": ("allowed-tools: [Read,\n  Bash(ls *)]\n", "`Bash(ls *)` —"),
    "multiline_flow_open_alone": (
        "allowed-tools: [\n  Read,\n  Bash(git *)\n]\n",
        "`Bash(git *)` —",
    ),
    "unclosed_flow": ("allowed-tools: [Read,\n  Read\n", "閉じていない"),
    "double_quoted_scalar": ('allowed-tools: "Read Bash(git *)"\n', "`Bash(git *)` —"),
    "single_quoted_scalar": ("allowed-tools: 'Read, Bash'\n", "`Bash` —"),
    "nested_flow_in_item": ("allowed-tools:\n  - [Bash]\n", "解釈できない付与 `[Bash]`"),
    "space_before_colon": ("allowed-tools : Bash\n", "`Bash` —"),
    "quoted_key": ('"allowed-tools": Bash\n', "`Bash` —"),
    "complex_key": ("? allowed-tools\n: Bash\n", "キーとして読めない行"),
    "duplicate_key":("allowed-tools: Read\nallowed-tools: Bash\n", "2 回以上"),
    "trailing_space_in_entry": ("allowed-tools:\n  - 'Bash(ls *) '\n", "`Bash(ls *)` —"),
    "tab_after_dash": ("allowed-tools:\n  -\tBash\n", "`Bash` —"),
    "space_in_tool": ("allowed-tools:\n  - Bash (ls *)\n", "解釈できない付与 `Bash (ls *)`"),
    "hash_inside_parens": ("allowed-tools:\n  - Bash(echo a #b)\n", "YAML ではコメントになる"),
    "dash_no_space_col0": ("allowed-tools:\n-Bash\n", "解析できない Bash"),
    "quoted_comma_inside_flow": (
        'allowed-tools: [Read, "Bash(git log, *)"]\n',
        "`Bash(git log, *)` —",
    ),
    "block_scalar": ("allowed-tools: |\n  Read Bash(ls *)\n", "`Bash(ls *)` —"),
}

PASS_CASES: dict[str, str] = {
    "exact_entries": (
        "allowed-tools:\n  - Bash(gh auth status)\n"
        '  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/s.py" show)\n  - AskUserQuestion\n'
    ),
    "star_in_non_bash_tool": "allowed-tools:\n  - Read(src/*)\n",
    "no_allowed_tools": "description: x\n",
    "next_key_is_not_a_continuation": "allowed-tools:\n  - Read\nmetadata:\n  author: Bash\n",
    "scalar_exact": "allowed-tools: Read Bash(ls) Write\n",
    "exact_with_comment": "allowed-tools:\n  - Bash(ls)  # read only\n  - Read\n",
    "flow_exact_multiline": "allowed-tools: [Read,\n  Bash(git status)]\n",
    "key_word_in_other_value": "description: allowed-tools の説明\nallowed-tools:\n  - Read\n",
    "hash_without_space":"allowed-tools:\n  - Bash(git log --format=#%h)\n",
}


def _run(root: pathlib.Path, script: pathlib.Path = SCRIPT, *, explicit_root: bool = True):
    cmd = [sys.executable, str(script)]
    if explicit_root:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_skill_allowed_tools", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def skill(self, rel: str, frontmatter_body: str, prefix: str = "") -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{prefix}---\nname: x\n{frontmatter_body}---\n\n# body\n", encoding="utf-8"
        )

    def ok_skill(self) -> None:
        self.skill("plugins/p/skills/ok/SKILL.md", "allowed-tools:\n  - Read\n")


class WildcardDetection(FixtureCase):
    def assert_fails(self, body: str, needle: str, prefix: str = "") -> None:
        self.ok_skill()
        self.skill("plugins/p/skills/bad/SKILL.md", body, prefix)
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
        for label, (body, needle) in FAIL_CASES.items():
            with self.subTest(label):
                self.setUp()
                self.assert_fails(body, needle)

    def test_bom_is_not_skipped(self) -> None:
        self.assert_fails("allowed-tools: Bash\n", "`Bash` —", prefix="\ufeff")

    def test_cases_pass(self) -> None:
        for label, body in PASS_CASES.items():
            with self.subTest(label):
                self.setUp()
                self.assert_passes(body)

    def test_reason_names_the_entry(self) -> None:
        self.ok_skill()
        self.skill("plugins/p/skills/bad/SKILL.md", "allowed-tools:\n  - Bash(ls *)\n")
        r = _run(self.root)
        self.assertIn("`Bash(ls *)`", r.stderr)


@unittest.skipUnless(yaml is not None, "PyYAML が無い環境では突き合わせない")
class AgreesWithYaml(unittest.TestCase):
    """自前 parser が問題なしとした読みは、YAML の読みと一致する。"""

    def test_parser_agrees_with_yaml_or_fails_closed(self) -> None:
        mod = _load_module()
        bodies = {k: v[0] for k, v in FAIL_CASES.items()} | PASS_CASES
        for label, body in bodies.items():
            with self.subTest(label):
                fm = body.rstrip("\n").split("\n")
                entries, problems = mod.parse_allowed_tools(fm)
                if problems or any(mod.violation(e) for e in entries):
                    continue  # 検査が FAIL にする形 (fail closed)。PASS にする読みだけを比べる
                try:
                    data = yaml.safe_load(body)
                except yaml.YAMLError:
                    self.fail(f"YAML として不正なのに問題なしと読んだ: {entries}")
                value = (data or {}).get("allowed-tools")
                if value is None:
                    expected: list[str] = []
                elif isinstance(value, str):
                    expected = [mod._unquote(t).strip() for t in mod._split_tokens(value)]
                else:
                    expected = [str(v).strip() for v in value]
                self.assertEqual(entries, expected)


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
        self.skill("plugins/other/skills/x/SKILL.md", "allowed-tools:\n  - Bash\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("plugins/other/skills/x/SKILL.md", r.stderr)

    def test_stale_allowlist_entry_fails(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Read\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ALLOWLIST", r.stderr)

    def test_missing_allowlisted_skill_fails_under_default_root(self) -> None:
        # 既定の root (スクリプトの 1 つ上) では、許可リストの skill が消えても FAIL
        script = self.root / "scripts" / SCRIPT.name
        script.parent.mkdir()
        shutil.copy(SCRIPT, script)
        self.ok_skill()
        r = _run(self.root, script, explicit_root=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"{ALLOWED_SCOPE}: ALLOWLIST", r.stderr)

    def test_new_skill_under_scope_passes_without_allowlist_edit(self) -> None:
        # skill の追加・削除で ALLOWLIST の変更が要らない
        self.skill("plugins/llms-docs/skills/brand-new/SKILL.md", "allowed-tools:\n  - Bash\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_one_skill_without_bash_is_fine_while_another_uses_it(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n")
        self.skill("plugins/llms-docs/skills/gone-exact/SKILL.md", "allowed-tools:\n  - Read\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_scope_does_not_cover_wildcard_in_scope_skill(self) -> None:
        self.skill("plugins/llms-docs/skills/new/SKILL.md", "allowed-tools:\n  - Bash(ls *)\n")
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("plugins/llms-docs/skills/new/SKILL.md: `Bash(ls *)`", r.stderr)

    def test_symlink_out_of_scope_is_not_covered(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n")
        self.skill("external/x/SKILL.md", "allowed-tools:\n  - Bash\n")
        link = self.root / "plugins/llms-docs/skills/link"
        link.symlink_to("../../../external/x", target_is_directory=True)
        r = _run(self.root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("plugins/llms-docs/skills/link/SKILL.md: `Bash`", r.stderr)

    def test_symlink_within_scope_stays_covered(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n")
        link = self.root / "plugins/llms-docs/skills/alias"
        link.symlink_to("researching-ai-sdk", target_is_directory=True)
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_scope_does_not_cover_other_plugins(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n")
        for rel in (
            "plugins/llms-docs2/skills/a/SKILL.md",
            "plugins/xllms-docs/skills/a/SKILL.md",
            "plugins/other/skills/a/SKILL.md",
        ):
            with self.subTest(rel):
                self.skill(rel, "allowed-tools:\n  - Bash\n")
                r = _run(self.root)
                self.assertEqual(r.returncode, 1)
                self.assertIn(f"{rel}: `Bash`", r.stderr)
                (self.root / rel).unlink()

    def test_codex_pr_review_grant_stays_per_skill(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n")
        self.skill(CODEX_REL, "allowed-tools:\n  - Bash\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.skill("plugins/codex-pr-review/skills/other/SKILL.md", "allowed-tools:\n  - Bash\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("plugins/codex-pr-review/skills/other/SKILL.md", r.stderr)

    def test_stale_codex_entry_fails(self) -> None:
        self.skill(ALLOWED_REL, "allowed-tools:\n  - Bash\n")
        self.skill(CODEX_REL, "allowed-tools:\n  - Read\n")
        r = _run(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{CODEX_REL}: ALLOWLIST", r.stderr)

    def test_missing_allowlisted_skill_is_skipped_under_explicit_root(self) -> None:
        self.ok_skill()
        r = _run(self.root)
        self.assertEqual(r.returncode, 0, r.stderr)


class RealRepo(unittest.TestCase):
    def test_repo_passes(self) -> None:
        r = _run(REPO_ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
