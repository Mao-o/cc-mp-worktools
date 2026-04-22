"""Bash tool 用 handler (0.3.3: 責務境界分解版)。

0.3.3 では 0.3.2 の誤爆ガード緩和 (三態判定 + 前置き正規化 + glob 候補列挙) の
**挙動は維持したまま**、ファイルが肥大化していた責務を ``handlers/bash/`` 配下の
pure helper に切り出した:

- ``handlers/bash/constants.py`` — compile-time 定数 (regex / frozenset)
- ``handlers/bash/segmentation.py`` — quote-aware セグメント分割 / hard-stop 検出
- ``handlers/bash/operand_lexer.py`` — glob 判定 / literalize / path 候補抽出
- ``handlers/bash/redirects.py`` — 安全リダイレクト剥離 / 残留 metachar 判定

このファイルに残しているのは以下 **3 つの責務** に限定:
1. **orchestration** — envelope → command 抽出 → segment 分解 → 判定 → 出力 JSON
2. **plugin ステート依存ロジック** — ``load_patterns`` / ``is_sensitive`` /
   ``normalize`` を呼ぶ処理 (``_operand_is_sensitive`` / ``_glob_operand_is_sensitive``
   / ``_scan_input_redirects`` / ``_analyze_segment``)
3. **test seam** — テストが ``handlers.bash_handler`` 名前空間から import する
   symbol の公開点 (``handle`` / ``_extract_input_redirect_targets`` /
   ``_normalize_segment_prefix`` / ``_operand_is_sensitive`` /
   ``_glob_operand_is_sensitive`` / ``_literalize`` / ``_glob_candidates``)

### 判定フロー (0.3.2 から変更なし)

1. **hard-stop** — ``$`` ``(`` ``)`` ``{`` ``}`` ``<`` バッククォート ``\\r`` を含む
   コマンドは静的解析不能。ただし ``<`` だけは target を抽出して先に operand scan に
   流す (``cat < .env`` を取り逃がさないため)。target 抽出に成功して機密一致なら
   **deny 固定**。それ以外は ``ask_or_allow``。
2. **segment split** — ``&&`` ``||`` ``;`` ``|`` ``\\n`` を quote-aware に分割。
3. **per-segment 解析** — 各セグメントで:
   - shlex.split → 失敗 → ``ask_or_allow``
   - 安全リダイレクト剥離 (``>/dev/null`` / ``2>&1`` 等)
   - **prefix normalize (限定版)** — env prefix (``FOO=1``) / ``env`` (option 無し
     のみ) / ``command`` (option 無しのみ) / ``builtin`` / ``nohup``、および
     abs/rel path で basename が上記 4 つに該当する場合のみ剥がす。それ以外
     (opaque wrapper, 任意 path exec, ``env -i``, etc.) は ``ask_or_allow``
   - 残留 metachar (``>`` ``&`` 等) → ``ask_or_allow``
   - shell keyword (``if``/``for``/``do`` 等) → ``ask_or_allow``
   - operand scan: 各 path 候補について
     - glob 含む → ``_glob_operand_is_sensitive`` (既定 rules への候補列挙) で
       True なら **deny 固定**、False なら allow
     - literal → ``_operand_is_sensitive`` (basename + URI/VCS pathspec 分割) で
       True なら **deny 固定**、False なら allow
4. **集約** — deny > ask > allow。

### patterns.txt 読込失敗 = 全 mode deny 固定 (0.3.2 から変更なし)

0.3.1 までは ``ask_or_deny`` だったが、autonomous モード対応で ``ask_or_allow`` を
広く使うことになり「policy が無いのに lenient で素通り」を避けるため ``make_deny``
固定に変更。Read/Edit handler 側は変更なし。
"""
from __future__ import annotations

import shlex

from core import logging as L
from core import output
from core.matcher import is_sensitive
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
    _INPUT_REDIRECT_RE,
    _OPAQUE_WRAPPERS,
    _REDIRECT_OP_TOKENS,
    _SAFE_READ_CMDS,
    _SAFE_REDIRECT_RE,
    _SAFE_REDIRECT_TARGETS,
    _SEGMENT_RESIDUAL_METACHARS,
    _SHELL_KEYWORDS,
    _SOURCE_CMDS,
    _TRANSPARENT_COMMANDS,
)
from handlers.bash.operand_lexer import (  # noqa: F401
    _find_path_candidates,
    _glob_candidates,
    _has_glob,
    _is_absolute_or_relative_path_exec,
    _literalize,
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


# -- 責務: test seam / plugin ステート依存ロジック ------------------------


def _normalize_segment_prefix(tokens: list[str]) -> list[str] | None:
    """セグメントの先頭から既知 prefix を剥がす。opaque wrapper 検出時は ``None``。

    剥がす対象 (autonomous モードでも実コマンドが ``cat .env`` 等と確定する形):
    - ``FOO=1`` 形式の env prefix (任意個)
    - ``env [ASSIGNMENTS...]`` (option 無しのみ)
    - ``command`` (option 無しのみ)
    - ``builtin``
    - ``nohup``
    - 上記の連鎖 (例: ``nohup command cat``)
    - 絶対/相対パスでも basename が上記 4 つ (``env``/``command``/``builtin``/``nohup``)
      に該当するもの (例: ``/usr/bin/env``)

    opaque (None) 扱い:
    - ``bash``/``sh``/``zsh``/``eval``/``python``/``sudo``/``awk``/``sed``/``xargs``/
      ``time``/``exec``/``!`` 等の ``_OPAQUE_WRAPPERS``
    - ``env`` / ``command`` のオプション付き呼び出し (``env -i``, ``env -u NAME``,
      ``env --``, ``command -p``, ``command --``)
    - 上記以外の絶対/相対パス実行 (``/bin/cat``, ``./script``)

    剥がし切って残ったトークン列を返す。空リストになることもある。

    この関数は ``test_prefix_normalize.py`` が直接 import するため patch seam。
    """
    result = list(tokens)
    while result:
        first = result[0]

        # 環境変数 prefix: FOO=1
        if _ENV_PREFIX_RE.match(first):
            result = result[1:]
            continue

        # 絶対/相対パス: basename が透過対象 (env / command / builtin / nohup) の
        # ときのみ剥がして basename に置換し、ループを継続。それ以外は opaque。
        if _is_absolute_or_relative_path_exec(first):
            basename = first.rsplit("/", 1)[-1]
            if not basename:
                return None
            if basename in _TRANSPARENT_COMMANDS or basename == "env":
                result = [basename] + result[1:]
                continue
            return None

        # env コマンド: env [ASSIGNMENTS...] cmd args
        # option (-i, -u NAME, --) を持つと semantics が変わるため opaque。
        if first == "env":
            rest = result[1:]
            while rest and _ENV_PREFIX_RE.match(rest[0]):
                rest = rest[1:]
            if rest and rest[0].startswith("-"):
                return None
            result = rest
            continue

        # command: command [-p|-v|-V|--] cmd args → option 付きは opaque
        if first == "command":
            rest = result[1:]
            if rest and rest[0].startswith("-"):
                return None
            result = rest
            continue

        # builtin / nohup: 先頭 1 つ剥がして継続
        if first in _TRANSPARENT_COMMANDS:
            result = result[1:]
            continue

        # opaque wrapper
        if first in _OPAQUE_WRAPPERS:
            return None

        # 通常 command (これ以上は剥がさない)
        break

    return result


def _glob_operand_is_sensitive(
    operand: str, rules: list[tuple[str, bool]]
) -> bool:
    """operand (glob 含む可) の具体化候補のうち ``is_sensitive`` が True を返すものが
    1 つでも存在するか。

    include/exclude の last-match-wins は ``is_sensitive`` 側が担保する。
    ``.env*`` は候補 ``.env`` が include 決着で True、``.env.example*`` は全候補が
    exclude 決着 (``!*.example``) で False に倒れる。
    """
    for cand in _glob_candidates(operand, rules):
        if is_sensitive(cand, rules):
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


def _extract_input_redirect_targets(command: str) -> list[str]:
    """``< file`` 形式の file (target) を抽出。quote は保たず raw で返す。

    シンプル regex のため shell quote を厳密に処理しないが、抽出に失敗しても
    後段の ``ask_or_allow`` に倒るだけで false allow には傾かない。heredoc
    (``<<EOF``), fd dup (``<&N``), process substitution (``<(...)``), 数値 fd
    前置 (``0<``) は除外される。

    この関数は ``test_input_redirect.py`` が直接 import するため patch seam。
    """
    return _INPUT_REDIRECT_RE.findall(command)


def _scan_input_redirects(
    command: str, cwd: str, rules: list[tuple[str, bool]]
) -> dict | None:
    """hard-stop コマンド内の ``< target`` を抽出し、機密一致を ``make_deny`` で返す。

    機密一致が見つかれば deny dict、見つからなければ ``None``。
    """
    for raw_target in _extract_input_redirect_targets(command):
        if _has_glob(raw_target):
            if _glob_operand_is_sensitive(raw_target, rules):
                L.log_info("bash_classify", "input_redirect_glob_match")
                return output.make_deny(
                    f"Bash 入力リダイレクト先 ({raw_target}) が機密パターンに "
                    "一致する候補を含みます。値が LLM コンテキストに露出する可能性が "
                    "あるため block します。"
                )
            continue
        try:
            if _operand_is_sensitive(raw_target, cwd, rules):
                L.log_info("bash_classify", "input_redirect_match")
                return output.make_deny(
                    f"Bash 入力リダイレクト先 ({raw_target}) が機密パターンに "
                    "一致します。値が LLM コンテキストに露出する可能性があるため "
                    "block します。"
                )
        except (ValueError, OSError):
            continue
    return None


def _analyze_segment(
    tokens: list[str],
    envelope: dict,
    rules: list[tuple[str, bool]],
) -> dict:
    """1 セグメント分の token 列を判定して hook 出力 dict を返す。

    機密 path 一致 → ``make_deny`` 固定。判定不能 → ``ask_or_allow``
    (default=ask, auto/bypass/plan=allow)。それ以外 → allow。
    """
    if not tokens:
        return output.make_allow()

    normalized = _normalize_segment_prefix(tokens)
    if normalized is None:
        L.log_info("bash_classify", "opaque_prefix_lenient")
        return output.ask_or_allow(
            "Bash コマンドが静的解析対象外の wrapper / インタプリタ / 任意 path "
            "実行で始まっています。",
            envelope,
        )
    if not normalized:
        return output.make_allow()

    if _segment_has_residual_metachar(normalized):
        L.log_info("bash_classify", "segment_residual_metachar_lenient")
        return output.ask_or_allow(
            "Bash セグメント内に解析対象外のリダイレクト / metachar が残っています。",
            envelope,
        )

    first = normalized[0]
    if first in _SHELL_KEYWORDS:
        L.log_info("bash_classify", f"shell_keyword_lenient:{first}")
        return output.ask_or_allow(
            f"シェル予約語 / 制御構文 ({first}) で始まるセグメントは静的解析対象外です。",
            envelope,
        )

    paths = _find_path_candidates(normalized)
    for p in paths:
        if not p:
            continue
        if _has_glob(p):
            if _glob_operand_is_sensitive(p, rules):
                L.log_info("bash_classify", f"glob_match:{first}")
                return output.make_deny(
                    f"Bash コマンド ({first}) の operand glob ({p}) が既定の機密 "
                    "パターンと交差します。値が LLM コンテキストに露出する可能性が "
                    "あるため block します。許可したい場合は patterns.local.txt に "
                    "`!<basename>` を追加してください。"
                )
            continue
        try:
            if _operand_is_sensitive(p, envelope.get("cwd", ""), rules):
                L.log_info("bash_classify", f"match:{first}")
                return output.make_deny(
                    f"Bash コマンド ({first}) の operand に機密パターンに一致する "
                    "ファイルが含まれています。処理内容に関わらず値が LLM コンテキスト "
                    "に露出する可能性があるため block します。許可したい場合は "
                    "patterns.local.txt に `!<basename>` を追加してください。"
                )
        except (ValueError, OSError):
            return output.ask_or_allow(
                "Bash コマンド内のパス正規化に失敗しました。",
                envelope,
            )

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
        return output.make_deny(
            "sensitive-files-guard: ガードポリシー (patterns.txt) が読み込めない "
            "ため全 Bash コマンドを block します。パッケージング / 設定を確認して "
            "ください。"
        )
    if not rules:
        return output.make_allow()

    cwd = envelope.get("cwd", "")

    # 1. hard-stop: 動的評価 / 入力リダイレクト / グループ化
    if _has_hard_stop(command):
        # ``< target`` 形式は target を抽出して先に operand scan に流す
        deny = _scan_input_redirects(command, cwd, rules)
        if deny is not None:
            return deny
        L.log_info("bash_classify", "hard_stop_lenient")
        return output.ask_or_allow(
            "Bash コマンドに動的展開 / heredoc / process 置換 / グループ化 "
            "($, バッククォート, $(...), <<, <(...), (), {}) が含まれています。",
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
            return output.ask_or_allow(
                "Bash コマンドの tokenize に失敗しました。",
                envelope,
            )
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
