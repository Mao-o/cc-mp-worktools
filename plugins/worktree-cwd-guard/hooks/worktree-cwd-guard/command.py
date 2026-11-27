"""Bash コマンドから「別 checkout を書き換える git 操作」を見つける。

見るのは git の書き込み操作だけ (checkout / commit / reset など、HEAD・index・作業ツリーを
変えるもの)。`git log` などの読み取りは対象外。git の対象ディレクトリは
`cd` / `chdir` / `pushd` / `popd` と `git -C <dir>` / `--work-tree` / `--git-dir`、
環境変数 `GIT_DIR` / `GIT_WORK_TREE` から決める。

行き先が変数などで静的に決まらない場合は「解決できない」として返し、呼び出し側は止めずに
注意だけ出す (うっかりの予防が目的で、意図的なすり抜けへの対策ではないため)。
読み取りが期限 (`_READ_SECONDS`) を超えたときも、それまでの判定に注意を足して返す。
"""
from __future__ import annotations

import os
import re
import shlex
import string
import time
import unicodedata
from array import array
from dataclasses import dataclass

from family import Family, _drive_readings, _msys_drive, norm, owner

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


# `git stash` の subcommand のうち、ref も作業ツリーも変えないもの。subcommand は最初の引数に
# しか書けない (`git stash -m list` は `list` という message の push)。それ以外は書き込みとして扱う
_STASH_READ = {"list", "show", "create"}
# `git apply` を「適用せずに表示・検査するだけ」にする option。`--apply` (略記を含む) と併用したら適用する
_APPLY_READ = {"--check", "--stat", "--numstat", "--summary"}


def _apply_is_read(args: list[str]) -> bool:
    opts = args[: args.index("--")] if "--" in args else args
    if any(a.startswith("--ap") and "--apply".startswith(a) for a in opts):
        return False
    return any(a in _APPLY_READ for a in opts)


def _is_mutating(sub: str, args: list[str]) -> bool:
    if sub == "branch":
        return any(a in _BRANCH_MOVE for a in args)
    if sub in _NESTED_MUTATING:
        action = next((a for a in args if not a.startswith("-")), None)
        return action in _NESTED_MUTATING[sub]
    if sub == "stash":
        return not (args and args[0] in _STASH_READ)
    if sub == "apply":
        return not _apply_is_read(args)
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
# 移動の組み込みが、どの shell の読みで移動するか。bash 3.2 / 5.2 と `zsh -f` で偽の git を走らせた実測に合わせている
# (`_analyze_tokens` が zsh の読みと bash の読みの両方で判定する)。bash の読みは bash 3.2 と 5 の和で、どちらかで移動するなら移動する
_BOTH = frozenset({"bash", "zsh"})
_BASH = frozenset({"bash"})
_ZSH = frozenset({"zsh"})
_NEITHER: frozenset[str] = frozenset()
# `cd` / `chdir` の option の文字。`-q` / `-s` は zsh だけ受け付ける (bash は失敗して移動しない)。`-e` は bash 3.2 と zsh が
# 失敗するが、bash 4.2 以降は移動するので bash の読みに入れる。表に無い文字 (`-n` / `-x` / `-@`) はどちらの shell でも失敗する
_CD_OPT_SHELLS = {"L": _BOTH, "P": _BOTH, "q": _ZSH, "s": _ZSH, "e": _BASH}
# `pushd` の option の文字。bash の pushd は `-n` しか受け付けないので、`-L` / `-P` も zsh だけ
_PUSHD_OPT_SHELLS = {"L": _ZSH, "P": _ZSH, "q": _ZSH, "s": _ZSH}
# 今の shell で動くときだけ移動として読む組み込み
_MOVERS = {"cd", "chdir", "pushd", "popd"}
# `builtin` の後ろで読む語。今の shell の組み込みとして動くもののうち、_command が読むものだけ。
# `builtin git` は組み込みが無いのでどちらの shell でも走らない。`builtin -- cd` は bash だけ、`builtin - cd` は zsh だけ
# 動くので読まない。`builtin command cd` は bash では残り zsh では外部の cd になって残らないので読まない。
# `builtin pushd` / `builtin popd` は対でしか読めないので、どちらも読まない (`_chdir` の読みに入れず、手前の場所のままにする)。
# `builtin chdir` は zsh だけ動くが、`chdir` の読みが両方の shell を判定する
_BUILTIN_READS = {"cd", "chdir", "export", "unset", "true", "false", ":", "builtin"}
# `builtin cd` で読む option。`-L` / `-P` は両方の shell で移動し、`-q` / `-s` は zsh だけ、`-e` は bash 4.2 以降だけ移動する
# (`_chdir_form` が両方の読みにする)。`-@` / `-n` はどちらの shell でも失敗するので読まない
_BUILTIN_CD_OPT = re.compile(r"-[LPqse]+")
# shell が展開してから cd に渡す語。行き先を文字列から決められない
_EXPANDED_WORD = re.compile(r"[$`*?\[{]")


def _builtin_reads(word: str, args: list[str]) -> bool:
    """`builtin <word> <args>` を `<word> <args>` と同じに読むか。

    読まない形は旧来の読み (移動も環境の変更も無かったもの) のまま。移動しない形や行き先の分からない形を読むと、
    手前の `cd <main>` が消えて、main で動く git を見逃す。
    """
    if word not in _BUILTIN_READS:
        return False
    if word in ("export", "unset"):
        # `unset -f` は関数を消す。`export -x` / `+x` は shell で割れる。`$x` は値が決まらない
        return not any(a.startswith(("-", "+")) or _dynamic(a) for a in args)
    if word not in ("cd", "chdir"):
        return True
    operands = _words(args)
    i = 0
    while i < len(operands) and _BUILTIN_CD_OPT.fullmatch(operands[i]):
        i += 1
    if i < len(operands) and operands[i] == "--":
        i += 1
    operands = operands[i:]
    if not operands:
        return True
    # 語が 2 つの `cd a b` は bash 3.2 が a へ移動し、zsh は置換として読んで失敗する。
    # `-` / `+1` / `-1` は直前のディレクトリや dir stack の参照で、`-x` は読めない option
    return len(operands) == 1 and not operands[0].startswith(("-", "+")) and not _EXPANDED_WORD.search(operands[0])


def _before_redirect(args: list[str]) -> list[str]:
    """redirect より前の語。redirect の直前の数字の語は fd として外す。"""
    for i, tok in enumerate(args):
        if tok.startswith(("<", ">")) or tok.endswith(">"):
            return args[: i - 1] if i and args[i - 1].isdigit() else args[:i]
    return args


def _words(args: list[str], closes: bool = True) -> list[str]:
    """移動の組み込みの語。redirect より後ろと、閉じとして読む末尾の `}` を除く。

    zsh は `{ cd <dir> }` の `}` を閉じとして読む (bash は構文エラーで何も走らせない)。開き括弧が無ければ `}` はただの語で、
    `cd }` は `}` という名前のディレクトリへの移動になる (closes=False)。
    """
    words = _before_redirect(args)
    return words[:-1] if closes and words[-1:] == ["}"] else words


def _strip_prefix(seg: list[str]) -> tuple[list[str], str | None, dict[str, str | None], bool, frozenset[str], frozenset[str]]:
    """先頭の予約語 / 代入 / wrapper (とそのオプション) を外す。

    返り値は (残り, wrapper が指定した作業ディレクトリ, コマンドに渡す環境変数の代入, 環境を
    空にして起動するか, 残りのコマンドが今の shell で動く shell の集合, `!` が予約語になる shell の集合)。代入の値 None は `env -u NAME` による削除。
    `env -C <dir> git ...` のように wrapper が移動先を持つ場合、その後の git はそのディレクトリで動く。
    `GIT_DIR=<dir> git ...` の代入は git の対象を変えるので呼び出し側で使う。

    今の shell で動かないコマンド (`env cd <dir>` / `nohup cd` / `sudo cd` / `exec cd`) の移動は後ろに残らない
    (macOS の /usr/bin/cd などを子プロセスで走らせるか、組み込みが無くて失敗する)。`command` は bash では今の shell で動き、
    zsh では外部コマンドを起動する。`command -v` などの他の option は何も起動しない。
    先頭の `time` は bash でも zsh でも今の shell で動かす。ただし zsh の `time -p ...` は `-p` をコマンドとして起動し、
    `-p` 以外の option は両方の shell でコマンドとして起動する。`time --` と 2 つ目の `time` (`time time` / `time -p time` / `time -- time`)
    は新しい bash だけ今の shell で動かす (bash 3.2 と zsh は外部コマンド)。`time -f x` の後ろと代入の後ろの `time` はどの版でも
    外部コマンドになる (zsh は代入の後ろの `time` を構文エラーにする)。
    `builtin` は両方の shell で今の shell で動くときだけ外す。`\\time` / `'time'` は外部の time で動くが、shlex が引用を外すので
    keyword の time と区別できず、今の shell で動くものとして読む。bash 3.2 の `! time` / `if time` も外部の time になるが、
    同じに読む (どちらも見逃す側で、受け入れている)。
    `!` は、後ろのコマンドが今の shell で動く shell でだけ予約語になる (`time -p ! false` の zsh と `time -- ! false` の bash 3.2 は
    外部コマンドの引数になる)。2 回書くと打ち消す。代入の後ろの `!` はどの shell でもコマンド名で、そこで止める
    """
    chdir = None
    assigns: dict[str, str | None] = {}
    clear = False
    shells = _BOTH
    after_command = False  # `command` の後ろ。代入や予約語も、ただのコマンド名になる
    negated = _NEITHER  # `!` が予約語になる shell の集合
    i = 0
    while i < len(seg):
        tok = seg[i]
        if _ASSIGNMENT.match(tok):
            name, _, value = tok.partition("=")
            assigns[name] = value
            if after_command:
                shells = _NEITHER
            i += 1
            continue
        if tok == "(" or tok in _KEYWORDS:
            if after_command:
                shells = _NEITHER
            elif tok == "!":
                if assigns:
                    break  # 代入の後ろの `!` は予約語ではなく、コマンド名
                negated ^= shells
            i += 1
            continue
        if tok == "builtin":
            if shells == _BOTH and i + 1 < len(seg) and _builtin_reads(seg[i + 1], seg[i + 2 :]):
                i += 1
                continue
            break
        if tok in _WRAPPERS:
            opts = _WRAPPER_VALUE_OPTS.get(tok, set())
            if tok == "command":
                shells &= _BASH
                after_command = True
            elif tok == "time":
                before = [t for t in seg[:i] if t != "(" and t not in _KEYWORDS]
                if any(t not in ("time", "-p", "--") for t in before):
                    shells = _NEITHER
                elif before:
                    shells &= _BASH  # 2 つ目の `time`
            else:
                shells = _NEITHER
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                name, eq, value = seg[i].partition("=")
                if tok == "time":
                    shells = shells & _BASH if seg[i] in ("-p", "--") else _NEITHER
                elif tok == "command" and seg[i] not in ("-p", "--"):
                    shells = _NEITHER
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
    return seg[i:], chdir, assigns, clear, shells, negated


def _is_git(tok: str) -> bool:
    return tok.replace("\\", "/").rsplit("/", 1)[-1].lower() in ("git", "git.exe")


def _dynamic(path: str) -> bool:
    return "$" in path or "`" in path


# `~name` の形の語の先頭 (`~` と `~/x` は含まない)
_TILDE_NAME = re.compile(r"~[^/\\]+")


def _expand_home(path: str) -> str:
    """`~` を home に展開する。`~name` は、その home が実在するときだけ展開する。

    POSIX の expanduser は知らない名前をそのまま返すが、Windows の expanduser は名前を調べずに `<home の親>\\<名前>` と推測する
    (`~+` が `C:\\Users\\+` になり、手前の `cd <main>` が消えて、main で動く git を見逃していた)。
    `~+` / `~-` / `~1` や知らない名前は、どの OS でも今の場所の下の語として読む。
    """
    named = _TILDE_NAME.match(path)
    if named and not os.path.isdir(os.path.expanduser(named.group())):
        return path
    return os.path.expanduser(path)


def _norm_once(path: str, norms: dict[str, str] | None) -> str:
    """norm(path)。norms があれば、正規化する前のパスの綴りを key に値を覚え、同じ綴りの realpath を繰り返さない。"""
    if norms is None:
        return norm(path)
    value = norms.get(path)
    if value is None:
        value = norms[path] = norm(path)
    return value


def _resolve(
    base: str | None,
    path: str,
    msys: bool = False,
    memo: dict[str, str] | None = None,
    norms: dict[str, str] | None = None,
) -> str | None:
    """base から見た path の正規化済み絶対パス。解決できなければ None。msys は Git Bash の読み (`_drive_readings`)。

    memo は、絶対パスになる語 (`~` の展開と Git Bash の読みの後で) を正規化した値の覚えで、語をそのまま key にする
    (1 つの読みの中で、同じ msys で使う)。絶対パスの行き先は base に関係なく同じ値になるので、状態ごとに同じ realpath を
    繰り返さない。値は覚えないときと同じ (1 つの読みの間にファイルシステムと home は変わらない)。相対パスは基点ごとに値が
    違うので memo には入れない。

    norms は、realpath に渡す綴り (相対パスは基点と繋いだもの) を key にした正規化の覚えで、`analyze_readings` の 1 回の
    呼び出しの全部の読み (shell・here-document の読み直し・Git Bash の読み・作業ディレクトリの読み) で 1 つを使う。読みはどれも
    同じ場所から同じ移動を読むので、覚えないと同じ realpath を読みの数だけ繰り返す (Windows は存在しない要素ごとに API を
    2 回呼ぶので重く、相対パスを深くする形が期限に届いていた)。値は覚えないときと同じ (呼び出しの間にファイルシステムは変わらない)。
    """
    if memo is not None and path in memo:
        return memo[path]
    if _dynamic(path):
        return None
    word = path
    path = _expand_home(path)
    if msys:
        path = _msys_drive(path)
    if os.path.isabs(path):
        resolved = _norm_once(path, norms)
        if memo is not None:
            memo[word] = resolved
        return resolved
    if base is None:
        return None
    return _norm_once(os.path.join(base, path), norms)


def _git_targets(
    args: list[str],
    cwd: str | None,
    fam: Family,
    env: dict[str, str] | None = None,
    msys: bool = False,
    norms: dict[str, str] | None = None,
) -> tuple[list[str], str | None, list[str], bool]:
    """git の global option を読み、(書き換わる場所のパス一覧, 解決できない理由, 残りの引数, git が読む repo がこの repo か) を返す。

    norms は `_resolve` の realpath の覚え (`analyze_readings` の 1 回の呼び出しで 1 つ)。

    書き換わる場所は 2 つある: HEAD / index を持つ repo 側 (`-C` / `--git-dir` で決まる) と、
    作業ツリー側 (`--work-tree`。無ければ、`--git-dir` だけが効いているときは今の場所、それ以外は repo 側と同じ)。
    どちらかが別 checkout なら止める。一覧と理由の両方を返すことがある (`GIT_DIR` が決まっていて、今の場所が決まらないとき)。
    repo は、git dir の指定があればそれが対応付く checkout、無ければ今の場所 (`-C` の後) で決まる。決まらなければ False。
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
                return [], f"git -C {value}", args[i:], False
            here = _resolve(here, value, msys, norms=norms)
        elif name == "--work-tree":
            work_tree = value
        elif name == "--git-dir":
            git_dir = value
    rest = args[i:]

    targets: list[str] = []
    unresolved = None
    if git_dir is not None:
        resolved = _resolve(here, git_dir, msys, norms=norms)
        if resolved is None:
            return [], f"--git-dir {git_dir}", rest, False
        mapped = dict(fam.gitdirs).get(resolved)
        if mapped is not None:
            targets.append(mapped)  # その git dir を持つ checkout の HEAD / index を書き換える
        elif resolved == fam.common_dir:
            targets.append(_norm_once(os.path.dirname(fam.common_dir), norms))
        elif resolved.startswith(fam.common_dir.rstrip(os.sep) + os.sep):
            return [], f"--git-dir {git_dir}", rest, False  # 同じ repo の、対応の分からない git dir
        in_repo = bool(targets)
        if work_tree is None:
            # git dir だけが効くとき、作業ツリーは今の場所になる (`GIT_DIR=<worktree>/.git git -C <main> reset --hard` は
            # main の作業ツリーを上書きする)。`git add` / `git commit` のように作業ツリーを書かない操作も同じに読む
            if here is not None:
                targets.append(here)
            else:
                unresolved = "cd の移動先"
    elif here is not None:
        targets.append(here)
        in_repo = owner(here, fam) is not None  # git は今の場所から repo を探す
    else:
        return [], "cd の移動先", rest, False
    if work_tree is not None:
        resolved = _resolve(here, work_tree, msys, norms=norms)
        if resolved is None:
            return [], f"--work-tree {work_tree}", rest, in_repo  # 作業ツリーが決まらなくても repo は決まっている
        targets.append(resolved)
    return targets, unresolved, rest, in_repo


def _worktree_operand(rest: list[str]) -> str | None:
    """`git worktree remove|move <worktree>` の `<worktree>` の語。remove / move でなければ None。"""
    if len(rest) < 2 or rest[1] not in ("remove", "move"):
        return None
    return next((a for a in rest[2:] if not a.startswith("-")), None)


def _worktree_targets(
    rest: list[str], here: str | None, msys: bool = False, norms: dict[str, str] | None = None
) -> list[str]:
    """`git worktree remove|move <path>` の `<path>` を、here からのパスとして読んだ先。norms は `_resolve` の realpath の覚え。"""
    operand = _worktree_operand(rest)
    resolved = _resolve(here, operand, msys, norms=norms) if operand is not None else None
    return [resolved] if resolved else []


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _exact(text: str) -> str:
    return text


def _folded(text: str) -> str:
    """大文字小文字を区別しない git (core.ignorecase) の比べ方。ASCII だけを小文字にし (`é` と `É` は別の名前)、
    macOS の git が引数を NFC にそろえるのに合わせる。"""
    return unicodedata.normalize("NFC", text).translate(_ASCII_LOWER)


def _ends_with_name(path: str, name: str) -> bool:
    """path が、区切りの直後 (か先頭) から name で終わるか。"""
    start = len(path) - len(name)
    return path.endswith(name) and (start == 0 or path[start - 1] == os.sep)


def _worktree_by_name(rest: list[str], fam: Family) -> set[str]:
    """`git worktree remove|move <worktree>` の `<worktree>` を、git が worktree の名前として解決する先。

    git はパスとして読む前に、`<worktree>` で終わる worktree のパス (区切りの直後から一致するもの) を数え、ちょうど 1 つなら
    それを選ぶ (2 つ以上ならパスとして読む)。今の場所に関係なく、`git worktree remove --force b` は `.../b` の worktree を消す。
    比べ方は core.ignorecase (macOS では既定で true) で変わるので、区別する読みと区別しない読みのそれぞれで数え、
    どちらかで 1 つに決まったものを返す。git が持つパスは realpath なので (`git worktree list` の行)、checkout root と比べる。
    """
    operand = _worktree_operand(rest)
    if operand is None:
        return set()
    found = set()
    for fold in (_exact, _folded):
        name = fold(os.path.normcase(operand))
        hits = [root for root in fam.roots if _ends_with_name(fold(root), name)]
        if len(hits) == 1:
            found.add(hits[0])
    return found


def _worktree_may_be_other(rest: list[str], fam: Family, msys: bool = False) -> bool:
    """git が読む repo か今の場所が決まらないときに、`git worktree remove|move <worktree>` が別の worktree を指しうるか。

    名前 (パスの末尾) が別の worktree に一致するときと、名前で決まらない相対パスのときに True。相対パスは基点が決まらないので、
    綴りに関係なくどの worktree にも届きうる (`../wt-b` や、symlink を通る `wt/<name>`・`lnk`。git は名前で決まらないとパスを
    realpath で比べる)。変数の語 (`"$Z"` / `"../$Z"`) も、どの worktree の名前にもパスにもなりうるので True。
    自分の worktree の名前と絶対パス (パスとして読む側で判定する) は False。
    """
    operand = _worktree_operand(rest)
    if operand is None:
        return False
    if _dynamic(operand):
        return True
    if _resolve(None, operand, msys) is not None:
        return False
    names = _worktree_by_name(rest, fam)
    if names:
        return any(o != fam.home for o in names)
    return True


def _chdir_form(cmd: str, args: list[str], closes: bool = True) -> tuple[frozenset[str], list[str]]:
    """`cd` / `chdir` / `pushd` の option を読み、(option と語の数の上で移動する shell の集合, 語) を返す。

    redirect の直前の数字の語は fd として外すが、語がそれだけのとき (`cd 2>x` と `cd 2 >x` は字句に分けた後では区別できない) は
    行き先として残す。語の無い `cd >x` は redirect の記号を語として返す (行き先は今の場所の下になり、旧来と同じ読み)。
    `pushd >x` は語なしとして返す (スタックの入れ替えで、行き先が分からない)。
    """
    table = _PUSHD_OPT_SHELLS if cmd == "pushd" else _CD_OPT_SHELLS
    shells = _ZSH if cmd == "chdir" else _BOTH  # bash に chdir は無い
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-" or a[1:].isdigit():
            break  # `-` と `-1` は option ではなく、直前のディレクトリ / dir stack の参照
        for letter in a[1:]:
            shells &= table.get(letter, _NEITHER)
        i += 1
    rest = args[i:]
    words = _words(rest, closes) or [t for t in rest[:1] if t != "}" and (cmd != "pushd" or t.isdigit())]
    if len(words) > 1:
        shells &= _BASH  # 語が 2 つの `cd a b` は bash 3.2 が a へ移動し (bash 5 は失敗する)、zsh は置換として読んで失敗する
    return shells, words


def _cd_target(words: list[str]) -> str | None:
    """`cd` / `pushd` の移動先。`cd -` (直前のディレクトリ) など決められなければ None。"""
    if not words:
        return "~"
    if words[0].startswith(("+", "-")):
        return None  # 直前のディレクトリ / dir stack の参照
    return words[0]


# here-document の開始 (`<<EOF` / `<<-'EOF'` / `<< "EOF"` / `<<\EOF` / `<<'END MSG'` / `<<'2EOF'`)。
# `<<<` (here-string) は除く。区切り語は shell の 1 単語で、引用符と `\` を外したものが終端行になる。
# 引用符の中には空白や数字始まりも書ける。`$x` / `${x}` は展開せず文字どおり比べる (bash / zsh と同じ)。`$'E'` は
# escape を含まないものだけ読む。`$(` や backtick、escape を含む `$'...'`、入れ子の `${` を含む語は読まない
# (here-document と見なさない。本文をコードとして読む)。zsh は区切り語に続けて書いた `(...)` も区切り語に含める
# (`<<E(x)` の終端行は `E(x)`。bash は構文エラー) ので、`(` が続く語も読まない
_HEREDOC = re.compile(
    r"(?<!<)<<(-?)[ \t]*"
    r"((?:'[^'\n]*'|\$'[^'\\\n]*'|\"(?:[^\"\\\n$`]|\\[\"\\])*\"|\\[^\n]|[^\s;&|()<>'\"\\$`]|\$\{[^\s{}'\"\\$`]*\}|\$(?![({'\"]))+)(?=[\s;&|)<>]|$)"
)
_HEREDOC_PART = re.compile(r"\$?'([^']*)'|\"((?:[^\"\\]|\\.)*)\"|\\(.)|(.)", re.S)


def _heredoc_delim(word: str) -> str:
    """区切り語から引用符と `\\` を外した終端行。"""
    out = []
    for m in _HEREDOC_PART.finditer(word):
        if m.group(2) is not None:
            out.append(re.sub(r"\\([\"\\])", r"\1", m.group(2)))
        else:
            out.append(next(g for g in m.groups() if g is not None))
    return "".join(out)


# この文字の直後 (と文字列の先頭) は単語の始まり。`#` は単語の始まりでだけ comment になる
_WORD_BREAK = " \t\n;&|()<>"
# 引用符の frame。`$'` は `$'...'` (中で `\'` が引用符を守る)。それ以外の frame は
# `(` = サブシェル、`((` = 算術 (`((` の 2 つ目の `(`)、`w(` = 単語に続けた `(` (zsh の glob の group)、
# `$(` = コマンド置換・プロセス置換 (単語の一部)、
# `"$(` = 二重引用符の中のコマンド置換 (中身は出さない)、`` ` `` = backtick
_QUOTES = ("'", '"', "$'")


def _skip_heredoc_body(command: str, i: int, strip_tabs: bool, delim: str) -> tuple[int, bool]:
    """i 行目から始まる here-document の本文と終端行を飛ばし、(その次の位置, 終端行があったか) を返す。"""
    while i < len(command):
        end = command.find("\n", i)
        end = len(command) if end < 0 else end
        line = command[i:end]
        i = end + 1
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return min(i, len(command)), True
    return len(command), False


class _Normalizer:
    """shlex に渡す前に、shell が字句に分ける前後で行うことを済ませる。

    - 行の継続 (`\\` + 改行) をつなぐ。単一引用符と `$'...'` の中では文字どおり残す
    - comment (単語の始まりの `#` から改行の手前まで。backtick の中では閉じの手前まで) を捨てる。
      改行はコマンドの区切りなので残す。単語の途中 (`feature#1`、`$(...)#`)・引用符の中・`\\#` は
      comment ではない
    - here-document の本文を取り除く。本文はコマンドの標準入力に渡る文字列で、shell は実行しない
      (`cat > x.sh <<'EOF'` で git を含む script を書くだけのことが多い)。引用符や comment の中の
      `<<` は here-document ではない。同じ行で閉じる `$(...)` や backtick の中で始まったものは、外の行を
      本文にしない
    - 二重引用符の中の `$(...)` は中身を出さずに `$()` にする (中の git は読まない。`git commit -m
      "$(cat <<'EOF' ...)"` の本文の引用符で、外の引用符の対応が崩れないように)
    - `$'...'` は、shlex が読める単一引用符の形に書き換える
    - 閉じない引用符があれば、その引用符を含む行から後ろを捨てる (bash はその手前の行までを実行する)

    引用符の規則は後段の shlex (posix) に合わせる (`'...'` の中に escape は無い / `"..."` と引用符の
    外では `\\` が次の 1 文字を守る)。shlex の comment は単語の途中の `#` でも始まり、改行まで
    読み捨てるため、`#` の次の行が前の行につながっていた。
    """

    def __init__(self, command: str, ignore: frozenset[int] = frozenset()):
        self.src = command
        self.ignore = ignore  # here-document と読まない `<<` の位置 (安全網の読み直しで使う)
        self.out: list[str] = []
        self.stack: list[str] = []  # 開いている引用符・括弧の frame (_QUOTES の説明を参照)
        # 本文が次の行から始まる here-document (`-` 付きか, 終端行, 書いた深さ, `<<` の位置)
        self.pending: list[tuple[bool, str, int, int]] = []
        self.quiet = 0  # 二重引用符の中の `$(` の深さ。0 でないときは出さない
        self.line_start = 0  # 引用符の外の改行の直後の out の位置
        self.word_start = True
        self.prev = ""  # 直前に読んだ素の 1 文字 (escape や引用符の後は空)
        # 閉じない frame か終わらない here-document を残して読み終えたか (run() の後で見る)。
        # True のときは、この読みが shell とずれて後ろを読み落としている可能性がある
        self.unclosed = False
        # 終わらない here-document (本文が最後まで続いたか、本文が始まらなかったもの) の `<<` の位置
        self.open_heredocs: set[int] = set()

    def emit(self, s: str) -> None:
        if not self.quiet:
            self.out.append(s)

    def run(self) -> str:
        src, n = self.src, len(self.src)
        i = 0
        while i < n:
            i = self._step(i, src[i], src[i + 1 : i + 2])
        # 本文の始まらなかった here-document (`<<E` で終わる行が最後の行) も終わらないものに数える
        self.open_heredocs.update(p[3] for p in self.pending)
        self.unclosed = bool(self.stack) or bool(self.open_heredocs)
        if any(f in _QUOTES for f in self.stack):
            return "".join(self.out[: self.line_start])
        return "".join(self.out)

    def _step(self, i: int, ch: str, nxt: str) -> int:
        top = self.stack[-1] if self.stack else ""
        if top == "'":
            self.emit(ch)
            if ch == "'":
                self.stack.pop()
            return i + 1
        if top == "$'":
            if ch == "\\":
                self.emit("'\\''" if nxt == "'" else ch + nxt)
                return i + 2
            self.emit(ch)
            if ch == "'":
                self.stack.pop()
            return i + 1
        if ch == "\\":
            # 行の継続 (引用符の外と二重引用符の中) は消すだけで、単語の始まりかどうかを変えない。
            # 末尾の `\` は shell も捨てる
            if nxt not in ("\n", ""):
                self.emit(ch + nxt)
                self.word_start, self.prev = False, ""
            return i + 2
        if top == '"':
            if ch == "$" and nxt == "(":
                self.emit("$(")
                self.quiet += 1
                self.stack.append('"$(')
                self.word_start, self.prev = True, ""
                return i + 2
            self.emit(ch)
            if ch == '"':
                self.stack.pop()
            return i + 1
        return self._code(i, ch, nxt, top)

    def _code_frame(self) -> str:
        """いちばん内側の、サブシェルの `(` と単語の `w(` 以外の frame (算術かコマンド置換か)。"""
        return next((f for f in reversed(self.stack) if f not in ("(", "w(")), "")

    def _code(self, i: int, ch: str, nxt: str, top: str) -> int:
        """引用符の外 (括弧や backtick の中を含む) の 1 文字。"""
        if ch in "'\"" or (ch == "$" and nxt == "'"):
            quote = "$'" if ch == "$" else ch
            self.stack.append(quote)
            self.emit("'" if quote == "$'" else ch)
            self.word_start, self.prev = False, ""
            return i + len(quote)
        if ch == "`":
            if top == "`":
                self.stack.pop()
                # `$(` の閉じと同じく、同じ行で閉じた backtick の中の here-document は外の行を本文にしない
                self.pending = [p for p in self.pending if p[2] <= len(self.stack)]
                self.word_start = False
            else:
                self.stack.append("`")
                self.word_start = True
            self.emit(ch)
            self.prev = ""
            return i + 1
        if ch == "#" and self.word_start:
            j = i
            while j < len(self.src) and self.src[j] != "\n" and not (top == "`" and self.src[j] == "`"):
                j += 1
            return j
        if ch == "<" and self._code_frame() != "((" and i not in self.ignore:
            # 算術の中の `<<` はシフト。here-document として読むと後ろの行を本文として捨てる
            m = _HEREDOC.match(self.src, i)
            if m:
                self.pending.append((m.group(1) == "-", _heredoc_delim(m.group(2)), len(self.stack), i))
                self.emit(m.group(0))
                self.word_start, self.prev = False, ""
                return m.end()
        if ch == "(":
            if self.prev == "(" and top in ("(", "$("):
                # `((` は算術。中の改行は here-document の本文を始めない (2 つ目の `(` を算術の frame にする)
                self.stack.append("((")
            elif self.prev in ("$", "<", ">"):
                self.stack.append("$(")
            elif not self.word_start and self.prev != "=":
                # 単語に続けた `(` は zsh の glob の group で単語の一部 (bash は構文エラー)。閉じの後の `#` は
                # comment ではなく、中の改行は here-document の本文を始めない
                self.stack.append("w(")
            else:
                self.stack.append("(")
        elif ch == ")" and top in ("(", "((", "w(", "$(", '"$('):
            self.stack.pop()
            if top not in ("(", "((", "w("):
                # 同じ行で閉じたコマンド置換の中の here-document は、外の行を本文にしない
                self.pending = [p for p in self.pending if p[2] <= len(self.stack)]
            if top == '"$(':
                self.quiet -= 1
            self.emit(ch)
            self.word_start, self.prev = top == "(", ""
            return i + 1
        self.emit(ch)
        self.word_start, self.prev = ch in _WORD_BREAK, ch
        if ch == "\n":
            if not any(f in _QUOTES for f in self.stack):
                self.line_start = len(self.out)
            # 本文はその here-document を書いた深さの改行から始まる。間にコマンド置換 (`$(` / `"$(` /
            # backtick / `<(`) や算術の `((` があれば、その中の改行ではまだ始まらない (サブシェルの `(` の中の
            # 改行では始まる)
            keep = []
            for p in self.pending:
                strip_tabs, delim, depth, pos = p
                if all(f == "(" for f in self.stack[depth:]):
                    i, closed = _skip_heredoc_body(self.src, i + 1, strip_tabs, delim)
                    i -= 1
                    if not closed:
                        self.open_heredocs.add(pos)
                else:
                    keep.append(p)
            self.pending = keep
        return i + 1


# 安全網の旧来の読み (0.1.5 まで) の here-document の開始。識別子の区切り語だけを読む
_LEGACY_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")


def _legacy_strip_heredocs(command: str) -> str:
    """旧来の読みの here-document の除去。引用符や comment を見ずに、行ごとに `<<EOF` を探す。"""
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _LEGACY_HEREDOC.finditer(line):
            strip_tabs, delim = m.group(1) == "-", m.group(3)
            while i < len(lines):
                body = lines[i]
                i += 1
                if (body.lstrip("\t") if strip_tabs else body) == delim:
                    break
    return "\n".join(out)


def _tokens(command: str, commenters: str) -> list[str] | None:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = commenters
        return list(lex)
    except ValueError:
        return None  # 分解できないコマンドは判定しない (git 自身もまず動かない)


# 安全網で、終わらない here-document を here-document と読まずに読み直す回数の上限
_REREAD_LIMIT = 8

# 読み取りの期限 (秒)。`analyze` の 1 回の呼び出し全体 (安全網の読み直しを含む) で 1 つで、最初の読みを字句に分け終えたところ
# から数える (`_Deadline.start`)。git を含む実コマンドの読み取りは
# 最大でも数十ミリ秒。hook の timeout (hooks.json の 10 秒) を超えるとコマンドはそのまま走るので、遅くなる形を個別に閉じるのでは
# なく、時間切れの経路そのものを閉じる。過ぎたら読むのをやめ、それまでの判定に注意を 1 つ足して返す (止めない)。
# README の「目的と限界」に同じ値を書いている。None は期限なし (時間の伸び方を測るテスト用)
_READ_SECONDS: float | None = 3.0


class _Deadline:
    """`analyze` の 1 回の呼び出しの期限。最初の読みを字句に分け終えたところから数える (`start`)。"""

    def __init__(self, seconds: float | None):
        self.seconds = seconds
        self._end: float | None = None  # `start` で決める
        self.passed = False

    def start(self) -> None:
        """期限を数え始める。2 回目以降は何もしない (安全網の読み直しも、最初の読みと同じ期限で読む)。

        字句に分ける処理 (正規化と shlex) は打ち切れないので、そこを期限に入れると、長い 1 語 (引用した 0.6 MB ほど) を
        字句に分けるだけで期限を使い切り、手前の git も読まずに注意だけになる (0.9.3 は読み終えて止めていた)。
        """
        if self._end is None and self.seconds is not None:
            self._end = time.monotonic() + self.seconds

    def check(self) -> bool:
        """期限を過ぎたか。一度過ぎたら、以後はずっと True。"""
        if not self.passed and self._end is not None and time.monotonic() >= self._end:
            self.passed = True
        return self.passed

    def finding(self) -> Finding:
        return Finding(False, f"コマンドの読み取りが期限 ({self.seconds:g} 秒) を超えたため、途中から後ろの git を確かめていない")


def _analyze_tokens(
    tokens: list[str], cwd: str, fam: Family, deadline: _Deadline | None = None, norms: dict[str, str] | None = None
) -> list[Finding]:
    """zsh の読みと bash の読みの両方で判定し、どちらかで見つかったものを返す。

    Bash tool の shell は環境で違い、`command cd` / `chdir` / `cd -q` などは shell で動きが割れる。shell ごとに通して読むので、
    片方の shell でしか起きない移動を、もう片方の失敗と組み合わせて読むことはない。

    bash は折り返した list を切らない読みと切る読みの両方を通す。切らない読みは、`&` で手前の list の開始時点の変数に戻すので、
    改行のあとの git が見る変数を取り違えることがある (`export GIT_DIR=<worktree>/.git; command export GIT_DIR=<main>/.git &&` と
    改行のあとの `true & git -C <main> reset --hard`)。
    Windows では、パスの綴りの読み (`_drive_readings`) ごとにも通す。期限を過ぎた後の読みは、最初の字句で返る。
    norms は realpath の覚え (`_resolve`)。どの読みにも同じものを渡す。
    """
    findings: list[Finding] = []
    seen: set[str] = set()
    for msys in _drive_readings():
        for shell, fold in (("zsh", True), ("bash", True), ("bash", False)):
            for f in _Analyzer(cwd, fam, shell, fold, msys, deadline, norms).run(tokens):
                if f.message not in seen:
                    seen.add(f.message)
                    findings.append(f)
    return findings


def _read(
    normalized: str, cwd: str, fam: Family, deadline: _Deadline | None = None, norms: dict[str, str] | None = None
) -> list[Finding]:
    # comment は正規化で捨てた。残った `#` は単語の一部
    tokens = _tokens(normalized, commenters="")
    if deadline is not None:
        deadline.start()
    return [] if tokens is None else _analyze_tokens(tokens, cwd, fam, deadline, norms)


def analyze(command: str, cwd: str, fam: Family) -> list[Finding]:
    return analyze_readings(command, ((cwd, fam),))


def analyze_readings(command: str, readings: tuple[tuple[str, Family], ...]) -> list[Finding]:
    """作業ディレクトリの読み (`family.detect_readings` の (cwd, Family)) ごとに判定し、どれかで見つかったものを返す (読みの和)。

    期限は全体で 1 つ (読みが 2 つになっても、hook の時間の上限までの余裕を減らさない)。読みが 1 つなら `analyze` と同じ。
    realpath の覚え (`_resolve` の norms) も全体で 1 つ。
    """
    if "git" not in command:
        return []
    deadline = _Deadline(_READ_SECONDS)
    norms: dict[str, str] = {}
    findings: list[Finding] = []
    seen: set[str] = set()
    for cwd, fam in readings:
        for f in _analyze(command, cwd, fam, deadline, norms):
            if f.message not in seen:
                seen.add(f.message)
                findings.append(f)
    if deadline.passed:
        findings.append(deadline.finding())
    return findings


def _analyze(command: str, cwd: str, fam: Family, deadline: _Deadline, norms: dict[str, str]) -> list[Finding]:
    normalizer = _Normalizer(command)
    findings = _read(normalizer.run(), cwd, fam, deadline, norms)
    if not normalizer.unclosed:
        return findings
    # 安全網: 閉じない frame か終わらない here-document が残ったときは、正規化の読みが shell とずれて
    # 後ろの行を読み落としている可能性がある。別の読みでも判定し、どれかで見つかったものを返す
    # (読みの和集合で止める側に倒す)。
    # - 正規化しない旧来の読み (行ごとの here-document の除去と、shlex の comment だけ)
    # - 終わらない here-document を here-document と読まない読み (shell がそこを here-document と読まなければ、
    #   後ろの行はコマンドとして走る。読み直して新たに終わらないものが出れば、それも外して繰り返す)
    readings = []
    if not deadline.check():
        legacy = _tokens(_legacy_strip_heredocs(command), commenters="#")
        if legacy is not None:
            readings.append(_analyze_tokens(legacy, cwd, fam, deadline, norms))
    ignore = frozenset(normalizer.open_heredocs)
    for _ in range(_REREAD_LIMIT):
        if not ignore or deadline.check():
            break
        again = _Normalizer(command, ignore)
        readings.append(_read(again.run(), cwd, fam, deadline, norms))
        if again.open_heredocs <= ignore:
            break
        ignore |= again.open_heredocs
    seen = {f.message for f in findings}
    for reading in readings:
        for f in reading:
            if f.message not in seen:
                seen.add(f.message)
                findings.append(f)
    return findings


# 1 つの「実行時にありうる状態」: (作業ディレクトリ, pushd のスタック, 直前のコマンドの成否)。
# 作業ディレクトリが静的に決まらなければ None。スタックは `_Stacks` の節の番号。成否は "ok" / "fail"
_Alt = tuple
_OPS = re.compile(r"&&|\|\||\|&|;;|[|&;()\n]")
# 空のスタックの節の番号
_EMPTY = 0


class _Stacks:
    """pushd のスタックの表。スタックは節の番号で持ち、節は (親の節の番号, 先頭の要素) の組を 1 つの番号に intern したもの。

    同じ中身のスタックは同じ番号になるので、状態の比較とハッシュは番号で済み、push / pop はスタックの深さに関係なく定数時間で済む。
    スタックを tuple で持つと、push のたびにスタックを複製し、状態のハッシュもスタックの深さに比例するので、`x || cd d<i>;` を
    6 回続けた後に `pushd <dir>;` を数千回続けた形で、読みの時間が状態の数 × 深さの 2 乗に伸びていた。表は 1 つの読み (`_Analyzer`) ごとに持つ。
    要素は行き先 (正規化済みの絶対パス) か、行き先の分からない None。

    深く重ねた形では節が「状態の数 × 深さ」個できる (底で割れた 64 本のスタックは、上の段を共有できない) ので、1 つの節を小さく
    持つ。節の情報は array に持ち、各節の最初の子 (最初に積んだ要素の番号とその節) も array に持つ。dict は、同じ節に 2 つ目以降の
    違う要素を積んだときだけ使う (key は (親の節の番号, 要素の番号) を 1 つの int にしたもの)。積み続ける形の節は、どれも子が 1 つ。
    """

    def __init__(self) -> None:
        # 空のスタックから外した残りは空のスタック (`popd +N` は空のスタックでも空のまま)
        self.parent = array("q", [_EMPTY])
        self.depth = array("q", [0])
        self.top: list[str | None] = [None]
        self._first_item = array("q", [-1])  # 節ごとの、最初の子の要素の番号 (-1 は子なし)
        self._first_node = array("q", [_EMPTY])  # 節ごとの、最初の子の節
        self._items: dict[str | None, int] = {}  # 要素ごとの番号
        self._others: dict[int, int] = {}  # 2 つ目以降の子
        self._unknown = [_EMPTY]  # 深さごとの、要素がすべて None のスタック

    def push(self, stack: int, item: str | None) -> int:
        code = self._items.get(item)
        if code is None:
            code = self._items[item] = len(self._items)
        first = self._first_item[stack]
        if first == code:
            return self._first_node[stack]
        key = None
        if first != -1:
            # 要素の番号は節の数より小さく、節は 2**32 個に届かない (1 つの節は数十バイトなので、メモリが先に尽きる)
            key = stack << 32 | code
            node = self._others.get(key)
            if node is not None:
                return node
        node = len(self.top)
        self.parent.append(stack)
        self.depth.append(self.depth[stack] + 1)
        self.top.append(item)
        self._first_item.append(-1)
        self._first_node.append(_EMPTY)
        if key is None:
            self._first_item[stack] = code
            self._first_node[stack] = node
        else:
            self._others[key] = node
        return node

    def unknown(self, depth: int) -> int:
        """要素がすべて行き先の分からないもの (None) の、深さ depth のスタック。"""
        while len(self._unknown) <= depth:
            self._unknown.append(self.push(self._unknown[-1], None))
        return self._unknown[depth]


# 状態の集合 (今の状態・移動が失敗した経路・`&` で戻す時点) の大きさの上限。普段の形は数個で収まる (git を含む実コマンド
# 12,725 件で最大 6、この suite の入力で最大 4)。分岐ごとに行き先が割れる形は状態が倍々に増え、6 回の分岐で 64 に届く。
# 上限に張り付いた形は状態の数に比例して遅くなるので (256 では 1,000 回の繰り返しで約 1.7 秒)、普段の最大の 10 倍ほどに抑えた。
# README の「目的と限界」に同じ値を書いている
_STATE_LIMIT = 64


def _cap(alts: set[_Alt], stacks: _Stacks) -> set[_Alt]:
    """状態が上限を超えたら、成否ごとに 1 つの状態にまとめる。成否ごとに、その成否の全状態で同じでない作業ディレクトリとスタックは、
    決まらないものにする。stacks は状態のスタックの番号を引く表。

    `x || cd d1; x || cd d2; ...` (相対パス) や `x || pushd /d1; ...` は、分岐ごとに行き先かスタックが割れて、状態が倍々に増える。
    hook が時間切れになると Claude Code はコマンドをそのまま走らせるので、形ごとではなく数で閉じる。まとめた後の git は、
    行き先の決まらない移動の後ろと同じく注意だけにする (止めない)。絶対パスの `cd <dir>` / `git -C <dir>` は、まとめた後も読む。

    その成否の全状態で同じ作業ディレクトリとスタックは、そのまま残す。割れたスタックは、その成否のどの状態でも積まれている
    深さだけ、行き先の分からない要素 (None) で残す。`()` にすると空のスタックと読まれ、popd が「失敗してその場に残る」読みになって止めすぎる
    (`pushd /t/x; <上限を超える形>; cd <main>; popd; git reset --hard` の popd は、どの状態でも `/t/x` の手前に戻る)。
    """
    if len(alts) <= _STATE_LIMIT:
        return alts
    out = set()
    for st in {a[2] for a in alts}:
        # 成否ごとに決める。成功の側だけが積んだ形 (`x && pushd <dir> && ...`) で、失敗の側の浅いスタックを成功の側に持たせない
        heres = {h for h, _, s in alts if s == st}
        kept = {k for _, k, s in alts if s == st}
        here = next(iter(heres)) if len(heres) == 1 else None
        stack = next(iter(kept)) if len(kept) == 1 else stacks.unknown(min(stacks.depth[k] for k in kept))
        out.add((here, stack, st))
    return out


class _Analyzer:
    """shell の実行をたどり、ありうる状態の集合を持ち回る。

    `&&` / `||` の後ろのコマンドは、直前が成功 / 失敗した状態でだけ実行する。cd は成功するものとして
    扱い、git や一般のコマンドは成功・失敗の両方がありうるとする。git を実行しうる状態のどれかで
    対象が別の checkout なら止める。

    移動が失敗して `||` の右に進む経路は、`failed_moves` で別に持つ。`alts` に混ぜないのは、右辺の後ろに残さないため
    (`cd <main>; cd <worktree> || exit 1; git add .` の `git add .` を main で動くと読まないように)。
    右辺が終わると (`&&` / `;` / 改行 / `&`) 捨てるので、`alts` の読みは減らない。
    """

    def __init__(
        self,
        cwd: str,
        fam: Family,
        shell: str,
        fold: bool = True,
        msys: bool = False,
        deadline: _Deadline | None = None,
        norms: dict[str, str] | None = None,
    ):
        self.shell = shell  # この読みの shell ("zsh" / "bash")
        self.fold = fold  # `&&` / `||` の直後の区切りで list を切らない読みか
        self.msys = msys  # パスの `/c/x` を Git Bash の読み (`C:\x`) で解決するか
        self.deadline = deadline  # 過ぎたら、残りの字句を読まずに返す
        self.fam = fam
        self.findings: list[Finding] = []
        self._seen: set[str] = set()
        self.stacks = _Stacks()  # 状態のスタックの番号を引く表
        self._absolute: dict[str, str] = {}  # 移動の行き先の絶対パスを正規化した値 (`_resolve` の memo)
        self._norms: dict[str, str] = {} if norms is None else norms  # realpath の覚え (`_resolve` の norms。ほかの読みと共有)
        self.alts: set[_Alt] = {(_norm_once(cwd, self._norms), _EMPTY, "ok")}
        self.env: dict[str, str] = {}  # export 済みの GIT_DIR / GIT_WORK_TREE
        self.shvars: dict[str, str] = {}  # 代入したが export していないもの
        self.op: str | None = None  # 次のコマンドの前にある && / ||
        self.in_pipe = False  # 今のコマンドが `|` の後ろ (pipeline の要素) か
        # `&` で戻す時点。bash は `&` が AND-OR list 全体を background にするので list の開始時点、
        # zsh は最後の pipeline だけを background にするので、list の開始時点からその pipeline の開始時点までの候補
        self.list_start = self._snapshot()
        self.scopes: list[tuple] = []  # `( ... )` に入るときの状態
        self.list_continues = False  # `&&` / `||` の直後で、まだコマンドを読んでいない (次の改行で list は切れない)
        self.braces = 0  # 開いている `{` の数 (語の末尾の `}` を閉じと読むか)
        self.failed_moves: set[_Alt] = set()  # 移動が失敗した状態と、それを `||` の右で実行した後の状態
        self.after_or = False  # `||` の後ろで、まだコマンドを読んでいない

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

    def _failed_moves_to_run(self) -> set[_Alt]:
        """今のコマンドを実行する、移動が失敗した経路の状態。`||` の後ろでなければ `|` の後ろか語に続く `$(` で、どの状態でも実行する。"""
        if self.after_or:
            return {a for a in self.failed_moves if a[2] == "fail"}
        return set(self.failed_moves)

    def _forget_failed_moves(self) -> None:
        self.failed_moves = set()
        self.after_or = False

    # --- 字句の走査 ---------------------------------------------------------

    def run(self, tokens: list[str]) -> list[Finding]:
        seg: list[str] = []
        deadline = self.deadline
        for tok in tokens:
            # 期限は字句ごとに見る。区切りの字句は 1 つの単純コマンドをまとめて処理し、その仕事は状態の数 (`_STATE_LIMIT`) ×
            # 語の数・語の長さで伸びるので、移動 (`_simple`) と git (`_check_git`) では状態ごとにも見る。越えるのは、1 つの状態の
            # 1 回の読み (1 つの相対パスの語の解決や、1 つの git の語) の分まで
            if deadline is not None and deadline.check():
                return self.findings
            if not (tok and set(tok) <= set(_PUNCT)):
                seg.append(tok)
                continue
            if tok.startswith((">", "<")) or tok.endswith(">"):
                seg.append(tok)  # リダイレクト記号はそのまま
                continue
            for op in _OPS.findall(tok):
                if op == "\n" and not seg and self.in_pipe:
                    continue  # `|` の後ろの改行は pipeline の続き
                self._flush(seg, next_is_pipe=op in ("|", "|&"))
                seg = []
                self._operator(op)
                self._bound()
        self._flush(seg, next_is_pipe=False)
        return self.findings

    def _bound(self) -> None:
        """状態の集合を上限 (`_STATE_LIMIT`) に収める。1 つのコマンドと演算子で増えるのは高々数倍なので、ここで毎回まとめれば足りる。"""
        self.alts = _cap(self.alts, self.stacks)
        self.failed_moves = _cap(self.failed_moves, self.stacks)
        start, env, shvars = self.list_start
        if len(start) > _STATE_LIMIT:
            self.list_start = (frozenset(_cap(set(start), self.stacks)), env, shvars)

    def _operator(self, op: str) -> None:
        if op in ("&&", "||"):
            self.op = op
            self.list_continues = True
            if self.shell == "zsh":
                # zsh の `&` は最後の pipeline だけを background にする。手前の `cd <dir> &&` は今の shell に残る。
                # その cd が失敗すれば手前の場所に残るので、それまでの候補も持つ (`cd -q <main>; cd <無いパス> && x & git ...`)
                alts, env, shvars = self._snapshot()
                self.list_start = (self.list_start[0] | alts, env, shvars)
            if op == "||":
                self.after_or = True
            else:
                # `cd <main>; cd <worktree> && false || git ...` の `||` を、移動の失敗の右辺と読まない
                self._forget_failed_moves()
        elif op in ("|", "|&"):
            self.op = None
        elif op == "&":
            # background で動いたものは今の shell に残らない。親 shell の状態はその手前の時点のまま
            self._restore(self.list_start)
            self.alts = {(h, s, "ok") for h, s, _ in self.alts}
            self._forget_failed_moves()
            self._new_list()
        elif op == "(":
            run, skipped = self._split()
            # `|| (...)` の括弧と、`|| x=$(...)` の手前の語を実行した経路の中身
            moved = self._failed_moves_to_run()
            self.scopes.append((frozenset(run), skipped, dict(self.env), dict(self.shvars), self.list_start, moved))
            self.alts = run | moved
            self._forget_failed_moves()
            self._new_list()
        elif op == ")":
            if self.scopes:
                entered, skipped, env, shvars, list_start, moved = self.scopes.pop()
                # 中の cd / export は外に残らない。成否は中身次第なので両方ありうる
                self.alts = self._both(entered) | skipped
                self.env, self.shvars, self.list_start = env, shvars, list_start
                self.failed_moves = self._both(moved)
            self.op = None
        else:  # ; ;; 改行
            # `||` の直後の改行は右辺の続き。list は切るが、移動の失敗の経路は次のコマンドまで持ち越す
            # (`&&` / `||` の直後の `;` は、bash は構文エラー。zsh は `;` を読み飛ばして右辺が続く)
            if not self.after_or:
                self._forget_failed_moves()
            start = self.list_start if self.fold and self.list_continues else None
            self._new_list()
            if start is not None:
                # `a &&\nb &` は 1 つの list。`&` で戻す候補に、改行の手前の list の開始時点も残す
                # (改行の時点の候補も捨てない。0.6.0 が止めていた形を緩めないため)
                alts, env, shvars = self.list_start
                self.list_start = (start[0] | alts, {**env, **start[1]}, {**shvars, **start[2]})

    def _new_list(self) -> None:
        self.op = None
        self.list_start = self._snapshot()

    def _flush(self, seg: list[str], next_is_pipe: bool) -> None:
        # pipeline の最後の要素は、zsh では今の shell で動き (中の cd が残る)、bash ではサブシェルで動く
        last = self.in_pipe and not next_is_pipe
        subshell = next_is_pipe or self.in_pipe
        self.in_pipe = next_is_pipe
        if not seg:
            return
        self.list_continues = False
        # 括弧の数は、そのコマンドを実行する状態が無くても数える (`false && { x }` の `{` と `}`)
        rest = _strip_prefix(seg)[0]
        self.braces += seg[: len(seg) - len(rest)].count("{")
        run, skipped = self._split()
        # 予約語だけの語 (`|| {` / `|| if` / `|| !`) はまだコマンドではなく、右辺の最初のコマンドはこの後ろにある
        if not all(t in _KEYWORDS for t in seg):
            self._step_failed_moves(seg, rest, run, last, subshell)
        if run:
            run = self._settle(seg, run, last, subshell)
        if self.braces and _before_redirect(rest)[-1:] == ["}"]:
            self.braces -= 1
        self.alts = run | skipped

    def _settle(self, seg: list[str], alts: set[_Alt], last: bool, subshell: bool) -> set[_Alt]:
        after = self._command(seg, alts)
        if last:
            return after if self.shell == "zsh" else self._both(alts)
        # pipeline の途中の要素はどちらの shell でもサブシェルで動くので、中の cd は外に残らない
        return self._both(alts) if subshell else after

    def _step_failed_moves(self, seg: list[str], rest: list[str], run: set[_Alt], last: bool, subshell: bool) -> None:
        """移動が失敗した経路で今のコマンドを読み、今のコマンドが移動なら失敗した経路を足す。

        今の shell で動かない移動 (`env cd`) や pipeline の要素の移動も足すが、`alts` に同じ失敗の状態が既にあるので読みは変わらない。
        """
        moved = self._failed_moves_to_run()
        self.after_or = False
        after: set[_Alt] = set()
        if moved:
            # この経路は右辺の中で捨てるので、`|| unset GIT_DIR` のような変更を `alts` の読みに持ち込まない
            env, shvars = dict(self.env), dict(self.shvars)
            after = self._settle(seg, moved, last, subshell)
            self.env, self.shvars = env, shvars
        # `!` の後ろの移動の失敗は読まない (`!` の成否を読まないのと同じ)
        if rest[:1] and rest[0] in _MOVERS and "!" not in seg[: len(seg) - len(rest)]:
            after |= {(h, s, "fail") for h, s, _ in run | moved}
        self.failed_moves = after

    # --- 1 コマンド -----------------------------------------------------------

    def _command(self, seg: list[str], alts: set[_Alt]) -> set[_Alt]:
        rest, chdir, assigns, clear, shells, negated = _strip_prefix(seg)
        after = self._simple(rest, chdir, assigns, clear, shells, alts)
        # `!` の成否は読まない。移動の読みはそのままで、`&&` / `||` の後ろはどちらも実行しうるとする
        return self._both(after) if negated else after

    def _simple(
        self,
        seg: list[str],
        chdir: str | None,
        assigns: dict[str, str | None],
        clear: bool,
        shells: frozenset[str],
        alts: set[_Alt],
    ) -> set[_Alt]:
        ok = {(h, s, "ok") for h, s, _ in alts}
        in_shell = self.shell in shells  # wrapper の後ろのコマンドが、この shell では今の shell で動くか
        if not seg:  # 代入だけ
            if not in_shell:
                return ok  # `env GIT_DIR=x` は今の shell の変数を変えない
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
        # 今の shell で動かない `env export` / `nohup unset` / zsh の `command export` は変数を変えない
        if cmd == "export" and in_shell:
            for tok in seg[1:]:
                name, eq, value = tok.partition("=")
                if name not in _GIT_ENV:
                    continue
                if eq:
                    self.shvars[name] = self.env[name] = value
                elif name in self.shvars:
                    self.env[name] = self.shvars[name]
            return ok
        if cmd == "unset" and in_shell:
            for name in seg[1:]:
                self.env.pop(name, None)
                self.shvars.pop(name, None)
            return ok
        # 今の shell で動かない移動は後ろに残らない (一般のコマンドと同じ。成否は外部コマンド次第)
        if cmd in _MOVERS and in_shell:
            args = seg[1:]
            # option と語の読みは状態に関係しないので、状態ごとに読み直さない
            form = None if cmd == "popd" else _chdir_form(cmd, args, self.braces > 0)
            out: set[_Alt] = set()
            for h, s, _ in alts:
                if self.deadline is not None and self.deadline.check():
                    break  # 状態ごとにも期限を見る (相対パスの解決は全要素を辿り直すので、深い場所や長い語で重い)
                out |= self._chdir(cmd, args, h, s, form)
            return out
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
                heres = {_resolve(h, chdir, self.msys, norms=self._norms) for h in heres}
            self._check_git(seg[1:], heres, env)
        return self._both(alts)

    def _chdir(
        self, cmd: str, args: list[str], here: str | None, stack: int, form: tuple[frozenset[str], list[str]] | None = None
    ) -> set[_Alt]:
        """今の shell で動く移動の組み込みを、1 つの状態から実行した後の状態。stack はスタックの節の番号 (`_Stacks`)。

        form は `_chdir_form(cmd, args, ...)` の値 (状態ごとに読み直さないよう、呼び出し側が 1 回だけ読んで渡す)。None なら読む。
        """
        if cmd == "popd":
            return self._popd(args, here, stack)
        push = self.stacks.push
        failed = {(here, stack, "fail")}  # option や語の数を受け付けない shell は失敗して動かない
        if cmd == "pushd" and args[:1] == ["-n"]:
            # bash は移動せずにスタックへ積み (次の popd がそこへ移動する)、zsh は bad option で失敗する
            if self.shell == "zsh":
                return failed
            words = _before_redirect(args[2:] if args[1:2] == ["--"] else args[1:])
            if words and not words[0].startswith(("+", "-")):
                return {(here, push(stack, _resolve(here, words[0], self.msys, self._absolute, self._norms)), "ok")}
            return {(here, push(stack, None), "ok")}  # 語なしと `+1` はスタックを回す。次の popd の行き先は分からない
        shells, words = _chdir_form(cmd, args, self.braces > 0) if form is None else form
        if self.shell not in shells:
            return failed
        if cmd == "pushd" and not words:
            # 語の無い pushd はスタックの先頭と入れ替える (`pushd >x` / `pushd -q` も同じ)
            return {(None, push(stack, here), "ok")}
        dest = _cd_target(words)
        new = _resolve(here, dest, self.msys, self._absolute, self._norms) if dest is not None else None
        return {(new, push(stack, here) if cmd == "pushd" else stack, "ok")}

    def _popd(self, args: list[str], here: str | None, stack: int) -> set[_Alt]:
        stacks = self.stacks
        words = _words(args, self.braces > 0)
        if words[:1] == ["-q"]:  # zsh だけ受け付ける (bash は失敗して動かない)
            if self.shell != "zsh":
                return {(here, stack, "fail")}
            words = words[1:]
        if words:
            return {(None, stacks.parent[stack], "ok")}  # `popd +N` は分からない
        if stack != _EMPTY:
            return {(stacks.top[stack], stacks.parent[stack], "ok")}
        # スタックが空に見える。呼び出し前のスタックは分からないが、新しい shell では失敗してその場に残る
        return {(None, _EMPTY, "ok"), (here, _EMPTY, "fail")}

    def _add(self, finding: Finding) -> None:
        if finding.message not in self._seen:
            self._seen.add(finding.message)
            self.findings.append(finding)

    def _check_git(self, args: list[str], heres: set, env: dict) -> None:
        fam = self.fam
        note: Finding | None = None
        for here in sorted(heres, key=lambda h: h or ""):
            if self.deadline is not None and self.deadline.check():
                break  # 語の多い 1 つの git (`git -C a -C a ...` を数万個) は、状態ごとの読みが重い
            targets, unresolved, rest, in_repo = _git_targets(args, here, fam, env, self.msys, self._norms)
            if not rest:
                return
            sub = rest[0]
            if sub == "worktree":
                # 相対パスの基点は作業ツリーの指定や git dir で変わる。どの基点でも別の worktree なら止める
                hits = []
                for base in targets or [None]:
                    for path in _worktree_targets(rest, base, self.msys, self._norms):
                        o = owner(path, fam)
                        if o is not None and path == o:
                            hits.append(o)
                # git はパスとして読む前に worktree の名前 (パスの末尾) として読む。名前を照合するのは、git が読む repo がこの repo のときだけ
                if in_repo:
                    hits += sorted(_worktree_by_name(rest, fam))
                others = [o for o in hits if o != fam.home]
                operand = _worktree_operand(rest)
                if not others:  # 止めるときは注意を足さない
                    if unresolved and _worktree_may_be_other(rest, fam, self.msys):
                        # git が読む repo か今の場所 (相対パスの基点の 1 つ) が決まらない (行き先の決まらない cd の後ろや、状態が上限を
                        # 超えた後) が、対象は別の worktree でありうる。ほかの書き込み (`cd "$X"; git reset --hard`) と同じく、止めずに注意を出す
                        note = note or Finding(False, f"`git worktree {rest[1]}` の対象 ({unresolved}) を静的に解決できない")
                    elif in_repo and operand is not None and _dynamic(operand):
                        # git が読む repo はこの repo だが、対象の語が変数 (`git worktree remove "$X"`)。どの worktree の名前にもパスにも
                        # なりうるので、ほかの書き込み (`git -C "$X" reset --hard`) と同じく、止めずに注意を出す
                        note = note or Finding(False, f"`git worktree {rest[1]}` の対象 ({operand}) を静的に解決できない")
                for o in others:
                    self._add(Finding(True, f"`git worktree {rest[1]}` が別の worktree ({o}) を対象にしている", o))
                continue
            if not _is_mutating(sub, rest[1:]):
                return
            if unresolved:
                note = note or Finding(False, f"`git {sub}` の対象 ({unresolved}) を静的に解決できない")
            # 対象ごとに記録する (許可リストで 1 つ外れても、残りの対象で止められるように)
            for target in targets:
                o = owner(target, fam)
                if o is not None and o != fam.home:
                    self._add(Finding(True, f"`git {sub}` が別の checkout ({o}) を対象にしている", o))
        if note is not None:
            self._add(note)
