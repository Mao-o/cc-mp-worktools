#!/usr/bin/env python3
"""唯一の I/O 副作用: session_id ベースの debounce store。

同一セッション内で同一ファイル×同一 tier への再通知を防ぐ。判定
(should_emit) と予約 (state 更新) を ``try_reserve_emit`` 1 回の呼び出し・
1 回のロック区間に統合し、check-then-act 分割による TOCTOU race を避ける
(``external-ai-assist/hooks/exitplan-review/__main__.py::reserve_slot`` の
同型パターンを踏襲)。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from judge import TIER_ORDER

try:
    import fcntl

    HAVE_FLOCK = True
except ImportError:  # Windows
    HAVE_FLOCK = False


def _base_dir() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / "file-split-advisor"


def _state_path(session_id: str) -> Path:
    # session_id を素のファイル名にしない: "/" や ".." が紛れ込むと TMPDIR 外への
    # 書き込みや例外につながりうるため、固定長・英数字のみのハッシュに変換する。
    hashed = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return _base_dir() / f"{hashed}.json"


def tier_rank(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return 0


def _parse(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def try_reserve_emit(session_id: str, abs_path: str, new_tier: str, max_emits: int) -> bool:
    """判定と予約を単一ロック区間で行う唯一の公開関数。

    - session_id が空: debounce 無効化、常に True (state 保存もしない)
    - 同一 tier 以下への再警告: False (ハイウォーターマーク方式、shrink→regrow で
      同一 tier に戻っても再警告しない)
    - emit 上限到達: False
    - ロック/IO 失敗: True (fail-open。advisory hook のため「通知が飛ぶ」方向に
      倒す方が「ロックできず起動不能」より安全という判断。Windows で
      ``fcntl`` が無い場合もロックなしで動作継続する)
    """
    if not session_id:
        return True

    state_file = _state_path(session_id)
    try:
        os.makedirs(state_file.parent, exist_ok=True)
        with open(state_file, "a+") as f:
            if HAVE_FLOCK:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                state = _parse(f.read())
                count = state.get("__emit_count__", 0)
                if not isinstance(count, int):
                    count = 0
                stored_tier = state.get(abs_path, "ok")
                if not isinstance(stored_tier, str) or stored_tier not in TIER_ORDER:
                    stored_tier = "ok"

                if count >= max_emits:
                    return False
                if tier_rank(new_tier) <= tier_rank(stored_tier):
                    return False

                state[abs_path] = new_tier
                state["__emit_count__"] = count + 1
                f.seek(0)
                f.truncate()
                f.write(json.dumps(state))
                f.flush()
                return True
            finally:
                if HAVE_FLOCK:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
