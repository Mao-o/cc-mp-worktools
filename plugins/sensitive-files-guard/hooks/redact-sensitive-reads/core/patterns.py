"""機密ファイルパターン読込 (薄い wrapper)。

``_shared.patterns`` の実装を呼び、read 側の ``logging.log_error`` を warn_callback
として注入する。``SHARED_PATTERNS`` 解決 (CLAUDE_PLUGIN_ROOT / __file__ 相対) は
このモジュールの責務として残す。
"""
from __future__ import annotations

import os
from pathlib import Path

from _shared.patterns import (  # noqa: F401
    _parse_patterns_text,
    _resolve_local_patterns_path,
)
from _shared.patterns import load_patterns as _shared_load_patterns

from . import logging as L


def _resolve_shared_patterns() -> Path:
    """既定 patterns.txt を解決する (プラグイン配置非依存)。"""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        candidate = Path(plugin_root) / "hooks" / "check-sensitive-files" / "patterns.txt"
        if candidate.exists():
            return candidate
    # __file__: <plugin>/hooks/redact-sensitive-reads/core/patterns.py
    here = Path(__file__).resolve()
    return here.parent.parent.parent / "check-sensitive-files" / "patterns.txt"


SHARED_PATTERNS = _resolve_shared_patterns()


def _warn_local_oserror(err_name: str) -> None:
    L.log_error("local_patterns_unavailable", err_name)


def load_patterns(
    patterns_file: Path | None = None,
) -> list[tuple[str, bool]]:
    """既定 patterns.txt + ローカル patterns.local.txt を読んで rules list を返す。

    Read 側は ``core.logging`` 経由で stderr + logfile に warning を出す。
    ローカル非存在は黙殺。既定 patterns.txt の読み取り失敗は例外として再送出。
    """
    path = patterns_file or SHARED_PATTERNS
    return _shared_load_patterns(path, warn_callback=_warn_local_oserror)
