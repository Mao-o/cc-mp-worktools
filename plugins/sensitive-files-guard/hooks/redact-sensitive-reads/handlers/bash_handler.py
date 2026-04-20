"""Bash tool 用 handler (Step 5, 0.3.0 でセグメント分割対応)。

0.2.0 までは shell メタ文字が 1 文字でもあれば即 ``ask_or_deny`` に倒していたが、
``git status && git log 2>/dev/null || true`` のような日常コマンドまで毎回
fail-closed になり実装ペースを落としていた。0.3.0 ではコマンドを以下の 2 段で
静的解析する:

1. **hard-stop** — 動的評価 / 入力リダイレクト / グループ化が含まれる場合は即
   ``ask_or_deny``。対象文字: ``$`` ``(`` ``)`` ``{`` ``}`` ``<`` バッククォート
   ``\\r``。``<`` を hard-stop に含めるのは入力リダイレクトで機密 path を読み込
   む ``cat < .env`` 類を取り逃がさないため。
2. **segment split** — quote を尊重しつつ ``&&`` ``||`` ``;`` ``|`` ``\\n`` で
   セグメントに分け、各セグメントで安全リダイレクト (``>/dev/null`` ``2>&1``
   ``&>/dev/null`` 等) を剥がしてから既存の単一コマンド判定に通す。

### 機密検出 → deny 固定

任意のセグメントが機密 path に触れる単純読み取り (``cat .env``, ``source .env``
等) に当たれば **deny 固定**。ask を挟まない (0.2.0 の意図を維持)。

### 安全と判定するリダイレクト

``_SAFE_REDIRECT_RE`` に一致する以下のみ剥がす:

- ``>/dev/null`` / ``1>/dev/null`` / ``2>/dev/null`` / ``&>/dev/null``
- ``>/dev/stderr`` / ``>/dev/stdout``
- ``2>&1`` / ``>&2`` / ``1>&2`` 等 fd 間複製

``> file.txt`` のような通常ファイルへのリダイレクトは **剥がさない** (書き込み
先が機密かもしれないため保守的に ask に倒す)。

### fail-closed する境界

- hard-stop metachar (``$`` ``<`` ``(`` ``)`` ``{`` ``}`` バッククォート) → ask
- セグメント内に剥がし切れないリダイレクト (``>``, ``&``, ``|`` 単独) → ask
- 絶対/相対パス実行、env prefix、shell wrapper (``bash`` ``eval`` 等) → ask
- シェル予約語 / 制御構文 (``if`` ``then`` ``for`` ``do`` ``time`` ``!`` 等)
  で始まるセグメント → ask。``for i in 1; do cat .env; done`` のような制御構文
  bypass を塞ぐ
- ``shlex.split`` / ``normalize`` / patterns 読込失敗 → ask
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
    "nl", "tac",
})
_SOURCE_CMDS = frozenset({"source", "."})

# hard-stop: 動的評価 / 入力リダイレクト / グループ化 — 静的に結果を決められない。
# ``<`` は入力リダイレクトで ``cat < .env`` など機密 path を取り逃がすので含める。
# ``>`` ``&`` ``|`` ``;`` ``\n`` は segment split 側で扱うので hard-stop には入れない。
_HARD_STOP_CHARS = frozenset("$`(){}<\r")

# セグメント分割対象: quote 外でこれらに当たれば区切る。
# 2 文字演算子 (``&&`` ``||``) と 1 文字演算子 (``;`` ``|`` ``\n``) を扱う。

# セグメント内に剥がしきれずに残ると fail-closed する metachar セット。
_SEGMENT_RESIDUAL_METACHARS = frozenset("&|<>")

# 安全リダイレクト: ``/dev/null`` / ``/dev/stderr`` / ``/dev/stdout`` / fd 複製。
# 1 トークン化されたもの (``2>/dev/null`` 等) に一致。
_SAFE_REDIRECT_RE = re.compile(
    r"^(?:&|[0-9]+)?>(?:&[0-9]+|/dev/null|/dev/stderr|/dev/stdout)$"
)
# 空白区切りで分割されたリダイレクト前半 (``2>`` + ``/dev/null`` 等) を扱うための受け皿。
_REDIRECT_OP_TOKENS = frozenset({">", "1>", "2>", "&>"})
_SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stderr", "/dev/stdout"})

# shell wrapper / privilege tool: 第 1 トークンがこれらなら fail-closed
_SHELL_WRAPPERS = frozenset({
    "bash", "sh", "zsh", "ksh", "fish", "dash",
    "env", "sudo", "doas",
    "command", "builtin", "exec", "eval",
    "xargs", "parallel",
    "python", "python3", "node", "ruby", "perl",
    "awk", "sed",
})

# シェル予約語 / 制御構文: 第 1 トークンがこれらなら fail-closed。
# segment split (``;`` / ``\n``) を挟むと ``do cat .env`` ``then cat .env`` のような
# 制御構文本体セグメントが未知コマンド扱いで allow される bypass を塞ぐ。
# 例: ``for i in 1; do cat .env; done``, ``if true; then cat .env; fi``
_SHELL_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "fi",
    "for", "while", "until", "do", "done",
    "case", "esac", "select",
    "function",
    "time",  # pipeline 前置: ``time cat .env`` が後続を実行する
    "!",     # 否定: ``! cat .env`` が後続を実行する
    "[[", "]]", "[", "]",
})

# 環境変数プレフィクス: ``FOO=1 cmd`` 形式の第 1 トークン
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _has_hard_stop(command: str) -> bool:
    """動的評価 / 入力リダイレクト / グループ化 chars が含まれるか。"""
    return any(c in _HARD_STOP_CHARS for c in command)


def _split_command_on_operators(command: str) -> list[str]:
    """quote を尊重しつつ ``&&`` ``||`` ``;`` ``|`` ``\\n`` でセグメントに分割。

    クォート内の演算子は区切らない (``echo "a && b"`` は 1 セグメント)。
    バックスラッシュエスケープは最低限 (``\\"`` / ``\\'``) のみ尊重。
    """
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    in_single = False
    in_double = False
    n = len(command)
    while i < n:
        c = command[i]
        if in_single:
            buf.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(c)
            if c == '"' and (i == 0 or command[i - 1] != "\\"):
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            i += 1
            continue
        # 2 文字演算子: && / ||
        if c in "&|" and i + 1 < n and command[i + 1] == c:
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        # 1 文字区切り: ; | \n
        if c in ";|\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if buf:
        segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _is_safe_redirect_token(tok: str) -> bool:
    """``2>/dev/null`` / ``&>/dev/null`` / ``2>&1`` 等、単一トークンの安全リダイレクト。"""
    return bool(_SAFE_REDIRECT_RE.match(tok))


def _strip_safe_redirects(tokens: list[str]) -> list[str]:
    """安全リダイレクト (/dev/null 等への出力 / fd 複製) を剥がす。

    - 1 トークン形式 ``2>/dev/null`` : そのまま一致で drop
    - 2 トークン形式 ``2>`` + ``/dev/null`` : ペアで drop

    入力リダイレクト (``<``) は hard-stop で既に弾いているので現れない前提。
    書き込み先が /dev/null 以外のリダイレクト (``> file.txt``) は残して
    後段で fail-closed させる (書き込み先が機密の可能性を潰すため)。
    """
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if _is_safe_redirect_token(tok):
            i += 1
            continue
        if tok in _REDIRECT_OP_TOKENS and i + 1 < n:
            nxt = tokens[i + 1]
            if nxt in _SAFE_REDIRECT_TARGETS or nxt.startswith("&"):
                i += 2
                continue
        out.append(tok)
        i += 1
    return out


def _find_path_candidates(tokens: list[str]) -> list[str]:
    """第 1 トークン以降から、``-`` で始まらないトークンを path 候補として抽出。

    ``--`` より後ろは無条件で path 扱い。誤検出は ``ask`` なので安全側。
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


def _is_absolute_or_relative_path_exec(token: str) -> bool:
    """``/bin/cat`` / ``./script`` / ``../foo`` のような path 実行か。"""
    return (
        token.startswith("/")
        or token.startswith("./")
        or token.startswith("../")
    )


def _segment_has_residual_metachar(tokens: list[str]) -> bool:
    """``_strip_safe_redirects`` 後もセグメントに残っている ``>`` ``&`` ``|`` ``<``
    を持つトークンがあるか。

    ``shlex.split`` で quote が剥がれた後、純粋な演算子トークン (``>``, ``&`` 等)
    や ``>file.txt`` のような非安全リダイレクト、``2>&`` のような不完全形が残って
    いたら fail-closed に倒す。

    クォート由来のトークン (``hello && world`` のような埋め込み値) は shlex が
    1 トークンにまとめるため、長さと記号比率で判別するのは困難。ここでは保守的に
    「トークン中に ``>`` か ``<`` のどれかが含まれていれば metachar 残留とみなす」。
    クォート内 metachar が ask に倒れる仕様は既存 README 記載どおり (0.2.0 と同挙動)。
    """
    for t in tokens:
        if any(c in _SEGMENT_RESIDUAL_METACHARS for c in t):
            return True
    return False


def _analyze_segment(
    tokens: list[str],
    envelope: dict,
    rules: list[tuple[str, bool]],
) -> dict:
    """1 セグメント分の token 列を判定して hook 出力 dict を返す。

    機密 path 一致 → deny 固定。判定不能 → ``ask_or_deny``。それ以外 → allow。
    """
    if not tokens:
        return output.make_allow()

    if _segment_has_residual_metachar(tokens):
        L.log_info("bash_classify", "segment_residual_metachar_fail_closed")
        return output.ask_or_deny(
            "Bash セグメント内に解析対象外のリダイレクト / metachar が残っています "
            "(fail-closed)。",
            envelope,
        )

    first = tokens[0]

    if _is_absolute_or_relative_path_exec(first):
        L.log_info("bash_classify", "abs_or_rel_exec_fail_closed")
        return output.ask_or_deny(
            "絶対パスまたは相対パスでの実行は静的解析対象外です (fail-closed)。",
            envelope,
        )

    if _ENV_PREFIX_RE.match(first):
        L.log_info("bash_classify", "env_prefix_fail_closed")
        return output.ask_or_deny(
            "環境変数プレフィクス付き実行は静的解析対象外です (fail-closed)。",
            envelope,
        )

    if first in _SHELL_WRAPPERS:
        L.log_info("bash_classify", "shell_wrapper_fail_closed")
        return output.ask_or_deny(
            f"shell wrapper / インタプリタ経由 ({first}) は静的解析対象外です "
            "(fail-closed)。",
            envelope,
        )

    if first in _SHELL_KEYWORDS:
        L.log_info("bash_classify", f"shell_keyword_fail_closed:{first}")
        return output.ask_or_deny(
            f"シェル予約語 / 制御構文 ({first}) で始まるセグメントは静的解析対象外です "
            "(fail-closed)。",
            envelope,
        )

    if first in _SAFE_READ_CMDS or first in _SOURCE_CMDS:
        paths = _find_path_candidates(tokens)
        for p in paths:
            if not p:
                continue
            try:
                abs_path = normalize(p, envelope.get("cwd", ""))
            except (ValueError, OSError):
                return output.ask_or_deny(
                    "Bash コマンド内のパス正規化に失敗しました。",
                    envelope,
                )
            if is_sensitive(abs_path, rules):
                L.log_info("bash_classify", f"match:{first}")
                return output.make_deny(
                    f"Bash コマンド ({first}) が機密パターンに一致するファイルに "
                    "触れようとしています。値が LLM コンテキストに露出するため "
                    "block します。許可したい場合は patterns.local.txt に "
                    "`!<basename>` を追加してください。"
                )
        return output.make_allow()

    # 未知のコマンド (git, npm, make, echo, ls 等) は allow
    return output.make_allow()


def _decision_of(result: dict) -> str | None:
    hook = result.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision")


def handle(envelope: dict) -> dict:
    """Bash tool の PreToolUse envelope を受け取り、hook 出力 dict を返す。

    envelope 例:
        {"tool_input": {"command": "cat .env", "description": "..."},
         "cwd": "...", "permission_mode": "..."}
    """
    tool_input = envelope.get("tool_input") or {}
    command = tool_input.get("command")

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

    # 1. hard-stop: 動的評価 / 入力リダイレクト / グループ化は静的解析不能
    if _has_hard_stop(command):
        L.log_info("bash_classify", "hard_stop_fail_closed")
        return output.ask_or_deny(
            "Bash コマンドに動的展開 / 入力リダイレクト / グループ化 "
            "($, バッククォート, $(...), <, (), {}) が含まれています。"
            "静的解析できないため安全側で一時停止します。",
            envelope,
        )

    # 2. segment split (&& / || / ; / | / \n, quote を尊重)
    segments = _split_command_on_operators(command)
    if not segments:
        return output.make_allow()

    # 3. 各セグメントを独立に判定。deny 優先、ask は最後に畳む。
    pending_ask: dict | None = None
    for seg in segments:
        try:
            tokens = shlex.split(seg, comments=False, posix=True)
        except ValueError as e:
            L.log_info("bash_classify", f"shlex_fail:{type(e).__name__}")
            return output.ask_or_deny(
                "Bash コマンドの tokenize に失敗しました。安全側で一時停止します。",
                envelope,
            )
        tokens = _strip_safe_redirects(tokens)

        result = _analyze_segment(tokens, envelope, rules)
        decision = _decision_of(result)

        if decision == "deny":
            # 機密一致 or bypass 中の fail-closed → 即 deny
            return result
        if decision == "ask" and pending_ask is None:
            # 最初の ask を保留。後続セグメントで deny が出れば deny 優先。
            pending_ask = result

    if pending_ask is not None:
        return pending_ask
    return output.make_allow()
