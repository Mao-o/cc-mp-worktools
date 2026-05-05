"""Redaction engine: format 判定 + dispatch + reason 組み立て。

入力は **file-like な bytes stream** (fd を ``os.fdopen(fd, "rb")`` で wrap した
もの、あるいはテスト用の ``BytesIO``)。path の再 open は行わない (TOCTOU 緩和)。

close 責務は呼出側 (read_handler の ``with`` ブロック) が持つ。engine は
close しない。

出力: ``permissionDecisionReason`` に入れるプレーンテキスト (1-2KB 目標、
ハード上限 3KB)。

0.6.0 で内部 soft-timeout (SIGALRM 1s) を撤廃した。dotenv parse は ReDoS の
経路がほぼなく、外部 hook timeout (2s) で十分なため。Windows 対応の論点は
``__main__._is_unsupported_platform`` 側で別途判断する。

0.9.0 で dotenv minimal info に value status / 生長さ / 識別子型 prefix /
placeholder hint を追加 (思想 2 = block 時は意図を汲んだメッセージを返す)。
詳細は ``redaction/dotenv.py`` および ``redaction/placeholders.py``。
"""
from __future__ import annotations

from typing import IO, Optional

from .dotenv import format_dotenv, redact_dotenv
from .jsonlike import format_jsonlike, redact_jsonlike
from .keyonly_scan import format_keyonly, scan_stream
from .opaque import format_opaque, redact_opaque
from .sanitize import escape_data_tag, sanitize_basename

# DATA 包装の guard marker。固定値にすることで E2E テストが deterministic になる。
DATA_GUARD = "sfg-v1"
from .tomllike import format_toml, redact_toml

# inline 読み込みの上限 (32KB + 1 byte 読んで truncate 判定)
MAX_INLINE_BYTES = 32 * 1024


def _detect_format(basename: str) -> str:
    """basename から format を推定する。確定しないときは 'opaque'。

    厳密 ``endswith`` で判定する。``.json.bak`` / ``.tomlike`` 等は opaque に落ちる。
    dotenv ファミリー (Step 3 以降):

    - ``.env`` / ``.env.*`` (既定)
    - ``foo.env`` など ``.env`` 拡張子ファイル
    - ``.envrc`` / ``*.envrc`` (direnv)
    """
    lower = basename.lower()
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".toml"):
        return "toml"
    if lower.endswith((".yaml", ".yml")):
        return "yaml"
    if lower == ".env" or lower.startswith(".env."):
        return "dotenv"
    if lower.endswith(".env") or lower.endswith(".envrc"):
        return "dotenv"
    return "opaque"


def build_reason(
    basename: str,
    format_name: str,
    body: str,
    extra_notes: Optional[list[str]] = None,
) -> str:
    """``<DATA untrusted>`` 包装 + 本文を組み立てる (Step 4 強化版)。

    - 外殻に固定 guard marker ``sfg-v1`` を付ける (決定的)
    - body と extra_notes を ``escape_data_tag`` で外殻破壊を防止
    - file 行は ``sanitize_basename`` で injection パターン除去済み
    """
    safe_name = sanitize_basename(basename)
    safe_body = escape_data_tag(body)
    lines = [
        f'<DATA untrusted="true" source="redact-hook" guard="{DATA_GUARD}">',
        "NOTE: sanitized data from a sensitive file. Real values are NOT in context.",
        f"file: {safe_name}",
        safe_body,
    ]
    if extra_notes:
        lines.extend(escape_data_tag(n) for n in extra_notes)
    lines.append("</DATA>")
    return "\n".join(lines)


def _read_inline_bytes(f: IO[bytes], limit: int) -> tuple[bytes, bool]:
    """file-like から最大 limit byte 読み、truncate 判定付きで返す。

    seek(0) してから read(limit + 1) する。呼出側が途中まで読んでいても
    先頭から読み直すため、seek 可能な stream が前提。
    """
    try:
        f.seek(0)
    except (OSError, AttributeError):
        # seek 不能な stream (pipe など) は現状非サポート
        pass
    raw = f.read(limit + 1)
    if len(raw) > limit:
        return raw[:limit], True
    return raw, False


def redact(f: IO[bytes], basename: str, size: int, truncated: bool = False) -> str:
    """file-like から読み、format 判定 → redaction → reason を返す。

    Args:
        f: 読み取り可能な bytes stream (fd を wrap したもの)。close しない。
        basename: ファイル basename (sanitize 前)。
        size: ファイル全体の byte 数 (``fstat.st_size``)。redaction path 選択用。
        truncated: 呼出側が既に truncate 判断をしている場合は True。

    Raises:
        redaction engine 内部の例外は握りつぶさない (呼出側が捕捉して
        ``ask_or_deny`` する)。
    """
    fmt = _detect_format(basename)
    extras: list[str] = []

    raw, was_truncated = _read_inline_bytes(f, MAX_INLINE_BYTES)
    if was_truncated or truncated:
        extras.append("note: content was truncated (>32KB); using head-only redaction.")
    text = raw.decode("utf-8", errors="replace")

    if fmt == "dotenv":
        info = redact_dotenv(text)
        body = format_dotenv(info)
        return build_reason(basename, fmt, body, extras)
    if fmt == "json":
        try:
            info = redact_jsonlike(text)
            body = format_jsonlike(info)
            return build_reason(basename, fmt, body, extras)
        except (ValueError, RecursionError):
            pass
    if fmt == "toml":
        try:
            info = redact_toml(text)
            body = format_toml(info)
            return build_reason(basename, fmt, body, extras)
        except Exception:
            pass
    # yaml / opaque / json 失敗 / toml 失敗 → opaque fallback
    info = redact_opaque(text, fmt_hint=fmt)
    body = format_opaque(info)
    return build_reason(basename, fmt, body, extras)


def redact_large_file(f: IO[bytes], basename: str) -> str:
    """32KB を超えるファイルは streaming で scan_stream に流す。

    呼出側は fd を ``os.fdopen(fd, "rb")`` で wrap したものを渡す。seek(0) は
    engine 側で行う。
    """
    fmt = _detect_format(basename)
    try:
        f.seek(0)
    except (OSError, AttributeError):
        pass
    keys, scanned = scan_stream(f)
    body = format_keyonly(keys, scanned, fmt_hint=fmt)
    return build_reason(basename, fmt, body)
