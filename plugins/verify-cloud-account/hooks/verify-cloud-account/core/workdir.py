"""コマンドが実際に走るディレクトリと、起動リポジトリの内外の判定 (v0.19.0)。

hook が検証に使う env は **Claude を起動したときの env** (+ settings の `env`) で、
期待値も起動ディレクトリ (`CLAUDE_PROJECT_DIR`) から探す。一方、利用者が
ディレクトリ単位の env ツールで CLI のアカウントを切り替えている構成では、
別のリポジトリで走るコマンド (`cd ../other && gcloud ...`) や、Bash の作業
ディレクトリ自体が起動リポジトリの外にある状態では、hook が見ている env と
「そのディレクトリで意図されたアカウント」が一致する保証が無い。

このモジュールは、各セグメントが走るディレクトリを
**hook input の `cwd` + コマンド中の `cd` / `pushd`** から静的に求め、起動
リポジトリ (`CLAUDE_PROJECT_DIR` を含む git の作業ツリー) の外かを判定する。
行き先を静的に決められない形 (`cd "$D"` / `cd -` / `popd` / glob など) は
**外として扱う** (分からないものを内側とみなすと誤 allow になる)。

subshell の括弧 (`(cd x && gcloud ...)`) は分解の時点で落ちるため、括弧を
抜けた後のセグメントも移動先にいるものとして扱う (外に倒れる = 安全側)。
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

from core.command_parser import extract_candidates

# 作業ディレクトリを変える組み込みコマンド。`builtin cd` は分解の時点で `cd` になる。
_CHDIR_COMMANDS = frozenset({"cd", "pushd"})
# 行き先を静的に決められないもの (popd はスタック次第)。
_UNKNOWN_DIR_COMMANDS = frozenset({"popd"})
# `cd` の option (`cd -P dir`)。値は取らない。
_CD_FLAGS = frozenset({"-L", "-P", "-e", "-@"})
# 値の中にあると静的に解決できない文字 (展開・glob)。
_DYNAMIC_CHARS = frozenset("$`*?[{")


def launch_root(project_dir: str) -> Path | None:
    """起動ディレクトリを含む git の作業ツリーのルート。git 管理外なら起動ディレクトリ。

    `.git` (ディレクトリまたは linked worktree の `.git` ファイル) を親方向に探す。
    **ホームディレクトリ自身とその上は見ない** — ホームを git で管理している構成
    (dotfiles) でルートがホームになると、ホーム配下の全リポジトリが「中」になり、
    外での実行を止める判定が働かなくなる。解決できない (存在しない等) なら None。
    """
    try:
        start = Path(os.path.realpath(project_dir))
    except (OSError, ValueError):
        return None
    home = _real_home()
    for directory in (start, *start.parents):
        if home is not None and directory == home:
            break
        try:
            if (directory / ".git").exists():
                return directory
        except OSError:
            return None
    return start


def _real_home() -> Path | None:
    home = os.environ.get("HOME")
    if not home:
        return None
    try:
        return Path(os.path.realpath(home))
    except (OSError, ValueError):
        return None


def is_inside(directory: str | None, root: Path | None) -> bool:
    """`directory` が `root` の中 (root 自身を含む) なら True。どちらか不明なら False。"""
    if directory is None or root is None:
        return False
    try:
        real = Path(os.path.realpath(directory))
    except (OSError, ValueError):
        return False
    return real == root or root in real.parents


def _chdir_target(segment: str, current: str | None) -> tuple[bool, str | None]:
    """`segment` が作業ディレクトリを変えるなら (True, 移動先 or None) を返す。

    移動先を静的に決められない形は (True, None)。変えないセグメントは (False, None)。
    """
    try:
        words = shlex.split(segment)
    except ValueError:
        # 引用符が閉じていない等。cd かどうかも決められない形だけを不明扱いにする。
        stripped = segment.lstrip()
        if stripped.split(maxsplit=1)[:1] and stripped.split(maxsplit=1)[0] in (
            _CHDIR_COMMANDS | _UNKNOWN_DIR_COMMANDS
        ):
            return True, None
        return False, None
    if not words:
        return False, None
    head, args = words[0], words[1:]
    if head in _UNKNOWN_DIR_COMMANDS:
        return True, None
    if head not in _CHDIR_COMMANDS:
        return False, None
    while args and args[0] in _CD_FLAGS:
        args = args[1:]
    if args and args[0] == "--":
        args = args[1:]
    if current is None:
        return True, None
    if not args:
        if head == "pushd":
            # 引数なしの pushd はスタックの先頭と入れ替える = 静的には不明。
            return True, None
        home = os.environ.get("HOME")
        return True, home or None
    if len(args) > 1:
        return True, None
    target = args[0]
    # shlex.split は `$D` を展開しないので、元のセグメントに残る展開・glob で判定する。
    if target == "-" or target.startswith("+") or any(c in _DYNAMIC_CHARS for c in target):
        return True, None
    if any(c in segment for c in "$`"):
        return True, None
    if target.startswith("~"):
        if target != "~" and not target.startswith("~/"):
            return True, None  # `~user` は静的に解決しない
        home = os.environ.get("HOME")
        if not home:
            return True, None
        target = home + target[1:]
    return True, os.path.normpath(os.path.join(current, target))


# 環境変数を変えるセグメント (v0.19.0)。ディレクトリ単位の固定は**起動時の env**で
# 判定するので、同じコマンドの中で値を変えた後のセグメントは固定済みとみなせない。
_ASSIGN_BUILTINS = frozenset({"export", "declare", "typeset", "readonly", "local"})
_UNSET_BUILTINS = frozenset({"unset"})
# 何を変えるか静的に読めないもの (ファイルや文字列を評価する)。
_OPAQUE_ENV_BUILTINS = frozenset({"source", ".", "eval", "set"})
ANY_ENV = "*"


def _env_names_changed(segment: str) -> set[str]:
    """セグメントが (後続のセグメントに対して) 変える環境変数の名前。読めなければ {ANY_ENV}。"""
    try:
        words = shlex.split(segment)
    except ValueError:
        return {ANY_ENV}
    if not words:
        return set()
    head = words[0]
    if head in _OPAQUE_ENV_BUILTINS:
        return {ANY_ENV}
    if head in _UNSET_BUILTINS:
        return {w for w in words[1:] if not w.startswith("-")} or set()
    if head in _ASSIGN_BUILTINS:
        return {w.split("=", 1)[0] for w in words[1:] if not w.startswith("-")}
    # 代入だけのセグメント (`X=1; cmd` の `X=1`)。export されていなくても、すでに
    # export 済みの変数なら後続の子プロセスに効く。
    if all("=" in w and w.split("=", 1)[0].isidentifier() for w in words):
        return {w.split("=", 1)[0] for w in words}
    return set()


def segment_env_changes(command: str) -> dict[str, set[str]]:
    """各セグメント → それより前のセグメントが変えた環境変数の名前 ({ANY_ENV} は不明)。"""
    changed: set[str] = set()
    result: dict[str, set[str]] = {}
    for segment, _inline_env in extract_candidates(command):
        result.setdefault(segment, set()).update(changed)
        changed |= _env_names_changed(segment)
    return result


def segment_dirs(command: str, cwd: str) -> dict[str, set[str | None]]:
    """各セグメント (分解した形の文字列) → それが走るディレクトリの集合。

    同じ文字列のセグメントが別のディレクトリで複数回現れることがあるため集合で返す。
    None は「静的に決められない」。
    """
    current: str | None = cwd or None
    result: dict[str, set[str | None]] = {}
    for segment, _inline_env in extract_candidates(command):
        changes, target = _chdir_target(segment, current)
        if changes:
            current = target
            continue
        result.setdefault(segment, set()).add(current)
    return result
