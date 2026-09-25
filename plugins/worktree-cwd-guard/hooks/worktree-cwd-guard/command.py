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
    "add", "am", "apply", "bisect", "checkout", "checkout-index", "cherry-pick", "clean", "commit",
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


# wrapper が値を取るオプション (次の token を消費する)。env -C / --chdir は作業ディレクトリを変える
_WRAPPER_VALUE_OPTS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-D", "--chdir", "-R", "--chroot", "-T", "--command-timeout", "-U", "--other-user", "-r", "--role", "-t", "--type"},
    "time": {"-f", "--format", "-o", "--output"},
    "nohup": set(),
    "command": set(),
    "exec": {"-a"},
}
_CHDIR_OPTS = {"env": {"-C", "--chdir"}, "sudo": {"-D", "--chdir"}}


def _strip_prefix(seg: list[str]) -> tuple[list[str], str | None]:
    """先頭の予約語 / 代入 / wrapper (とそのオプション) を外す。

    返り値は (残り, wrapper が指定した作業ディレクトリ)。`env -C <dir> git ...` のように
    wrapper が移動先を持つ場合、その後の git はそのディレクトリで動く。
    """
    chdir = None
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok == "(" or tok in _KEYWORDS or _ASSIGNMENT.match(tok):
            i += 1
            continue
        if tok in _WRAPPERS:
            opts = _WRAPPER_VALUE_OPTS.get(tok, set())
            i += 1
            while i < len(seg) and seg[i].startswith("-") and seg[i] != "-":
                name, eq, value = seg[i].partition("=")
                if seg[i] == "--":
                    i += 1
                    break
                if name in opts and not eq:
                    value = seg[i + 1] if i + 1 < len(seg) else ""
                    i += 2
                else:
                    i += 1
                if name in _CHDIR_OPTS.get(tok, set()):
                    chdir = value
            continue
        break
    return seg[i:], chdir


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


def _git_targets(args: list[str], cwd: str | None, fam: Family) -> tuple[list[str], str | None, list[str]]:
    """git の global option を読み、(書き換わる場所のパス一覧, 解決できない理由, 残りの引数) を返す。

    書き換わる場所は 2 つある: HEAD / index を持つ repo 側 (`-C` / `--git-dir` で決まる) と、
    作業ツリー側 (`--work-tree`、無ければ repo 側と同じ)。どちらかが別 checkout なら止める。
    """
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
                return [], f"git -C {value}", args[i:]
            here = _resolve(here, value)
        elif name == "--work-tree":
            work_tree = value
        elif name == "--git-dir":
            git_dir = value
    rest = args[i:]

    targets: list[str] = []
    if git_dir is not None:
        resolved = _resolve(here, git_dir)
        if resolved is None:
            return [], f"--git-dir {git_dir}", rest
        mapped = dict(fam.gitdirs).get(resolved)
        if mapped is not None:
            targets.append(mapped)  # その git dir を持つ checkout の HEAD / index を書き換える
        elif resolved == fam.common_dir:
            targets.append(norm(os.path.dirname(fam.common_dir)))
        elif resolved.startswith(fam.common_dir.rstrip(os.sep) + os.sep):
            return [], f"--git-dir {git_dir}", rest  # 同じ repo の、対応の分からない git dir
    elif here is not None:
        targets.append(here)
    else:
        return [], "cd の移動先", rest
    if work_tree is not None:
        resolved = _resolve(here, work_tree)
        if resolved is None:
            return [], f"--work-tree {work_tree}", rest
        targets.append(resolved)
    return targets, None, rest


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


_CD_OPTS = {"-L", "-P", "-e", "-@", "-n"}


def _cd_target(args: list[str]) -> str | None:
    """`cd` / `pushd` の移動先。`cd -` (直前のディレクトリ) など決められなければ None。"""
    i = 0
    while i < len(args) and args[i] in _CD_OPTS:
        i += 1
    if i < len(args) and args[i] == "--":
        i += 1
    if i >= len(args):
        return "~"
    if args[i] == "-" or args[i].startswith(("+", "-")):
        return None  # 直前のディレクトリ / dir stack の参照
    return args[i]


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
    # 実行時にありうる作業ディレクトリの候補。`&&` / `||` の後の cd は実行されないことが
    # あるので、候補を置き換えずに足す。候補のどれかが別 checkout なら止める
    state: dict = {"here": {norm(cwd)}}
    scopes: list[set] = []  # `( ... )` に入るときの候補

    def process(seg: list[str], conditional: bool) -> None:
        seg, chdir = _strip_prefix(seg)
        if not seg:
            return
        heres = state["here"]
        if seg[0] in ("cd", "pushd"):
            dest = _cd_target(seg[1:])
            new = {_resolve(h, dest) if dest is not None else None for h in heres}
            state["here"] = heres | new if conditional else new
            return
        if chdir is not None:
            heres = {_resolve(h, chdir) for h in heres}
        if not _is_git(seg[0]):
            return
        sub_findings: list[Finding] = []
        for here in sorted(heres, key=lambda h: h or ""):
            targets, unresolved, rest = _git_targets(seg[1:], here, fam)
            if not rest:
                return
            sub = rest[0]
            if sub == "worktree":
                base = targets[0] if targets else None
                for path in _worktree_targets(rest, base):
                    o = owner(path, fam)
                    if o is not None and o != fam.home and path == o:
                        findings.append(Finding(True, f"`git worktree {rest[1]}` が別の worktree ({o}) を対象にしている", o))
                        return
                continue
            if sub not in _MUTATING:
                return
            if unresolved:
                sub_findings.append(Finding(False, f"`git {sub}` の対象 ({unresolved}) を静的に解決できない"))
                continue
            for target in targets:
                o = owner(target, fam)
                if o is not None and o != fam.home:
                    findings.append(Finding(True, f"`git {sub}` が別の checkout ({o}) を書き換える", o))
                    return
        findings.extend(sub_findings[:1])

    seg: list[str] = []
    conditional = False  # 今の segment が && / || の後ろにあるか
    for tok in tokens:
        if tok and set(tok) <= set(_PUNCT):
            # 記号だけの token。`(` / `)` はサブシェルの出入り (中の cd は外に影響しない)
            is_sep = any(c in tok for c in "();|&\n") and not (tok.startswith((">", "<")) or tok.endswith(">"))
            if is_sep:
                process(seg, conditional)
                seg = []
                conditional = "&&" in tok or "||" in tok
            for c in tok:
                if c == "(":
                    scopes.append(set(state["here"]))
                elif c == ")" and scopes:
                    state["here"] = scopes.pop()
            if not is_sep:
                seg.append(tok)  # リダイレクト記号はそのまま
            continue
        seg.append(tok)
    process(seg, conditional)
    return findings
