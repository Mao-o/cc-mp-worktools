"""Bash コマンド文字列から `gh pr create` / `gh pr ready` の呼び出しを見つける。

ゲートを走らせるかどうかの判定だけが目的で、shell を完全に解釈するわけではない。
`cd <dir> && gh pr create` の `cd` は追跡する (ゲートを走らせる repo が変わるため)。
cd が何度もあるときは順に合成する (`cd plugins/x && ... && cd ../..` は repo root に戻る)。
heredoc の本文はシェルのコードではないので、構文解析の前に空行にする。

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
# 区切りは対象の語の一部として読む。対象の直後は空白か、次のリダイレクトの演算子
_WORD_PART = r"""(?:'[^']*'|"[^"]*"|\$\([^()]*\)|`[^`]*`|[^\s'";&|<>])"""



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


def _strip_comments(command: str, heredocs: bool = True, quoted_bodies: list[tuple[int, int]] | None = None) -> str:
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
    """
    out: list[str] = []
    quote = ""  # "" | "'" | '"'
    escaped_end = -1  # 直近の `\\X` を足した直後の len(out) (エスケープした空白は単語の区切りではない)
    # 本文を待っている heredoc (区切り語, <<-, 引用あり, 登録したときのコマンド置換の深さ)
    pending: list[tuple[str, bool, bool, int]] = []
    arith = 0  # `((` / `$((` の中の括弧の深さ。中の `<<` はシフト演算子で heredoc ではない
    brace = 0  # `${` の中の波括弧の深さ。中の `<<` は展開の語で heredoc ではない
    subs: list[list] = []  # 開いているコマンド置換 (`$(` は ["(", 括弧の深さ]、backtick は ["`", 0])
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
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
        elif heredocs and command.startswith("((", i):
            arith = 2
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
            if subs[-1][1] == 0:
                pending = _close_substitution(subs, pending)
            out.append(ch)
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


def find_invocations(command: str) -> list[Invocation]:
    """コマンド中の `gh pr create` / `gh pr ready` をすべて順に返す。

    `gh pr create --draft && gh pr ready` のように 1 つのコマンドで draft 作成と
    ready 化を続ける形があるため、最初の 1 つだけを見て判定してはいけない。
    """
    if "gh" not in command or "pr" not in command:
        return []
    joined = _join_continuations(command)
    try:
        tokens = _tokens(_strip_comments(command))
    except ValueError:
        return [
            Invocation(
                kind="ready" if m.group(1) == "ready" else "create",
                draft=bool(re.search(r"\s(?:--draft|-d)\b", joined)) and m.group(1) != "ready",
                parsed=False,
            )
            for m in _FALLBACK.finditer(joined)
        ]

    found: list[Invocation] = []
    cd = _CdState()
    segments = _segments(tokens)
    for index, segment in enumerate(segments):
        seg, assigns, wrappers = _strip_prefix(segment.tokens)
        if not seg:
            continue
        if seg[0] == "cd":
            uncertain_here = (
                segment.depths[len(segment.tokens) - len(seg)] > 0
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
                cd_steps=cd.steps,
                cd_unresolved=cd.unresolved,
            )
        )
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
        tokens = _tokens(stripped)
    except ValueError:
        return True
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
    stripped = _strip_comments(command, quoted_bodies=spans)
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


def unresolved_reason(command: str, found: list[Invocation]) -> str | None:
    """解析しきれていない PR 操作がありそうなら、その理由を返す。

    shell の書き方は無数にあり (コマンド置換・pushd・関数定義…)、個別に追うと取りこぼす。
    実行位置に現れる PR 操作の数と、構文解析で認識できた数が合わない場合は
    「検査対象を特定できない」として扱い、呼び出し側で止める。
    """
    raw = _join_continuations(command)
    if "gh" not in raw or "pr" not in raw:
        return None  # 安全網の正規表現はどれも gh と pr を含む (継続行をつないだ後で見る)
    expected = 0
    # 数え上げは、区切り語に引用がある heredoc の本文を空行にした文字列で行う (構文解析と同じく本文を数えない)
    counted = _join_continuations(_blank_quoted_bodies(command))
    long_prefix = _LONG_PREFIX.search(counted)
    if long_prefix and _PR_OP_ANYWHERE.search(counted, long_prefix.start()):
        return "PR 操作の前の前置き (リダイレクト・代入など) が長すぎて位置を確かめられない"
    for m in _EXEC_POS.finditer(counted):
        if m.group("op") == "ready" and "--undo" in m.group("args"):
            continue
        expected += 1
    # bash -c の中身と pushd / popd は、引用のある本文の中のものも加工前の文字列で見る (止める側)
    for m in _NESTED_SHELL.finditer(raw):
        if _EXEC_POS.search(m.group(2)):
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
