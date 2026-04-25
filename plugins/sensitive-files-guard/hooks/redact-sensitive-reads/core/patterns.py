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
    _resolve_local_patterns_paths,
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


def _warn_local(msg: str) -> None:
    """warn_callback — deprecation 通知と OS エラー通知を区別して logfile に記録する。

    deprecation は毎回の hook 実行で出てしまうため ``log_info`` (logfile のみ) で
    静かに残す。OS エラーは ``log_error`` で stderr + logfile の両方に出す。
    いずれも ``permissionDecisionReason`` には載せない (LLM 文脈毎回混入ノイズ回避)。
    """
    if msg == "deprecated_config_dir":
        L.log_info(
            "patterns_local_deprecated_dir",
            "fallback $XDG_CONFIG_HOME path used; migrate to ~/.claude/sensitive-files-guard/patterns.local.txt (0.6.0 will remove fallback)",
        )
    else:
        L.log_error("local_patterns_unavailable", msg)


_warn_local_oserror = _warn_local  # 後方互換 alias


def load_patterns(
    patterns_file: Path | None = None,
) -> list[tuple[str, bool]]:
    """既定 patterns.txt + ローカル patterns.local.txt を読んで rules list を返す。

    Read 側は ``core.logging`` 経由で stderr + logfile に warning を出す。
    ローカル非存在は黙殺。既定 patterns.txt の読み取り失敗は例外として再送出。
    """
    path = patterns_file or SHARED_PATTERNS
    return _shared_load_patterns(path, warn_callback=_warn_local)
