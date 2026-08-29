"""_common テスト共通のパス設定と偽 CLI 生成。

`python3 -m unittest discover tests` を `hooks/_common/` で回すため、`hooks/` を
sys.path に載せて `from _common import ...` を解決する (本番は各 hook の `__main__.py`
が同じ挿入を行う)。
"""
import os
import shlex
import sys
import time
from pathlib import Path

_COMMON_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _COMMON_DIR.parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

PLUGIN_ROOT = _HOOKS_DIR.parent

# 2026-08-20 の Stop hook 実出力相当 (内部バックログの指摘): 前置き 1 文 + フェンス付き sentinel
FENCED_CLEAN_WITH_PREAMBLE = "critical 指摘はない\n\n```\nREVIEW_CLEAN\n```\n"
FENCED_CLEAN = "```\nREVIEW_CLEAN\n```"


def write_script(directory: str, name: str, body: str) -> str:
    """bash script を実行可能ファイルとして書き、そのパスを返す。"""
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" + body)
    os.chmod(path, 0o755)
    return path


def hanging_cli(
    directory: str,
    name: str = "fake-cli",
    *,
    ignore_term: bool = False,
    grandchild_ignores_term: bool = False,
    grandchild_detached_from_pipe: bool = False,
    leader_exits: bool = False,
    pid_file: str | None = None,
    partial_output: str = "partial\n",
) -> str:
    """stdout に 1 行書いた後、孫 (`sleep 30`) を起動する偽 CLI。既定では孫が stdout を継承する。

    - `pid_file`: 孫の pid を書く (停止確認用)
    - `ignore_term`: 親子とも SIGTERM を無視する (孫にも継承されるので SIGKILL 段の検証)
    - `grandchild_ignores_term`: 孫だけ SIGTERM を無視する (親は TERM で死んで zombie になり、
      SIGKILL 段でリーダーが zombie のままグループに届くかの検証)
    - `grandchild_detached_from_pipe`: 孫は SIGTERM を無視し、かつ stdout / stderr を
      /dev/null に向ける (pipe を握らないので親が死ぬと EOF が来る。「EOF = 停止」と
      見なすとこの孫が残る。Codex P2 の回帰テスト用)
    - `leader_exits`: 親は孫を起動して即 exit する (cursor-agent 本体が落ちて helper だけが
      残るケース。リーダーが最初から zombie)
    """
    lines = []
    if ignore_term:
        lines.append("trap '' TERM")
    lines.append(f"printf '%s' {shlex.quote(partial_output)}")
    if grandchild_detached_from_pipe:
        lines.append("( trap '' TERM; exec sleep 30 ) >/dev/null 2>&1 &")
    elif grandchild_ignores_term:
        lines.append("( trap '' TERM; exec sleep 30 ) &")
    else:
        lines.append("sleep 30 &")
    if pid_file:
        lines.append(f"echo $! > {shlex.quote(pid_file)}")
    lines.append("exit 0" if leader_exits else "wait")
    return write_script(directory, name, "\n".join(lines) + "\n")


def read_pid(pid_file: str, timeout: float = 3.0) -> int:
    """偽 CLI が書いた孫 pid を読む (書かれるまで少し待つ)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(pid_file) as f:
                text = f.read().strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"pid file が書かれなかった: {pid_file}")


def wait_until_dead(pid: int, timeout: float = 3.0) -> bool:
    """pid が消えるか zombie になるまで待つ。timeout 内にそうなれば True。

    PID 1 が孤児を reap しない環境 (Linux コンテナ等) では死んだ孫が zombie のまま残り
    `os.kill(pid, 0)` が成功し続けるので、zombie も「死んだ」扱いにする。
    """
    from _common import subproc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if subproc.pid_is_zombie(pid):
            return True
        time.sleep(0.05)
    return False


def ensure_killed(pid: int) -> None:
    """テスト後始末: 生き残った孫を SIGKILL する。"""
    try:
        os.kill(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass
