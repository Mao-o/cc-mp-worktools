#!/usr/bin/env python3
"""PostToolUse(Write|Edit) hook: 行数 tier + 構造シグナルに基づき、分割検討を
促す advisory メモを ``additionalContext`` で返す。

block/deny は一切しない (advisor)。判定ロジックは judge.py、debounce は
state.py に分離。何が起きても exit 0 (fail-open) を徹底する。
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

_PKG_DIR = str(Path(__file__).resolve().parent)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import change  # noqa: E402
import judge  # noqa: E402
import language  # noqa: E402
import message  # noqa: E402
import metrics as metrics_mod  # noqa: E402
import source  # noqa: E402
import state  # noqa: E402

DEFAULT_MAX_EMITS = 20
DEFAULT_SCALE = 1.0


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_max_emits() -> int:
    raw = os.environ.get("FILE_SPLIT_ADVISOR_MAX_EMITS", "").strip()
    if not raw:
        return DEFAULT_MAX_EMITS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_EMITS


def _get_scale() -> float:
    """``FILE_SPLIT_ADVISOR_SCALE``: 全閾値 (note/review/warn/strong) に掛ける倍率。

    未設定・数値変換失敗・0 以下・非有限値 (``nan``/``inf``/``1e400`` 等) は
    すべて既定 (1.0) にフォールバックする。``nan``/``inf`` は ``float()`` の
    変換自体は成功し ``value <= 0`` も False を返すため、有限性チェックを
    別途行わないと素通りする (P2-3)。非有限な scale は実効閾値を
    nan/inf にし、``line_count >= threshold`` が常に False になって全ファイル
    が無言になる (plugin の実質的な無効化)。
    """
    raw = os.environ.get("FILE_SPLIT_ADVISOR_SCALE", "").strip()
    if not raw:
        return DEFAULT_SCALE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SCALE
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_SCALE
    return value


def _get_ignore_patterns() -> tuple[str, ...]:
    return source.load_ignore_globs(os.environ.get("FILE_SPLIT_ADVISOR_IGNORE", ""))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    if payload.get("tool_name") not in ("Write", "Edit"):
        return

    if _is_truthy(os.environ.get("FILE_SPLIT_ADVISOR_DISABLED", "")):
        return

    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return

    file_path = tool_input.get("file_path", "")
    if not file_path or not isinstance(file_path, str):
        return

    tool_name = payload.get("tool_name", "")
    cwd = payload.get("cwd", "")
    session_id = payload.get("session_id", "")

    path = source.resolve_path(file_path, cwd)

    # 一時領域 (scratchpad 等) 配下のファイルは常時 skip (cwd 自体がそこに
    # 無い限り)。Claude が分析用ダンプ・handoff メモをそこに書く運用があり、
    # プロジェクト外のファイルにまで分割助言を出すのは有用でないため。
    if source.should_skip_temp_dir(path, cwd):
        return

    # FILE_SPLIT_ADVISOR_CWD_ONLY=1 の opt-in (既定 off): --add-dir で cwd 外を
    # 正当に編集する運用を壊さないよう、既定では cwd 外でも通常どおり判定する。
    if _is_truthy(os.environ.get("FILE_SPLIT_ADVISOR_CWD_ONLY", "")) and source.is_outside_cwd(
        path, cwd
    ):
        return

    # FILE_SPLIT_ADVISOR_IGNORE (カンマ区切り glob) + ~/.claude/file-split-advisor/
    # ignore.local.txt (gitignore 風 glob) に一致するファイルを skip する。
    if source.matches_ignore_glob(path, _get_ignore_patterns()):
        return

    if source.should_skip_by_name(path):
        return

    # 拡張子 allowlist。Markdown / JSON / YAML / CSV 等の非コードファイルを
    # 行数だけで分割対象として扱わない。
    if not language.is_code_path(path):
        return

    loaded = source.load_text(path)
    if loaded is None:
        return

    if language.is_generated_by_content(loaded.lines[:5]):
        return

    lang = language.detect_language(path)
    role = "test" if language.is_test_path(path) else "normal"

    file_metrics = metrics_mod.compute(loaded, lang, path)
    verdict = judge.judge(file_metrics, lang, role, scale=_get_scale())

    # tier が ok でも state を更新する: 記録した行数を最新に保たないと、縮んで
    # ok まで戻ったファイルが古い行数と比較され、その後の成長を誤って抑制する。
    growth = change.classify_growth(tool_name, tool_input, loaded.text)
    max_emits = _get_max_emits()
    if not state.try_reserve_emit(
        session_id,
        str(path),
        verdict.tier,
        max_emits,
        line_count=file_metrics.line_count,
        growth=growth,
        emit_candidate=verdict.should_emit,
    ):
        return

    display_path = path
    if cwd:
        try:
            display_path = path.relative_to(cwd)
        except ValueError:
            display_path = path

    text = message.build(display_path, lang, role, verdict, file_metrics)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }
    json.dump(output, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[file-split-advisor] fatal: {e}", file=sys.stderr)
