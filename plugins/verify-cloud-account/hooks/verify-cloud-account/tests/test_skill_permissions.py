"""skill の `allowed-tools` が、読み取り専用の builder 呼び出しだけを確認なしにすることを固定する。

`allowed-tools` は制限ではなく付与で、skill を呼んだターンの間、一致したコマンドを
権限確認なしで通す。builder を `*` 付きで付与すると、期待値を書き換える `set` /
`remove` / `auto-switch` の `--commit` や、期待値を表示する `--show-values` まで確認なしで
通り、誤った呼び出しやプロンプトインジェクションで、この plugin が守る期待値を
変えられる (マージ前レビューの指摘)。argparse は option の省略形 (`--show` /
`--com`) も受け付けるので、`*` で「この option だけは除く」は書けない。付与は
引数まで書いた完全一致に限る。

照合は Claude Code の docs (permissions の Wildcard patterns) の規則を広めに見積もって
再現する: `*` は空白を含む任意の文字列、末尾の ` *` は引数なしの素のコマンドにも
一致、括弧の無い `Bash` はすべての Bash に一致。広めの照合で一致しなければ、実際の
照合でも一致しない。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import _testutil  # noqa: F401

from services import ALL as SERVICES  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = PLUGIN_ROOT / "skills"
BUILDER = (
    'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/accounts_builder.py"'
)
SERVICE_NAMES = tuple(svc.ACCOUNT_KEY for svc in SERVICES)
PIN_SERVICE_NAMES = ("aws", "gcloud", "firebase")

# skill ごとに確認なしにする Bash コマンド。何も書かず値も出さない形を、引数まで書く。
EXPECTED_GRANTS = {
    "accounts-init": tuple(
        f"{BUILDER} init --service {name} --dry-run" for name in SERVICE_NAMES
    ),
    "accounts-migrate": (f"{BUILDER} migrate --dry-run",),
    "accounts-show": (f"{BUILDER} show",)
    + tuple(f"{BUILDER} show --service {name}" for name in SERVICE_NAMES),
    "auto-switch": (
        "gh auth status",
        f"{BUILDER} show",
        f"{BUILDER} auto-switch --enable --dry-run",
        f"{BUILDER} auto-switch --disable --dry-run",
    ),
    "project-accounts": (f"{BUILDER} pin-env",)
    + tuple(f"{BUILDER} pin-env --service {name}" for name in PIN_SERVICE_NAMES)
    + (f"{BUILDER} show",),
}

# 確認なしで通ってはいけない builder の呼び出し: 書き込み、値の表示、任意のパス。
NEEDS_CONFIRMATION = tuple(
    f"{BUILDER} {args}"
    for args in (
        "set --service aws --value 111122223333 --commit",
        "set --service github --from-cli --commit",
        "remove --service aws --commit",
        "auto-switch --enable --commit",
        "auto-switch --enable --com",
        "auto-switch --disable --commit",
        "init --service github --commit",
        "init --service github --value someone --commit",
        "migrate --commit",
        "pin-env --show-values",
        "pin-env --show",
        "pin-env --service gcloud --show-values",
        "show --show-values",
        "show --service github --show-values",
        "init --service github --dry-run --show-values",
        "migrate --dry-run --show-values",
        "pin-env --path /elsewhere/accounts.local.json",
        "show --path /elsewhere/accounts.local.json",
    )
)

# 本文のコマンドのうち、これを含むものは確認を通す (書き込み・値の表示・値を渡す形)。
_CONFIRMED_OPTIONS = ("--commit", "--show-values", "--path", "--value", "--host", "--from-cli")


def _skill_text(skill: str) -> str:
    return (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def _allowed_tools(text: str) -> tuple[list[str], str]:
    """frontmatter の allowed-tools (YAML のブロック列) と、同じ行に書かれた値。"""
    lines = text.splitlines()
    end = lines.index("---", 1) if lines and lines[0] == "---" else 0
    entries: list[str] = []
    inline = ""
    in_list = False
    for line in lines[1:end]:
        if line.startswith("allowed-tools:"):
            inline = line[len("allowed-tools:"):].strip()
            in_list = True
            continue
        if in_list:
            match = re.match(r"\s+-\s+(.+)$", line)
            if match is None:
                in_list = False
                continue
            entry = match.group(1).strip()
            if len(entry) >= 2 and entry[0] == entry[-1] and entry[0] in "'\"":
                entry = entry[1:-1]
            entries.append(entry)
    return entries, inline


def _rule_matches(rule: str, command: str) -> bool:
    """Bash の permission rule が command に一致するか (docs の規則を広めに見積もる)。"""
    if rule == "Bash":
        return True
    match = re.fullmatch(r"Bash\((.*)\)", rule, re.DOTALL)
    if match is None:
        return False
    spec = match.group(1)
    if spec.endswith(":*"):
        spec = spec[:-2] + " *"
    if spec.endswith(" *"):
        pattern = ".*".join(re.escape(part) for part in spec[:-2].split("*")) + "(?: .*)?"
    else:
        pattern = ".*".join(re.escape(part) for part in spec.split("*"))
    return re.fullmatch(pattern, command, re.DOTALL) is not None


def _body_commands(text: str) -> list[str]:
    """本文のコードブロックにある builder / gh のコマンドを、書式を展開して返す。

    `[--service <svc>]` は付けない形と付けた形の両方、`<service>` / `<svc>` は全 service、
    それ以外の `<...>` は例の値に置き換える。
    """
    lines, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            inside = not inside
            continue
        if inside and (stripped.startswith(BUILDER) or stripped.startswith("gh ")):
            lines.append(stripped)
    commands = []
    for line in lines:
        variants = [line]
        optional = re.search(r" \[([^\]]+)\]", line)
        if optional:
            variants = [
                line[: optional.start()] + line[optional.end():],
                line[: optional.start()] + " " + optional.group(1) + line[optional.end():],
            ]
        for variant in variants:
            if re.search(r"<(?:service|svc)>", variant):
                named = [re.sub(r"<(?:service|svc)>", name, variant) for name in SERVICE_NAMES]
            else:
                named = [variant]
            commands.extend(re.sub(r"<[^>]+>", "example-value", c) for c in named)
    return commands


class TestRuleMatcher(unittest.TestCase):
    """照合の再現が docs の例どおりであること (下の検査が空振りしないため)。"""

    def test_documented_examples(self):
        cases = (
            ("Bash(npm run build)", "npm run build", True),
            ("Bash(npm run build)", "npm run build --watch", False),
            ("Bash(npm run *)", "npm run test --watch", True),
            ("Bash(npm run *)", "npm run", True),
            ("Bash(npm run *)", "npm install", False),
            ("Bash(ls *)", "ls", True),
            ("Bash(ls *)", "lsof", False),
            ("Bash(ls*)", "lsof", True),
            ("Bash(git * main)", "git push origin main", True),
            ("Bash(ls:*)", "ls -la", True),
            ("Bash", "anything at all", True),
            ("AskUserQuestion", "ls", False),
        )
        for rule, command, expected in cases:
            with self.subTest(rule=rule, command=command):
                self.assertEqual(_rule_matches(rule, command), expected)


class TestSkillAllowedTools(unittest.TestCase):
    def test_every_skill_is_covered(self):
        """skill を足したら EXPECTED_GRANTS にも足す (検査の空振り防止)。"""
        skills = sorted(path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md"))
        self.assertEqual(skills, sorted(EXPECTED_GRANTS))

    def test_allowed_tools_is_a_block_list(self):
        """この検査が読める形 (YAML のブロック列) で書かれていること。"""
        for skill in EXPECTED_GRANTS:
            with self.subTest(skill=skill):
                entries, inline = _allowed_tools(_skill_text(skill))
                self.assertEqual(inline, "")
                self.assertIn("AskUserQuestion", entries)

    def test_bash_grants_are_exactly_the_read_only_forms(self):
        for skill, expected in EXPECTED_GRANTS.items():
            with self.subTest(skill=skill):
                entries, _inline = _allowed_tools(_skill_text(skill))
                grants = [e for e in entries if e == "Bash" or e.startswith("Bash(")]
                self.assertEqual(sorted(grants), sorted(f"Bash({c})" for c in expected))

    def test_writes_and_reveals_are_not_preapproved(self):
        for skill, expected in EXPECTED_GRANTS.items():
            entries, _inline = _allowed_tools(_skill_text(skill))
            # 付与した形に option を足しただけのコマンドも通らないこと。
            extended = tuple(
                f"{command} {suffix}"
                for command in expected
                if command.startswith(BUILDER)
                for suffix in ("--commit", "--show-values", "--path /elsewhere/x.json")
            )
            for command in NEEDS_CONFIRMATION + extended:
                with self.subTest(skill=skill, command=command):
                    matched = [rule for rule in entries if _rule_matches(rule, command)]
                    self.assertEqual(matched, [])

    def test_commands_in_the_body_follow_the_grants(self):
        """本文のコマンドは、読み取り専用なら確認なし、書き込み・値の表示は確認ありになる。"""
        for skill in EXPECTED_GRANTS:
            text = _skill_text(skill)
            entries, _inline = _allowed_tools(text)
            commands = _body_commands(text)
            with self.subTest(skill=skill):
                self.assertTrue(commands, "本文からコマンドを拾えていない")
            for command in commands:
                confirmed = any(opt in command.split() for opt in _CONFIRMED_OPTIONS)
                with self.subTest(skill=skill, command=command):
                    matched = any(_rule_matches(rule, command) for rule in entries)
                    self.assertEqual(matched, not confirmed)


if __name__ == "__main__":
    unittest.main()
