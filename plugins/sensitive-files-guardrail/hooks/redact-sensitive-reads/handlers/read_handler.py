"""Read tool 用 handler。

normalize → classify → (O_NOFOLLOW で fd open) → redact → deny/ask のパイプライン。
path の再 open は行わず、``open_regular`` で得た fd をそのまま engine に渡すことで
TOCTOU を緩和する。全ての内部例外は fail-closed で ``ask_or_deny`` に倒す。
"""
from __future__ import annotations

import os

from core import logging as L
from core import messages as M
from core import output
from _shared.matcher import is_sensitive
from _shared.npmrc import (
    MAX_NPMRC_BYTES,
    bytes_auth_scan,
    is_npmrc_basename,
)
from _shared.patterns import resolve_project_root
from core.patterns import load_patterns
from core.safepath import classify, normalize, open_regular
from redaction.engine import MAX_INLINE_BYTES, redact, redact_large_file


def _npmrc_gate(f, size: int) -> tuple[bool, int, list[str]]:
    """開いた ``.npmrc`` の fd が block 対象か (0.34.0)。

    戻り値は ``(block するか, 認証行の件数, キー名)``。件数 / キー名は
    deny reason の根拠 1 行 (``M.npmrc_auth_prefix``) にだけ使い、**値は
    一切持ち出さない**。fail-closed 経路 (上限超 / decode 不能) は
    ``(True, 0, [])`` — 認証行を見つけたわけではないため。

    ``size`` は ``open_regular`` の ``fstat`` 由来。上限 (``MAX_NPMRC_BYTES``)
    を超えるものは中身を見ずに block (fail-closed) — ``.npmrc`` は通常
    1KB 未満なので、それより 2 桁大きいファイルは想定外の形として扱う。

    読み終わったら ``seek(0)`` で巻き戻す。block する場合は同じ fd を
    ``redact`` が先頭から読み直すため (``redaction/engine`` も冒頭で
    ``seek(0)`` するが、ここで戻しておけば seek 不能な stream でも
    「ゲートが先頭を食った」状態にならない)。

    ``OSError`` は握らず呼出側 (``handle`` の ``except Exception``) に渡す —
    そちらは ``ask_or_deny`` (fail-closed) なので方向は同じ。
    """
    if size > MAX_NPMRC_BYTES:
        return True, 0, []
    f.seek(0)
    raw = f.read(MAX_NPMRC_BYTES + 1)
    f.seek(0)
    if len(raw) > MAX_NPMRC_BYTES:
        return True, 0, []
    return bytes_auth_scan(raw)


def handle(envelope: dict) -> dict:
    """Read tool の PreToolUse envelope を受け取り、hook 出力 dict を返す。

    envelope 例:
        {"tool_input": {"file_path": "..."}, "cwd": "...",
         "permission_mode": "bypassPermissions" | ...}
    """
    tool_input = envelope.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    cwd = envelope.get("cwd", "")

    if not isinstance(raw_path, str) or not raw_path:
        return output.make_allow()

    try:
        rules = load_patterns(cwd=cwd)
    except (FileNotFoundError, OSError) as e:
        L.log_error("patterns_unavailable", type(e).__name__)
        return output.ask_or_deny(M.policy_unavailable("pause"), envelope)

    if not rules:
        return output.make_allow()

    try:
        path = normalize(raw_path, cwd)
    except (ValueError, OSError) as e:
        L.log_error("normalize_failed", type(e).__name__)
        return output.ask_or_deny(M.read_ask("normalize_failed"), envelope)

    basename = path.name
    # root は [project:] セクションの key と同じ値 (path 形 rule の基準、0.24.0)
    if not is_sensitive(path, rules, root=resolve_project_root(cwd)):
        return output.make_allow()

    cls = classify(path)
    L.log_info("classify", cls)

    if cls == "symlink":
        return output.ask_or_deny(M.read_ask("symlink"), envelope)
    if cls == "directory":
        return output.ask_or_deny(M.read_ask("directory"), envelope)
    if cls == "special":
        return output.ask_or_deny(M.read_ask("special"), envelope)
    if cls == "missing":
        return output.make_allow()
    if cls == "error":
        return output.ask_or_deny(M.read_ask("io_error"), envelope)

    try:
        fd, size = open_regular(path)
    except OSError as e:
        L.log_error("open_regular_failed", type(e).__name__)
        return output.ask_or_deny(M.read_ask("open_failed"), envelope)

    npmrc_prefix = ""
    try:
        with os.fdopen(fd, "rb") as f:
            # 0.34.0 (内部バックログ): ``.npmrc`` だけは **内容ゲート**を通す。
            # 認証らしい行 (``//registry…:_authToken`` / ``_auth`` /
            # ``keyfile`` / URL 埋め込み credential など、定義は
            # ``_shared/npmrc.py`` のモジュール docstring) が 1 行も無ければ
            # ただの pnpm / npm 設定ファイルなので allow に倒す。判定できない
            # 事情 (読めない / decode 不能 / 上限超) は deny 側 (fail-closed)。
            # 既定 patterns からは外さない — 名前は候補に残し、開いた中身で
            # 確定する。
            if is_npmrc_basename(basename):
                blocked, auth_count, auth_keys = _npmrc_gate(f, size)
                if not blocked:
                    L.log_info("npmrc_gate", "no_auth_line")
                    return output.make_allow()
                # deny の根拠 (認証行の件数とキー名) を reason の先頭に足す。
                # 既存 minimal info は ini の keys-only scan なので認証キーを
                # 拾わず、「なぜ block したか」が reason から読めないため。
                npmrc_prefix = M.npmrc_auth_prefix(auth_count, auth_keys)
            if size > MAX_INLINE_BYTES:
                reason = redact_large_file(f, basename)
            else:
                reason = redact(f, basename, size)
    except Exception as e:
        L.log_error("redaction_failed", type(e).__name__)
        return output.ask_or_deny(M.read_ask("redaction_failed"), envelope)

    # 0.34.0: ``.npmrc`` の根拠 1 行は **予算を引いてから** 前置する。
    # ``fit_read_reason`` は <DATA> ブロック 1 個を前提に折り畳むので、
    # 先に連結すると prefix が解析対象に混ざる。また最後の盲目 cut
    # (``output._truncate``) は末尾から削るので、prefix を先頭に置けば
    # deny の根拠そのものが切り落とされることはない。
    if npmrc_prefix:
        budget = output.MAX_REASON_BYTES - len(npmrc_prefix.encode("utf-8"))
        return output.make_deny(npmrc_prefix + M.fit_read_reason(reason, budget))

    # 0.26.0: reason (<DATA> 包装の 1 ブロック) が 3KB 予算を超える
    # 場合、以前は core.output._truncate の盲目 byte cut だけに頼っており、
    # 鍵数の多い dotenv / json / yaml で閉じタグと末尾 note が key 行の途中で
    # 失われていた。M.fit_read_reason が閉じタグ・末尾 note を保護したまま
    # 折り畳む (収まらなければ入力をそのまま返し、_truncate が最終防御を担う
    # ので verdict には影響しない)。
    return output.make_deny(M.fit_read_reason(reason))
