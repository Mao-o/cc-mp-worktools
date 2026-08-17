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
- ``failure_kind``: 失敗理由の slug。成功時は空文字。

## failure_kind (0.16.0 で導入)

0.15.0 までは全失敗を ``(None, None)`` に潰していたため、呼出側は
「minimal info が無い」ことしか分からず、reason に理由も next action も
書けなかった (silent degradation)。kind を返すことで:

- ``core.messages._append_minimal_info`` が ``minimal info: unavailable
  (<理由>)`` + kind 別 next action を出せる
- ``handlers.bash_handler`` が kind をログに残せる (原因分布の計測)

| kind | 契機 |
|---|---|
| ``no_operand`` | operand が空 |
| ``normalize_failed`` | ``normalize`` が ``ValueError`` / ``OSError`` |
| ``stat_failed`` | ``classify`` が例外 or ``"error"`` (権限 / IO) |
| ``unresolved`` | ``classify`` == ``"missing"`` (cwd から解決できない) |
| ``not_regular`` | ``classify`` == ``"symlink"`` / ``"special"`` |
| ``open_failed`` | ``open_regular`` が ``OSError`` (``ELOOP`` 等) |
| ``redact_failed`` | 読み込み / redact 中の内部例外 |

kind は ``core.logging`` の detail 文字種ホワイトリストを通る短い slug に
限る (path / basename / 値は絶対に含めない)。

deny 動作の判定境界には影響しない。reason 文字列の情報量だけが拡張される。
"""
from __future__ import annotations

import os

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
_CLASSIFY_FAILURE_KIND: dict[str, str] = {
    "missing": "unresolved",
    "symlink": "not_regular",
    "special": "not_regular",
    "error": "stat_failed",
}


def render_for_bash(
    operand: str,
    cwd: str,
) -> tuple[str | None, dict | None, str]:
    """operand path を Bash deny 用に minimal info 化する。

    Args:
        operand: Bash の token 内で抽出された path 候補 (literal)。
        cwd: ``envelope["cwd"]``。``operand`` が相対パスのとき結合する。

    Returns:
        (reason_text, dotenv_info, failure_kind):
        - ``reason_text``: ``build_reason`` 込みの ``<DATA>`` 包装文字列。
          失敗時 ``None``。
        - ``dotenv_info``: dotenv format のときのみ ``redact_dotenv`` の戻り値
          dict (``keys`` / ``entries`` / ``format``)。それ以外 ``None``。
        - ``failure_kind``: 失敗理由の slug (モジュール docstring の表を参照)。
          成功時は空文字。
    """
    if not operand:
        return (None, None, "no_operand")
    try:
        path = normalize(operand, cwd)
    except (ValueError, OSError):
        return (None, None, "normalize_failed")
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
