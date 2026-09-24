"""制限時間つきの subprocess 実行。

ゲート全体で 1 つの Deadline を共有し、各コマンドは残り時間 (と個別の上限の
小さい方) で打ち切る。時間切れは GateTimeout として呼び出し側まで上げる。
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path


class GateTimeout(Exception):
    pass


class Deadline:
    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._end - time.monotonic()


def run(
    args: list[str],
    cwd: Path,
    deadline: Deadline,
    cap: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """args を実行する。起動できない場合は OSError をそのまま上げる。"""
    remaining = deadline.remaining()
    if remaining <= 0:
        raise GateTimeout(f"制限時間切れ ({args[0]} の実行前)")
    timeout = remaining if cap is None else min(remaining, cap)
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        if cap is not None and cap < remaining:
            # 個別の上限に当たっただけでゲート全体はまだ時間がある。
            # 呼び出し側が「このコマンドは失敗した」として扱えるように返す。
            return subprocess.CompletedProcess(args, 124, "", f"timeout after {cap}s")
        raise GateTimeout(f"制限時間切れ ({' '.join(args[:3])})") from e


def git(args: list[str], cwd: Path, deadline: Deadline, cap: float | None = None):
    return run(["git", *args], cwd, deadline, cap)
