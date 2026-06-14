"""Bash deny 時の minimal info レンダリング (0.10.0, E3 / E4 で導入)。

Read handler 同等の流れ (normalize → classify → ``open_regular`` → redact) を
operand path から走らせて、Bash deny の reason に埋め込む文字列を生成する。
dotenv の場合は ``redact_dotenv`` の info dict も併せて返す (E4: grep extraction
の照合用)。

戻り値:

- ``reason_text``: ``build_reason`` 込みの ``<DATA untrusted>`` 包装文字列。
  失敗時 ``None``。
- ``dotenv_info``: dotenv format のときのみ ``redact_dotenv`` の戻り値 dict。
  それ以外 ``None``。

失敗の典型ケース (どれも ``(None, None)`` を返し、Bash 側 deny は generic
reason に降りる):

- normalize 失敗 (``ValueError`` / ``OSError``)
- regular file ではない (symlink / special / missing / error)
- ``open_regular`` 失敗 (権限 / O_NOFOLLOW で symlink 検知 = ``ELOOP`` 等)
- redact 失敗 (内部例外を握り潰し)

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


def render_for_bash(
    operand: str,
    cwd: str,
) -> tuple[str | None, dict | None]:
    """operand path を Bash deny 用に minimal info 化する。

    Args:
        operand: Bash の token 内で抽出された path 候補 (literal)。
        cwd: ``envelope["cwd"]``。``operand`` が相対パスのとき結合する。

    Returns:
        (reason_text, dotenv_info):
        - ``reason_text``: ``build_reason`` 込みの ``<DATA>`` 包装文字列。
          失敗時 ``None``。
        - ``dotenv_info``: dotenv format のときのみ ``redact_dotenv`` の戻り値
          dict (``keys`` / ``entries`` / ``format``)。それ以外 ``None``。
    """
    if not operand:
        return (None, None)
    try:
        path = normalize(operand, cwd)
    except (ValueError, OSError):
        return (None, None)
    try:
        cls = classify(path)
    except (OSError, ValueError):
        # NUL byte 等で lstat 自体が ValueError を出すケースを吸収。
        return (None, None)
    if cls != "regular":
        return (None, None)
    basename = path.name
    try:
        fd, size = open_regular(path)
    except OSError:
        return (None, None)
    try:
        with os.fdopen(fd, "rb") as f:
            if size > MAX_INLINE_BYTES:
                # 32KB 超は streaming 鍵抽出にフォールバック (Read 同等)。
                # info dict は返さない (jsonlike / yaml 等で format が混在するため)。
                reason = redact_large_file(f, basename)
                return (reason, None)
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
                return (reason, info)
            # dotenv 以外は engine.redact を再利用 (json / toml / yaml / opaque)。
            f.seek(0)
            reason = redact(f, basename, size)
            return (reason, None)
    except Exception:
        # redact 内部例外を含めて、ここで握り潰して generic reason に降りる。
        # Read handler 側は ask_or_deny に倒すが、Bash 側は deny 確定済みの
        # 流れに乗っているため reason 文字列のみ降格すればよい。
        return (None, None)
