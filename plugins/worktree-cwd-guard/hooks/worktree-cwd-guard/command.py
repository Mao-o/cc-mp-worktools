"""Bash コマンドから「別 checkout を書き換える git 操作」を見つける。

見るのは git の書き込み操作だけ (checkout / commit / reset など、HEAD・index・作業ツリーを
変えるもの)。`git log` などの読み取りは対象外。git の対象ディレクトリは
`cd` / `pushd` / `popd` と `git -C <dir>` / `--work-tree` / `--git-dir`、
環境変数 `GIT_DIR` / `GIT_WORK_TREE` から決める。

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
_GIT_ENV = {"GIT_DIR", "GIT_WORK_TREE"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# HEAD・index・作業ツリーを変える subcommand
_MUTATING = {
    "add", "am", "apply", "bisect", "checkout", "checkout-index", "cherry-pick", "clean", "commit",
    "merge", "mv", "pull", "rebase", "reset", "restore", "revert", "rm", "stash", "switch",
    "update-index",
}
# `git branch` のうち、checkout 中の branch を動かして HEAD を書き換える形
_BRANCH_MOVE = {"-m", "-M", "--move"}


# 入れ子の subcommand を持つもののうち、作業ツリーや index を書き換える形
_NESTED_MUTATING = {
    "submodule": {"add", "update", "deinit", "init", "sync", "absorbgitdirs", "set-branch", "set-url"},
    "sparse-checkout": {"set", "add", "init", "reapply", "disable"},
}


def _is_mutating(sub: str, args: list[str]) -> bool:
    if sub == "branch":
        return any(a in _BRANCH_MOVE for a in args)
    if sub in _NESTED_MUTATING:
        action = next((a for a in args if not a.startswith("-")), None)
        return action in _NESTED_MUTATING[sub]
    return sub in _MUTATING


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


def _strip_prefix(seg: list[str]) -> tuple[list[str], str | None, dict[str, str | None], bool]:
    """先頭の予約語 / 代入 / wrapper (とそのオプション) を外す。

    返り値は (残り, wrapper が指定した作業ディレクトリ, コマンドに渡す環境変数の代入, 環境を
    空にして起動するか)。代入の値 None は `env -u NAME` による削除。
    `env -C <dir> git ...` のように wrapper が移動先を持つ場合、その後の git はそのディレクトリで動く。
    `GIT_DIR=<dir> git ...` の代入は git の対象を変えるので呼び出し側で使う。
    """
    chdir = None
    assigns: dict[str, str | None] = {}
    clear = False
    i = 0
    while i < len(seg):
        tok = seg[i]
        if _ASSIGNMENT.match(tok):
            name, _, value = tok.partition("=")
            assigns[name] = value
            i += 1
            continue
        if tok == "(" or tok in _KEYWORDS:
            i += 1
            continue
        if tok in _WRAPPERS:
            opts = _WRAPPER_VALUE_OPTS.get(tok, set())
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                name, eq, value = seg[i].partition("=")
                if seg[i] == "--":
                    i += 1
                    break
                if tok == "env" and seg[i] in ("-", "-i", "--ignore-environment"):
                    # 環境を空にして起動する。手前の代入も export 済みの変数も git に届かない
                    clear = True
                    assigns = {}
                    i += 1
                    continue
                if name in opts and not eq:
                    value = seg[i + 1] if i + 1 < len(seg) else ""
                    i += 2
                else:
                    i += 1
                if name in _CHDIR_OPTS.get(tok, set()):
                    chdir = value
                if tok == "env" and name in ("-u", "--unset"):
                    assigns[value] = None
            continue
        break
    return seg[i:], chdir, assigns, clear


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


def _git_targets(
    args: list[str], cwd: str | None, fam: Family, env: dict[str, str] | None = None
) -> tuple[list[str], str | None, list[str]]:
    """git の global option を読み、(書き換わる場所のパス一覧, 解決できない理由, 残りの引数) を返す。

    書き換わる場所は 2 つある: HEAD / index を持つ repo 側 (`-C` / `--git-dir` で決まる) と、
    作業ツリー側 (`--work-tree`、無ければ repo 側と同じ)。どちらかが別 checkout なら止める。
    """
    here = cwd
    env = env or {}
    # 環境変数 GIT_DIR / GIT_WORK_TREE は、同じ意味の option が無ければ効く
    work_tree = env.get("GIT_WORK_TREE")
    git_dir = env.get("GIT_DIR")
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


# here-document の開始 (`<<EOF` / `<<-'EOF'` / `<< "EOF"`)。`<<<` (here-string) は除く
_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")


def _strip_heredocs(command: str) -> str:
    """here-document の本文を取り除く。

    本文はコマンドの標準入力に渡る文字列で、shell は実行しない (`cat > x.sh <<'EOF'` で git を
    含む script を書くだけのことが多い)。shlex は here-document を知らないので、字句に分ける前に
    外しておかないと本文をコマンドとして読んでしまう。
    """
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            strip_tabs, delim = m.group(1) == "-", m.group(3)
            while i < len(lines):
                body = lines[i]
                i += 1
                if (body.lstrip("\t") if strip_tabs else body) == delim:
                    break
    return "\n".join(out)


def analyze(command: str, cwd: str, fam: Family) -> list[Finding]:
    if "git" not in command:
        return []
    command = _strip_heredocs(command)
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []  # 分解できないコマンドは判定しない (git 自身もまず動かない)

    return _Analyzer(cwd, fam).run(tokens)


# 1 つの「実行時にありうる状態」: (作業ディレクトリ, pushd のスタック, 直前のコマンドの成否)。
# 作業ディレクトリが静的に決まらなければ None。成否は "ok" / "fail"
_Alt = tuple
_OPS = re.compile(r"&&|\|\||\|&|;;|[|&;()\n]")


class _Analyzer:
    """shell の実行をたどり、ありうる状態の集合を持ち回る。

    `&&` / `||` の後ろのコマンドは、直前が成功 / 失敗した状態でだけ実行する。cd は成功するものとして
    扱い、git や一般のコマンドは成功・失敗の両方がありうるとする。git を実行しうる状態のどれかで
    対象が別の checkout なら止める。
    """

    def __init__(self, cwd: str, fam: Family):
        self.fam = fam
        self.findings: list[Finding] = []
        self._seen: set[str] = set()
        self.alts: set[_Alt] = {(norm(cwd), (), "ok")}
        self.env: dict[str, str] = {}  # export 済みの GIT_DIR / GIT_WORK_TREE
        self.shvars: dict[str, str] = {}  # 代入したが export していないもの
        self.op: str | None = None  # 次のコマンドの前にある && / ||
        self.in_pipe = False  # 今のコマンドが `|` の後ろ (pipeline の要素) か
        self.list_start = self._snapshot()  # 今の AND-OR list の開始時点 (`&` で戻す)
        self.scopes: list[tuple] = []  # `( ... )` に入るときの状態

    # --- 状態 -------------------------------------------------------------

    def _snapshot(self) -> tuple:
        return (frozenset(self.alts), dict(self.env), dict(self.shvars))

    def _restore(self, snap: tuple) -> None:
        self.alts = set(snap[0])
        self.env, self.shvars = dict(snap[1]), dict(snap[2])

    @staticmethod
    def _both(alts) -> set[_Alt]:
        return {(h, s, st) for h, s, _ in alts for st in ("ok", "fail")}

    def _split(self) -> tuple[set[_Alt], set[_Alt]]:
        """今のコマンドを実行する状態と、`&&` / `||` で飛ばす状態に分ける。"""
        if self.op is None:
            return set(self.alts), set()
        want = "ok" if self.op == "&&" else "fail"
        run = {a for a in self.alts if a[2] == want}
        return run, self.alts - run

    # --- 字句の走査 ---------------------------------------------------------

    def run(self, tokens: list[str]) -> list[Finding]:
        seg: list[str] = []
        for tok in tokens:
            if not (tok and set(tok) <= set(_PUNCT)):
                seg.append(tok)
                continue
            if tok.startswith((">", "<")) or tok.endswith(">"):
                seg.append(tok)  # リダイレクト記号はそのまま
                continue
            for op in _OPS.findall(tok):
                self._flush(seg, next_is_pipe=op in ("|", "|&"))
                seg = []
                self._operator(op)
        self._flush(seg, next_is_pipe=False)
        return self.findings

    def _operator(self, op: str) -> None:
        if op in ("&&", "||"):
            self.op = op
        elif op in ("|", "|&"):
            self.op = None
        elif op == "&":
            # AND-OR list 全体が非同期 (サブシェル) で動く。親 shell の状態は list の開始時点のまま
            self._restore(self.list_start)
            self.alts = {(h, s, "ok") for h, s, _ in self.alts}
            self._new_list()
        elif op == "(":
            run, skipped = self._split()
            self.scopes.append((frozenset(run), skipped, dict(self.env), dict(self.shvars), self.list_start))
            self.alts = run
            self._new_list()
        elif op == ")":
            if self.scopes:
                entered, skipped, env, shvars, list_start = self.scopes.pop()
                # 中の cd / export は外に残らない。成否は中身次第なので両方ありうる
                self.alts = self._both(entered) | skipped
                self.env, self.shvars, self.list_start = env, shvars, list_start
            self.op = None
        else:  # ; ;; 改行
            self._new_list()

    def _new_list(self) -> None:
        self.op = None
        self.list_start = self._snapshot()

    def _flush(self, seg: list[str], next_is_pipe: bool) -> None:
        subshell = next_is_pipe or self.in_pipe
        self.in_pipe = next_is_pipe
        if not seg:
            return
        run, skipped = self._split()
        if run:
            after = self._command(seg, run)
            # pipeline の要素はサブシェルで動くので、中の cd は外に残らない
            run = self._both(run) if subshell else after
        self.alts = run | skipped

    # --- 1 コマンド -----------------------------------------------------------

    def _command(self, seg: list[str], alts: set[_Alt]) -> set[_Alt]:
        seg, chdir, assigns, clear = _strip_prefix(seg)
        ok = {(h, s, "ok") for h, s, _ in alts}
        if not seg:  # 代入だけ
            for k, v in assigns.items():
                if k in _GIT_ENV and v is not None:
                    self.shvars[k] = v
                    if k in self.env:
                        self.env[k] = v
            return ok
        cmd = seg[0]
        if cmd in ("true", ":"):
            return ok
        if cmd == "false":
            return {(h, s, "fail") for h, s, _ in alts}
        if cmd == "export":
            for tok in seg[1:]:
                name, eq, value = tok.partition("=")
                if name not in _GIT_ENV:
                    continue
                if eq:
                    self.shvars[name] = self.env[name] = value
                elif name in self.shvars:
                    self.env[name] = self.shvars[name]
            return ok
        if cmd == "unset":
            for name in seg[1:]:
                self.env.pop(name, None)
                self.shvars.pop(name, None)
            return ok
        if cmd in ("cd", "pushd", "popd"):
            return {self._chdir(cmd, seg[1:], h, s) for h, s, _ in alts}
        if _is_git(cmd):
            env = {} if clear else dict(self.env)
            for k, v in assigns.items():
                if k in _GIT_ENV:
                    if v is None:
                        env.pop(k, None)
                    else:
                        env[k] = v
            heres = {h for h, _, _ in alts}
            if chdir is not None:
                heres = {_resolve(h, chdir) for h in heres}
            self._check_git(seg[1:], heres, env)
        return self._both(alts)

    @staticmethod
    def _chdir(cmd: str, args: list[str], here: str | None, stack: tuple) -> _Alt:
        if cmd == "popd":
            if not args and stack:
                return (stack[-1], stack[:-1], "ok")
            return (None, stack[:-1], "ok")  # 呼び出し前のスタック / `popd +N` は分からない
        if cmd == "pushd" and not args:
            return (None, stack + (here,), "ok")  # 引数なしの pushd はスタックの先頭と入れ替える
        dest = _cd_target(args)
        new = _resolve(here, dest) if dest is not None else None
        return (new, stack + (here,) if cmd == "pushd" else stack, "ok")

    def _add(self, finding: Finding) -> None:
        if finding.message not in self._seen:
            self._seen.add(finding.message)
            self.findings.append(finding)

    def _check_git(self, args: list[str], heres: set, env: dict) -> None:
        fam = self.fam
        note: Finding | None = None
        for here in sorted(heres, key=lambda h: h or ""):
            targets, unresolved, rest = _git_targets(args, here, fam, env)
            if not rest:
                return
            sub = rest[0]
            if sub == "worktree":
                base = targets[0] if targets else None
                for path in _worktree_targets(rest, base):
                    o = owner(path, fam)
                    if o is not None and o != fam.home and path == o:
                        self._add(Finding(True, f"`git worktree {rest[1]}` が別の worktree ({o}) を対象にしている", o))
                continue
            if not _is_mutating(sub, rest[1:]):
                return
            if unresolved:
                note = note or Finding(False, f"`git {sub}` の対象 ({unresolved}) を静的に解決できない")
                continue
            # 対象ごとに記録する (許可リストで 1 つ外れても、残りの対象で止められるように)
            for target in targets:
                o = owner(target, fam)
                if o is not None and o != fam.home:
                    self._add(Finding(True, f"`git {sub}` が別の checkout ({o}) を書き換える", o))
        if note is not None:
            self._add(note)
