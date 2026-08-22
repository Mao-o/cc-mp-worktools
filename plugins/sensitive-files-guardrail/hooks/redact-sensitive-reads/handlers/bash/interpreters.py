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
``awk '{print}' .env`` の deny を ask に後退させない)。比較演算子 ``>`` や
論理和 ``||`` も一致するが、それは 0.17.0 まで hard-stop (``$`` ``{``) で ask
だった形であり後退ではない (非機密 operand で ask / autonomous では allow)。
完全な awk / sed 文法解析は **しない** (思想 1: うっかり露出予防の射程。
敵対的バイパス対策は非目的)。
"""
from __future__ import annotations

import re

_AWK_FIRST_TOKENS = frozenset({"awk", "gawk", "mawk", "nawk"})
_SED_FIRST_TOKENS = frozenset({"sed", "gsed"})

# awk: ``system()`` / ``getline`` (ファイル・コマンドからの読込) / pipe
# (``print | "cmd"``・``"cmd" | getline``) / 出力リダイレクト (``print > "f"``)。
_AWK_DYNAMIC_RE = re.compile(r"system\s*\(|getline|\||>")

# sed: ``e`` (コマンド実行: command と s///e flag)、``r`` / ``R`` (ファイル読込 =
# 内容出力)、``w`` / ``W`` (ファイル書出)。コマンド境界 (先頭 / ``;`` / ``{`` /
# 空白 / アドレス ``1`` ``$`` ``/re/`` の直後) にある 1 文字コマンドのみ。
# s の flag は区切り文字が任意なので「非英数字 + flags + e + 終端」で近似する。
_SED_CMD_RE = re.compile(r"(?<![A-Za-z_])[erRwW](?=\s|$|;|\})")
_SED_S_FLAG_E_RE = re.compile(r"[^A-Za-z0-9_\s][gIiMmp0-9]*e[gIiMmp0-9]*(?=\s|$|;|\})")


def _sed_scripts(rest: list[str]) -> list[str]:
    """sed の引数列からスクリプト文字列だけを取り出す。

    ``-e SCRIPT`` / ``-eSCRIPT`` / ``--expression=SCRIPT`` / ``-ne SCRIPT`` のように
    ``e`` で終わる短縮オプション束、いずれも無ければ最初の非オプション引数。
    ファイル operand は含めない (``data.r`` のような名前を誤検出しないため)。
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
        elif t.startswith("-") and not t.startswith("--") and len(t) > 1:
            if t.endswith("e"):
                # ``-ne`` ``-Ee`` など: 次の引数がスクリプト
                expect = True
                saw_e = True
            elif "e" in t[1:]:
                # ``-es/x/y/`` のように e の直後にスクリプトが続く形
                scripts.append(t.split("e", 1)[1])
                saw_e = True
        elif t.startswith("--"):
            continue
        elif not saw_e and not positional_taken:
            scripts.append(t)
            positional_taken = True
    return scripts


def _program_dynamic_construct(tokens: list[str]) -> str | None:
    """awk / sed segment のプログラム文字列に動的構文があれば種別を返す。

    Args:
        tokens: ``shlex.split`` 済み (safe redirect 除去後) の token 列。

    Returns:
        ``"awk_dynamic"`` / ``"awk_program_file"`` / ``"sed_dynamic"`` /
        ``"sed_script_file"``、該当しなければ ``None``。
    """
    if not tokens:
        return None
    first = tokens[0].rsplit("/", 1)[-1]
    rest = tokens[1:]
    if first in _AWK_FIRST_TOKENS:
        for t in rest:
            # ``-f progfile`` / ``--file=progfile``: プログラム本体を検査できない
            if t == "-f" or t.startswith("--file") or (
                t.startswith("-f") and not t.startswith("--")
            ):
                return "awk_program_file"
        if any(_AWK_DYNAMIC_RE.search(t) for t in rest):
            return "awk_dynamic"
        return None
    if first in _SED_FIRST_TOKENS:
        for t in rest:
            if t == "-f" or t.startswith("--file") or (
                t.startswith("-f") and not t.startswith("--")
            ):
                return "sed_script_file"
        for script in _sed_scripts(rest):
            if _SED_CMD_RE.search(script) or _SED_S_FLAG_E_RE.search(script):
                return "sed_dynamic"
        return None
    return None
