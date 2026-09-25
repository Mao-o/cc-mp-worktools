"""テスト共通: hook ディレクトリを sys.path に通し、main checkout + linked worktree を作る。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))


def sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True)
    return r.stdout


def make_repo(base: Path) -> tuple[Path, Path, Path]:
    """(main checkout, worktree A, worktree B) を作る。A は main の中 (.claude/worktrees/a) に置く。"""
    main = base / "repo"
    main.mkdir(parents=True)
    sh(main, "init", "-q", "-b", "main")
    sh(main, "config", "user.name", "Test")
    sh(main, "config", "user.email", "test@example.com")
    sh(main, "config", "commit.gpgsign", "false")
    (main / "README.md").write_text("# demo\n", encoding="utf-8")
    sh(main, "add", "-A")
    sh(main, "commit", "-q", "-m", "init")
    wt_a = main / ".claude" / "worktrees" / "a"
    wt_b = base / "wt-b"
    sh(main, "worktree", "add", "-q", "-b", "lane-a", str(wt_a))
    sh(main, "worktree", "add", "-q", "-b", "lane-b", str(wt_b))
    return main, wt_a, wt_b
