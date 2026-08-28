#!/usr/bin/env python3
"""PostToolUse(Write|Edit) hook: 行数 tier + 構造シグナルに基づき、分割検討を
促す advisory メモを ``additionalContext`` で返す。

block/deny は一切しない (advisor)。判定ロジックは judge.py、debounce は
state.py に分離。何が起きても exit 0 (fail-open) を徹底する。
"""
from __future__ import annotations

import json
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
    verdict = judge.judge(file_metrics, lang, role)

    # tier が ok のファイルでは state を触らない: 大半の編集で I/O を発生させ
    # ないため。ok のファイルを成長判定の基準として残す必要もない (次に review
    # 以上へ育ったときは Edit の行差分か「記録なし = 判定不能」で決まる)。
    if verdict.tier == "ok":
        return

    growth = change.classify_growth(tool_name, tool_input)
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
