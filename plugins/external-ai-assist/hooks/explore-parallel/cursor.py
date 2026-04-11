"""Cursor Agent 並走アナライザ。

Explore の prompt を受け取って cursor agent をバックグラウンド起動し、
post フェーズで結果を取得して additionalContext 用文字列として返す。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

from state import cleanup, paths

NAME = "cursor"
TIMEOUT_SEC = 60
POLL_INTERVAL_SEC = 3
MAX_OUTPUT_BYTES = 8000

_PROMPT_TEMPLATE = (
    "以下のタスクについて、cursor のセマンティック検索(意味ベースのコード検索)を活かした"
    "補助調査を返してください。grep/glob による文字列一致調査は並走する別エージェント(Explore)"
    "が担当するため、**重複を避けて**以下 4 点に集中してください:\n"
    "\n"
    "1. **キーワードでは引っかからない関連コード**: 同じ概念を別の名前で実装している箇所\n"
    "2. **類似実装パターン**: 同じ課題を別の場所で解決している既存コード(参考実装)\n"
    "3. **間接依存**: import では追いにくい動的ロード・設定経由の結合・DI 等\n"
    "4. **変更の波及範囲**: タスク説明に直接出てこないが影響を受けそうな関連箇所\n"
    "\n"
    "各項目は 1-3 行、ファイルパスと関係性を明示。該当が無い項目は 'なし' と記す。"
    "単純なファイル一覧・役割一覧・README 的な説明は書かない(Explore が担当)。"
    "\n\nタスク: {prompt}"
)
_CONTEXT_HEADER = (
    "## Cursor Agent による補助調査結果 (Explore と重複しない関連情報に焦点)\n\n"
)


def is_available() -> bool:
    return shutil.which("cursor") is not None


def pre(tool_use_id: str, prompt: str) -> None:
    """cursor agent をバックグラウンド起動し、PID を記録する。"""
    result_file, pid_file = paths(NAME, tool_use_id)

    full_prompt = _PROMPT_TEMPLATE.format(prompt=prompt)

    with open(result_file, "wb") as rf, open(os.devnull, "wb") as devnull:
        proc = subprocess.Popen(
            ["cursor", "agent", "--trust", "-p", full_prompt],
            stdout=rf,
            stderr=devnull,
            stdin=devnull,
            start_new_session=True,
        )

    pid_file.write_text(str(proc.pid))


def post(tool_use_id: str) -> str | None:
    """cursor agent を最大 TIMEOUT_SEC 秒待ち、結果を整形して返す。"""
    result_file, pid_file = paths(NAME, tool_use_id)

    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid = None

        if pid:
            waited = 0
            while waited < TIMEOUT_SEC and _is_running(pid):
                time.sleep(POLL_INTERVAL_SEC)
                waited += POLL_INTERVAL_SEC

            if _is_running(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                    print(
                        f"[{NAME}] timeout ({TIMEOUT_SEC}s) — killed",
                        file=sys.stderr,
                    )
                except ProcessLookupError:
                    pass

        cleanup(pid_file)

    if not result_file.is_file():
        return None

    try:
        raw = result_file.read_bytes()[:MAX_OUTPUT_BYTES]
        data = raw.decode("utf-8", errors="replace").strip()
    except OSError:
        data = ""
    finally:
        cleanup(result_file)

    if not data:
        return None

    return _CONTEXT_HEADER + data


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
