#!/usr/bin/env python3
"""唯一の I/O 副作用: session_id ベースの debounce store。

同一セッション内で同一ファイル×同一 tier への再通知を防ぐ。判定
(should_emit) と予約 (state 更新) を ``try_reserve_emit`` 1 回の呼び出し・
1 回のロック区間に統合し、check-then-act 分割による TOCTOU race を避ける
(``external-ai-assist/hooks/exitplan-review/__main__.py::reserve_slot`` の
同型パターンを踏襲)。

0.2.0 から、パスごとの記録は「通知済み tier」に加えて「直近に観測した行数」を
持つ。`Write` は envelope から編集前の行数が分からないため、同一セッション内の
直近行数と比べて縮んだ/変わらなかった場合を抑制するのに使う。行数は emit の
有無に関わらず記録するが、**tier は emit したときだけ進める** — emit せずに
tier を進めると、その後に本当に成長したとき同一 tier とみなされて恒久的に
抑制されてしまうため。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from change import GREW, NOT_GREW, UNKNOWN
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


def _read_record(state: dict, abs_path: str) -> tuple[str, int | None]:
    """パス記録を ``(tier, lines)`` に正規化する。

    0.1.0 の記録は tier 文字列そのもの。dict 形式 (0.2.0 以降) と両方受ける。
    """
    raw = state.get(abs_path)
    if isinstance(raw, str):
        return (raw if raw in TIER_ORDER else "ok"), None
    if isinstance(raw, dict):
        tier = raw.get("tier")
        if not isinstance(tier, str) or tier not in TIER_ORDER:
            tier = "ok"
        lines = raw.get("lines")
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 0:
            lines = None
        return tier, lines
    return "ok", None


def _resolve_growth(growth: str, line_count: int | None, stored_lines: int | None) -> str:
    """envelope から成長方向が決まらない場合に、直近の観測行数で補う。"""
    if growth != UNKNOWN:
        return growth
    if line_count is None or stored_lines is None:
        return UNKNOWN
    return GREW if line_count > stored_lines else NOT_GREW


def try_reserve_emit(
    session_id: str,
    abs_path: str,
    new_tier: str,
    max_emits: int,
    *,
    line_count: int | None = None,
    growth: str = UNKNOWN,
    emit_candidate: bool = True,
) -> bool:
    """観測の記録と emit 予約を単一ロック区間で行う唯一の公開関数。

    ``emit_candidate=False`` (judge が emit しないと判定したファイル) でも
    呼んでよい。その場合 ``line_count`` だけを記録して False を返す。

    - ``growth`` が ``NOT_GREW``: False (ファイルを大きくしない編集)
    - ``growth`` が ``UNKNOWN`` かつ直近行数の記録あり: 行数比較で決める
    - session_id が空: debounce 無効化。state を読み書きせず、envelope 由来の
      成長判定と ``emit_candidate`` だけで返す
    - 同一 tier 以下への再警告: False (ハイウォーターマーク方式、shrink→regrow で
      同一 tier に戻っても再警告しない)
    - emit 上限到達: False
    - ロック/IO 失敗: 記録を諦め、state 無しの場合と同じ判断に倒す (advisory hook
      のため「ロックできず起動不能」より「通知が飛ぶ」方向が安全。Windows で
      ``fcntl`` が無い場合もロックなしで動作継続する)
    """
    if not session_id:
        # 記録先が無いので、envelope 由来の成長判定だけで決める。抑制する場合も
        # 行数を残せないため、次の呼び出しは再び「記録なし」から始まる。
        return emit_candidate and growth != NOT_GREW

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
                if isinstance(count, bool) or not isinstance(count, int):
                    count = 0
                stored_tier, stored_lines = _read_record(state, abs_path)
                effective_growth = _resolve_growth(growth, line_count, stored_lines)

                granted = (
                    emit_candidate
                    and effective_growth != NOT_GREW
                    and count < max_emits
                    and tier_rank(new_tier) > tier_rank(stored_tier)
                )

                # tier は emit したときだけ進める (docstring の設計理由を参照)。
                record: dict = {"tier": new_tier if granted else stored_tier}
                effective_lines = line_count if line_count is not None else stored_lines
                if effective_lines is not None:
                    record["lines"] = effective_lines
                state[abs_path] = record
                state["__emit_count__"] = count + 1 if granted else count

                f.seek(0)
                f.truncate()
                f.write(json.dumps(state))
                f.flush()
                return granted
            finally:
                if HAVE_FLOCK:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        # state を読めなかったので session_id が空の場合と同じ判断に倒す。
        return emit_candidate and growth != NOT_GREW
