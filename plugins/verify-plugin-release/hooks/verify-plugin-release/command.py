"""Bash コマンド文字列から `gh pr create` / `gh pr ready` の呼び出しを見つける。

ゲートを走らせるかどうかの判定だけが目的で、shell を完全に解釈するわけではない。
`cd <dir> && gh pr create` の `cd` は追跡する (ゲートを走らせる repo が変わるため)。
cd が何度もあるときは順に合成する (`cd plugins/x && ... && cd ../..` は repo root に戻る)。
heredoc の本文はシェルのコードではないので、構文解析の前に空行にする。
代入語 (`NAME=...`) の値は、コマンド置換・引用符の対応で 1 語として読む (値の中の空白で語を分けない)。

shlex で分解できないコマンド (クォートの閉じ忘れ等) でも、文字列に
`gh pr create` / `gh pr ready` が現れるなら検出扱いにする。ゲートは異常時に
止める側に倒す方針なので、「解析できないから素通し」にはしない。
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# shlex の punctuation_chars に改行を含め、改行も区切りとして扱う
# (複数行のコマンドを 1 セグメントに潰さないため)。
_PUNCT = "();<>|&\n"
_WRAPPERS = {"env", "command", "exec", "time", "nohup"}
# 制御構文の予約語。`if x; then gh pr create; fi` の `then gh ...` を読めるようにする
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
# `gh pr new` は `gh pr create` の別名
_CREATE = {"create", "new"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 実行位置に置かれた gh pr create / new / ready (コマンド置換 `$(` や backtick の中を含む)。
# 文中の言及 ("... run gh pr create later") は拾わないよう、直前が区切りか行頭のものに限る
# コマンド語の前のリダイレクト (`>/dev/null gh` / `2>&1 gh` / `<<'END MSG' gh`) も数える。構文解析は
# これを gh として見つけるので、数えないと「見つけた数 = 数えた数」が別の gh の見落としを隠す
# 1 つのリダイレクトは atomic group で読み方を 1 通りに固定する。`>>f` を `>` + `>f` とも読める形の
# 繰り返しは、一致しないときの後戻りが個数に対して指数時間になり、hook が時間切れで素通しになる。
# atomic group の構文 `(?>...)` は Python 3.11 からなので、先読みで捕まえてから後方参照で消費する形
# (`(?=(?P<g>...))(?P=g)`) で書く。先読みは一度成功すると後戻りしないので、同じく読み方が 1 通りに決まる
# (3.9 / 3.10 で import が re.error で落ちると、hook がエラー扱いになりゲートが一切走らない)
# 演算子は shell の演算子そのもの (`-` は `<<-` の一部か、`<&-` / `>&-` の対象。`2>-` の `-` はファイル名)
# 対象の語は shell と同じく引用の外の `;` `&` `|` `<` `>` で終わる (`&>f&>f` は `&>f` が 2 つ)。区切りをまたいで
# 読むと、`&>f&>f...` の `&` ごとに残り全部を読み直して個数の 2 乗の時間になる。`$(...)` と backtick の中の
# 区切りは対象の語の一部として読む。対象の直後は空白か、次のリダイレクトの演算子。`\X` と `$'...'` は shell と同じ
# 組で読む (`V=\ y gh` の `\ ` は語の途中で、`V=\\ gh` の `\\` の後ろの空白は語の区切り)
_WORD_PART = r"""(?:\$'(?:\\.|[^'\\])*'|\\.|'[^']*'|"[^"]*"|\$\([^()]*\)|`[^`]*`|[^\s'";&|<>])"""


def _atomic(pattern: str, name: str) -> str:
    """`(?>pattern)` と同じく、一度読んだら後戻りしない group (Python 3.9 / 3.10 でも解釈できる形)。"""
    return r"(?=(?P<%s>%s))(?P=%s)" % (name, pattern, name)


_REDIRECT_PREFIX = (
    _atomic(r"\d*(?:&>>?|<<-|<<<?|<>|[<>]&|>>|>\||[<>])\s*" + _WORD_PART + r"+", "redir")
    + r"(?:\s+|(?=\d*(?:&>|[<>])))"
)
_EXEC_START = r"(?:^|[;&|(\n`{]|\$\()\s*"
_PREFIX_ITEM = (
    r"(?:(?:if|then|else|elif|do|while|until|!|env|command|exec|time|nohup)\s+"
    r"|" + _atomic(r"[A-Za-z_][A-Za-z0-9_]*=" + _WORD_PART + r"*", "assign") + r"\s+|" + _REDIRECT_PREFIX + r")"
)
# 前置きの繰り返しには上限を置く。`&>f&>f ...` のように前置きの中に実行位置の始まり (`&` など) が並ぶと、
# 始まりごとに残り全部を読み直して個数の 2 乗の時間になるため。上限を超える前置きの後ろの PR 操作は
# ここでは数えないので、_LONG_PREFIX で別に見つけて止める
_PREFIX_LIMIT = 32
_PR_OP = (
    r"gh(?:\.exe)?\s+(?:(?:-R|--repo)(?:\s+|=)\S+\s+)*pr\s+(?:(?:-R|--repo)(?:\s+|=)\S+\s+)*(?P<op>create|new|ready)\b"
)
_EXEC_POS = re.compile(
    _EXEC_START + _PREFIX_ITEM + "{0,%d}" % _PREFIX_LIMIT + r"(?:[^\s;&|<>]*/)?" + _PR_OP + r"(?P<args>[^;&|\n)`]*)"
)
_LONG_PREFIX = re.compile(_EXEC_START + _PREFIX_ITEM + "{%d}" % (_PREFIX_LIMIT + 1))
# `\X` を組で読まない読み方。二重引用符の中の `V=\"p q\" gh` は、中身を読み直す shell では代入語で、組で読むと
# 語が割れて数えられない。組で読んだ数と大きい方を採る (組で読む方が見つける形もある)
_WORD_PART_NO_PAIR = r"""(?:'[^']*'|"[^"]*"|\$\([^()]*\)|`[^`]*`|[^\s'";&|<>])"""
_EXEC_POS_NO_PAIR = re.compile(_EXEC_POS.pattern.replace(_WORD_PART, _WORD_PART_NO_PAIR))
_LONG_PREFIX_NO_PAIR = re.compile(_LONG_PREFIX.pattern.replace(_WORD_PART, _WORD_PART_NO_PAIR))
_PR_OP_ANYWHERE = re.compile(_PR_OP)
# shell の名前 (sh / bash / zsh / ksh / dash / ash / ssh / csh / tcsh / mksh / yash / fish)
_SHELLS = r"(?:ba|z|k|da|a|c|tc|mk|ya|fi)?sh"
# `bash -c '...'` / `sh -lc "..."` / `eval "..."` の中身。中の PR 操作は構文解析の対象外になる
_NESTED_SHELL = re.compile(
    r"(?:\b" + _SHELLS + r"(?:\.exe)?\s+(?:-\w+\s+)*-\w*c\w*|\beval)\s+(['\"])(.*?)\1",
    re.S,
)
_DIR_CHANGERS = re.compile(r"(?:^|[;&|(\n`{]|\$\()\s*(?:pushd|popd)\b")
_FALLBACK = re.compile(r"(?:^|[\s;&|(])gh\s+pr\s+(create|new|ready)\b")


@dataclass(frozen=True)
class Invocation:
    kind: str  # "create" | "ready"
    base: str | None = None  # --base で明示された base branch
    draft: bool = False  # draft PR として作成する
    target: str | None = None  # `gh pr ready <target>` の対象 (番号 / branch / URL)
    head: str | None = None  # `gh pr create --head` で明示された head branch
    repo: str | None = None  # `-R / --repo` で明示された repo (OWNER/REPO か HOST/OWNER/REPO)
    cd: str | None = None  # それまでの `cd <dir>` を合成した移動先 (表示用。解決には cd_steps を使う)
    parsed: bool = True  # False = shlex で分解できず文字列一致で検出した
    # 起点 (hook の cwd、または最後の絶対パスの cd) から順に辿る cd の引数
    cd_steps: tuple[str, ...] = ()
    # 静的に決まらない移動先 (`$` / `-` / サブシェル等の中の cd に続く相対の cd)。None = 決まる
    cd_unresolved: str | None = None


# shell が単語の先頭と読む位置の直前に来る文字 (これらの直後の `#` からがコメント)
_WORD_START_PREV = frozenset(" \t\r\n;&|()<>")


def _join_continuations(s: str) -> str:
    """`\\`+改行の継続行をつなぐ (安全網用。引用符は見ないので、つなぎすぎても締める側に倒れる)。"""
    return re.sub(r"(?<!\\)((?:\\\\)*)\\\n", r"\1", s)


def _strip_comments(
    command: str,
    heredocs: bool = True,
    quoted_bodies: list[tuple[int, int]] | None = None,
    dq_bodies: bool = True,
) -> str:
    """shell と同じ規則でコメントを落とし、`\\`+改行の継続行をつなぐ。

    shlex の既定の commenters (`#`) は単語の途中の `#` (`feature#1`、`${x##*/}`) でも
    コメントと読み、しかも読み捨てるときに改行まで消費するので、次の行の
    `gh pr create` が前の行につながって見失われる。そこで shlex には commenters を
    使わせず、ここで単語の先頭 (直前が空白か区切り) の `#` から改行の手前までだけを落とす。
    引用符の中の `#` と、単語の途中の `#` は落とさない。改行そのものは残す
    (複数行のコマンドを 1 セグメントに潰さないため)。

    heredoc (`<<EOF` / `<<'EOF'` / `<<"EOF"` / `<<\\EOF` / `<<-EOF` など) の本文と終わりの
    区切り語の行は、改行だけを残して空行にする。本文はシェルのコードではないので、本文の
    奇数個の `'` を引用符の始まりと読んで「解析できない」で止めないため。本文は、`<<` の
    ある行が (引用符の外の) 改行で終わった次の行から始まる。引用符・コメントの中の `<<`、
    `<<<` (here-string)、`((` / `$((` の中の `<<` (シフト演算子)、`${...}` の中の `<<` は
    heredoc ではない。コマンド置換 (`$(...)` / backtick) の中の heredoc は、本文が始まる前に
    置換が閉じると本文を持たない (bash / zsh 実測。後ろの行はコマンドとして走る)。置換の中で
    本文が始まるものがあれば、本文の終わりがシェルで割れるので、コマンド全体を従来どおり
    (heredoc を読み飛ばさずに) 読む。heredocs=False はその従来の読み方 (0.3.2 と同じ)。

    quoted_bodies にリストを渡すと、読み飛ばした本文のうち区切り語に引用がある (shell が展開しない)
    ものの範囲 (command の中の [開始, 終わり)) を足す。従来の読み方に戻したときは空にする。

    二重引用符の中のコマンド置換 (`--body "$(cat <<'EOF' ...)"`) で本文が始まる heredoc も、本文を空行にする
    (_dq_substitution_bodies)。本文の範囲を確かめられないときは、この読み飛ばしだけをやめて読み直す
    (dq_bodies=False)。ここで空行にした本文は quoted_bodies に足さない (安全網は加工前を数える)。
    """
    out: list[str] = []
    quote = ""  # "" | "'" | '"'
    # 語の途中である out の長さ (直前が `\\X` か、置換の閉じ括弧のとき。直後の `#` はコメントの始まりではない)
    escaped_end = -1
    # 本文を待っている heredoc (区切り語, <<-, 引用あり, 登録したときのコマンド置換の深さ)
    pending: list[tuple[str, bool, bool, int]] = []
    arith = 0  # `((` / `$((` の中の括弧の深さ。中の `<<` はシフト演算子で heredoc ではない
    arith_in_word = False  # 今の `((` は `$((` (閉じた後ろも同じ語)
    brace = 0  # `${` の中の波括弧の深さ。中の `<<` は展開の語で heredoc ではない
    subs: list[list] = []  # 開いているコマンド置換 (`$(` は ["(", 括弧の深さ]、backtick は ["`", 0])
    i = 0
    n = len(command)
    blanks: dict[int, int] = {}  # 二重引用符の中の置換で読み飛ばす本文 (開始 → 終わり)
    # この位置より前の `$(` は読み終えている (同じ範囲を読み直すと個数の 2 乗の時間になる)
    dq_scan_from = 0 if heredocs and dq_bodies else n
    while i < n:
        ch = command[i]
        if quote == '"' and i in blanks:
            i = blanks[i]  # 本文は引用符の中の文字列なので、改行を残す必要が無い
            continue
        if quote == "'":
            out.append(ch)
            if ch == "'":
                quote = ""
        elif ch == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                pass  # 継続行: 両方とも捨てて前後をつなぐ (単一引用符の外ならどこでも)
            else:
                out.append(ch)
                out.append(command[i + 1])
                escaped_end = len(out)
            i += 2
            continue
        elif quote == '"':
            if i >= dq_scan_from and command.startswith("$(", i):
                read = _dq_substitution_bodies(command, i + 2)
                if read is None:
                    if quoted_bodies is not None:
                        quoted_bodies.clear()
                    return _strip_comments(command, heredocs, quoted_bodies, dq_bodies=False)
                blanks.update(read[0])
                dq_scan_from = read[1]
            out.append(ch)
            if ch == '"':
                quote = ""
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif heredocs and arith:
            if ch == "(":
                arith += 1
            elif ch == ")":
                arith -= 1
            out.append(ch)
            if arith == 0 and arith_in_word:
                escaped_end = len(out)  # `$((1))#x` の `#` は語の途中
        elif heredocs and command.startswith("((", i):
            arith = 2
            arith_in_word = bool(out) and out[-1] == "$"
            out.append("((")
            i += 2
            continue
        elif heredocs and command.startswith("${", i):
            brace += 1
            out.append("${")
            i += 2
            continue
        elif heredocs and brace and ch in "{}":
            brace += 1 if ch == "{" else -1
            out.append(ch)
        elif heredocs and command.startswith("$(", i) and not command.startswith("$((", i):
            subs.append(["(", 1])
            out.append("$(")
            i += 2
            continue
        elif heredocs and ch == "`":
            if subs and subs[-1][0] == "`":
                pending = _close_substitution(subs, pending)
            else:
                subs.append(["`", 0])
            out.append(ch)
        elif heredocs and ch in "()" and subs and subs[-1][0] == "(":
            subs[-1][1] += 1 if ch == "(" else -1
            out.append(ch)
            if subs[-1][1] == 0:
                pending = _close_substitution(subs, pending)
                escaped_end = len(out)  # `$(a)#x` の `#` は語の途中 (`(a)#x` の `#` はコメント)
        elif heredocs and command.startswith("<<<", i):
            out.append("<<<")  # here-string は本文を持たない
            i += 3
            continue
        elif heredocs and command.startswith("<<", i) and not brace:
            j = i + 2
            strip_tabs = j < n and command[j] == "-"
            word = _heredoc_word(command, j + 1 if strip_tabs else j)
            if word is None:
                # 区切り語が読めない (閉じていない引用符など) ときは従来どおり本文もコードとして読む
                out.append("<<")
                i += 2
                continue
            delim, quoted, end = word
            pending.append((delim, strip_tabs, quoted, len(subs)))
            out.append(command[i:end])
            i = end
            continue
        elif ch == "\n" and pending and not brace and subs:
            # コマンド置換の中で本文が始まる heredoc は、本文がどこで終わるかが bash と zsh で割れる
            # (bash 3.2 は閉じ括弧で切り、zsh は区切り語の行まで読む)。この本文をコードとして読むと
            # 引用符の対応がずれ、後ろの heredoc を読み飛ばした結果が別の形で「解析できる」ことがある。
            # コマンド全体を、heredoc を読み飛ばさない従来の読み方に戻す
            if quoted_bodies is not None:
                quoted_bodies.clear()
            return _strip_comments(command, heredocs=False)
        elif ch == "\n" and pending and not brace:
            out.append("\n")
            i += 1
            for delim, strip_tabs, quoted, _level in pending:
                start = i
                i, terminated = _skip_heredoc_body(command, i, delim, strip_tabs, quoted, out)
                # 終わりの行が見つからない本文は、読み方が shell とずれている (区切り語の読み違いなど)
                # ことがあるので、安全網からは隠さない
                if quoted and terminated and quoted_bodies is not None:
                    quoted_bodies.append((start, i))
            pending = []
            continue
        elif ch == "#" and (not out or (out[-1] in _WORD_START_PREV and escaped_end != len(out))):
            while i < n and command[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _strip_for_parsing(command: str, quoted_bodies: list[tuple[int, int]] | None = None) -> str:
    """構文解析に渡す文字列。`$'` があるコマンドは、二重引用符の中の置換の本文を読み飛ばさない。

    shlex は `$'\\''` の引用符の対応を shell と違う形で読む。ずれた読みを本文の `"` が止めていることがあるので、
    本文を消して「解析できる」にしない。
    """
    return _strip_comments(command, quoted_bodies=quoted_bodies, dq_bodies="$'" not in command)


def _close_substitution(subs: list[list], pending: list[tuple[str, bool, bool, int]]) -> list:
    """一番内側のコマンド置換を閉じ、その中で登録して本文が始まっていない heredoc を捨てる。"""
    level = len(subs)
    subs.pop()
    return [p for p in pending if p[3] < level]


# heredoc の区切り語を終わらせる文字 (shell の metacharacter)
_HEREDOC_WORD_END = frozenset(" \t\n;&|()<>")


def _heredoc_word(command: str, j: int) -> tuple[str, bool, int] | None:
    """`<<` (と `-`) の直後の位置 j から区切り語を読む。

    (引用を外した区切り語, 引用やエスケープを含むか, 区切り語の直後の位置) を返す。
    区切り語が無い・引用符が閉じていない・継続行をまたぐときは None (heredoc として扱わない)。
    """
    n = len(command)
    while j < n and command[j] in " \t":
        j += 1
    start = j
    delim: list[str] = []
    quoted = False
    while j < n and command[j] not in _HEREDOC_WORD_END:
        ch = command[j]
        if ch == "\\":
            if j + 1 >= n or command[j + 1] == "\n":
                return None
            delim.append(command[j + 1])
            quoted = True
            j += 2
        elif ch == "'":
            close = command.find("'", j + 1)
            if close < 0:
                return None
            delim.append(command[j + 1 : close])
            quoted = True
            j = close + 1
        elif ch == '"':
            k = j + 1
            while k < n and command[k] != '"':
                if command[k] == "\\" and k + 1 < n and command[k + 1] in '\\"$`':
                    k += 1
                delim.append(command[k])
                k += 1
            if k >= n:
                return None
            quoted = True
            j = k + 1
        else:
            delim.append(ch)
            j += 1
    text = "".join(delim)
    if j == start or "\n" in text:
        return None
    return text, quoted, j


def _skip_heredoc_body(
    command: str, i: int, delim: str, strip_tabs: bool, quoted: bool, out: list[str]
) -> tuple[int, bool]:
    """位置 i から heredoc の本文を終わりの区切り語の行まで読み飛ばし、改行だけを out に足す。

    終わりの行は、行全体が区切り語と一致する行 (`<<-` は行頭のタブを除いて比べる)。
    区切り語に引用が無い heredoc では、shell と同じく `\\`+改行で行をつないでから比べる
    (`ab\\`+改行+`EOF` は終わりの行ではなく、`EO\\`+改行+`F` は終わりの行)。
    終わりの行が無ければコマンドの最後までが本文 (shell も同じ)。
    (読み終えた位置, 終わりの行で終わったか) を返す。
    """
    n = len(command)
    while i < n:
        parts: list[str] = []
        while True:
            e = command.find("\n", i)
            line = command[i:] if e < 0 else command[i:e]
            i = n if e < 0 else e + 1
            if e >= 0:
                out.append("\n")
            trailing = len(line) - len(line.rstrip("\\"))
            if not quoted and e >= 0 and trailing % 2 == 1:
                parts.append(line[:-1])
                continue
            parts.append(line)
            break
        logical = "".join(parts)
        if (logical.lstrip("\t") if strip_tabs else logical) == delim:
            return i, True
    return i, False


def _closes_early(body: str) -> bool:
    """本文の途中の `)` で置換が閉じ、続く `"` で外側の引用符も閉じる読み方がありうるか。

    bash 3.2 はコマンド置換を括弧と引用符の対応だけで読むので、本文の `)` で置換を閉じる
    (`foo)"` + 改行 + `cd x` の後ろの行はコードとして走る。zsh は区切り語の行まで本文と読む)。
    """
    quote = ""
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return '"' in body[i:]
            depth -= 1
        i += 1
    return False


def _dq_substitution_bodies(command: str, start: int) -> tuple[dict[int, int], int] | None:
    """二重引用符の中の `$(` の直後 (start) から置換の終わりまで読み、中で本文が始まる heredoc の範囲を返す。

    ({本文の開始: 終わりの行の次}, 置換の直後の位置)。置換が閉じなければ位置はコマンドの長さ。
    本文の終わりの行が無い・本文の範囲が shell で割れる・引用の無い区切り語の本文に置換がある・本文があるのに
    置換が閉じないときは None
    (bash 3.2 は閉じ括弧で本文を切り、zsh は区切り語の行まで読む。どちらかに決めずに従来の読み方に戻す)。
    算術や `${...}` の中の `<<` は区別しない。本文と読み違えても、終わりの行が無ければ None になり、
    あっても二重引用符の中の文字列を空行にするだけで、安全網は加工前を数える。
    """
    n = len(command)
    bodies: dict[int, int] = {}
    pending: list[tuple[str, bool, bool]] = []
    quote = ""
    depth = 1
    i = start
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif ch == "\\":
            i += 2
            continue
        elif quote == '"':
            if ch == '"':
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return bodies, i + 1
        elif ch == "#" and command[i - 1] in _WORD_START_PREV:
            while i < n and command[i] != "\n":
                i += 1
            continue
        elif command.startswith("<<<", i):
            i += 3
            continue
        elif command.startswith("<<", i):
            j = i + 2
            strip_tabs = j < n and command[j] == "-"
            word = _heredoc_word(command, j + 1 if strip_tabs else j)
            if word is None:
                return None
            pending.append((word[0], strip_tabs, word[1]))
            i = word[2]
            continue
        elif ch == "\n" and pending:
            i += 1
            for delim, strip_tabs, quoted in pending:
                body_start = i
                # 終わりの行が無い本文はコマンドの最後まで読むので、置換が閉じずに下の None に届く
                i, _terminated = _skip_heredoc_body(command, i, delim, strip_tabs, quoted, [])
                body = command[body_start:i]
                # 引用の無い区切り語の本文の置換は shell が走らせるので、読み飛ばさない
                if not quoted and ("$(" in body or "`" in body):
                    return None
                if _closes_early(body):
                    return None
                bodies[body_start] = i
            pending = []
            continue
        i += 1
    return None if bodies else ({}, n)


def _is_separator(tok: str) -> bool:
    """punctuation だけの token が区切りを含むか。

    shlex は `);` のように隣り合う記号を 1 token にまとめるため、token 全体ではなく
    中に区切り文字があるかで判定する。`>&` / `&>` などのリダイレクトは区切りではない。
    """
    if not set(tok) <= set(_PUNCT):
        return False
    if any(c in tok for c in ";|\n") or "&&" in tok:
        return True  # `&&>` は `&&` の後ろにリダイレクトが付いたもの (`&>` ではない)
    return "&" in tok and not tok.startswith((">", "<")) and not tok.endswith(">")


def _separator_kind(tok: str) -> str:
    """区切りの種類: "or" (`||`) / "pipe" (`|` `|&`) / "and" (`&&`) / "bg" (`&`) / "seq" (`;` 改行)。"""
    s = tok.replace("(", "").replace(")", "")
    if "||" in s:
        return "or"
    if "|" in s:
        return "pipe"
    if "&&" in s:
        return "and"
    if "&" in s:
        return "bg"
    return "seq"


def _split_trailing_redirect(tok: str) -> tuple[str, str]:
    """区切りの token を (区切り, 後ろに付いたリダイレクト演算子) に分ける。`&&>` は `&&` + `>`、改行 + `&>` は `&>`。"""
    cut = next((k for k, c in enumerate(tok) if c in "<>"), len(tok))
    if 0 < cut < len(tok) and tok[cut - 1] == "&" and cut >= 2 and tok[cut - 2] != "&":
        cut -= 1
    sep, redirect = tok[:cut], tok[cut:]
    if not redirect or not set(redirect) <= _REDIRECT_CHARS or not _is_separator(sep):
        return tok, ""
    return sep, redirect


@dataclass
class _Segment:
    tokens: list[str]
    depths: list[int]  # 各 token の時点の括弧 (サブシェル・コマンド置換) の深さ
    before: str = ""  # 直前の区切りの種類 ("" = コマンドの先頭)
    after: str = ""  # 直後の区切りの種類 ("" = コマンドの末尾)


def _segments(tokens: list[str]) -> list[_Segment]:
    segments: list[_Segment] = []
    cur = _Segment([], [])
    depth = 0
    for tok in tokens:
        punct = bool(tok) and set(tok) <= set(_PUNCT)
        if punct:
            depth = max(0, depth + tok.count("(") - tok.count(")"))
        if tok and _is_separator(tok):
            # shlex は隣り合う記号を 1 token にまとめるので、区切りの直後のリダイレクト (`;>f` / 改行 + `<<EOF`)
            # が区切りに飲まれる。リダイレクトの部分は次のセグメントの先頭に回す
            sep, redirect = _split_trailing_redirect(tok)
            kind = _separator_kind(sep)
            cur.after = kind
            if cur.tokens:
                segments.append(cur)
            cur = _Segment([], [], before=kind)
            if redirect:
                cur.tokens.append(redirect)
                cur.depths.append(depth)
        else:
            cur.tokens.append(tok)
            cur.depths.append(depth)
    if cur.tokens:
        segments.append(cur)
    return segments


def _strip_prefix(seg: list[str]) -> tuple[list[str], dict[str, str], list[str]]:
    """先頭の `(` / 予約語 / wrapper / `VAR=value` を外し、(残り, 代入, 外した wrapper) を返す。"""
    assigns: dict[str, str] = {}
    wrappers: list[str] = []
    i = 0
    while i < len(seg):
        tok = seg[i]
        if _ASSIGNMENT.match(tok):
            name, _, value = tok.partition("=")
            assigns[name] = value
            i += 1
            continue
        if tok == "(" or tok in _WRAPPERS or tok in _KEYWORDS:
            if tok in _WRAPPERS:
                wrappers.append(tok)
            i += 1
            continue
        # コマンド語の前のリダイレクト (`<<EOF gh pr create` / `2>/dev/null cd x`) は、演算子と対象を外す
        if tok and set(tok) <= _REDIRECT_CHARS:
            i += 2
            continue
        if tok.isdigit() and i + 1 < len(seg) and seg[i + 1] and set(seg[i + 1]) <= _REDIRECT_CHARS:
            i += 3
            continue
        break
    return seg[i:], assigns, wrappers


# リダイレクト演算子だけでできた token (`<` `<<` `<<<` `>` `>>` `>&` `&>` `<&` `<>`)。区切り (`;` `|` 改行) は含まない
_REDIRECT_CHARS = frozenset("<>&")

# 後ろの cd を外部プロセスとして走らせる wrapper (シェルの cwd は変わらない。exec は shell ごと置き換える)
_DETACHING_WRAPPERS = {"env", "nohup", "exec"}


@dataclass(frozen=True)
class _CdState:
    steps: tuple[str, ...] = ()
    unresolved: str | None = None
    # いずれかの step が、シェルの cwd を変えないかもしれない位置 (サブシェル・パイプ・`&`・
    # env / nohup / exec の後) にある
    uncertain: bool = False

    @property
    def display(self) -> str | None:
        if not self.steps:
            return None
        path = self.steps[0]
        for s in self.steps[1:]:
            path = path.rstrip("/") + "/" + s
        return path


def _unstatic(arg: str) -> bool:
    """値が静的に決まらない cd の引数 (`$` / backtick を含む、`-` / `-P` などオプションで始まる)。"""
    return any(c in arg for c in "$`") or arg.startswith("-")


def _next_cd(state: _CdState, arg: str | None, uncertain_here: bool) -> _CdState:
    """cd を 1 つ進める。相対パスは前の移動先に join し、絶対パス (`/` `~`) は置き換える。

    引数なしの cd は従来どおり起点 (hook の cwd) に戻す。シェルの cwd を変えないかもしれない
    cd に相対の cd が続くと、どちらを起点にするか決まらないので「静的に解決できない」にする。
    """
    if arg is None:
        return _CdState()
    if state.steps and uncertain_here and arg.startswith(("/", "~")):
        # `cd A || cd /abs`: 絶対パスでも、走らないかもしれない cd は前の移動先を置き換えるとは限らない
        steps = (*state.steps, arg)
        return _CdState(steps, " → ".join(steps), True)
    if arg.startswith(("/", "~")) or not state.steps:
        return _CdState((arg,), arg if _unstatic(arg) else None, uncertain_here)
    steps = (*state.steps, arg)
    unresolved = state.unresolved or (arg if _unstatic(arg) else None)
    if unresolved is None and (state.uncertain or uncertain_here):
        unresolved = " → ".join(steps)
    return _CdState(steps, unresolved, state.uncertain or uncertain_here)


def _is_gh(tok: str) -> bool:
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in ("gh", "gh.exe")


def _parse_create(args: list[str]) -> Invocation:
    base = None
    head = None
    repo = None
    draft = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-B", "--base") and i + 1 < len(args):
            base = args[i + 1]
            i += 2
            continue
        if a in ("-H", "--head") and i + 1 < len(args):
            head = args[i + 1]
            i += 2
            continue
        if a in _READY_VALUE_OPTS and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("--base="):
            base = a.split("=", 1)[1]
        elif a.startswith("--head="):
            head = a.split("=", 1)[1]
        elif a in ("-d", "--draft"):
            draft = True
        i += 1
    return Invocation(kind="create", base=base or None, head=head or None, repo=repo or None, draft=draft)


# `gh pr ready` で値を取るオプション (位置引数と取り違えないため)。
_READY_VALUE_OPTS = {"-R", "--repo"}


def _parse_ready(args: list[str]) -> Invocation | None:
    target = None
    repo = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--undo":
            return None  # ready -> draft への巻き戻しは対象外
        if a in _READY_VALUE_OPTS:
            repo = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
            i += 1
            continue
        if not a.startswith("-") and target is None:
            target = a
        i += 1
    return Invocation(kind="ready", target=target, repo=repo)


def _split_global_flags(args: list[str]) -> tuple[str | None, list[str]]:
    """`gh -R owner/repo pr create` のように subcommand の前に置いた --repo を取り出す。"""
    repo = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in _READY_VALUE_OPTS:
            repo = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif a.startswith("--repo="):
            repo = a.split("=", 1)[1]
            i += 1
        else:
            break
    return repo, args[i:]


# `cd A || exit` の右に置かれると、cd に失敗したときに後ろのコマンドを走らせない語
_TERMINATORS = {"exit", "return"}


def _terminates(segments: list[_Segment], index: int) -> bool:
    """segments[index] が exit / return で始まるか (`cd A || exit 1` の右側)。"""
    if index >= len(segments):
        return False
    rest, _assigns, _wrappers = _strip_prefix(segments[index].tokens)
    prefix = segments[index].tokens[: len(segments[index].tokens) - len(rest)]
    if "{" not in prefix:
        # `cd A || exit 1 | tee` の exit はパイプの中 (サブシェル) で走り、shell を終わらせない
        return bool(rest) and rest[0] in _TERMINATORS and segments[index].after not in ("pipe", "bg")
    return _brace_group_exits(segments, index)


def _brace_group_exits(segments: list[_Segment], index: int) -> bool:
    """segments[index] から始まる `{ ...; }` が、どの経路でも exit / return に届くか。

    `cd A || { echo fail >&2; exit 1; }` の右側。数えるのは波括弧の一番外側に `;` / 改行で並んだ
    exit / return だけ。`&&` / `||` / パイプの後ろ、`if` / `while` などの中、入れ子の `{ }` やサブシェルの中の
    exit は走らないことがあるので数えない (止める側に倒す)。閉じ括弧の後ろがパイプか `&` なら
    (`{ ...; exit 1; } | tee log`)、波括弧ごとサブシェルで走り shell を終わらせないので数えない。
    閉じ括弧が見つからないときも数えない。
    """
    depth = 0
    exits = False
    for j in range(index, len(segments)):
        seg = segments[j]
        rest, _assigns, _wrappers = _strip_prefix(seg.tokens)
        prefix = seg.tokens[: len(seg.tokens) - len(rest)]
        depth += prefix.count("{")
        if rest and rest[0] == "}":
            depth -= 1
            if depth <= 0:
                return exits and seg.after not in ("pipe", "bg")
            continue
        if (
            depth == 1
            and rest
            and rest[0] in _TERMINATORS
            # `then exit` / `( exit` / `env exit` / `X=1 exit` は一番外側の素の exit ではない
            and all(tok == "{" for tok in prefix)
            and (j == index or seg.before == "seq")
            and seg.after not in ("pipe", "bg")
        ):
            exits = True
    return False


def _tokens(stripped: str) -> list[str]:
    """_strip_comments を通した文字列を shell の語と記号に分ける (閉じていない引用符は ValueError)。"""
    lex = shlex.shlex(stripped, posix=True, punctuation_chars=_PUNCT)
    lex.commenters = ""  # コメントは _strip_comments で落とす (shlex は改行まで食う)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    return list(lex)


_ASSIGN_HEAD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# 配列の代入 (`X=(a b)`) の値の始まり
_ARRAY_HEAD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\(")
# 値に語の区切りを含みうるもの (shlex は 1 語に読めない)
_VALUE_WITH_CODE = re.compile(r"[$<>]\(|`|\$\{|^[A-Za-z_][A-Za-z0-9_]*=\(")
# 語を終わらせる文字 (引用の外)
_WORD_END = frozenset(" \t\r\n;&|<>")
# 代入語の直後がこれ (かコマンドの終わり) なら、代入の後ろにコマンド語は無い
_NO_COMMAND_AFTER = frozenset(";&|\n)")


@dataclass
class _Frame:
    kind: str  # "code" | "sub" (`$(`) | "paren" (`(`) | "array" | "dq" | "brace" (`${`) | "bt" (backtick)
    start: int = 0  # 中身の開始位置
    word: int = -1  # 読んでいる語の開始位置 (コードの frame だけ)
    in_command: bool = True  # 次の語がコマンド語か、その前置きの位置にある
    redirect: bool = False  # 次の語はリダイレクトの対象
    literal_sq: bool = False  # `'` は文字 (二重引用符の中の `${`)


_CODE_KINDS = frozenset({"code", "sub", "paren", "array"})


@dataclass(frozen=True)
class _AssignWord:
    start: int
    end: int
    in_dq: bool  # 二重引用符の中のコマンド置換にある


def _scan_assignments(s: str) -> tuple[list[_AssignWord], list[tuple[int, int]], list[tuple[int, int]]]:
    """コマンド語の前置きの位置にある代入語のうち、値にコマンド置換などを含み、後ろに語が続くものを見つける。

    シェルの語の読み方 (引用符・`$(` / `${` / backtick の入れ子) で最後まで 1 回だけ走査する。戻り値の 2 つ目は、
    一番外側のコマンド置換 (`$(` / backtick) の中身の範囲。3 つ目は、深さを問わず backtick の中身の範囲 (走査は
    backtick の中の語を読まない)。引用符や置換が閉じないと、そこで読むのをやめる
    (それまでに見つけたものだけを返す。読めなかった部分は従来の読み方と安全網に任せる)。
    """
    words: list[_AssignWord] = []
    subs: list[tuple[int, int]] = []
    backticks: list[tuple[int, int]] = []
    stack = [_Frame("code")]
    dq_level = 0
    sub_level = 0
    n = len(s)

    def end_word(frame: _Frame, end: int, next_ch: str) -> None:
        if frame.word < 0:
            return
        word = s[frame.word : end]
        if frame.redirect:
            frame.redirect = False
        elif word.isdigit() and next_ch in "<>":
            pass  # `2>f` の fd 番号
        elif _ASSIGN_HEAD.match(word):
            if frame.in_command and _VALUE_WITH_CODE.search(word):
                k = end
                while k < n and s[k] in " \t\r":
                    k += 1
                # 語の直後に続けて書けるのはリダイレクトだけ (`X=$(a)>f gh`。`X=$(a)(b)` は後ろにコマンド語が無い)
                if k < n and s[k] not in _NO_COMMAND_AFTER and (k > end or s[k] in "<>"):
                    words.append(_AssignWord(frame.word, end, dq_level > 0))
        elif word == "{":
            frame.in_command = True  # `function f { ...`
        elif word not in _WRAPPERS and word not in _KEYWORDS:
            frame.in_command = False
        frame.word = -1

    i = 0
    while i < n:
        frame = stack[-1]
        ch = s[i]
        code = frame.kind in _CODE_KINDS
        if ch == "\\":
            if code and frame.word < 0:
                frame.word = i
            i += 2
            continue
        if frame.kind == "bt":
            if ch == "`":
                stack.pop()
                sub_level -= 1
                backticks.append((frame.start, i))
                if sub_level == 0:
                    subs.append((frame.start, i))
            i += 1
            continue
        if code and frame.word >= 0 and (s.startswith("<(", i) or s.startswith(">(", i)):
            # 代入の値のプロセス置換 (`X=<(a b) gh pr create`)。語の途中でないものはリダイレクトと同じ扱い
            stack.append(_Frame("sub", i + 2))
            sub_level += 1
            i += 2
            continue
        if ch == "`" or s.startswith("$(", i) or s.startswith("${", i):
            if code and frame.word < 0:
                frame.word = i
            if ch == "`":
                stack.append(_Frame("bt", i + 1))
                sub_level += 1
                i += 1
            elif s[i + 1] == "(":
                stack.append(_Frame("sub", i + 2))
                sub_level += 1
                i += 2
            else:
                stack.append(_Frame("brace", i + 2, literal_sq=frame.kind == "dq" or frame.literal_sq))
                i += 2
            continue
        if frame.kind == "dq":
            if ch == '"':
                stack.pop()
                dq_level -= 1
            i += 1
            continue
        if s.startswith("$'", i) and code:
            # `$'...'` の中の `\\'` は文字
            if frame.word < 0:
                frame.word = i
            i += 2
            while i < n and s[i] != "'":
                i += 2 if s[i] == "\\" else 1
            i += 1
            continue
        if ch == "'" and not frame.literal_sq:
            close = s.find("'", i + 1)
            if close < 0:
                break
            if code and frame.word < 0:
                frame.word = i
            i = close + 1
            continue
        if ch == '"':
            if code and frame.word < 0:
                frame.word = i
            stack.append(_Frame("dq", i + 1))
            dq_level += 1
            i += 1
            continue
        if frame.kind == "brace":
            if ch == "{":
                stack.append(_Frame("brace", i + 1, literal_sq=frame.literal_sq))
            elif ch == "}":
                stack.pop()
            i += 1
            continue
        # ここから先はコードの frame
        if ch == "#" and frame.word < 0:
            while i < n and s[i] != "\n":
                i += 1
            continue
        if ch == "(":
            if frame.word >= 0 and _ARRAY_HEAD.fullmatch(s, frame.word, i + 1):
                stack.append(_Frame("array", i + 1, in_command=False))
            else:
                end_word(frame, i, ch)
                stack.append(_Frame("paren", i + 1))
            i += 1
            continue
        if ch == ")":
            end_word(frame, i, ch)
            if len(stack) > 1:
                stack.pop()
                if frame.kind == "sub":
                    sub_level -= 1
                    if sub_level == 0:
                        subs.append((frame.start, i))
                elif frame.kind == "paren":
                    stack[-1].in_command = True  # zsh の `f() X=$(a) gh ...` (波括弧の無い本文)
            else:
                frame.in_command = True  # `case` の分岐の `a)`
            i += 1
            continue
        if ch in _WORD_END:
            end_word(frame, i, ch)
            if ch in "<>":
                frame.redirect = True
            elif ch in ";&|\n" and s[i - 1 : i] not in ("<", ">"):
                frame.in_command = True
            i += 1
            continue
        if frame.word < 0:
            frame.word = i
        i += 1
    return words, subs, backticks


def _opaque_assignments(s: str) -> tuple[str, frozenset[str]]:
    """見つけた代入語を引用して 1 語にした文字列と、その語の集合を返す (二重引用符の中のものは対象外)。

    二重引用符の中に置くと引用が文字になる。別の代入語の値の中のものは、外側の語を読むときに _collect が読む。
    """
    if "$'" in s:
        # shlex は `$'\\''` の引用符の対応を shell と違う形で読むので、語を書き換えても後ろの読みが合わない
        return s, frozenset()
    parts: list[str] = []
    opaque: set[str] = set()
    prev = 0
    for word in sorted(_scan_assignments(s)[0], key=lambda w: w.start):
        if word.in_dq or word.start < prev:
            continue
        text = s[word.start : word.end]
        parts += [s[prev : word.start], shlex.quote(text)]
        opaque.add(text)
        prev = word.end
    parts.append(s[prev:])
    return "".join(parts), frozenset(opaque)


# backtick の中でエスケープとして読むもの
_BACKTICK_ESCAPE = re.compile(r"\\([`$\\])")


def _split_assignments(s: str) -> str:
    """見つけた代入語の直後と、一番外側のコマンド置換の直前に区切りを入れる (安全網の数え上げ用)。

    値の中の置換の入れ子は正規表現で読めないので、代入語の後ろのコマンド語を、区切りの後ろのコマンド語として
    数えられるようにする。置換の直前の区切りは、PR 操作の引数の中の PR 操作 (`gh pr create "$(gh pr ready)"`)
    を引数として読み飛ばさないため。単一引用符の中は走査で読み飛ばすので、説明の文字列は数えない。
    backtick の中身は、エスケープを 1 段外して同じように読む (shell も中身をそう読み直す)。
    """
    words, subs, backticks = _scan_assignments(s)
    sep = " ;_=_ "
    # 置換の開始の 1 文字前は `(` か backtick の直前 (`$` の直後でも、正規表現は `(` を実行位置の始まりと読む)
    edits = [(w.end, w.end, sep) for w in words] + [(start - 1, start - 1, sep) for start, _end in subs]
    edits += [(start, end, _split_assignments(_BACKTICK_ESCAPE.sub(r"\1", s[start:end]))) for start, end in backticks]
    parts: list[str] = []
    prev = 0
    for start, end, text in sorted(edits):
        parts += [s[prev:start], text]
        prev = end
    parts.append(s[prev:])
    return "".join(parts)


def _tokens_opaque(stripped: str) -> tuple[list[str], frozenset[str]]:
    """_tokens と同じ。後ろに語が続く代入語の値を 1 語として読み、その語の集合も返す。"""
    text, opaque = _opaque_assignments(stripped)
    if opaque:
        try:
            return _tokens(text), opaque
        except ValueError:
            pass  # 従来の読み方に任せる
    return _tokens(stripped), frozenset()


# 代入の値の中のコマンド置換を読む深さの上限。これより深いものは構文解析で読まない (安全網が数えて止める)
_NESTED_LIMIT = 8


def _has_pr_op(args: list[str]) -> bool:
    """語の並びの中に `gh pr create / new / ready` があるか。"""
    return any(
        _is_gh(a) and b == "pr" and (c in _CREATE or c == "ready") for a, b, c in zip(args, args[1:], args[2:])
    )


# cwd を変えうるコマンド語 (引数の位置は見ない。`git add .` の `.` は移動ではない)
# (`cd` / `chdir` は語の位置を問わず見る。pushd / popd は unresolved_reason が止める)
_CWD_COMMANDS = frozenset({"eval", "source", ".", "builtin"})
# 以降の `;` で繋いだ cd が、走るかどうか分からない本体の中にありうる構文の始まり
_BLOCK_WORDS = frozenset({"if", "while", "until", "for", "case", "select", "repeat", "foreach", "{"})


def _cd_unsure(tokens: list[str]) -> bool:
    """cd の結果が静的に確かでないか。確かなのは、トップレベルの素の `cd <dir>` を `;` か `&&` で繋いだものだけ。

    これ以外の cd (サブシェル・パイプ・`&`・`||`・条件の後ろ・構文の本体・wrapper や前置きの付いたもの・
    引数なし) や、cwd を変えうるコマンド語、展開で決まるコマンド語があれば True。形を 1 つずつ数えず、
    確かな形だけを通す。
    """
    in_block = False
    prev_bare = False
    for segment in _segments(tokens):
        toks = segment.tokens
        bare = False
        if "cd" in toks or "chdir" in toks:
            bare = (
                not in_block
                and len(toks) == 2
                and toks[0] == "cd"
                and not _unstatic(toks[1])
                and not any(segment.depths)
                # `cd 無い && cd /abs; ...` は左が失敗すると元の cwd に残るのに、絶対パスは前の移動先を置き換えて、
                # 無い移動先を確かめる機会を消す (相対パスなら合成した先が無いので止まる)
                and (
                    segment.before in ("", "seq")
                    or (segment.before == "and" and prev_bare and not toks[1].startswith(("/", "~")))
                )
                and segment.after in ("", "seq", "and")
            )
            if not bare:
                return True
        rest, _assigns, _wrappers = _strip_prefix(toks)
        if rest and (rest[0] in _CWD_COMMANDS or "$" in rest[0] or "`" in rest[0]):
            return True
        in_block = in_block or any(tok in _BLOCK_WORDS for tok in toks)
        prev_bare = bare
    return False


def _collect(
    tokens: list[str], opaque: frozenset[str], found: list[Invocation], cd: _CdState, level: int, unsure: bool
) -> None:
    """tokens の PR 操作を順に found に足し、cd を進める。level は代入の値の中のコマンド置換の深さ。

    unsure は「cd が確かでないので、新しい読み方で見つけたものの検査先を信じない」(_cd_unsure)。
    """
    segments = _segments(tokens)
    for index, segment in enumerate(segments):
        if level < _NESTED_LIMIT:
            # 値の中の置換は、代入の後ろのコマンドより先に走る。サブシェルなので、中の cd は外へ持ち出さない
            for tok in segment.tokens:
                if tok not in opaque:
                    continue
                for start, end in _scan_assignments(tok)[1]:
                    try:
                        inner, inner_opaque = _tokens_opaque(tok[start:end])
                    except ValueError:
                        continue  # 読めない置換の中の PR 操作は安全網が数える
                    _collect(inner, inner_opaque, found, cd, level + 1, unsure or _cd_unsure(inner))
        seg, assigns, wrappers = _strip_prefix(segment.tokens)
        if not seg:
            continue
        if seg[0] == "cd":
            uncertain_here = (
                level > 0
                or segment.depths[len(segment.tokens) - len(seg)] > 0
                or segment.before == "pipe"
                or segment.after in ("pipe", "bg")
                or any(w in _DETACHING_WRAPPERS for w in wrappers)
                # `cd A || cd B`: 右は A に移れたときだけ走らず、左は失敗しても後ろが続く。
                # `cd A || exit` の左は、失敗すると後ろが走らないので cwd は A に決まる
                or segment.before == "or"
                or (segment.after == "or" and not _terminates(segments, index + 1))
            )
            cd = _next_cd(cd, seg[1] if len(seg) > 1 else None, uncertain_here)
            continue
        if not _is_gh(seg[0]):
            continue
        global_repo, rest = _split_global_flags(seg[1:])
        if not rest or rest[0] != "pr":
            continue
        # `gh pr -R owner/repo create` のように pr と action の間にも --repo を置ける
        pr_repo, action = _split_global_flags(rest[1:])
        rest = ["pr", *action]
        global_repo = pr_repo or global_repo
        if len(rest) < 2:
            continue
        if rest[1] in _CREATE:
            inv = _parse_create(rest[2:])
        elif rest[1] == "ready":
            inv = _parse_ready(rest[2:])
            if inv is None:
                continue
        else:
            continue
        found.append(
            Invocation(
                kind=inv.kind,
                base=inv.base,
                head=inv.head,
                repo=inv.repo or global_repo or assigns.get("GH_REPO") or None,
                draft=inv.draft,
                target=inv.target,
                cd=cd.display,
                # 引数の中の PR 操作 (`--base a >(gh pr create --base b)`) は別の呼び出しで、この構文解析では
                # 分けられない (後ろの --base が前の値を上書きしている)
                parsed=not unsure and not _has_pr_op(rest[2:]),
                cd_steps=cd.steps,
                cd_unresolved=cd.unresolved,
            )
        )


def find_invocations(command: str) -> list[Invocation]:
    """コマンド中の `gh pr create` / `gh pr ready` をすべて順に返す。

    `gh pr create --draft && gh pr ready` のように 1 つのコマンドで draft 作成と
    ready 化を続ける形があるため、最初の 1 つだけを見て判定してはいけない。
    """
    if "gh" not in command or "pr" not in command:
        return []
    joined = _join_continuations(command)
    try:
        stripped = _strip_for_parsing(command)
        tokens, opaque = _tokens_opaque(stripped)
    except ValueError:
        return [
            Invocation(
                kind="ready" if m.group(1) == "ready" else "create",
                draft=bool(re.search(r"\s(?:--draft|-d)\b", joined)) and m.group(1) != "ready",
                parsed=False,
            )
            for m in _FALLBACK.finditer(joined)
        ]

    # 値を 1 語に読むことと、二重引用符の中の本文を読み飛ばすことで、これまで「解析できない」で止めていた形が読める。
    # cd が確かでなければ、その移動先を検査先にすると実際の $PWD と違うことがあるので、これまでどおり止める
    new_reading = bool(opaque) or stripped != _strip_comments(command, dq_bodies=False)
    found: list[Invocation] = []
    _collect(tokens, opaque, found, _CdState(), 0, new_reading and _cd_unsure(tokens))
    return found


# 標準入力をシェルのコードとして読みうるコマンド語 (`bash <<'EOF'` の本文は実行される)
# (`sudo bash` / `xargs sh` / `/bin/zsh` などは語の位置を問わない。`run.sh` のようなファイル名は除く。
# `.` は実行位置に置かれたものだけ)
_SHELL_READER = re.compile(
    r"(?:^|[^\w.-])(?:" + _SHELLS + r"|ssh|eval|source)(?:\.exe)?(?![\w.-])"
    r"|(?:^|[;&|(\n`{]|\$\()\s*\.\s"
)
# 本文の外の shell の名前は、引用符の中の語 (`--title 'source map を直す'`) でも数える。引用符の対応を
# 左から取ると、コメントや引用の無い本文にある奇数個の `'` / `"` で対応を取り違え、本文を読む shell を
# 見失うため (止める側に倒す)
# 引用符と `\` を外したコマンド語がこれに一致するもの (`b'as'h` / `b\ash` / `/bin/zsh` の類) も本文を読む
_SHELL_READER_WORD = re.compile(r"(?:" + _SHELLS + r"|ssh|eval|source|\.)(?:\.exe)?")


def _may_read_stdin_as_code(stripped: str) -> bool:
    """コマンド語のどれかが shell の名前か、展開で決まる語 (`"$BASH"` / `${SH:-bash}`) か。

    stripped は _strip_comments を通した文字列 (本文は空行)。分けられなければ True (止める側)。
    """
    try:
        # 代入の値を 1 語にした読みと、値の中の語も見る従来の読みの両方で探す (`X=$(a b) b'as'h <<'EOF'`)
        token_lists = [_tokens(stripped), _tokens_opaque(stripped)[0]]
    except ValueError:
        return True
    for tokens in token_lists:
        for segment in _segments(tokens):
            rest, _assigns, _wrappers = _strip_prefix(segment.tokens)
            if not rest:
                continue
            word = rest[0]
            if "$" in word or "`" in word or _SHELL_READER_WORD.fullmatch(word.rsplit("/", 1)[-1]):
                return True
    return False


def _blank_quoted_bodies(command: str) -> str:
    """区切り語に引用がある heredoc の本文を改行だけにした文字列を返す (安全網の数え上げ用)。

    引用のある区切り語の本文は、shell が `$(` も backtick も展開しないただの文字列なので、PR の本文で
    `gh pr create` の使い方を説明しても数えない。本文を読みうるコマンド (sh / bash / eval / source 等) が
    コマンドのどこかにあるか、コマンド語が展開で決まるときは、本文が実行されうるので加工しない。
    引用の無い区切り語の本文も加工しない。
    """
    spans: list[tuple[int, int]] = []
    stripped = _strip_for_parsing(command, quoted_bodies=spans)
    if not spans or _may_read_stdin_as_code(stripped):
        return command
    parts: list[str] = []
    prev = 0
    for start, end in spans:
        parts.append(command[prev:start])
        parts.append("\n" * command.count("\n", start, end))
        prev = end
    parts.append(command[prev:])
    blanked = "".join(parts)
    return command if _SHELL_READER.search(blanked) else blanked


def _count_pr_ops(text: str) -> int:
    """実行位置にある PR 操作を数える (draft に戻す `gh pr ready --undo` は除く)。"""
    # `\` が無ければ 2 つの読みは同じ (同じ走査を 2 回しない)
    patterns = (_EXEC_POS, _EXEC_POS_NO_PAIR) if "\\" in text else (_EXEC_POS,)
    return max(
        sum(1 for m in pattern.finditer(text) if not (m.group("op") == "ready" and "--undo" in m.group("args")))
        for pattern in patterns
    )


def unresolved_reason(command: str, found: list[Invocation]) -> str | None:
    """解析しきれていない PR 操作がありそうなら、その理由を返す。

    shell の書き方は無数にあり (コマンド置換・pushd・関数定義…)、個別に追うと取りこぼす。
    実行位置に現れる PR 操作の数と、構文解析で認識できた数が合わない場合は
    「検査対象を特定できない」として扱い、呼び出し側で止める。
    """
    raw = _join_continuations(command)
    if "gh" not in raw or "pr" not in raw:
        return None  # 安全網の正規表現はどれも gh と pr を含む (継続行をつないだ後で見る)
    # 数え上げは、区切り語に引用がある heredoc の本文を空行にした文字列で行う (構文解析と同じく本文を数えない)
    counted = _join_continuations(_blank_quoted_bodies(command))
    for pattern in (_LONG_PREFIX, _LONG_PREFIX_NO_PAIR):
        long_prefix = pattern.search(counted)
        if long_prefix and _PR_OP_ANYWHERE.search(counted, long_prefix.start()):
            return "PR 操作の前の前置き (リダイレクト・代入など) が長すぎて位置を確かめられない"
    expected = _count_pr_ops(counted)
    # 代入の値の入れ子は正規表現で読めないので、構文解析と同じ語の読みで分けた文字列でも数える
    # (構文解析が見つけるようになった gh を数えないと、別の場所の見落としと数が合ってしまう)
    expected = max(expected, _count_pr_ops(_split_assignments(_strip_comments(command))))
    # bash -c の中身と pushd / popd は、引用のある本文の中のものも加工前の文字列で見る (止める側)
    for m in _NESTED_SHELL.finditer(raw):
        if _EXEC_POS.search(m.group(2)) or _EXEC_POS_NO_PAIR.search(m.group(2)):
            return "PR 操作が別の shell (bash -c / eval 等) の中にある"
    if expected > len(found) or any(not inv.parsed for inv in found):
        return "PR 操作の位置を解析できない (コマンド置換・サブシェル等の中にある)"
    if found and _DIR_CHANGERS.search(raw):
        return "pushd / popd による移動先を追跡できない"
    return None


def find_invocation(command: str) -> Invocation | None:
    """最初の `gh pr create` / `gh pr ready` を返す。無ければ None。"""
    found = find_invocations(command)
    return found[0] if found else None
