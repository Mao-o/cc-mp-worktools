"""Bash コマンドから「別 checkout を書き換える git 操作」を見つける。

見るのは git の書き込み操作だけ (checkout / commit / reset など、HEAD・index・作業ツリーを
変えるもの)。`git log` などの読み取りは対象外。git の対象ディレクトリは
`cd <dir>` と `git -C <dir>` / `--work-tree` / `--git-dir` から決める。

行き先が変数などで静的に決まらない場合は「解決できない」として返し、呼び出し側は止めずに
注意だけ出す (うっかりの予防が目的で、意図的なすり抜けへの対策ではないため)。
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

from family import Family, norm, owner

_PUNCT = "();<>|&\n"
_WRAPPERS = {"env", "command", "exec", "time", "nohup", "sudo"}
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# HEAD・index・作業ツリーを変える subcommand
_MUTATING = {
    "add", "am", "apply", "checkout", "checkout-index", "cherry-pick", "clean", "commit",
    "merge", "mv", "pull", "rebase", "reset", "restore", "revert", "rm", "stash", "switch",
    "update-index",
}
# 値を取る git の global option (`=` なしで次の token を消費する)
_GLOBAL_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"}


@dataclass(frozen=True)
class Finding:
    blocked: bool  # True = 別 checkout への書き込み / False = 解決できず判定していない
    message: str
    root: str | None = None  # 書き換えられる checkout の root (blocked のとき)


def _is_separator(tok: str) -> bool:
    if not set(tok) <= set(_PUNCT):
        return False
    if any(c in tok for c in ";|\n"):
        return True
    return "&" in tok and not tok.startswith((">", "<")) and not tok.endswith(">")


def _segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok and _is_separator(tok):
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def _strip_prefix(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg) and (
        seg[i] == "(" or seg[i] in _WRAPPERS or seg[i] in _KEYWORDS or _ASSIGNMENT.match(seg[i])
    ):
        i += 1
    return seg[i:]


def _is_git(tok: str) -> bool:
    return tok.replace("\\", "/").rsplit("/", 1)[-1].lower() in ("git", "git.exe")


def _dynamic(path: str) -> bool:
    return "$" in path or "`" in path


def _resolve(base: str | None, path: str) -> str | None:
    """base から見た path の正規化済み絶対パス。解決できなければ None。"""
    if _dynamic(path):
        return None
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return norm(path)
    if base is None:
        return None
    return norm(os.path.join(base, path))


def _git_target(args: list[str], cwd: str | None, fam: Family) -> tuple[str | None, str | None, list[str]]:
    """git の global option を読み、(対象 checkout のパス, 解決できない理由, 残りの引数) を返す。"""
    here = cwd
    work_tree = None
    git_dir = None
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            break
        name, eq, value = a.partition("=")
        if name in _GLOBAL_VALUE_OPTS and not eq:
            if i + 1 >= len(args):
                break
            value = args[i + 1]
            i += 2
        else:
            i += 1
        if name == "-C":
            if _dynamic(value):
                return None, f"git -C {value}", args[i:]
            here = _resolve(here, value)
        elif name == "--work-tree":
            work_tree = value
        elif name == "--git-dir":
            git_dir = value
    rest = args[i:]

    if work_tree is not None:
        resolved = _resolve(here, work_tree)
        return resolved, None if resolved else f"--work-tree {work_tree}", rest
    if git_dir is not None:
        resolved = _resolve(here, git_dir)
        if resolved is None:
            return None, f"--git-dir {git_dir}", rest
        if resolved == fam.common_dir:
            # 共有 git dir を直接指す = main checkout の HEAD / index を操作する
            return norm(os.path.dirname(fam.common_dir)), None, rest
        return here, None, rest
    return here, None if here else "cd の移動先", rest


def _worktree_targets(rest: list[str], here: str | None) -> list[str]:
    """`git worktree remove|move <path>` が消す / 動かす checkout。"""
    if len(rest) < 2 or rest[1] not in ("remove", "move"):
        return []
    paths = [a for a in rest[2:] if not a.startswith("-")]
    out = []
    if paths:
        resolved = _resolve(here, paths[0])
        if resolved:
            out.append(resolved)
    return out


def analyze(command: str, cwd: str, fam: Family) -> list[Finding]:
    if "git" not in command:
        return []
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []  # 分解できないコマンドは判定しない (git 自身もまず動かない)

    findings: list[Finding] = []
    here: str | None = norm(cwd)
    for seg in _segments(tokens):
        seg = _strip_prefix(seg)
        if not seg:
            continue
        if seg[0] in ("cd", "pushd"):
            target = seg[1] if len(seg) > 1 else "~"
            here = _resolve(here, target)
            continue
        if not _is_git(seg[0]):
            continue
        target, unresolved, rest = _git_target(seg[1:], here, fam)
        if not rest:
            continue
        sub = rest[0]
        if sub == "worktree":
            for path in _worktree_targets(rest, target):
                o = owner(path, fam)
                if o is not None and o != fam.home and path == o:
                    findings.append(Finding(True, f"`git worktree {rest[1]}` が別の worktree ({o}) を対象にしている", o))
            continue
        if sub not in _MUTATING:
            continue
        if unresolved:
            findings.append(Finding(False, f"`git {sub}` の対象 ({unresolved}) を静的に解決できない"))
            continue
        o = owner(target, fam) if target else None
        if o is not None and o != fam.home:
            findings.append(Finding(True, f"`git {sub}` が別の checkout ({o}) を書き換える", o))
    return findings
