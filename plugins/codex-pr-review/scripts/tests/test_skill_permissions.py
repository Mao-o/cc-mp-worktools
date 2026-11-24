"""skill の `allowed-tools` が `Bash` を単独で付与している理由が、SKILL.md の手順と揃っていることを固定する。

`allowed-tools` は制限ではなく付与で、`Bash` 単独はすべての Bash コマンドを確認なしにする。
この skill が打つコマンドはどれも呼び出しごとに変わる引数 (PR 番号・since・サマリ・
ファイルパス) を取り、完全一致の `Bash(<コマンド>)` では列挙できないので、単独の付与を残して
理由を SKILL.md に書いている。ここでは次を確かめる:

- 単独の付与があるときだけ理由の段落がある (付与を絞ったのに理由だけ残る、の逆も落とす)
- 理由が、手順で使う同梱スクリプトと `gh pr <sub>` をすべて名指ししている (手順にコマンドを
  足したら、理由も見直させる)
- 理由の前提 (同梱スクリプトは引数を必須にしている) が、スクリプトの実際の挙動と合っている
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
_PLUGIN_ROOT = _SCRIPTS.parent
_SKILL = _PLUGIN_ROOT / "skills" / "iterating-pr-codex-reviews" / "SKILL.md"
_BASH = shutil.which("bash")

REASON_LEAD = "`allowed-tools` の `Bash` を完全一致の `Bash(<コマンド>)` に絞らず単独で付与しているのは"
# 手順に出るが skill が打たないコマンドを書いた節 (アンチパターンの例示)
_NOT_RUN_SECTIONS = ("## アンチパターン",)


def _split_skill(text: str) -> tuple[list[str], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise AssertionError("SKILL.md の frontmatter が読めない")
    end = lines.index("---", 1)
    return lines[1:end], "\n".join(lines[end + 1:])


def _allowed_tools(frontmatter: list[str]) -> list[str]:
    """`allowed-tools` の付与 (空白・カンマ区切りの scalar、flow list、block list)。"""
    keys = [i for i, line in enumerate(frontmatter) if line.startswith("allowed-tools:")]
    if len(keys) != 1:
        raise AssertionError(f"allowed-tools のキーが {len(keys)} 個ある")
    idx = keys[0]
    rest = frontmatter[idx][len("allowed-tools:"):].strip()
    if rest:
        return [t for t in re.split(r"[\s,]+", rest.strip("[]")) if t]
    entries = []
    for line in frontmatter[idx + 1:]:
        match = re.match(r"\s*-\s+(.+)$", line)
        if match is None:
            break
        entries.append(match.group(1).strip().strip("'\""))
    return entries


def _sections(body: str) -> list[tuple[str, str]]:
    """`## ` の見出しごとに (見出し行, 本文) に分ける。最初の見出しより前は見出し "" とする。"""
    parts: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            parts.append((heading, "\n".join(buf)))
            heading, buf = line, []
        else:
            buf.append(line)
    parts.append((heading, "\n".join(buf)))
    return parts


def _reason(body: str) -> str:
    """理由の段落 (REASON_LEAD から次の見出しまで)。無ければ空文字。"""
    start = body.find(REASON_LEAD)
    if start < 0:
        return ""
    nxt = re.search(r"^#", body[start:], re.MULTILINE)
    return body[start:start + nxt.start()] if nxt else body[start:]


def _procedure(body: str) -> str:
    """手順の本文 (理由の段落とアンチパターンの節を除く)。"""
    kept = "\n".join(
        f"{heading}\n{text}"
        for heading, text in _sections(body)
        if not heading.startswith(_NOT_RUN_SECTIONS)
    )
    reason = _reason(kept)
    return kept.replace(reason, "") if reason else kept


class SkillAllowedToolsReasonTest(unittest.TestCase):
    def setUp(self):
        self.frontmatter, self.body = _split_skill(_SKILL.read_text(encoding="utf-8"))
        self.grants = _allowed_tools(self.frontmatter)
        # 読み違いで空になると、下の照合が黙って通る
        self.assertIn("Read", self.grants)

    def test_reason_exists_only_with_bare_bash_grant(self):
        self.assertEqual(
            "Bash" in self.grants,
            REASON_LEAD in self.body,
            f"付与 {self.grants} と理由の段落の有無が揃っていない",
        )

    def test_reason_names_every_bundled_script(self):
        if "Bash" not in self.grants:
            self.skipTest("Bash 単独の付与が無い")
        reason = _reason(self.body)
        on_disk = {p.name for p in _SCRIPTS.glob("*.sh")}
        in_procedure = set(re.findall(r"\bpr-[a-z0-9-]+\.sh\b", _procedure(self.body)))
        self.assertEqual(len(on_disk), 3)
        self.assertTrue(in_procedure)
        for name in sorted(on_disk | in_procedure):
            with self.subTest(script=name):
                self.assertIn(f"`{name}`", reason)

    def test_reason_names_every_gh_pr_subcommand_in_the_procedure(self):
        if "Bash" not in self.grants:
            self.skipTest("Bash 単独の付与が無い")
        reason = _reason(self.body)
        used = set(re.findall(r"\bgh pr ([a-z][a-z-]*)", _procedure(self.body)))
        # 読み違いで空にならないことだけを見る (足された subcommand は下の照合で落とす)
        self.assertLessEqual({"ready", "checks", "view", "merge"}, used)
        for sub in sorted(used):
            with self.subTest(sub=sub):
                self.assertIn(f"`gh pr {sub}`", reason)

    def test_reason_covers_target_repo_commands(self):
        if "Bash" not in self.grants:
            self.skipTest("Bash 単独の付与が無い")
        # マージ前のチェックは対象 repo のテスト・lint を打つ。repo ごとに違うので列挙できない
        self.assertIn("対象 repo のテスト", _procedure(self.body))
        self.assertIn("対象 repo のテスト", _reason(self.body))


_FAKE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
"""


@unittest.skipIf(_BASH is None or os.name == "nt", "bash が必要")
class ScriptsRequireArgumentsTest(unittest.TestCase):
    """理由の前提: 同梱スクリプトは最初の引数を必須にしている (無ければ gh / git を呼ばずに止まる)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name in ("gh", "git"):
            fake = bin_dir / name
            fake.write_text(_FAKE.format(python=sys.executable), encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.log = tmp / "calls.log"
        self.cwd = tmp
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log)}

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_script_stops_without_its_first_argument(self):
        scripts = sorted(_SCRIPTS.glob("*.sh"))
        self.assertEqual(len(scripts), 3)
        for script in scripts:
            with self.subTest(script=script.name):
                self.log.unlink(missing_ok=True)
                r = subprocess.run(
                    [_BASH, str(script)], cwd=self.cwd, env=self.env,
                    capture_output=True, text=True, encoding="utf-8", timeout=60,
                )
                calls = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
                self.assertEqual(calls, "")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("Usage", r.stderr)


if __name__ == "__main__":
    unittest.main()
