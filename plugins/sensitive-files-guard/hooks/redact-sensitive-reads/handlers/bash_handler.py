"""Bash tool 用 handler (0.3.2: 誤爆ガード緩和版)。

0.3.1 までは shell wrapper / hard-stop / 制御構文 / glob を全て ``ask_or_deny`` に
倒していたため、autonomous モード (auto / bypassPermissions) でユーザーが選択した
「日常コマンドを止めない」意図と衝突して誤爆していた。0.3.2 では「機密が確定した
ものは常に deny、確定できない静的解析失敗は default=ask / auto/bypass=allow に分離」
という三態判定 (``ask_or_allow``) に切り替える。

### 判定フロー

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

### Glob 候補列挙 (``_glob_operand_is_sensitive``)

operand が展開しうる literal 候補のうち、既存 ``is_sensitive`` で True を返すものが
1 つでも存在するかで判定する。候補は (a) operand 自身の literal stem、(b) 各 rule の
literal stem を operand glob に fnmatch して match するもの、(c) (operand_stem + rule_stem)
/ (rule_stem + operand_stem) の連結で operand glob に match するもの。これにより
last-match-wins (include/exclude の順序) は既存 ``is_sensitive`` で自然に整合する。
``cat .env*`` は候補 ``.env`` が include 決着で deny、``cat .env.example*`` は全候補が
exclude 決着で allow に倒れる。

### patterns.txt 読込失敗 = 全 mode deny 固定 (0.3.2 変更)

0.3.1 までは ``ask_or_deny`` だったが、autonomous モード対応で ``ask_or_allow`` を
広く使うことになり「policy が無いのに lenient で素通り」を避けるため ``make_deny``
固定に変更。Read/Edit handler 側は変更なし。
"""
from __future__ import annotations

import os
import re
import shlex
from fnmatch import fnmatchcase

from core import logging as L
from core import output
from core.matcher import is_sensitive
from core.patterns import load_patterns
from core.safepath import normalize

# 0.3.1 以降、通常コマンドと未知コマンドを区別せず全て同じ operand 判定に通す
# ため、このセットは **ドキュメント目的** でのみ保持している。処理ロジックからは
# 参照しない。
_SAFE_READ_CMDS = frozenset({
    "cat", "less", "more", "head", "tail", "bat", "view",
    "nl", "tac",
})
_SOURCE_CMDS = frozenset({"source", "."})

# hard-stop: 動的評価 / 入力リダイレクト / グループ化 — 静的に結果を決められない。
# ``<`` は target 抽出を試みた上で残りを ``ask_or_allow`` に倒す。
_HARD_STOP_CHARS = frozenset("$`(){}<\r")

# セグメント分割対象: quote 外でこれらに当たれば区切る。
# 2 文字演算子 (``&&`` ``||``) と 1 文字演算子 (``;`` ``|`` ``\n``)。

# セグメント内に剥がしきれずに残ると ``ask_or_allow`` する metachar セット。
_SEGMENT_RESIDUAL_METACHARS = frozenset("&|<>")

# 安全リダイレクト: ``/dev/null`` / ``/dev/stderr`` / ``/dev/stdout`` / fd 複製。
# 1 トークン化されたもの (``2>/dev/null`` 等) に一致。
_SAFE_REDIRECT_RE = re.compile(
    r"^(?:&|[0-9]+)?>(?:&[0-9]+|/dev/null|/dev/stderr|/dev/stdout)$"
)
# 空白区切りで分割されたリダイレクト前半 (``2>`` + ``/dev/null`` 等) を扱うための受け皿。
_REDIRECT_OP_TOKENS = frozenset({">", "1>", "2>", "&>"})
_SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stderr", "/dev/stdout"})

# 透過 prefix (option 無し限定): ``command`` / ``builtin`` / ``nohup`` の 3 つ。
# ``env`` は assignments + option の扱いがあるため別ハンドル (``_normalize_segment_prefix``)。
_TRANSPARENT_COMMANDS = frozenset({"command", "builtin", "nohup"})

# opaque wrapper: 静的解析不能。``ask_or_allow`` (default=ask, auto/bypass=allow)。
# ``time`` ``!`` ``exec`` は 0.3.2 で _SHELL_KEYWORDS から移動 (shell 文法要素 /
# プロセス置換挙動として opaque 扱いに統一)。
_OPAQUE_WRAPPERS = frozenset({
    "bash", "sh", "zsh", "ksh", "fish", "dash",
    "eval",
    "python", "python3", "node", "ruby", "perl",
    "awk", "sed",
    "xargs", "parallel",
    "sudo", "doas",
    "exec",   # ``exec -a name cmd`` 等プロセス置換系
    "time",   # pipeline 前置 / shell keyword 的挙動
    "!",      # 否定: ``! cat .env`` で後続を実行
})

# シェル予約語 / 制御構文: 第 1 トークンがこれらなら ``ask_or_allow``。
# segment split を挟むと ``do cat .env`` ``then cat .env`` のような制御構文本体
# セグメントが未知コマンド扱いで allow される bypass を塞ぐ。
# ``time`` / ``!`` / ``exec`` は ``_OPAQUE_WRAPPERS`` 側に移動 (0.3.2)。
_SHELL_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "fi",
    "for", "while", "until", "do", "done",
    "case", "esac", "select",
    "function", "coproc",
    "[[", "]]", "[", "]",
})

# glob 文字: operand にこれらが含まれると bash の pathname expansion 対象。
_GLOB_CHARS = frozenset("*?[")

# 環境変数プレフィクス: ``FOO=1 cmd`` 形式の第 1 トークン
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# ``< target`` 形式の target 抽出。heredoc (``<<``), fd dup (``<&N``),
# process substitution (``<(``), 数値 fd 前置 (``0<``) を除外するため、``<`` の
# 直前文字が ``<`` ``&`` 数字 のいずれでもないことを確認する。
# ``\s+`` を必須にすることで ``cat<.env`` のような空白無しケースは取りこぼす
# (これらは hard-stop 後段の ``ask_or_allow`` に倒る)。
_INPUT_REDIRECT_RE = re.compile(r"(?:^|[^<&0-9])<\s+(\S+)")


def _has_hard_stop(command: str) -> bool:
    """動的評価 / 入力リダイレクト / グループ化 chars が含まれるか。"""
    return any(c in _HARD_STOP_CHARS for c in command)


def _split_command_on_operators(command: str) -> list[str]:
    """quote を尊重しつつ ``&&`` ``||`` ``;`` ``|`` ``\\n`` でセグメントに分割。

    クォート内の演算子は区切らない (``echo "a && b"`` は 1 セグメント)。

    ダブルクォート内のバックスラッシュエスケープは Bash 仕様どおり数える:
    直前の連続バックスラッシュが **偶数個** なら ``"`` はエスケープ**されていない**
    (= クォートを閉じる)、**奇数個** ならエスケープされている (= クォート内に留まる)。
    シングルクォートは Bash 仕様上エスケープ不可なので ``'`` 単発で常に閉じる。
    """
    segments: list[str] = []
    buf: list[str] = []
    bs_run = 0
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
            if c == '"' and bs_run % 2 == 0:
                in_double = False
                bs_run = 0
            elif c == "\\":
                bs_run += 1
            else:
                bs_run = 0
            i += 1
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            bs_run = 0
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

    入力リダイレクト (``<``) は hard-stop 側で扱う前提。書き込み先が /dev/null 以外の
    リダイレクト (``> file.txt``) は残して後段で fail-closed (``ask_or_allow``) させる。
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
            if nxt in _SAFE_REDIRECT_TARGETS:
                i += 2
                continue
        out.append(tok)
        i += 1
    return out


def _is_absolute_or_relative_path_exec(token: str) -> bool:
    """``/bin/cat`` / ``./script`` / ``../foo`` のような path 実行か。"""
    return (
        token.startswith("/")
        or token.startswith("./")
        or token.startswith("../")
    )


def _has_glob(token: str) -> bool:
    """operand に shell glob 文字 (``*``, ``?``, ``[``) が含まれるか。"""
    return any(c in _GLOB_CHARS for c in token)


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


def _literalize(pattern: str) -> str:
    """fnmatch glob 文字 (``*`` ``?`` ``[...]``) を除去した最小 literal 表現。

    例: ``.env*`` → ``.env``, ``*.env.*`` → ``.env.``, ``[.]env`` → ``env``,
    ``?ecret*`` → ``ecret``, ``id_rsa*`` → ``id_rsa``。
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c in ("*", "?"):
            i += 1
            continue
        if c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(c)
                i += 1
            else:
                i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _glob_candidates(
    operand: str, rules: list[tuple[str, bool]]
) -> list[str]:
    """operand の具体化候補を既定 rules の stem から生成する。

    生成源:
    1. operand 自身の literal stem (``_literalize(operand)``)
    2. 各 rule の literal stem を operand の glob に fnmatch して match するもの
       (例: op=``id_*`` に rule pt_stem=``id_rsa`` が match → 候補入り)

    ``SFG_CASE_SENSITIVE`` 未設定時は lower 比較で候補生成する (``is_sensitive``
    側の opt-out と整合)。

    Note: プランの初期案には (op_stem+pt_stem) / (pt_stem+op_stem) の **連結候補**
    を加える項目もあったが、``*.log`` に対して ``.env`` rule との連結 ``.env.log``
    が候補化されてしまい、``is_sensitive(".env.log")`` が ``.env.*`` rule で True に
    なる結果 ``cat *.log`` が deny されてしまう問題があった。usability 上 ``*.log``
    は allow しておきたいので、連結候補は採用しない。``cred*.json`` ``id_*``
    ``*.envrc`` 等の交差は (2) の rule pt_stem direct match だけで網羅できる。
    """
    cs = os.environ.get("SFG_CASE_SENSITIVE") == "1"
    op = operand if cs else operand.lower()
    op_stem = _literalize(op)
    candidates: set[str] = {op_stem} if op_stem else set()

    for pattern, _ in rules:
        pat = pattern if cs else pattern.lower()
        pt_stem = _literalize(pat)
        if pt_stem and fnmatchcase(pt_stem, op):
            candidates.add(pt_stem)
    return [c for c in candidates if c]


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


def _segment_has_residual_metachar(tokens: list[str]) -> bool:
    """``_strip_safe_redirects`` 後もセグメントに残っている ``>`` ``&`` ``|`` ``<``
    を持つトークンがあるか。
    """
    for t in tokens:
        if any(c in _SEGMENT_RESIDUAL_METACHARS for c in t):
            return True
    return False


def _find_path_candidates(tokens: list[str]) -> list[str]:
    """第 1 トークン以降から、path 候補を抽出。

    拾う形式:
    - ``--`` より後ろは無条件で path 扱い
    - 非 option トークン (``-`` で始まらない) はそのまま path 候補
    - ``--opt=value`` / ``-o=value`` の ``=`` 以降 (RHS) を候補に追加
    - 短形 option に value が **連結** した形 ``-X<value>`` (``-f.env`` 等) は
      ``tok[2:]`` を候補に追加
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
        if tok.startswith("--"):
            if "=" in tok:
                rhs = tok.split("=", 1)[1]
                if rhs:
                    candidates.append(rhs)
            continue
        if tok.startswith("-"):
            if "=" in tok:
                rhs = tok.split("=", 1)[1]
                if rhs:
                    candidates.append(rhs)
            elif len(tok) > 2:
                candidates.append(tok[2:])
            continue
        candidates.append(tok)
    return candidates


def _extract_input_redirect_targets(command: str) -> list[str]:
    """``< file`` 形式の file (target) を抽出。quote は保たず raw で返す。

    シンプル regex のため shell quote を厳密に処理しないが、抽出に失敗しても
    後段の ``ask_or_allow`` に倒るだけで false allow には傾かない。heredoc
    (``<<EOF``), fd dup (``<&N``), process substitution (``<(...)``), 数値 fd
    前置 (``0<``) は除外される。
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
    (default=ask, auto/bypass=allow)。それ以外 → allow。
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
