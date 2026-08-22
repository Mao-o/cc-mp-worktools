"""awk / sed のプログラム文字列に含まれる「動的構文」の検出 (0.18.0 review)。

0.18.0 で ``_has_hard_stop`` がシングルクォート内を無視するようになり、
``awk 'BEGIN { system("cat .env") }'`` のようにプログラム文字列の内側でコマンド
実行 / ファイル入出力を行う形が ``{`` ``(`` の hard-stop に当たらなくなった
(0.17.0 までは hard-stop で ask)。operand scan は ``.env`` がプログラム文字列の
内側にあるため検出できない (token としては path ではない)。

シングルクォートは **Bash の展開** を止めるだけで、呼び出されるプログラムに
とっては不活性ではない。awk / sed (0.17.0 で opaque から外した 2 つ) について、
プログラム文字列が「コマンド実行 / operand 以外のファイル入出力」を含む場合は
hard-stop 相当 (``ask_or_allow``) に戻す。他のインタプリタ (``python -c`` /
``perl -e`` / ``bash -c`` 等) は ``_OPAQUE_WRAPPERS`` で ask に倒れている。

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
positive 側に倒れても ask で済む)。完全な文法解析は **しない** (思想 1:
うっかり露出予防の射程。敵対的バイパス対策は非目的)。
"""
from __future__ import annotations

import re

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


def _is_script_file_opt(t: str) -> bool:
    """``-f FILE`` / ``-fFILE`` / ``--file[=FILE]`` (プログラム本体を検査できない)。"""
    return t == "-f" or t.startswith("--file") or (
        t.startswith("-f") and not t.startswith("--")
    )


def _sed_scripts(rest: list[str]) -> list[str]:
    """sed の引数列からスクリプト文字列の **候補** を取り出す。

    ``-e SCRIPT`` / ``-eSCRIPT`` / ``--expression[=SCRIPT]`` / ``-ne SCRIPT`` の
    ような ``e`` を含む短縮オプション束、いずれも無ければ最初の非オプション引数。
    短縮オプション束は **過剰包含** に倒す (``-nes/x/y/`` は ``e`` 以降を
    スクリプト、``e`` で終われば次の引数もスクリプト候補): 候補を増やしても
    ask 側にしか倒れない。``-eSCRIPT`` 密着形は束より **先** に判定する
    (review R4: ``-e's/(x)/cat .env/e'`` は ``e`` で終わるため束と誤認していた)。
    ファイル operand は含めない。
    """
    scripts: list[str] = []
    expect = False
    saw_e = False
    positional_taken = False
    for t in rest:
        if expect:
            scripts.append(t)
            expect = False
            continue
        if t in ("-e", "--expression"):
            expect = True
            saw_e = True
        elif t.startswith("--expression="):
            scripts.append(t.split("=", 1)[1])
            saw_e = True
        elif t.startswith("-e") and not t.startswith("--"):
            # ``-eSCRIPT`` 密着形 (束判定より先)
            scripts.append(t[2:])
            saw_e = True
        elif t.startswith("-") and not t.startswith("--") and "e" in t[1:]:
            # ``-ne`` ``-Ee`` (次の引数がスクリプト) / ``-nes/x/y/`` (e 以降)。
            # 過剰包含: 両方を候補にする
            saw_e = True
            after = t[1:].split("e", 1)[1]
            if after:
                scripts.append(after)
            else:
                expect = True
        elif t.startswith("-") and len(t) > 1:
            continue
        elif not saw_e and not positional_taken:
            scripts.append(t)
            positional_taken = True
    return scripts


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
        if any(_is_script_file_opt(t) for t in rest):
            return "sed_script_file"
        for script in _sed_scripts(rest):
            found = _sed_script_dynamic(script)
            if found is not None:
                return found
        return None
    return None
