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
抜けた後のセグメントも移動先にいるものとして扱う (外に倒れる = 安全側)。条件
(`false && cd x;` / `if ...; then cd x; fi`) やパイプ (`cd x | cat`) の中の `cd` も、
実行されたものとして追う。

**この過大見積もりはディレクトリの内外判定にとってだけ安全側**。移動先がどこでも
「外かもしれない」に倒れるだけなので誤 allow にはならないが、移動先を使って**どの
ファイルか**を決める用途 (kubectl の相対パスの kubeconfig。v0.28.0) では、実際には
走らない `cd` を追うと別のファイルで照合して誤 allow になる。その用途では
`chdir_is_sequential` で、`cd` がどれも確実に順に実行される形かを先に確かめる。
ディレクトリ単位の固定の判定 (v0.29.3) は、確実に順に実行される形でなければ
`segment_dirs(..., maybe_skipped=True)` で移動前のディレクトリも含めて確かめる。
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from core.command_parser import (
    BACKGROUND,
    LIST_TERMINATORS,
    _ansi_c_quote_end,
    _array_value_end,
    _skip_opaque,
    extract_candidate_readings,
    extract_candidates,
    external_chdir_candidates,
    split_with_separators,
)

# 作業ディレクトリを変える組み込みコマンド。`builtin cd` は分解の時点で `cd` になる。
_CHDIR_COMMANDS = frozenset({"cd", "pushd"})
# 行き先を静的に決められないもの (popd はスタック次第)。`chdir` は zsh の cd の同義語で、bash には
# 無い (v0.29.3)。どちらのシェルで走るか分からないので、行き先は決められない扱いにする。
_UNKNOWN_DIR_COMMANDS = frozenset({"popd", "chdir"})
# `cd` の option (`cd -P dir`)。値は取らない。`-e` / `-@` は bash 3.2 が invalid option、zsh が
# `cd old new` の置換形として失敗するので含めない (引数が 2 つになり、行き先は決められない扱い)。
_CD_FLAGS = frozenset({"-L", "-P"})
# 同じコマンドの前のほうで CDPATH を代入する形 (`CDPATH=x; cd sub` / `export CDPATH=x`)。空の代入は除く。
_CDPATH_ASSIGN = re.compile(r"\bCDPATH=(?![\s;&|)]|$)")
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


def _chdir_target(
    segment: str, current: str | None, inline_env: dict | None = None
) -> tuple[bool, str | None]:
    """`segment` が作業ディレクトリを変えるなら (True, 移動先 or None) を返す。

    移動先を静的に決められない形は (True, None)。変えないセグメントは (False, None)。
    `inline_env` はセグメントの前置きの代入 (`CDPATH=x cd sub` の `CDPATH`)。
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
    physical = False
    while args and args[0] in _CD_FLAGS:
        if args[0] in ("-P", "-L"):
            physical = args[0] == "-P"  # bash と同じく後のものが勝つ
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
    if _cdpath_applies(target, inline_env):
        return True, None
    if physical:
        # `cd -P lnk/..` は symlink を解いてから `..` を辿る (実体の親。bash と同じ)。
        return True, os.path.realpath(os.path.join(current, target))
    return True, os.path.normpath(os.path.join(current, target))


def _cdpath_applies(target: str, inline_env: dict | None) -> bool:
    """`CDPATH` が `cd <target>` の行き先を変えうるか (v0.29.3)。

    `CDPATH` (環境か、セグメントの前置きの代入) が空でなく、行き先が `/` で始まらず、最初の
    要素が `.` / `..` でない相対パスのとき True。bash は `CDPATH` の候補を先に探し、zsh は
    作業ディレクトリを先に探すので、シェルで行き先が割れる (呼び出し側は決められない扱い)。
    同じコマンドの中の `export CDPATH=...` は追わない。
    """
    if target.startswith("/") or target.split("/", 1)[0] in (".", ".."):
        return False
    return bool(os.environ.get("CDPATH") or (inline_env or {}).get("CDPATH"))


# 環境変数を変えるセグメント (v0.19.0)。ディレクトリ単位の固定は**起動時の env**で
# 判定するので、同じコマンドの中で値を変えた後のセグメントは固定済みとみなせない。
_ASSIGN_BUILTINS = frozenset({"export", "declare", "typeset", "readonly", "local"})
_UNSET_BUILTINS = frozenset({"unset"})
# 何を変えるか静的に読めないもの (ファイルや文字列を評価する)。
_OPAQUE_ENV_BUILTINS = frozenset({"source", ".", "eval", "set"})
ANY_ENV = "*"
# シェルの変数名。
_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 代入の組み込みの引数の名前の形 (`NAME` / `NAME[添字]` / 追記の `NAME+`)。
_ASSIGNED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?")
# 名前の位置にあると、シェルが展開して別の名前を作りうる文字 (置換・展開と glob・brace 展開)。
_UNREADABLE_NAME_CHARS = frozenset("$`*?[{")
# 語の頭の配列代入 (`arr=(1 2)`)。値の中の空白で語を切らない。
_ARRAY_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\s]*\])?\+?=\(")
# リダイレクトの語 (`>f` / `2>&1` / `&>f` / `<<<x`)。演算子だけの語は、次の語が行き先。
_REDIRECT_WORD = re.compile(r"[0-9]*(?:<<<|<<-|<<|<>|<&|>>|>&|>\||<|>)|&>>?")


def _env_names_changed(segment: str) -> set[str]:
    """セグメントが (後続のセグメントに対して) 変える環境変数の名前。読めなければ {ANY_ENV}。

    代入の組み込み (`export` / `declare` / `typeset` / `readonly` / `local`) の引数に名前を静的に読めない語
    (`export $(cat .env | xargs)` / `export $V` / `declare -x `...``) があれば、どの env を変えたか決められない
    ので {ANY_ENV} (v0.36.7。`_has_unreadable_name`)。0.36.6 までは shlex で分けた語の `=` の前を名前として
    数え、`$(cat` などの断片を名前と読んでいた (実際の CLI は展開した名前の値で走る。bash 3.2 / zsh -f で実測)。
    名前を読める語 (`export FOO=$(cat f)` の FOO) は 0.36.6 と同じ読み。
    `read` は引数の名前、`printf` は `-v` の名前に値を入れる (v0.36.7。`_read_names` / `_printf_names`)。
    """
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
        if _has_unreadable_name(segment):
            return {ANY_ENV}
        return {w.split("=", 1)[0] for w in words[1:] if not w.startswith("-")}
    if head == "read":
        return _read_names(words[1:])
    if head == "printf":
        return _printf_names(words[1:])
    # 代入だけのセグメント (`X=1; cmd` の `X=1`)。export されていなくても、すでに
    # export 済みの変数なら後続の子プロセスに効く。
    if all("=" in w and w.split("=", 1)[0].isidentifier() for w in words):
        return {w.split("=", 1)[0] for w in words}
    return set()


def _has_unreadable_name(segment: str) -> bool:
    """代入の組み込みのセグメントの引数に、名前を静的に読めない語があるか (v0.36.7)。

    語の最初の `=` より前 (`=` が無ければ語の全体) が名前の形 (`NAME` / `NAME[添字]` / `NAME+`) でなく、`$`・
    backtick・glob・brace 展開の文字 (`*` `?` `[` `{`) を含む語 (`$(cat .env | xargs)` / `$V` / `${N}=x` /
    `"$K=$V"` / `` `cat f` `` / `{AWS_PROFILE,X}=prod`) は、シェルが展開した結果の名前に値が入る (bash 3.2 /
    zsh -f で実測)。値を作る展開は、どれも名前より後ろか、名前の前の `$` / backtick から始まるので、最初の
    `=` で切ってよい (`"FOO=$X"` / `FOO=$(cat f)` の名前は FOO)。リダイレクトの語 (`>f` / `2>/dev/null`。
    演算子だけの語は次の語も) は名前に数えない。引用符の中の記号も数える (多めに数える側。名前の形にならない
    語は、シェルが名前でないとして失敗する)。
    """
    skip = False
    for word in _shell_words(segment)[1:]:
        if skip:
            skip = False
            continue
        redirect = _REDIRECT_WORD.match(word)
        if redirect:
            skip = redirect.end() == len(word)
            continue
        head = word.split("=", 1)[0]
        if not _ASSIGNED_NAME.fullmatch(head) and any(c in _UNREADABLE_NAME_CHARS for c in head):
            return True
    return False


def _shell_words(segment: str) -> list[str]:
    """`segment` をシェルの語 (引用符を外す前) に分ける (v0.36.7)。

    引用符・置換 (`_skip_opaque`)・語の頭の配列代入の値 (`arr=(1 2)`) の中の空白では切らない。閉じない配列代入
    (`a=(1 2`) を一度見たら、後ろの語の配列代入の値は読まない (閉じを探す走査は末尾まで進むので、
    `a=( a=( a=( ...` の語ごとに走査し直すと語の数の 2 乗になる)。
    """
    words: list[str] = []
    start = None
    arrays = True
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c in " \t\n":
            if start is not None:
                words.append(segment[start:i])
                start = None
            i += 1
            continue
        if start is None:
            start = i
            array = _ARRAY_WORD.match(segment, i) if arrays else None
            if array:
                end = _array_value_end(segment, array.end() - 1)
                if end is not None:
                    i = end
                    continue
                arrays = False
        end = _skip_opaque(segment, i)
        i = end if end is not None and end > i else i + 1
    if start is not None:
        words.append(segment[start:])
    return words


def _read_names(args: list[str]) -> set[str]:
    """`read` が値を入れる変数の名前 (v0.36.7。`args` は shlex で分けた引数)。

    名前の形の語を全部数える (option の値 (`-p プロンプト`) やリダイレクトの行き先も、名前の形なら数える。多めに
    数える側。bash の `-a 配列名` と、値を取らない zsh の `-p` の後ろの名前を落とさない)。zsh の
    `名前?プロンプト` は `?` の前を名前とする。名前を静的に読めない語 (`read "$V"`) は数えない (option の値と
    区別しない。名前を変数で渡す形は稀)。
    """
    names = set()
    for word in args:
        name = word.split("?", 1)[0]
        if _SHELL_NAME.fullmatch(name):
            names.add(name)
    return names


def _printf_names(args: list[str]) -> set[str]:
    """`printf -v 名前` が値を入れる変数の名前 (v0.36.7。`args` は shlex で分けた引数)。

    `-v名前` の結合形も読む。`--` か option でない語 (書式) から後ろは見ない。`名前[添字]` は名前の部分。
    名前を静的に読めない語 (`printf -v "$V" x`) は、どの env を変えたか決められないので {ANY_ENV}。
    """
    for i, word in enumerate(args):
        if word == "--" or not word.startswith("-"):
            return set()
        if word.startswith("-v"):
            target = word[2:] or (args[i + 1] if i + 1 < len(args) else "")
            name = _SHELL_NAME.match(target)
            if name:
                return {name.group()}
            return {ANY_ENV} if "$" in target or "`" in target else set()
    return set()


def segment_env_changes(command: str) -> dict[str, set[str]]:
    """各セグメント → それより前のセグメントが変えた環境変数の名前 ({ANY_ENV} は不明)。

    bash と zsh で分解が割れる形は、読みごとに辿った結果の和を返す
    (`command_parser.extract_candidate_readings`)。
    """
    result: dict[str, set[str]] = {}
    for reading in extract_candidate_readings(command):
        changed: set[str] = set()
        for segment, _inline_env, _sep in reading:
            result.setdefault(segment, set()).update(changed)
            changed |= _env_names_changed(segment)
    return result


# `segment_dirs(..., maybe_skipped=True)` が追う「いまいる可能性のあるディレクトリ」の数の上限
# (v0.29.3)。`cd` ごとに「走った / 走らなかった」の両方を残すので、相対パスの `cd` が続くと
# 倍々に増える。超えたら「決められない」(None) に畳む (呼び出し側は固定されていない側に倒す)。
MAX_POSSIBLE_DIRS = 32


def segment_dirs(
    command: str,
    cwd: str,
    *,
    maybe_skipped: bool = False,
    sure: frozenset[str] = frozenset(),
) -> dict[str, set[str | None]]:
    """各セグメント (分解した形の文字列) → それが走るディレクトリの集合。

    同じ文字列のセグメントが別のディレクトリで複数回現れることがあるため集合で返す。
    None は「静的に決められない」。

    単独の `&` で終わるリスト (`cd sub & kubectl ...` の `cd sub`、
    `cd sub && kubectl get x & aws ...` の `cd sub && kubectl get x`) はサブシェルで走るので、
    その中の `cd` はリストの中の後続にだけ効き、`&` の後ろには効かない (v0.29.0)。
    bash と zsh で分解が割れる形は、読みごとに辿った結果の和を返す
    (`command_parser.extract_candidate_readings`)。

    `maybe_skipped` (v0.29.3) は、どの `cd` も「走らなかった (または後続に効かなかった)」
    可能性を残す: `cd` の後は、移動前と移動後のディレクトリの和にいるものとする。括弧・条件・
    パイプの中の `cd` (`chdir_is_sequential` が False の形) で、移動前のディレクトリを落とさない
    ための見積もり (`(cd sub && true); firebase deploy` の firebase は起動側で走る)。
    可能性の数が `MAX_POSSIBLE_DIRS` を超えたら None に畳む。`sure` (`sure_chdirs` の結果) に
    入っている `cd` のセグメントは、`maybe_skipped` でも移動前を残さない (確実に走って後続に効く)。
    ただし `&&` の右にある `cd` (`false && cd sub && x; y`) は、その `&&` の鎖が終わったところで
    鎖の始まりのディレクトリも残す (`false` が失敗すると `y` は移動前で走る)。
    """
    result: dict[str, set[str | None]] = {}
    cdpath_assigned = _CDPATH_ASSIGN.search(command) is not None
    for reading in extract_candidate_readings(command):
        current: frozenset[str | None] = frozenset({cwd or None})
        list_start = current
        chain_start = current
        conditional = False  # 今の `&&` の鎖の中で、鎖の先頭でない確実な cd が効いた
        prev_sep = ""
        for segment, inline_env, sep in reading:
            if prev_sep != "&&":
                chain_start = current
            env = {**(inline_env or {}), "CDPATH": "?"} if cdpath_assigned else inline_env
            moved = [_chdir_target(segment, here, env) for here in current]
            if moved and moved[0][0]:
                targets = frozenset(target for _changes, target in moved)
                keep = maybe_skipped and segment not in sure
                current = (current | targets) if keep else targets
                if maybe_skipped and not keep and prev_sep == "&&":
                    conditional = True
            else:
                result.setdefault(segment, set()).update(current)
            if sep != "&&" and conditional:
                current = current | chain_start
                conditional = False
            if len(current) > MAX_POSSIBLE_DIRS:
                current = frozenset({None})
            if sep == BACKGROUND:
                current = list_start
            if sep in LIST_TERMINATORS:
                list_start = current
            prev_sep = sep
    return result


# 引用符の外にあると、`segment_dirs` が追う `cd` が実際には走らない (または後続に効かない)
# 可能性がある記号 (v0.28.0)。括弧 = subshell / コマンド置換 / 関数、波括弧 = グループ・
# 関数本体、`|` = パイプ (各段は subshell) と `||`、backtick = コマンド置換。
_NON_SEQUENTIAL_CHARS = frozenset("(){}|`")
# 条件・繰り返しの中の `cd` は走るかどうか (何回走るか) が条件次第。
_CONTROL_WORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac"}
)
_WORD_BREAKS = frozenset(" \t\n;&|()<>")


def _unquoted(command: str) -> str:
    r"""引用符の中とエスケープした文字を `_` に置き換えた文字列 (語の区切りは保つ)。

    `$'...'` は ANSI-C 引用符として読み、中の `\'` / `\\` では閉じない (`command_parser` の
    `_skip_opaque` と同じ規則。`$$` は先に 2 文字進めるので `$$'` は普通の引用符)。
    `'...'` と同じに読むと `echo $'\'' ; (cd sub; true) ; ...` の `(` を引用符の中と読み、
    `cd sub` が後続に効くものとして扱う。
    """
    out: list[str] = []
    quote = None
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote is None and command.startswith("$$", i):
            out.append("$$")
            i += 2
            continue
        if quote is None and command.startswith("$'", i):
            end = _ansi_c_quote_end(command, i)
            end = n if end is None else end
            out.append("_" * (end - i))
            i = end
            continue
        if quote == "'":
            if c == "'":
                quote = None
            out.append("_")
        elif quote == '"':
            if c == "\\" and i + 1 < n:
                out.append("__")
                i += 2
                continue
            if c == '"':
                quote = None
            out.append("_")
        elif c == "\\" and i + 1 < n:
            out.append("__")
            i += 2
            continue
        elif c in ("'", '"'):
            quote = c
            out.append("_")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _has_lone_ampersand(text: str) -> bool:
    """`&&` でもリダイレクト (`2>&1` / `&>f` / `>&f`) でもない `&` (背景実行) があるか。"""
    for i, c in enumerate(text):
        if c != "&":
            continue
        prev = text[i - 1] if i > 0 else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if prev == "&" or nxt == "&":
            continue
        if prev in "<>" or nxt == ">":
            continue
        return True
    return False


def chdir_is_sequential(command: str) -> bool:
    """コマンド中の `cd` / `pushd` / `popd` がどれも、確実に順に実行される形なら True。

    `segment_dirs` は括弧・条件・パイプを落としたまま `cd` を追うので、
    `(cd sub; true) && kubectl ...` や `false && cd sub; kubectl ...` の kubectl を
    `sub` で走るものとして扱う。内外の判定ではそれで安全側だが、移動先で**どのファイルか**を
    決める用途では誤 allow になる。その用途の呼び出し側は、これが False なら走る
    ディレクトリを「決められない」として扱う。

    False にする形: 外部プログラムとして走る `cd` (`nohup cd sub` / `env cd sub` 等。
    `command_parser.external_chdir_candidates`) がある。作業ディレクトリを変えるセグメントがあり、かつ引用符の外に `(` `)` `{`
    `}` `|` (`||` を含む) backtick・背景実行の `&`・制御構文の語 (`if` / `for` / `case` 等)
    のどれかがある。または `&&` の右の cd の後に `;` / 改行で次のリストが続く
    (`false && cd sub; kubectl ...`)。`cd` が無ければ常に True (`segment_dirs` は移動を
    追わない)。`&&` と `;` と改行だけで繋いだ形 (`cd a; cd b && kubectl ...` /
    `mkdir -p sub && cd sub && kubectl ...`) は True。
    """
    if not _changes_dir(command):
        return True
    # 外部プログラムとして走る cd (`nohup cd sub` / `env cd sub`。v0.29.3) は分解すると `cd sub` に
    # 見えるが、シェルは移動しない。
    if external_chdir_candidates(command):
        return False
    text = _unquoted(command)
    if any(c in _NON_SEQUENTIAL_CHARS for c in text):
        return False
    if _has_lone_ampersand(text):
        return False
    word: list[str] = []
    for c in text + " ":
        if c in _WORD_BREAKS:
            if "".join(word) in _CONTROL_WORDS:
                return False
            word = []
        else:
            word.append(c)
    return not _conditional_chdir_before_next_list(command, text)


def _changes_dir(command: str) -> bool:
    return any(
        _chdir_target(segment, "/")[0] for segment, _env in extract_candidates(command)
    )


# `cd` の直後にあると、その `cd` が走らなかった (失敗した) まま後続が走りうる区切り。
_UNSURE_SEPARATORS = frozenset({"|", "||", "|&", "&"})
# `cd sub || exit` の `exit`: cd が失敗したらシェルが終わるので、後続は移動後で走る。`return` は
# 関数の外では失敗するだけで後続が走る (bash / zsh で実測) ので入れない。
_ABORT_WORDS = frozenset({"exit"})


def sure_chdirs(command: str) -> frozenset[str]:
    """確実に走り、後続のセグメントに効く `cd` / `pushd` のセグメント (分解した形) の集合 (v0.29.3)。

    `chdir_is_sequential` はコマンド全体で 1 つの答えを出すので、`cd sub && firebase deploy | tail`
    のように `cd` より**後ろ**にだけパイプ・`||`・背景実行・波括弧・制御構文がある形でも False に
    なる。ディレクトリ単位の固定の判定 (`segment_dirs(..., maybe_skipped=True, sure=...)`) では、
    ここで返す `cd` の後は移動後のディレクトリだけで確かめる。

    `cd` を確実とみなす条件 (どれかを満たさなければ確実でない):

    - それより前 (自分のセグメントを含む) の引用符の外に `(` `)` `{` `}` backtick・制御構文の語が
      無い (後で閉じる範囲の中かもしれない)。分割したセグメントの中の `|` / 単独の `&` も同じ
    - 直前の区切りが `|` / `||` / `|&` でない (`echo x | cd sub` はパイプの中。zsh では最後の段が
      シェル自身で走るので移動し、bash では移動しない)。それより前の、完結したパイプライン
      (`git status | head; cd sub && ...`) は見ない
    - 直後の区切りが `&&` / `;` / 改行 / 末尾、または `|| exit`
    - 外部プログラムとして走る cd ではない (`external_chdir_candidates`)。セグメントが 1 候補だけ
      (シェル経由の wrapper の中身を分割し直したものでない)

    `&&` の右の `cd` (`false && cd sub && firebase deploy; echo done`) は、同じ `&&` の鎖の中の後続に
    だけ確実に効く。鎖が終わったら移動前も残すのは `segment_dirs` 側が行う。
    括弧は分解で落ちるので `(cd sub && firebase deploy)` は確実とみなさない。読みが 2 つ以上ある
    コマンド (`command_parser.extract_candidate_readings`) は、どの `cd` も確実とみなさない。
    0.29.9 までの代入の値の読み (`legacy_values`) は数えない (シェルの読み方が割れるのではなく、
    0.29.9 の候補を落とさないための読み。数えると `cd sub && X=$(/bin/date +%s) firebase deploy` の
    `cd sub` を確実とみなさず、firebase を起動側でも確かめてしまう。v0.29.10)。
    """
    if len(extract_candidate_readings(command, legacy_values=False)) != 1:
        return frozenset()
    external = external_chdir_candidates(command)
    raw = split_with_separators(command)
    n = len(raw)
    sure: set[str] = set()
    unsure: set[str] = set()
    tainted = False  # ここまでに括弧・波括弧・制御構文の語などがあった (後ろ全部に効く)
    after_unsure = False  # 直前の区切りがパイプか `||` (直後の 1 セグメントにだけ効く)
    for i, (seg, sep) in enumerate(raw):
        seg_tainted = _taints(seg)
        # `cd sub || exit` (exit がパイプの中なら subshell が終わるだけなので除く)
        aborts = (
            sep == "||"
            and i + 1 < n
            and _first_word(raw[i + 1][0]) in _ABORT_WORDS
            and raw[i + 1][1] not in _UNSURE_SEPARATORS
        )
        cands = extract_candidates(seg)
        cds = [c for c, _env in cands if _chdir_target(c, "/")[0]]
        if cds:
            ok = (
                not tainted
                and not seg_tainted
                and not after_unsure
                and len(cands) == 1
                and (sep not in _UNSURE_SEPARATORS or aborts)
                and cds[0] not in external
            )
            (sure if ok else unsure).update(cds)
        if seg_tainted:
            tainted = True
        after_unsure = sep in _UNSURE_SEPARATORS and sep != BACKGROUND
    return frozenset(sure - unsure)


def _taints(segment: str) -> bool:
    """分割したセグメントの引用符の外に、括弧・波括弧・パイプ・backtick・背景実行・制御構文の語があるか。"""
    text = _unquoted(segment)
    if any(c in _NON_SEQUENTIAL_CHARS for c in text) or _has_lone_ampersand(text):
        return True
    word: list[str] = []
    for c in text + " ":
        if c in _WORD_BREAKS:
            if "".join(word) in _CONTROL_WORDS:
                return True
            word = []
        else:
            word.append(c)
    return False


def _first_word(segment: str) -> str:
    parts = segment.split(None, 1)
    return parts[0] if parts else ""


def _conditional_chdir_before_next_list(command: str, text: str) -> bool:
    """`X && cd sub; kubectl ...` の形 (`&&` の右の cd の後に `;` / 改行で次のリストが続く)。

    `X` が失敗すると cd は走らず、`;` の後は元のディレクトリで走る。同じ `&&` の鎖の中の
    後続 (`mkdir -p sub && cd sub && kubectl ...`) は cd が走ったときだけ走るので確実。
    `text` は `_unquoted(command)` (同じ長さ。引用符の中の `;` / `&&` で切らない)。
    """
    lists: list[list[str]] = []
    start = 0
    chain: list[str] = []
    i = 0
    n = len(text)
    while i <= n:
        if i == n or text[i] in ";\n":
            chain.append(command[start:i])
            lists.append(chain)
            chain = []
            start = i + 1
        elif text.startswith("&&", i):
            chain.append(command[start:i])
            start = i + 2
            i += 2
            continue
        i += 1
    while lists and not any(part.strip() for part in lists[-1]):
        lists.pop()  # 末尾の `;` / 改行
    for chain in lists[:-1]:
        if any(_changes_dir(part) for part in chain[1:]):
            return True
    return False
