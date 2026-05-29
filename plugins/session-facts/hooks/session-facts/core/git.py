from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


def run(cmd: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def git_root(start: Path) -> Path:
    cp = run(["git", "rev-parse", "--show-toplevel"], start)
    if cp.returncode == 0 and cp.stdout.strip():
        return Path(cp.stdout.strip())
    return start.resolve()


def git_root_or_none(start: Path) -> "Optional[Path]":
    cp = run(["git", "rev-parse", "--show-toplevel"], start)
    if cp.returncode == 0 and cp.stdout.strip():
        return Path(cp.stdout.strip())
    return None


def is_git_repo(root: Path) -> bool:
    cp = run(["git", "rev-parse", "--is-inside-work-tree"], root)
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def git_ls_files(root: Path) -> List[str]:
    cp = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if cp.returncode != 0:
        return []
    return [raw.decode("utf-8", "surrogateescape") for raw in cp.stdout.split(b"\0") if raw]


def current_branch(root: Path) -> Optional[str]:
    """Return the current branch name, or None on detached HEAD / failure."""
    cp = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    if cp.returncode != 0:
        return None
    name = cp.stdout.strip()
    if not name or name == "HEAD":  # "HEAD" == detached
        return None
    return name


def upstream_ref(root: Path) -> Optional[str]:
    """Return the upstream tracking ref (e.g. 'origin/main'), or None."""
    cp = run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        root,
    )
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip()
    return None


def ahead_behind(root: Path) -> Optional[Tuple[int, int]]:
    """Return (ahead, behind) relative to the upstream, or None when there is
    no upstream configured."""
    cp = run(
        ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        root,
    )
    if cp.returncode != 0:
        return None
    parts = cp.stdout.split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (ahead, behind)


def recent_commits(root: Path, n: int = 3, subject_max: int = 72) -> List[str]:
    """Return up to ``n`` recent commits as 'hash subject (relative date)'."""
    cp = run(
        ["git", "log", f"-{n}", "--pretty=format:%h\x1f%s\x1f%cr"],
        root,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    out: List[str] = []
    for line in cp.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, subject, rel = parts
        if len(subject) > subject_max:
            subject = subject[: subject_max - 1].rstrip() + "…"
        out.append(f"{sha} {subject} ({rel})")
    return out
