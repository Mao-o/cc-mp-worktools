"""テスト共通: plugin の hook ディレクトリを sys.path に通し、使い捨て git repo を作る。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))


def sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True
    )
    return r.stdout


def write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def write_json(root: Path, rel: str, data: object) -> None:
    write(root, rel, json.dumps(data, indent=2) + "\n")


def commit_all(root: Path, msg: str) -> None:
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", msg)


PASSING_TEST = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING_TEST = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ng(self):\n        self.fail('boom')\n"


def add_plugin(root: Path, name: str, version: str | None = "0.1.0", tests: str | None = PASSING_TEST) -> None:
    manifest: dict[str, object] = {"name": name, "description": f"{name} plugin"}
    if version is not None:
        manifest["version"] = version
    write_json(root, f"plugins/{name}/.claude-plugin/plugin.json", manifest)
    write(root, f"plugins/{name}/CHANGELOG.md", "# Changelog\n")
    write(root, f"plugins/{name}/hooks/{name}/__main__.py", "print('hi')\n")
    if tests is not None:
        write(root, f"plugins/{name}/hooks/{name}/tests/__init__.py", "")
        write(root, f"plugins/{name}/hooks/{name}/tests/test_x.py", tests)


def make_marketplace(root: Path, plugins: list[str]) -> Path:
    """main に 1 commit ある marketplace repo を作る。"""
    root.mkdir(parents=True, exist_ok=True)
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "user.name", "Test")
    sh(root, "config", "user.email", "test@example.com")
    sh(root, "config", "commit.gpgsign", "false")
    sh(root, "config", "core.autocrlf", "false")
    write_json(
        root,
        ".claude-plugin/marketplace.json",
        {
            "name": "demo-market",
            "owner": {"name": "Demo"},
            "metadata": {"description": "demo"},
            "plugins": [{"name": p, "source": f"./plugins/{p}"} for p in plugins],
        },
    )
    for p in plugins:
        add_plugin(root, p)
    commit_all(root, "init")
    return root


def bump(root: Path, name: str, version: str) -> None:
    rel = f"plugins/{name}/.claude-plugin/plugin.json"
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    data["version"] = version
    write_json(root, rel, data)
