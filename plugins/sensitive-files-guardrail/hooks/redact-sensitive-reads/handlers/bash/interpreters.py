"""awk / sed のプログラム文字列に含まれる「動的構文」と、シングルクォート引数を
別のインタプリタに委譲する形の検出 (0.18.0 review)。

0.18.0 で ``_has_hard_stop`` がシングルクォート内を無視するようになり、
``awk 'BEGIN { system("cat .env") }'`` のようにプログラム文字列の内側でコマンド
実行 / ファイル入出力を行う形が ``{`` ``(`` の hard-stop に当たらなくなった
(0.17.0 までは hard-stop で ask)。operand scan は ``.env`` がプログラム文字列の
内側にあるため検出できない (token としては path ではない)。

シングルクォートは **Bash の展開** を止めるだけで、呼び出されるプログラムに
とっては不活性ではない。本モジュールは 2 つの経路で hard-stop 相当
(``ask_or_allow``) に戻す:

1. awk / sed (0.17.0 で opaque から外した 2 つ) のプログラム文字列が「コマンド
   実行 / operand 以外のファイル入出力」を含む (``_program_dynamic_construct``)
2. segment が **シングルクォート内に hard-stop char を含み**、かつ first token
   以外に (または first token として) シェル / インタプリタ / 委譲コマンド
   (``find -exec sh -c '...'`` / ``ssh host '...'`` / ``watch '...'``) が現れる
   (``_delegated_interpreter``)。クォート内の ``$`` ``{`` はその nested
   インタプリタにとって生きている

他のインタプリタが first token の形 (``python -c`` / ``perl -e`` / ``bash -c``)
は ``_OPAQUE_WRAPPERS`` で ask に倒れている。

判定は operand scan の **後** に行う (機密 operand 確定の deny を優先し、
``awk '{print}' .env`` の deny を ask に後退させない)。awk は比較演算子 ``>`` や
論理和 ``||`` も一致するが、それは 0.17.0 まで hard-stop (``$`` ``{``) で ask
だった形であり後退ではない (非機密 operand で ask / autonomous では allow)。

sed は regex 近似ではなく **スクリプトを先頭から走査する小さな parser** で
コマンド位置を求める (review R4: 密着引数 ``r.env`` / ``2r/etc/hostname`` と
``-e`` 密着スクリプトが ``e`` で終わる形を regex が取りこぼした)。アドレス
(``N`` ``$`` ``/re/`` ``\\cREc`` ``,`` ``~`` ``+`` ``!``)、``s`` / ``y`` の区切り
文字で囲まれた本文、``a`` ``i`` ``c`` のテキスト、ラベル、コメントを読み飛ばし、
コマンド文字が ``e`` / ``r`` / ``R`` / ``w`` / ``W`` (および ``s`` の ``e`` / ``w``
flag) なら動的と判定する。未知のコマンド文字は 1 文字進めるだけ (false
positive 側に倒れても ask で済む)。オプションの引数 (``-l N`` 等) は値を
読み飛ばす (review R5: ``-l 80`` の ``80`` を positional script と誤認した)。
GNU / BSD で引数の有無が異なるオプション (``-l`` / ``-i``) は **過剰包含**
(次の token を候補に入れつつ positional とは数えない) に倒す。完全な文法解析は
**しない** (思想 1: うっかり露出予防の射程。敵対的バイパス対策は非目的)。
"""
from __future__ import annotations

import re

from handlers.bash.constants import (
    _GIT_GLOBAL_VALUE_OPTS,
    _METADATA_ONLY_FIRST_TOKENS,
    _OPAQUE_WRAPPERS,
    _SAFE_READ_FIRST_TOKENS,
)

_AWK_FIRST_TOKENS = frozenset({"awk", "gawk", "mawk", "nawk"})
_SED_FIRST_TOKENS = frozenset({"sed", "gsed"})

# 引数文字列をシェル / インタプリタに渡さないコマンド (「inert」)。シングル
# クォート内 hard-stop char の緩和 (0.18.0) は **この first token にだけ** 適用する
# (review R6: 「委譲コマンド」の有限 allowlist では git の shell alias
# (``git -c alias.x='!…' x``) 等を追い切れない。緩和する側を有限 allowlist に
# すれば、未知のコマンドは 0.17.0 と同じ ask に倒れる = fail-closed)。
# pager 系 (``less '+!cmd'`` / ``view '+!cmd'``) は引数から shell を起動しうるので
# safe-read allow-list にあっても緩和しない (``ack`` は ``--pager`` を shell 経由で
# 起動する)。``curl`` は URL の ``{a,b}`` ``[1-3]`` を **curl 自身が glob 展開**
# する (``file:///…/.en{v,x}`` で機密ファイルが読める) ので inert ではない
# (review R8)。``git`` は ``-c`` / ``--config-env`` の値が shell に渡りうる上、
# サブコマンドにも shell を起動する option (``rebase --exec`` / ``bisect run`` /
# ``submodule foreach`` / ``grep -O`` / ``--upload-pack``) があるため、
# ``_GIT_INERT_SUBCOMMANDS`` にあるサブコマンドだけ緩和する。``find`` は
# ``-exec`` 系、``rg`` は ``--pre``、``sort`` は ``--compress-program`` を guard。
_PAGER_LIKE = frozenset({"less", "more", "view", "bat", "ack"})
_QUOTE_RELAX_FIRST_TOKENS = (
    (_SAFE_READ_FIRST_TOKENS - _PAGER_LIKE)
    | _METADATA_ONLY_FIRST_TOKENS
    | _AWK_FIRST_TOKENS
    | _SED_FIRST_TOKENS
    | frozenset({
        "git", "find",
        "sort", "uniq", "cut", "tr", "paste", "column", "diff", "comm", "cmp",
        "jq", "date", "seq", "expr", "true", "false", "sleep",
        "cp", "mv", "rm", "mkdir", "rmdir", "touch", "chmod", "chown", "ln",
        "md5sum", "sha1sum", "sha256sum", "shasum", "base64", "gzip", "gunzip",
    })
)

# inert な first token でも、この prefix で始まる option は外部プログラム /
# shell コマンドを受け取る (値にクォート内 hard-stop があれば委譲とみなす)。
_DELEGATING_OPTIONS: dict[str, tuple[str, ...]] = {
    "rg": ("--pre",),                    # preprocessor command
    "ag": ("--pager",),                  # pager command (review R10)
    "sort": ("--compress-program",),     # 圧縮プログラム
}

# git: 緩和してよいサブコマンド (shell コマンドを受け取る option を持たない)。
# ここに無いサブコマンド (``rebase --exec`` / ``bisect run`` / ``filter-branch`` /
# ``submodule foreach`` / ``difftool`` / ``mergetool`` / ``hook`` / ``grep -O`` /
# ``fetch`` ``clone`` ``push`` の ``--upload-pack`` ``--receive-pack`` …) と
# **未知のサブコマンド (= 設定済み alias かもしれない)** は委譲扱い。
_GIT_INERT_SUBCOMMANDS = frozenset({
    "ls-files", "check-ignore", "status", "log", "show", "diff", "rev-parse",
    "rev-list", "branch", "tag", "describe", "cat-file", "blame", "shortlog",
    "name-rev", "for-each-ref", "ls-tree", "symbolic-ref", "reflog", "stash",
    "worktree", "add", "commit", "checkout", "switch", "restore", "reset",
    "merge", "cherry-pick", "revert", "rm", "mv", "init", "apply",
    "format-patch", "count-objects", "fsck", "gc", "var", "version",
    "remote", "config",
})

# awk: ``system()`` / ``getline`` (ファイル・コマンドからの読込) / pipe
# (``print | "cmd"``・``"cmd" | getline``) / 出力リダイレクト (``print > "f"``)。
_AWK_DYNAMIC_RE = re.compile(r"system\s*\(|getline|\||>")

# sed のコマンド位置で動的と見なす 1 文字コマンド: e (実行) / r R (ファイル読込 =
# 内容出力) / w W (ファイル書出)。
_SED_DYNAMIC_CMDS = frozenset("erRwW")
# ``s`` の flag: これらを読み飛ばした先に e / w があれば動的 (順不同に対応)。
_SED_S_PLAIN_FLAGS = frozenset("gpIiMm0123456789")

# 引数 (シングルクォート文字列) を別のシェル / インタプリタに渡す、first token
# としては opaque 扱いされないコマンド。``ssh host 'cmd'`` はリモートシェル、
# ``watch 'cmd'`` / ``script -c`` / ``su -c`` は sh -c、``osascript -e`` は
# ``do shell script``、``docker`` / ``kubectl`` はコンテナ内シェルに渡しうる。
_SHELL_DELEGATORS = frozenset({
    "ssh", "su", "watch", "script", "expect", "osascript",
    "tmux", "screen", "chroot", "nsenter", "docker", "podman", "kubectl",
    "at", "batch", "crontab", "make", "npm", "npx", "yarn", "pnpm", "bun",
})
# find の実行アクション: 後続 token をコマンドとして実行する。
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# first token 以外に現れたら「委譲」と見なす token (basename 比較)。
_DELEGATING_TOKENS = (
    _OPAQUE_WRAPPERS | _AWK_FIRST_TOKENS | _SED_FIRST_TOKENS
    | _SHELL_DELEGATORS | _FIND_EXEC_ACTIONS
)


def _is_script_file_opt(t: str) -> bool:
    """awk の ``-f FILE`` / ``-fFILE`` / ``--file[=FILE]`` / gawk ``-E`` / ``--exec``
    (プログラム本体を検査できない)。"""
    if t in ("-f", "-E") or t.startswith("--file") or t.startswith("--exec"):
        return True
    return (t.startswith("-f") or t.startswith("-E")) and not t.startswith("--")


# GNU sed の long option。一意な prefix 省略を受理する (``--expr`` = ``--expression``)
# ので、省略形を解決してから引数の有無を判定する (review R8)。
_SED_LONG_OPTS = (
    "expression", "file", "line-length", "in-place", "quiet", "silent",
    "regexp-extended", "separate", "unbuffered", "null-data", "zero-terminated",
    "posix", "debug", "sandbox", "follow-symlinks", "binary", "help", "version",
)


def _resolve_sed_long_opt(name: str) -> str | None:
    """``--NAME`` を GNU sed の一意な prefix 規則で正規名に解決する (不明 / 曖昧は None)。"""
    if name in _SED_LONG_OPTS:
        return name
    cands = [o for o in _SED_LONG_OPTS if o.startswith(name)]
    return cands[0] if len(cands) == 1 else None


def _sed_scripts(rest: list[str]) -> tuple[list[str], bool]:
    """sed の引数列からスクリプト文字列の **候補** と ``-f`` の有無を取り出す。

    ``-e SCRIPT`` / ``-eSCRIPT`` / ``--expression[=SCRIPT]`` / ``-ne SCRIPT`` の
    ような短縮オプション束、いずれも無ければ最初の非オプション引数 (``--`` 以降
    は全て非オプション)。``-eSCRIPT`` 密着形は束の ``e`` として先に判定する
    (review R4)。値を取るオプション (``-l N`` / ``--line-length N``) は値を
    スクリプトと誤認しないよう読み飛ばす (review R5) が、GNU / BSD で引数の
    有無が異なる ``-l`` / ``-i`` / ``-I`` の裸形は **過剰包含** に倒す: 次の
    token を候補に入れつつ positional とは数えない (候補を増やしても ask 側に
    しか倒れない)。ファイル operand は含めない。

    Returns:
        ``(候補スクリプト列, -f / --file が指定されたか)``
    """
    scripts: list[str] = []
    script_file = False
    expect_script = False
    expect_file = False
    ambiguous_next = False
    positional_taken = False
    opts_done = False
    for t in rest:
        if expect_script:
            scripts.append(t)
            expect_script = False
            continue
        if expect_file:
            script_file = True
            expect_file = False
            continue
        if ambiguous_next:
            scripts.append(t)
            ambiguous_next = False
            continue
        if opts_done or not t.startswith("-") or t == "-":
            if not positional_taken:
                scripts.append(t)
                positional_taken = True
            continue
        if t == "--":
            opts_done = True
            continue
        if t.startswith("--"):
            name, eq, val = t[2:].partition("=")
            full = _resolve_sed_long_opt(name)
            if full == "expression":
                if eq:
                    scripts.append(val)
                else:
                    expect_script = True
            elif full == "file":
                if eq:
                    script_file = True
                else:
                    expect_file = True
            elif full == "line-length" and not eq:
                ambiguous_next = True
            elif full is None and not eq:
                # 不明 / 曖昧な long option: 値を取るかもしれないので次の token を
                # 候補に入れつつ positional とは数えない (過剰包含)
                ambiguous_next = True
            continue
        # 短縮オプション束
        letters = t[1:]
        k = 0
        while k < len(letters):
            ch = letters[k]
            attached = letters[k + 1:]
            if ch == "e":
                if attached:
                    scripts.append(attached)
                else:
                    expect_script = True
                break
            if ch == "f":
                if not attached:
                    expect_file = True
                script_file = True
                break
            if ch == "l":
                if attached and attached.isdigit():
                    break  # GNU ``-l80``
                if not attached:
                    ambiguous_next = True  # GNU ``-l 80`` / BSD ``-l`` (引数なし)
                    break
                k += 1  # BSD ``-ln`` = -l -n
                continue
            if ch in "iI":
                if not attached:
                    ambiguous_next = True  # BSD ``-i ext`` / GNU ``-i`` (引数なし)
                break  # 密着 suffix (GNU ``-i.bak``) は残りを消費
            k += 1
    return scripts, script_file


def _sed_script_dynamic(script: str) -> str | None:
    """sed スクリプトを走査し、コマンド位置に動的コマンドがあればその種別を返す。

    Returns:
        ``"sed_dynamic:<cmd>"`` (例 ``sed_dynamic:r`` / ``sed_dynamic:s///e``)、
        無ければ ``None``。
    """
    n = len(script)

    def skip_delimited(i: int, delim: str) -> int:
        # ``i`` は区切り文字の次。閉じ区切りの次の位置を返す (``\\`` エスケープ対応)。
        while i < n:
            ch = script[i]
            if ch == "\\":
                i += 2
                continue
            if ch == delim:
                return i + 1
            i += 1
        return n

    def skip_text(i: int) -> int:
        # ``a`` / ``i`` / ``c`` のテキスト: 行末まで。行末が ``\\`` なら次行も続く
        # (classic な ``a\\`` + 改行 + テキスト形もこれで読める)。
        while True:
            k = script.find("\n", i)
            end = n if k < 0 else k
            seg = script[i:end]
            cont = seg.endswith("\\") and not seg.endswith("\\\\")
            i = n if k < 0 else k + 1
            if not cont or i >= n:
                return i

    i = 0
    while i < n:
        c = script[i]
        # 区切り・空白
        if c in " \t\n;":
            i += 1
            continue
        # --- アドレス ---
        if c.isdigit():
            while i < n and script[i].isdigit():
                i += 1
            continue
        if c == "$":
            i += 1
            continue
        if c == "/":
            i = skip_delimited(i + 1, "/")
            while i < n and script[i] in "IM":
                i += 1
            continue
        if c == "\\" and i + 1 < n:
            i = skip_delimited(i + 2, script[i + 1])
            while i < n and script[i] in "IM":
                i += 1
            continue
        if c in ",~+!":
            i += 1
            continue
        # --- コマンド ---
        if c == "#":
            k = script.find("\n", i)
            i = n if k < 0 else k + 1
            continue
        if c in "{}":
            i += 1
            continue
        if c in ":btT":
            # ラベル (定義 / 分岐先): ``;`` / 改行 / ``}`` まで
            i += 1
            while i < n and script[i] not in ";\n}":
                i += 1
            continue
        if c in "sy":
            if i + 1 >= n:
                return None
            delim = script[i + 1]
            j = skip_delimited(i + 2, delim)
            j = skip_delimited(j, delim)
            if c == "s":
                while j < n and script[j] in _SED_S_PLAIN_FLAGS:
                    j += 1
                if j < n and script[j] in "ew":
                    return f"sed_dynamic:s///{script[j]}"
            i = j
            continue
        if c in "aic":
            i = skip_text(i + 1)
            continue
        if c in _SED_DYNAMIC_CMDS:
            return f"sed_dynamic:{c}"
        if c in "qQlL":
            i += 1
            while i < n and script[i].isdigit():
                i += 1
            continue
        if c == "v":
            i += 1
            while i < n and script[i] not in ";\n}":
                i += 1
            continue
        # ``= d D g G h H n N p P x z F`` と未知の文字: 1 文字進める (lenient)
        i += 1
    return None


def _program_dynamic_construct(tokens: list[str]) -> str | None:
    """awk / sed segment のプログラム文字列に動的構文があれば種別を返す。

    Args:
        tokens: ``shlex.split`` 済み (safe redirect 除去後) の token 列。

    Returns:
        ``"awk_dynamic"`` / ``"awk_program_file"`` / ``"sed_dynamic:<cmd>"`` /
        ``"sed_script_file"``、該当しなければ ``None``。
    """
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    rest = tokens[1:]
    if first in _AWK_FIRST_TOKENS:
        if any(_is_script_file_opt(t) for t in rest):
            return "awk_program_file"
        if any(_AWK_DYNAMIC_RE.search(t) for t in rest):
            return "awk_dynamic"
        return None
    if first in _SED_FIRST_TOKENS:
        scripts, script_file = _sed_scripts(rest)
        if script_file:
            return "sed_script_file"
        for script in scripts:
            found = _sed_script_dynamic(script)
            if found is not None:
                return found
        return None
    return None


def _delegated_interpreter(tokens: list[str]) -> str | None:
    """segment が引数を別のシェル / インタプリタに委譲する形なら、その token を返す。

    first token 以外に ``_DELEGATING_TOKENS`` (``sh`` / ``bash`` / ``python3`` /
    ``xargs`` / awk / sed / find の ``-exec`` 系 …) が現れる形
    (``find . -exec sh -c '…' ';'``)。first token 自体が委譲コマンドの場合は
    ``_QUOTE_RELAX_FIRST_TOKENS`` に無いので ``_quoted_hard_stop_reason`` が先に
    ``not_inert`` を返す。

    Returns:
        ``"delegate:<token>"`` (例 ``delegate:sh`` / ``delegate:-exec``)、
        該当しなければ ``None``。
    """
    for t in tokens[1:]:
        base = t.rsplit("/", 1)[-1]
        if base in _DELEGATING_TOKENS:
            return f"delegate:{base}"
    return None


def _git_global_options(tokens: list[str]) -> tuple[bool, bool, str | None]:
    """git のサブコマンド前の global option を走査し、
    ``(shell alias をインラインで定義しているか, -c / --config-env があるか,
    サブコマンド)`` を返す。

    ``git -c alias.x='!cmd' x`` は Git が ``!`` 以降を shell で実行する形。
    ``-c core.pager=… `` / ``-c core.sshCommand=…`` / ``-c credential.helper=…``
    も値が shell に渡りうる。サブコマンド以降の ``-c`` は別物 (``git commit -c``)
    なので見ない。
    """
    args = tokens[1:]
    shell_alias = False
    has_config = False
    subcommand: str | None = None
    i = 0
    while i < len(args):
        t = args[i]
        if t == "-c" or (t.startswith("-c") and not t.startswith("--")):
            has_config = True
            if t == "-c":
                val = args[i + 1] if i + 1 < len(args) else ""
                i += 2
            else:
                val = t[2:]
                i += 1
            # Git の section / 変数名は大文字小文字非依存 (``Alias.x`` も alias)
            key = val.split("=", 1)[0].lower()
            if key.startswith("alias.") and "=!" in val:
                shell_alias = True
            continue
        if t.startswith("--config-env"):
            has_config = True
            i += 1 if "=" in t else 2
            continue
        if t in _GIT_GLOBAL_VALUE_OPTS:
            # ``-c`` / ``--config-env`` は上で処理済み。残りは値を 1 token 取る
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        subcommand = t
        break
    return shell_alias, has_config, subcommand


def _inline_shell_delegation(tokens: list[str]) -> str | None:
    """クォート状態に関係なく常に ask に倒す「インラインの shell 委譲」。

    現状は ``git -c alias.<name>=!…`` のみ (値が ``!`` で始まる alias は shell
    コマンド)。``.env`` が alias 本文の中にあると operand scan でも見えない。
    """
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    if first == "git":
        shell_alias, _, _ = _git_global_options(tokens)
        if shell_alias:
            return "git-shell-alias"
    return None


def _quoted_hard_stop_reason(tokens: list[str]) -> str | None:
    """シングルクォート内に hard-stop char が残る segment に対し、0.18.0 の緩和を
    **適用してはいけない** 理由を返す (``None`` なら緩和してよい)。

    呼び出し側は ``segmentation._has_quoted_hard_stop`` が真のときだけ呼ぶこと。
    その条件下では 0.17.0 まで hard-stop で ask だったので、ここで ask に戻しても
    後退にはならない。逆にクォート内 hard-stop が無い segment には適用しない
    (``grep -r python3 .`` のような普通のコマンドを ask に倒さないため)。

    Returns:
        ``"not_inert:<first>"`` (first token が inert allow-list 外) /
        ``"delegate:git -c"`` (git の ``-c`` / ``--config-env`` 付き) /
        ``"delegate:git <sub>"`` (inert でない / 未知の git サブコマンド) /
        ``"delegate:<first> <option>"`` (外部プログラムを受け取る option) /
        ``"delegate:<token>"`` (first token 以外への委譲)、緩和してよければ ``None``。
    """
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    if first not in _QUOTE_RELAX_FIRST_TOKENS:
        return f"not_inert:{first}"
    if first == "git":
        _, has_config, sub = _git_global_options(tokens)
        if has_config:
            return "delegate:git -c"
        if sub is None or sub not in _GIT_INERT_SUBCOMMANDS:
            return f"delegate:git {sub or '?'}"
    for prefix in _DELEGATING_OPTIONS.get(first, ()):
        if any(t.startswith(prefix) for t in tokens[1:]):
            return f"delegate:{first} {prefix}"
    return _delegated_interpreter(tokens)
