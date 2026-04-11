from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Sequence


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
