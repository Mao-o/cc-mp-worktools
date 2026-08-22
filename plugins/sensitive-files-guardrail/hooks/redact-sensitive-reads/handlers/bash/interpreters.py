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

from handlers.bash.constants import _OPAQUE_WRAPPERS

_AWK_FIRST_TOKENS = frozenset({"awk", "gawk", "mawk", "nawk"})
_SED_FIRST_TOKENS = frozenset({"sed", "gsed"})

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
    """awk の ``-f FILE`` / ``-fFILE`` / ``--file[=FILE]`` (プログラム本体を検査できない)。"""
    return t == "-f" or t.startswith("--file") or (
        t.startswith("-f") and not t.startswith("--")
    )


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
            if name == "expression":
                if eq:
                    scripts.append(val)
                else:
                    expect_script = True
            elif name == "file":
                if eq:
                    script_file = True
                else:
                    expect_file = True
            elif name == "line-length" and not eq:
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

    呼び出し側は「segment のシングルクォート内に hard-stop char がある」ときだけ
    呼ぶこと (``segmentation._has_quoted_hard_stop``)。その条件下では 0.17.0 まで
    hard-stop で ask だったので、ここで ask に戻しても後退にはならない。
    逆にクォート内 hard-stop が無い segment には適用しない (``grep -r python3 .``
    のような普通のコマンドを ask に倒さないため)。

    Returns:
        ``"delegate:<token>"`` (例 ``delegate:sh`` / ``delegate:-exec`` /
        ``delegate:ssh``)、該当しなければ ``None``。
    """
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    if first in _SHELL_DELEGATORS:
        return f"delegate:{first}"
    for t in tokens[1:]:
        base = t.rsplit("/", 1)[-1]
        if base in _DELEGATING_TOKENS:
            return f"delegate:{base}"
    return None
