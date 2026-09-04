"""Bash deny 時の minimal info レンダリング (0.10.0, E3 / E4 で導入)。

Read handler 同等の流れ (normalize → classify → ``open_regular`` → redact) を
operand path から走らせて、Bash deny の reason に埋め込む文字列を生成する。
dotenv の場合は ``redact_dotenv`` の info dict も併せて返す (E4: grep extraction
の照合用)。

戻り値 (0.16.0 で 3-tuple 化):

- ``reason_text``: ``build_reason`` 込みの ``<DATA untrusted>`` 包装文字列。
  失敗時 ``None``。
- ``dotenv_info``: dotenv format のときのみ ``redact_dotenv`` の戻り値 dict。
  それ以外 ``None``。
- ``status``: 解決経路 / 失敗理由の slug。

## status (0.16.0 で導入)

0.15.0 までは全失敗を ``(None, None)`` に潰していたため、呼出側は
「minimal info が無い」ことしか分からず、reason に理由も next action も
書けなかった (silent degradation)。status を返すことで:

- ``core.messages._append_minimal_info`` が ``minimal info: unavailable
  (<理由>)`` + kind 別 next action を出せる
- ``handlers.bash_handler`` が失敗 kind をログに残せる (原因分布の計測)

| status | 意味 |
|---|---|
| ``""`` | 成功 (``cwd`` 基準で解決) |
| ``project_root`` | 成功。ただし ``cwd`` では解決できず **project root 基準**で解決した候補 |
| ``no_operand`` | operand が空 |
| ``normalize_failed`` | ``normalize`` が ``ValueError`` / ``OSError`` |
| ``stat_failed`` | ``classify`` が例外 or ``"error"`` (権限 / IO) |
| ``unresolved`` | ``classify`` == ``"missing"`` (どの基準でも解決できない) |
| ``not_regular`` | ``classify`` == ``"symlink"`` / ``"directory"`` / ``"special"`` |
| ``open_failed`` | ``open_regular`` が ``OSError`` (``ELOOP`` 等) |
| ``redact_failed`` | 読み込み / redact 中の内部例外 |

status は ``core.logging`` の detail 文字種ホワイトリストを通る短い slug に
限る (path / basename / 値は絶対に含めない)。

## project root フォールバック (0.16.0)

相対 operand が ``cwd`` 基準で ``missing`` になったときだけ、
``_shared.patterns._resolve_project_key`` (``$CLAUDE_PROJECT_DIR`` 優先、
無ければ ``.git`` を上へ探索) が返す project root を基準に **1 回だけ**
再解決する。同一コマンド内の先行 ``cd`` は hook 起動時点の ``cwd`` に反映
されないため、``cd <repo> && grep KEY sub/.env`` のような形で minimal info が
丸ごと落ちていたのを救う経路。

``missing`` 以外の失敗 (symlink / 権限 / 解析失敗) では再解決しない —
``cwd`` 基準でファイルが実在している以上、別の基準を試すのは別ファイルを
読むことになるため。

再解決で得た情報は **別ディレクトリの同名ファイルである可能性**があるので、
``status`` を ``"project_root"`` にして呼出側に候補である旨を明示させる
(``core.messages`` がラベルと注記を付ける)。あわせて 4 番目の戻り値
``resolved_base`` で project root の basename を返し、「どのディレクトリ配下を
読んだのか」を reason から判別できるようにする — ラベルだけでは「候補かも
しれない」としか言えず、実際に取り違えたのかを読み手が確認できないため。

deny 動作の判定境界には影響しない。reason 文字列の情報量だけが拡張される。
"""
from __future__ import annotations

import os
from pathlib import Path

# project root 解決は 0.15.0 で patterns.local.txt の ``[project:...]`` 用に
# 導入した実装を再利用する ($CLAUDE_PROJECT_DIR 優先 → .git 上方探索)。
# private symbol だが plugin 内部での共有であり、解決規則を 2 箇所に持ちたく
# ないため意図的に internal import する。
from _shared.patterns import _resolve_project_key  # noqa: F401
from core.safepath import classify, normalize, open_regular
from redaction.dotenv import format_dotenv, redact_dotenv

# engine.py の private symbol を internal import で再利用する。0.10.0 時点で
# 公開 API への昇格は不要 (本モジュールと engine.py の 2 箇所からのみ参照)。
from redaction.engine import (  # noqa: F401  (private symbol intentional reuse)
    MAX_INLINE_BYTES,
    _detect_format,
    build_reason,
    redact,
    redact_large_file,
)


# ``classify`` の戻り値 → failure_kind。``"regular"`` のみ成功。
#
# 内部バックログ: ``classify`` が 0.28.0 で ``directory`` を ``special`` から
# 分離した (S_ISDIR を明示分岐)。ディレクトリは元々「非通常ファイル」として
# ``not_regular`` に分類されていたので、ここも合わせて ``not_regular`` に
# マップする — 更新しないと ``.get(cls, "stat_failed")`` の既定にフォール
# バックし、ディレクトリを「ファイル状態の確認に失敗した (権限 / IO)」と
# 誤表示する regression になる。
_CLASSIFY_FAILURE_KIND: dict[str, str] = {
    "missing": "unresolved",
    "symlink": "not_regular",
    "directory": "not_regular",
    "special": "not_regular",
    "error": "stat_failed",
}


def render_for_bash(
    operand: str,
    cwd: str,
) -> tuple[str | None, dict | None, str, str]:
    """operand path を Bash deny 用に minimal info 化する。

    Args:
        operand: Bash の token 内で抽出された path 候補 (literal)。
        cwd: ``envelope["cwd"]``。``operand`` が相対パスのとき結合する。

    Returns:
        (reason_text, dotenv_info, status, resolved_base):
        - ``reason_text``: ``build_reason`` 込みの ``<DATA>`` 包装文字列。
          失敗時 ``None``。
        - ``dotenv_info``: dotenv format のときのみ ``redact_dotenv`` の戻り値
          dict (``keys`` / ``entries`` / ``format``)。それ以外 ``None``。
        - ``status``: 解決経路 / 失敗理由の slug (モジュール docstring の表)。
          ``cwd`` 基準で解決できたときは空文字。
        - ``resolved_base``: ``status == "project_root"`` のときだけ、解決基準に
          した project root の **basename 1 要素**。それ以外は空文字。
          reason に載せてどのディレクトリ配下を読んだか判別可能にするためのもので、
          **ログには渡さない** (ログ規則: basename は NG)。
    """
    if not operand:
        return (None, None, "no_operand", "")
    try:
        path = normalize(operand, cwd)
    except (ValueError, OSError):
        return (None, None, "normalize_failed", "")
    reason, info, status = _render_path(path)
    if status != "unresolved" or os.path.isabs(operand):
        # 成功、または missing 以外の失敗 (= cwd 基準でファイルは実在する)。
        # 絶対 operand は基準を変えても同じ path になるので再解決しない。
        return (reason, info, status, "")
    return _render_from_project_root(operand, cwd)


def _render_from_project_root(
    operand: str,
    cwd: str,
) -> tuple[str | None, dict | None, str, str]:
    """相対 operand を project root 基準で 1 回だけ再解決する (0.16.0)。

    先行 ``cd`` で ``cwd`` がずれているケースを救う。成功したら status を
    ``"project_root"`` にして「別ディレクトリの同名ファイルかもしれない候補」
    であることを呼出側に伝え、判別材料として project root の basename
    (1 要素だけ) を返す。再解決も失敗したら元の ``"unresolved"`` を返す
    (呼出側の文言・ログは変わらない)。

    basename だけを返すのは、``matched_operand`` が既に相対 path (ディレクトリ
    要素を含みうる) を出しているのに対し、絶対 path 全体は出さないという既存
    方針 (顧客名・環境名リーク対策) を崩さないため。モデルは自分が意図した
    ディレクトリ名と突き合わせるだけで、取り違えかどうかを判定できる。
    """
    root = _resolve_project_key(cwd)
    if not root:
        return (None, None, "unresolved", "")
    if os.path.normpath(root) == os.path.normpath(cwd or "."):
        # 基準が同じなら再試行しても同じ結果。
        return (None, None, "unresolved", "")
    try:
        alt = normalize(operand, root)
    except (ValueError, OSError):
        return (None, None, "unresolved", "")
    reason, info, status = _render_path(alt)
    if reason is None:
        # 再解決先でも読めなかった。原因は元の cwd 基準の結果に揃える。
        return (None, None, "unresolved", "")
    return (reason, info, "project_root", os.path.basename(os.path.normpath(root)))


def _render_path(path: Path) -> tuple[str | None, dict | None, str]:
    """解決済み絶対 path 1 本を minimal info 化する (基準の違いは呼出側の責務)。"""
    try:
        cls = classify(path)
    except (OSError, ValueError):
        # NUL byte 等で lstat 自体が ValueError を出すケースを吸収。
        return (None, None, "stat_failed")
    if cls != "regular":
        return (None, None, _CLASSIFY_FAILURE_KIND.get(cls, "stat_failed"))
    basename = path.name
    try:
        fd, size = open_regular(path)
    except OSError:
        return (None, None, "open_failed")
    try:
        with os.fdopen(fd, "rb") as f:
            if size > MAX_INLINE_BYTES:
                # 32KB 超は streaming 鍵抽出にフォールバック (Read 同等)。
                # info dict は返さない (jsonlike / yaml 等で format が混在するため)。
                reason = redact_large_file(f, basename)
                return (reason, None, "")
            fmt = _detect_format(basename)
            if fmt == "dotenv":
                # dotenv は info dict も返す。E4 で keys[] を grep_keys と照合
                # するため。
                f.seek(0)
                raw = f.read(MAX_INLINE_BYTES + 1)
                text = raw.decode("utf-8", errors="replace")
                info = redact_dotenv(text)
                body = format_dotenv(info)
                reason = build_reason(basename, fmt, body)
                return (reason, info, "")
            # dotenv 以外は engine.redact を再利用 (json / toml / yaml / opaque)。
            f.seek(0)
            reason = redact(f, basename, size)
            return (reason, None, "")
    except Exception:
        # redact 内部例外を含めて、ここで握り潰して generic reason に降りる。
        # Read handler 側は ask_or_deny に倒すが、Bash 側は deny 確定済みの
        # 流れに乗っているため reason 文字列のみ降格すればよい。
        return (None, None, "redact_failed")
