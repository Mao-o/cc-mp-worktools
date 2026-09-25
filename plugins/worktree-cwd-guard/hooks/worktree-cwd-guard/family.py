"""作業ディレクトリが linked worktree かを判定し、同じ repo の checkout 一覧を得る。

「同じ repo の checkout」= `git worktree list` に並ぶ main checkout と全 linked worktree。
これらは object DB と ref を共有しているが、HEAD・index・作業ツリーは checkout ごとに別で、
別 checkout のそれを書き換えるのがこの plugin の防ぎたい事故。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT = 5


@dataclass(frozen=True)
class Family:
    home: str  # この session の worktree root (正規化済み)
    roots: tuple[str, ...]  # 同じ repo の全 checkout の root (home を含む)
    common_dir: str  # 共有 git dir (main checkout の .git)


def norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _linked_marker(start: Path) -> bool:
    """git を起動せずに「linked worktree の中らしいか」を判定する (高速な早期 return 用)。

    linked worktree の root には `.git` ディレクトリではなく `gitdir: <common>/worktrees/<name>`
    を書いた `.git` ファイルがある。submodule も `.git` ファイルを持つが、指す先は
    `.../modules/...` なので区別できる。
    """
    for d in (start, *start.parents):
        marker = d / ".git"
        if marker.is_dir():
            return False
        if marker.is_file():
            try:
                text = marker.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            line = text.strip().splitlines()[0] if text.strip() else ""
            return line.startswith("gitdir:") and "/worktrees/" in line.replace("\\", "/")
    return False


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def detect(cwd: str) -> Family | None:
    """cwd が linked worktree の中なら Family を返す。それ以外 (main checkout / repo 外) は None。"""
    start = Path(cwd)
    if not start.is_dir() or not _linked_marker(start):
        return None
    out = _git(["rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"], start)
    if out is None:
        return None
    lines = out.splitlines()
    if len(lines) < 3:
        return None
    top, git_dir, common = lines[0], lines[1], lines[2]
    git_dir_abs = norm(start / git_dir)
    common_abs = norm(start / common)
    if git_dir_abs == common_abs:
        return None  # main checkout
    listing = _git(["worktree", "list", "--porcelain"], start)
    roots = []
    for line in (listing or "").splitlines():
        if line.startswith("worktree "):
            roots.append(norm(line[len("worktree ") :]))
    home = norm(top)
    if home not in roots:
        roots.append(home)
    return Family(home=home, roots=tuple(roots), common_dir=common_abs)


def owner(path: str, family: Family) -> str | None:
    """path (正規化済み) を含む最も深い checkout root。どれにも含まれなければ None。"""
    best = None
    for root in family.roots:
        if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
            if best is None or len(root) > len(best):
                best = root
    return best
