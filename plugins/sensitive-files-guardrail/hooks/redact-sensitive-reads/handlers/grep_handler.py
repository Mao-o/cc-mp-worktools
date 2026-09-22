"""Grep tool 用 handler (0.34.0、最小対応)。

## なぜ Grep を対象に入れるか

``output_mode: "content"`` の Grep は**一致行をそのまま返す**ので、機密
ファイルを指した Grep は値の一部をコンテキストに載せる。0.33.x までの
「Grep / Glob は対象外」という整理は、シェルの動的展開と書き込み防止
(sandbox) を理由にしていたが、どちらも Grep には当てはまらない
(Grep は shell を介さず、名前付きの path / glob を受け取るだけ)。

ただし **macOS / Linux の既定では Grep ツールは tool set に載らない**
(公式 tools reference 逐語: Claude は Bash の ``find`` / ``grep`` を使う)。
Grep が実際に呼ばれるのは Windows 既定 / ``--tools`` ``--allowedTools`` で
明示指名した場合 / Bash が deny されている場合 / subagent の tools に Grep が
あって Bash が無い場合。つまりこの handler は「第一級の読み取り経路を塞ぐ」
ものではなく、**Bash 経由の ``grep`` と判定を揃えるための対称性**の対応。

## 判定 (Bash handler と同じ基準)

- ``tool_input.path`` が**ファイル**を指し機密パターンに一致 → **deny**
- ``tool_input.glob`` が機密名 (literal) / dotenv stem に展開されうる glob
  (``_glob_operand_is_dotenv_match``) に一致 → **deny**
- ``tool_input.glob`` がそれ以外の**ワイルドカードを含む** glob
  (``*.pem`` / ``id_rsa*`` / ``*.env`` / ``*.py``) → **``ask_or_allow``**
  (default=ask / autonomous=allow)。Bash の ``glob_uncertain`` と同じ三態
- それ以外 (``path`` 未指定 / ディレクトリ / 非機密 literal) → **allow**
- 内部例外は ``__main__`` の catch-all が ``ask_or_deny`` に倒す

**``ask`` を作らないのはディレクトリ走査だけ** (0.34.0 のマージ前レビュー
P2-3、ユーザー判定): ``path`` がディレクトリ / 未指定のときは、その配下の
機密ファイルの行が結果に混ざりうるが **allow** にする。Bash の
``grep -r X .`` と同じ既知の限界として扱う (ここで deny / ask すると
``python -m venv .env`` のように機密名のディレクトリがある構成で全検索が
止まる)。``output_mode`` は判定に使わない — ``files_with_matches`` でも
ファイル名は漏れるが、それは「basename は漏れる」という既存の非目的の範囲。

### glob の三態を Bash に揃えた理由と、揃え方の限界

0.34.0 の初版は「``ask`` は作らない」方針で wildcard glob を全部 allow に
していたが、それだと **Bash より緩い**: 同じ意図の ``grep X *.pem`` は Bash
handler で ``ask_or_allow`` になる。しかも Grep が実際に呼ばれる条件の 1 つは
「Bash が deny されている」なので、**Bash の ask が効かない状況でだけ Grep が
使われ、そこで Grep はより緩い**という順序になっていた。

揃え先は Bash の **positional operand** (``grep X *.py`` = ask)。Grep の
``glob`` は「検索対象を絞る filter」なので Bash での真の同型は
``grep -rn X --include='*.py' .`` (実測 allow) とも読めるが、判定境界は
positional 側 (= 過剰 ask 側) に倒した。``_glob_operand_is_dotenv_match`` /
``_has_glob`` をそのまま再利用するので、判定表は MATRIX の Bash glob 行と
1:1 で並ぶ。

### ブレース展開 (0.34.0 のマージ前レビュー P2-2)

Claude Code の ``glob`` は ``"*.{ts,tsx}"`` のようなブレース alternation を
解釈する。Bash 側には**同等の扱いが無い** (``{`` は hard-stop metachar として
``ask_or_allow`` に倒れるだけで、展開はしない) ため、Grep 側にだけ
「``{a,b}`` を展開して**各分岐で**判定する」を足す。結論は分岐の中で最も強い
ものを採る (deny > ask > allow) ので、**deny 方向にしか動かない**。
展開できない形 (閉じていない / 分岐数・ネストが上限超) は wildcard 扱いのまま
``ask_or_allow`` に落とす。

## tool_input の実形 (CLI 2.1.278 実測)

``{"pattern": ..., "path": ..., "glob": ..., "output_mode": ..., "-i": ...,
"-n": ..., "type": ..., "head_limit": ...}``。``path`` は **cwd 相対でも
絶対でも来る**し、ファイルでもディレクトリでも来る。``path`` / ``glob`` /
``output_mode`` は省略時 **key ごと存在しない**。この handler が見るのは
``path`` と ``glob`` だけで、他のキーは無視する。
"""
from __future__ import annotations

from pathlib import PurePath

from core import logging as L
from core import messages as M
from core import output
from _shared.matcher import is_sensitive, root_relative
from _shared.patterns import resolve_project_root
from core.patterns import load_patterns
from core.safepath import classify, normalize


# ブレース展開の上限。``{a,b}{c,d}…`` は直積なので、上限を置かないと 2 秒の
# hook 予算を展開だけで使い切りうる。上限を超えた形は「判定できない wildcard」
# として ``ask_or_allow`` に落ちる (= 展開の失敗は緩い側ではなく ask 側)。
_MAX_BRACE_GROUPS = 8
_MAX_BRACE_BRANCHES = 64

# Grep の ``glob`` でワイルドカードと見なす文字。Bash の ``_GLOB_CHARS``
# (``*?[``) に ``{`` を足したもの — Claude Code の Grep は ``"*.{ts,tsx}"`` の
# ブレース alternation を解釈するため (Bash は ``{`` を hard-stop として扱う
# ので ``_GLOB_CHARS`` には入っていない)。
_BRACE_CHAR = "{"


def _split_top_level(body: str) -> list[str]:
    """ブレース本体を**最上位の**カンマで分ける (ネストした ``{}`` は保つ)。"""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def _expand_braces(glob: str) -> list[str] | None:
    """``{a,b}`` を直積展開した文字列のリスト。展開できなければ ``None``。

    ``None`` になるのは「``{`` の数が上限超」「閉じていない」「分岐数が上限超」。
    呼出側はこれを **allow ではなく ``ask_or_allow``** として扱う。
    """
    if _BRACE_CHAR not in glob:
        return [glob]
    if glob.count(_BRACE_CHAR) > _MAX_BRACE_GROUPS:
        return None
    i = glob.index(_BRACE_CHAR)
    depth = 0
    close = -1
    for j in range(i, len(glob)):
        if glob[j] == "{":
            depth += 1
        elif glob[j] == "}":
            depth -= 1
            if depth == 0:
                close = j
                break
    if close < 0:
        return None  # 閉じていないブレース
    prefix, body, suffix = glob[:i], glob[i + 1 : close], glob[close + 1 :]
    out: list[str] = []
    for alt in _split_top_level(body):
        sub = _expand_braces(prefix + alt + suffix)
        if sub is None:
            return None
        out.extend(sub)
        if len(out) > _MAX_BRACE_BRANCHES:
            return None
    return out


def _glob_verdict(glob: str, rules: list[tuple[str, bool]]) -> str:
    """``tool_input.glob`` の判定を ``"deny"`` / ``"pause"`` / ``"allow"`` で返す。

    分岐ごとに Bash operand と**同じ規則**を当て、最も強い結論を採る:

    - ``_glob_operand_is_dotenv_match``: dotenv stem (``.env`` / ``.envrc``)
      に shell の pathname expansion で展開されうる → ``deny``
    - ワイルドカードを含むそれ以外 (``*.pem`` / ``id_rsa*`` / ``*.env`` /
      ``*.py``) → ``pause`` (Bash の ``glob_uncertain`` と同じ三態)
    - ワイルドカードを含まない literal → basename として ``is_sensitive``
      (一致すれば ``deny``、しなければ ``allow``)

    ``{`` を含む glob は、展開結果が全部 literal 非機密でも ``pause`` 止まり
    (``{`` 自体をワイルドカードと見なす)。展開は ``deny`` 方向にしか動かさない。

    ``dotglob`` は渡さない: Grep の ``glob`` は shell ではなく Claude Code
    側が解釈するので、``shopt -s dotglob`` に相当する状態が無い。
    """
    # 循環 import を避けるため関数内 import (operand_lexer は bash handler の
    # サブモジュールだが、glob の意味論はツール非依存なのでそのまま使える)
    from handlers.bash.operand_lexer import (
        _glob_operand_is_dotenv_match,
        _has_glob,
    )

    branches = _expand_braces(glob)
    if branches is None:
        # 展開できない形は「判定できない wildcard」として ask に落とす
        return "pause"

    # ``{`` 自体をワイルドカードと見なす: 展開結果が全部 literal 非機密でも
    # ``ask`` 止まりにする (Bash は ``cat {a,b}`` を hard-stop で ask にする
    # ので結論が揃う)。展開は **deny 方向にしか動かさない**。
    verdict = "pause" if _BRACE_CHAR in glob else "allow"
    for branch in branches:
        if _glob_operand_is_dotenv_match(branch):
            return "deny"
        if _has_glob(branch):
            verdict = "pause"
            continue
        # literal glob は path とは限らない文字列なので basename だけで判定
        # する (Bash operand と同じ ``parts=False`` 相当。path 形 rule も
        # 基準を確定できないので評価しない)
        if is_sensitive(PurePath(branch), rules, parts=False, root=None):
            return "deny"
    return verdict


def handle(envelope: dict) -> dict:
    """Grep tool の PreToolUse envelope を受け取り、hook 出力 dict を返す。

    envelope 例:
        {"tool_input": {"pattern": "KEY", "path": ".env"}, "cwd": "...",
         "permission_mode": "default"}
    """
    tool_input = envelope.get("tool_input") or {}
    raw_path = tool_input.get("path")
    raw_glob = tool_input.get("glob")
    cwd = envelope.get("cwd", "")

    has_path = isinstance(raw_path, str) and raw_path
    has_glob = isinstance(raw_glob, str) and raw_glob
    if not has_path and not has_glob:
        return output.make_allow()

    try:
        rules = load_patterns(cwd=cwd)
    except (FileNotFoundError, OSError) as e:
        L.log_error("patterns_unavailable", type(e).__name__)
        return output.ask_or_deny(M.policy_unavailable("pause", "Grep"), envelope)

    if not rules:
        return output.make_allow()

    root = resolve_project_root(cwd)

    if has_path:
        try:
            path = normalize(raw_path, cwd)
        except (ValueError, OSError) as e:
            L.log_error("normalize_failed", type(e).__name__)
            return output.ask_or_deny(M.read_ask("normalize_failed"), envelope)

        if is_sensitive(path, rules, root=root):
            cls = classify(path)
            L.log_info("grep_classify", cls)
            if cls == "symlink":
                # Read handler と同じ扱い (リンク先が意図した参照か判らない)
                return output.ask_or_deny(M.read_ask("symlink"), envelope)
            if cls == "special":
                return output.ask_or_deny(M.read_ask("special"), envelope)
            if cls == "error":
                return output.ask_or_deny(M.read_ask("io_error"), envelope)
            if cls == "regular":
                L.log_info("grep_classify", "path_match")
                return output.make_deny(
                    M.grep_deny(
                        "path", raw_path, relpath=root_relative(path, root) or ""
                    )
                )
            # directory / missing: ファイルの内容を直接返す指定ではないので
            # allow (モジュール docstring の「ディレクトリ走査」節)。

    if has_glob:
        verdict = _glob_verdict(raw_glob, rules)
        if verdict == "deny":
            L.log_info("grep_classify", "glob_match")
            return output.make_deny(M.grep_deny("glob", raw_glob))
        if verdict == "pause":
            L.log_info("grep_classify", "glob_uncertain")
            # lenient allow の開示 note は Bash と**同じ絞り**を通す
            # (``core/output.py`` の「載せる対象は全件ではない」契約)。
            # glob だけを渡すので、``*.pem`` には載り ``*.py`` には載らない。
            from handlers.bash_handler import _gate_lenient_note

            return _gate_lenient_note(
                output.ask_or_allow(M.grep_pause(raw_glob), envelope),
                raw_glob,
                rules,
            )

    return output.make_allow()
