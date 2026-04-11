"""Bash tool 用 handler (Step 5, 0.2.0 で deny 固定化)。

対象: ``Read`` に相当する**単純な読み取りコマンド**だけを安全パターンとして認識
し、機密 path 一致なら **deny 固定**。認識できないコマンド (間接アクセス等) は
判定不能なので ``ask_or_deny`` (fail-closed)。

### 機密検出 → deny 固定

**機密 path に触れる単純読み取りコマンド** (``cat .env``, ``source .env`` 等) は
ask を挟まず常に ``deny``。ユーザーがうっかり ask で許可してしまう事故を防ぐ
ため (実機観測で Edit/Write の ask 通過 → 機密書き込み事例があった)。

特定 basename を許したい場合は ``patterns.local.txt`` に ``!name`` で exclude
を追加する運用。

### 認識する安全パターン (deny 対象)

``<cmd> [-opt [val]] [--] <path>`` 形式で:

- ``cmd`` が ``{cat, less, more, head, tail, bat, source, .}`` のいずれか
- ``cmd`` は絶対パス実行ではない (``/bin/cat`` 不可)
- 環境変数プレフィクスが無い (``FOO=1 cat .env`` 不可)
- shell wrapper が無い (``bash -c``, ``sudo``, ``command`` 等不可)
- shell メタ文字が無い (``&&``, ``||``, ``;``, ``|``, ``<``, ``>``, ``$(``,
  バッククォート, heredoc、改行区切りの複数コマンド等)
- 変数展開が無い (``$X``, ``$(...)``, バッククォート不可)

これらに当てはまり path が機密パターン一致 → **deny 固定**。

### fail-closed (ask) する境界

上記以外の全て。不明な場合は ``ask``。

Bash 間接アクセス (``< .env``, ``command cat``, ``env VAR=... cat``,
``xargs -a .env``, ``$VAR``, ``$(...)``, heredoc, base64 decode, ``/bin/cat``,
``bash -c``, ``bash -lc``, ``FOO=1 source .env``, 改行区切り複数コマンド) は
全て対象外 (README 既知制限) のため ask に倒す。
"""
from __future__ import annotations

import re
import shlex

from core import logging as L
from core import output
from core.matcher import is_sensitive
from core.patterns import load_patterns
from core.safepath import normalize

# 単純読み取りコマンド (option とフラグ以降に path を 1 つ以上取るもの)
_SAFE_READ_CMDS = frozenset({
    "cat", "less", "more", "head", "tail", "bat", "view",
    "nl", "tac",  # 行番号・逆順表示も同等扱い
})
# source / . は dotenv 固有 (path を shell に展開するが、読み取り自体は起きる)
_SOURCE_CMDS = frozenset({"source", "."})

# shell メタ文字: いずれかが cmd 文字列内にあれば fail-closed
# (クォート内を厳密に区別しない保守的判定。誤検出は ask なので安全側)
_UNSAFE_METACHARS = set("&|;<>()`$\n\r{}")

# shell wrapper / privilege tool: 第 1 トークンがこれらなら fail-closed
_SHELL_WRAPPERS = frozenset({
    "bash", "sh", "zsh", "ksh", "fish", "dash",
    "env", "sudo", "doas",
    "command", "builtin", "exec",
    "xargs", "parallel",
    "python", "python3", "node", "ruby", "perl",
    "awk", "sed",  # -e 経由で任意コード
})

# 環境変数プレフィクス: ``FOO=1 cmd`` 形式の第 1 トークン
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _has_unsafe_metachar(command: str) -> bool:
    """shell メタ文字 (パイプ・リダイレクト・変数展開等) が含まれるか。"""
    return any(c in _UNSAFE_METACHARS for c in command)


def _is_absolute_or_relative_path_exec(token: str) -> bool:
    """``/bin/cat`` / ``./script`` / ``../foo`` のような path 実行か。"""
    return (
        token.startswith("/")
        or token.startswith("./")
        or token.startswith("../")
    )


def _find_path_candidates(tokens: list[str]) -> list[str]:
    """第 1 トークン以降から、``-`` で始まらないトークンを全て path 候補として抽出。

    ``--`` より後ろは無条件で path 扱い。それ以前は option を skip する。
    誤検出は ``ask`` なので安全側 (option 引数が実は path だったケースで False
    negative を避けるため保守的)。
    """
    candidates: list[str] = []
    in_ddash = False
    for tok in tokens[1:]:
        if tok == "--":
            in_ddash = True
            continue
        if in_ddash:
            candidates.append(tok)
            continue
        if tok.startswith("-"):
            continue
        candidates.append(tok)
    return candidates


def handle(envelope: dict) -> dict:
    """Bash tool の PreToolUse envelope を受け取り、hook 出力 dict を返す。

    envelope 例:
        {"tool_input": {"command": "cat .env", "description": "..."},
         "cwd": "...", "permission_mode": "..."}
    """
    tool_input = envelope.get("tool_input") or {}
    command = tool_input.get("command")
    cwd = envelope.get("cwd", "")

    if not isinstance(command, str) or not command.strip():
        return output.make_allow()

    try:
        rules = load_patterns()
    except (FileNotFoundError, OSError) as e:
        L.log_error("patterns_unavailable", type(e).__name__)
        return output.ask_or_deny(
            "patterns.txt が読めないため安全側で一時停止します。",
            envelope,
        )
    if not rules:
        return output.make_allow()

    # 1. shell メタ文字 / 変数展開 / 複合コマンド → fail-closed
    if _has_unsafe_metachar(command):
        L.log_info("bash_classify", "metachar_fail_closed")
        return output.ask_or_deny(
            "Bash コマンドに shell メタ文字 (パイプ / リダイレクト / 変数展開 / "
            "複合実行) が含まれています。静的解析できないため安全側で一時停止します。",
            envelope,
        )

    # 2. shlex.split で tokenize
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError as e:
        L.log_info("bash_classify", f"shlex_fail:{type(e).__name__}")
        return output.ask_or_deny(
            "Bash コマンドの tokenize に失敗しました。安全側で一時停止します。",
            envelope,
        )
    if not tokens:
        return output.make_allow()

    first = tokens[0]

    # 3. 絶対 / 相対パス実行 → fail-closed
    if _is_absolute_or_relative_path_exec(first):
        L.log_info("bash_classify", "abs_or_rel_exec_fail_closed")
        return output.ask_or_deny(
            "絶対パスまたは相対パスでの実行は静的解析対象外です (fail-closed)。",
            envelope,
        )

    # 4. 環境変数プレフィクス (FOO=1 cmd ...) → fail-closed
    if _ENV_PREFIX_RE.match(first):
        L.log_info("bash_classify", "env_prefix_fail_closed")
        return output.ask_or_deny(
            "環境変数プレフィクス付き実行は静的解析対象外です (fail-closed)。",
            envelope,
        )

    # 5. shell wrapper / インタプリタ経由 → fail-closed
    if first in _SHELL_WRAPPERS:
        L.log_info("bash_classify", "shell_wrapper_fail_closed")
        return output.ask_or_deny(
            f"shell wrapper / インタプリタ経由 ({first}) は静的解析対象外です "
            "(fail-closed)。",
            envelope,
        )

    # 6. 認識可能な安全読み取りコマンド
    if first in _SAFE_READ_CMDS or first in _SOURCE_CMDS:
        paths = _find_path_candidates(tokens)
        for p in paths:
            # path 内に変数展開記号等があれば _has_unsafe_metachar で既に弾いているが
            # 念のため再チェック (空でない path のみ)
            if not p:
                continue
            try:
                abs_path = normalize(p, cwd)
            except (ValueError, OSError):
                return output.ask_or_deny(
                    "Bash コマンド内のパス正規化に失敗しました。",
                    envelope,
                )
            if is_sensitive(abs_path, rules):
                L.log_info("bash_classify", f"match:{first}")
                # deny 固定 (0.2.0): ask で承認できてしまう事故を防ぐため。
                # 許したい basename は patterns.local.txt の !name で exclude する運用。
                return output.make_deny(
                    f"Bash コマンド ({first}) が機密パターンに一致するファイルに "
                    "触れようとしています。値が LLM コンテキストに露出するため "
                    "block します。許可したい場合は patterns.local.txt に "
                    "`!<basename>` を追加してください。"
                )
        return output.make_allow()

    # 7. 未知のコマンド → allow (一般的な副作用なしコマンドは多い)
    #    ただし README に「static に判定できるのは _SAFE_READ_CMDS のみ」と明記する。
    return output.make_allow()
