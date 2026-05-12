"""Bash tool 用 handler (0.3.3 で責務境界分解、0.7.0 で input redirect 撤廃、
0.8.0 で prefix normalize / glob 候補列挙を撤廃、0.10.0 で deny 経路に
``render_for_bash`` + ``extract_grep_keys`` を統合、0.11.0 で hard-stop の
segment 単位再評価へ移行)。

責務境界:

- ``handlers/bash/constants.py`` — compile-time 定数 (regex / frozenset)
- ``handlers/bash/segmentation.py`` — quote-aware セグメント分割 / hard-stop 検出
- ``handlers/bash/operand_lexer.py`` — glob 判定 / path 候補抽出 / dotenv glob 判定
- ``handlers/bash/redirects.py`` — 安全リダイレクト剥離 / 残留 metachar 判定

このファイルは以下 **3 つの責務** に限定:

1. **orchestration** — envelope → command 抽出 → segment 分解 → 判定 → 出力 JSON
2. **plugin ステート依存ロジック** — ``load_patterns`` / ``is_sensitive`` /
   ``normalize`` を呼ぶ処理 (``_operand_is_sensitive`` / ``_analyze_segment``)
3. **test seam** — ``handlers.bash_handler`` 名前空間から import される symbol
   (``handle`` / ``_operand_is_sensitive``)

### 判定フロー

1. **segment split** — ``&&`` ``||`` ``;`` ``|`` ``\\n`` を quote-aware に分割。
2. **per-segment 解析** — 各セグメントで:
   - **hard-stop 再判定 (0.11.0)** — ``$`` ``(`` ``)`` ``{`` ``}`` ``<``
     バッククォート ``\\r`` を含む segment は静的解析不能のため
     ``ask_or_allow`` を ``pending_ask`` に格納して **continue** (他 segment の
     deny 検出を続ける)。0.10.0 までは command 全体に hard-stop が 1 つでも
     あると early return していたが、``cat .env | sed 's/(=)/X/'`` のような
     複合で sed segment の ``(`` が原因で全体 ask に倒れ autonomous で素通り
     していたため、segment 単位再評価に細粒度化。攻撃シナリオ ``cat <(echo
     \\(\\)) < .env`` は全 segment hard-stop となるため挙動不変 (思想 1
     整合)。0.3.4〜0.6.x で ``<`` のみ target を抽出していた経路は 0.7.0 で
     撤廃済み。
   - shlex.split → 失敗 → ``ask_or_allow`` を ``pending_ask`` に格納して continue
   - 安全リダイレクト剥離 (``>/dev/null`` / ``2>&1`` 等)
   - **opaque first token 判定 (0.8.0)** — 第一トークンが env-assignment
     (``FOO=1``) / opaque wrapper (``env`` / ``command`` / ``builtin`` /
     ``nohup`` / ``bash`` / ``sudo`` / ``eval`` / ``time`` 等) / 絶対/相対 path
     exec (``/bin/cat`` / ``./script``) のいずれかなら ``ask_or_allow``。
     0.3.2 で導入していた prefix normalize (``FOO=1 cat .env`` →
     ``cat .env`` と解釈して deny) は 0.8.0 で撤廃。
   - 残留 metachar (``>`` ``&`` 等) → ``ask_or_allow``
   - shell keyword (``if``/``for``/``do`` 等) → ``ask_or_allow``
   - operand scan: 各 path 候補について
     - glob 含む → ``_glob_operand_is_dotenv_match`` (operand glob が
       ``.env`` / ``.envrc`` literal に fnmatch) で True なら **deny 固定**、
       False なら ``ask_or_allow``。0.3.2 で導入した既定 rules への候補列挙
       (``_glob_operand_is_sensitive`` / ``_glob_candidates``) は 0.8.0 で撤廃
     - literal → ``_operand_is_sensitive`` (basename + URI/VCS pathspec 分割) で
       True なら **deny 固定**、False なら allow
3. **集約** — deny > ask > allow。``pending_ask`` は最後に畳む。

### patterns.txt 読込失敗 = 全 mode deny 固定

autonomous モードで ``ask_or_allow`` を広く使うため「policy が無いのに lenient
で素通り」を避けて ``make_deny`` 固定。Read/Edit handler 側は ``ask_or_deny``。
"""
from __future__ import annotations

import shlex

from core import logging as L
from core import messages as M
from core import output
from _shared.matcher import is_sensitive
from core.patterns import load_patterns
from core.safepath import normalize

# -- 責務: compile-time 定数の再 export (test seam) -----------------------
# 既存テストが ``from handlers.bash_handler import X`` で参照している可能性を考え、
# constants / segmentation / operand_lexer / redirects の symbol を名前空間に
# 再提示する。定義本体はサブモジュール側に置き、このモジュールは views のみ。
from handlers.bash.constants import (  # noqa: F401
    _ENV_PREFIX_RE,
    _GLOB_CHARS,
    _HARD_STOP_CHARS,
    _OPAQUE_WRAPPERS,
    _REDIRECT_OP_TOKENS,
    _SAFE_READ_CMDS,
    _SAFE_READ_FIRST_TOKENS,
    _SAFE_REDIRECT_RE,
    _SAFE_REDIRECT_TARGETS,
    _SEGMENT_RESIDUAL_METACHARS,
    _SHELL_KEYWORDS,
    _SOURCE_CMDS,
)
from handlers.bash.grep_extract import (  # noqa: F401
    extract_grep_keys,
    is_grep_command,
)
from handlers.bash.operand_lexer import (  # noqa: F401
    _find_path_candidates,
    _glob_operand_is_dotenv_match,
    _has_glob,
)
from handlers.bash.redirects import (  # noqa: F401
    _is_safe_redirect_token,
    _segment_has_residual_metachar,
    _strip_safe_redirects,
)
from handlers.bash.segmentation import (  # noqa: F401
    _has_hard_stop,
    _split_command_on_operators,
)
from redaction.file_render import render_for_bash


# -- 責務: test seam / plugin ステート依存ロジック ------------------------


def _is_opaque_first_token(token: str) -> bool:
    """セグメントの第一トークンが「うっかり書く形ではない prefix 系」か。

    True なら ``ask_or_allow("opaque_prefix")`` に倒す。0.3.2 で導入した
    prefix normalize (``FOO=1 cat .env`` を ``cat .env`` と解釈して deny) は
    0.8.0 で撤廃 (思想 1: うっかり露出予防が目的、敵対的防御は非目的)。

    判定対象:
    - ``FOO=1`` 形式の env-assignment
    - 絶対/相対パス実行 (``/bin/cat`` / ``./script`` / ``../foo``)
    - ``_OPAQUE_WRAPPERS`` (``env`` / ``command`` / ``builtin`` / ``nohup`` /
      ``bash`` / ``sudo`` / ``eval`` / ``time`` / ``exec`` / ``!`` / ``python`` 等)
    """
    if not token:
        return False
    if _ENV_PREFIX_RE.match(token):
        return True
    if token.startswith(("/", "./", "../")):
        return True
    if token in _OPAQUE_WRAPPERS:
        return True
    return False


def _operand_is_sensitive(
    raw: str,
    cwd: str,
    rules: list[tuple[str, bool]],
) -> bool:
    """operand (literal path / URI / VCS pathspec) が機密パターンに該当するか。

    - 通常 path: ``normalize(raw, cwd)`` の basename を ``is_sensitive`` で判定
    - URI (``file://.env``): ``normalize`` が ``file:/.env`` に潰すため同じく検知
    - VCS pathspec (``HEAD:.env``, ``user@host:/p/.env``): コロンで分割して各片の
      basename も追加で判定

    ``normalize`` 失敗 (ValueError / OSError) は再送出 (呼び出し側で fail-closed)。
    """
    abs_path = normalize(raw, cwd)
    if is_sensitive(abs_path, rules):
        return True
    if ":" in raw:
        for piece in raw.split(":"):
            if not piece or piece == raw:
                continue
            try:
                piece_path = normalize(piece, cwd)
            except (ValueError, OSError):
                continue
            if is_sensitive(piece_path, rules):
                return True
    return False


def _build_deny_response(
    tokens: list[str],
    operand: str,
    envelope: dict,
) -> dict:
    """Bash deny 確定時の hook 出力 dict を組み立てる (0.10.0 で導入)。

    E3: ``render_for_bash`` で operand path の minimal info を取得して
    ``bash_deny`` の ``file_render`` / ``dotenv_info`` 引数に渡す。
    E4: first_token が grep family なら ``extract_grep_keys`` で env-var 名
    候補を抽出して ``grep_keys`` に渡す。

    failure (file 不在 / parse 失敗 / open 失敗) は ``render_for_bash`` 側で
    ``(None, None)`` に潰され、``bash_deny`` は generic 相当の reason に降りる。
    deny 動作の判定境界には影響しない。
    """
    first = tokens[0] if tokens else ""
    tool_input = envelope.get("tool_input") or {}
    command_str = tool_input.get("command") or ""
    cwd = envelope.get("cwd", "")
    file_render, dotenv_info = render_for_bash(operand, cwd)
    grep_keys = extract_grep_keys(tokens) if is_grep_command(first) else None
    return output.make_deny(
        M.bash_deny(
            first_token=first,
            operand=operand,
            command=command_str,
            file_render=file_render or "",
            dotenv_info=dotenv_info,
            grep_keys=grep_keys,
        )
    )


def _analyze_segment(
    tokens: list[str],
    envelope: dict,
    rules: list[tuple[str, bool]],
) -> dict:
    """1 セグメント分の token 列を判定して hook 出力 dict を返す。

    機密 path 一致 → ``make_deny`` 固定 (0.10.0 で reason に minimal info /
    matched_pattern_keys を埋め込み)。判定不能 → ``ask_or_allow``
    (default=ask, auto/bypass=allow)。それ以外 → allow。

    0.12.0: ``first_token`` が ``_SAFE_READ_FIRST_TOKENS`` (副作用なしの見る・
    数える系 allow-list) に該当する場合、``_segment_has_residual_metachar`` の
    ask 経路を **スキップ** して operand scan に直行する。`grep foo > /tmp/x` /
    `ls > listing.txt` 等の調査用ワンライナーを ask に倒さないため。機密 path
    redirect (例: ``grep foo > .env``) は operand scan で deny 固定なので
    safety net が残る。``_OPAQUE_WRAPPERS`` / ``_SHELL_KEYWORDS`` とは disjoint
    のため、これらの ask 経路は allow-list ヒットでは自動的に通らない。
    """
    if not tokens:
        return output.make_allow()

    first = tokens[0]
    is_safe_read = first in _SAFE_READ_FIRST_TOKENS

    if _is_opaque_first_token(first):
        L.log_info("bash_classify", "opaque_prefix_lenient")
        return output.ask_or_allow(
            M.bash_lenient("opaque_prefix"),
            envelope,
        )

    if not is_safe_read and _segment_has_residual_metachar(tokens):
        L.log_info("bash_classify", "segment_residual_metachar_lenient")
        return output.ask_or_allow(
            M.bash_lenient("residual_metachar"),
            envelope,
        )

    if first in _SHELL_KEYWORDS:
        L.log_info("bash_classify", f"shell_keyword_lenient:{first}")
        return output.ask_or_allow(
            M.bash_lenient("shell_keyword", detail=first),
            envelope,
        )

    if is_safe_read:
        L.log_info("bash_classify", f"safe_read_allowlist:{first}")

    paths = _find_path_candidates(tokens)
    pending_glob_ask: dict | None = None
    for p in paths:
        if not p:
            continue
        if _has_glob(p):
            if _glob_operand_is_dotenv_match(p):
                L.log_info("bash_classify", f"glob_match:{first}")
                return _build_deny_response(tokens, p, envelope)
            if pending_glob_ask is None:
                L.log_info("bash_classify", "glob_uncertain_lenient")
                pending_glob_ask = output.ask_or_allow(
                    M.bash_lenient("opaque_prefix"),
                    envelope,
                )
            continue
        try:
            if _operand_is_sensitive(p, envelope.get("cwd", ""), rules):
                L.log_info("bash_classify", f"match:{first}")
                return _build_deny_response(tokens, p, envelope)
        except (ValueError, OSError):
            return output.ask_or_allow(
                M.bash_lenient("normalize_failed"),
                envelope,
            )

    if pending_glob_ask is not None:
        return pending_glob_ask
    return output.make_allow()


def _decision_of(result: dict) -> str | None:
    hook = result.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision")


# -- 責務: orchestration -------------------------------------------------


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
        return output.make_deny(M.policy_unavailable("deny"))
    if not rules:
        return output.make_allow()

    # 1. segment split (&& / || / ; / | / \n, quote を尊重)
    #    0.11.0 (F1): hard-stop は segment 単位で再評価する。0.10.0 までは
    #    command 全体に hard-stop が 1 つでもあると early return していたが、
    #    ``cat .env | sed 's/(=)/X/'`` のような複合で sed segment の ``(`` が
    #    原因で全体 ask に倒れ autonomous で素通りしていたため細粒度化。
    #    攻撃シナリオ ``cat <(echo \\(\\)) < .env`` は全 segment hard-stop と
    #    なるため挙動不変 (思想 1 整合)。
    segments = _split_command_on_operators(command)
    if not segments:
        return output.make_allow()

    # 2. 各セグメントを独立に判定。deny 優先、ask は最後に畳む。
    #    hard-stop / shlex 失敗の segment は pending_ask に格納して continue
    #    (他 segment の deny 検出を続ける)。
    pending_ask: dict | None = None
    for seg in segments:
        if _has_hard_stop(seg):
            L.log_info("bash_classify", "hard_stop_lenient")
            if pending_ask is None:
                pending_ask = output.ask_or_allow(
                    M.bash_lenient("hard_stop"),
                    envelope,
                )
            continue

        try:
            tokens = shlex.split(seg, comments=False, posix=True)
        except ValueError as e:
            L.log_info("bash_classify", f"shlex_fail:{type(e).__name__}")
            if pending_ask is None:
                pending_ask = output.ask_or_allow(
                    M.bash_lenient("tokenize_failed"),
                    envelope,
                )
            continue
        tokens = _strip_safe_redirects(tokens)

        result = _analyze_segment(tokens, envelope, rules)
        decision = _decision_of(result)

        if decision == "deny":
            return result
        if decision == "ask" and pending_ask is None:
            pending_ask = result

    if pending_ask is not None:
        return pending_ask
    return output.make_allow()
