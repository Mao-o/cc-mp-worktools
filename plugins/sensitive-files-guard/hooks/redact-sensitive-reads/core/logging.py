"""秘密非混入ログ。

呼出側は第二引数に path / 値 / basename / command 文字列を絶対に渡してはならない。
渡してよいのはエラー種別・関数名・処理時間・classify 結果などの
「公開しても安全な情報」のみ。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

LOG_PATH = Path.home() / ".claude" / "logs" / "redact-hook.log"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def log_error(category: str, detail: str = "") -> None:
    """エラーログを記録する。detail は公開可情報のみを想定。

    stderr にも category を出力 (Claude Code UI で可視化される)。
    ファイル書込失敗は握りつぶす (hook の責務ではない)。
    """
    line = f"{_now()} ERROR {category} {detail}\n".rstrip() + "\n"
    try:
        sys.stderr.write(f"[redact-hook] {category}\n")
    except OSError:
        pass
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass


def log_info(category: str, detail: str = "") -> None:
    """INFO ログ (stderr には出さない)。detail は公開可情報のみ。"""
    line = f"{_now()} INFO  {category} {detail}\n".rstrip() + "\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass
