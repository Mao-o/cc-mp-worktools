"""Cursor Agent 並走アナライザ。

Explore の prompt を受け取って cursor agent を読み取り専用 (`--mode plan`) でバックグラウンド
起動し、post フェーズで結果を取得して additionalContext 用文字列として返す。起動 argv は
review 系 2 hook と同じ `_common.cursorcli.readonly_argv` (調査用途で作業ツリーを書き換えない)。

停止は **process group ごと** (`killpg`) 行う。`pre` は `start_new_session=True` で起動する
ので pgid == pid で、リーダーだけを止めると cursor-agent (node) の孫プロセスが残る。
signal を送る前に cmdline で pid の同一性を確認する (PID 再利用対策)。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from pathlib import Path

from _common import cursorcli, hooklog, subproc

from state import cleanup, paths

NAME = cursorcli.NAME
TIMEOUT_SEC = 60
POLL_INTERVAL_SEC = 3
MAX_OUTPUT_BYTES = 8000

#: SIGTERM を送ってから SIGKILL に切り替えるまでの猶予 (秒)。
KILL_GRACE_SEC = 2.0

#: 停止待ちの probe 間隔 (秒)。
KILL_POLL_SEC = 0.05

log = hooklog.make_logger(f"explore-parallel/{NAME}")

#: PID 再利用の検出に使う起動 argv の署名 (`agent --trust --print --mode plan`)。
#: `readonly_argv` から導出するので、起動形が変わっても署名だけ古いまま取り残されない。
#:
#: **argv[0] (実行ファイル名) は照合しない**。`cursor` は実体へ `exec` するシムのことが
#: あり、その場合 ps が返すのは実体側の名前 (`cursor-agent` 等) になる。引数は `exec
#: "$REAL" "$@"` で保たれるので、名前ではなくフラグの組み合わせで見る。名前まで
#: 要求すると「シム環境では一切 kill できない」= ガードではなく停止処理の無効化になる。
#: 署名は argv の先頭側にあるため、ps が末尾を切り詰めても落ちない。
_SIGNATURE_TOKENS = tuple(t for t in cursorcli.readonly_argv("")[1:] if t)

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
    return cursorcli.is_available()


def pre(tool_use_id: str, prompt: str) -> None:
    """cursor agent を読み取り専用でバックグラウンド起動し、PID を記録する。"""
    result_file, pid_file = paths(NAME, tool_use_id)

    full_prompt = _PROMPT_TEMPLATE.format(prompt=prompt)

    with open(result_file, "wb") as rf, open(os.devnull, "wb") as devnull:
        proc = subprocess.Popen(
            cursorcli.readonly_argv(full_prompt),
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
                if terminate(pid):
                    log(f"timeout ({TIMEOUT_SEC}s) — killed")

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


def reap_orphan(pid_file: Path) -> None:
    """TTL 超過の pid ファイルが指す analyzer を停止する (残骸 GC 用)。

    post が来なかった経路 (Agent ツールの失敗・ユーザー中断・セッション終了、および
    `async` hook が `claude -p` の teardown で kill された場合) では停止も掃除も
    走らないため、`state.stale_entries` が拾った残骸をここで止める。
    """
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    if not _is_running(pid):
        return
    if terminate(pid):
        log(f"孤児 analyzer (pid {pid}) を停止")


def terminate(pid: int) -> bool:
    """analyzer の process group を停止する (SIGTERM → 猶予 → SIGKILL)。停止を試みたら True。

    `pre` は `start_new_session=True` で起動しているので **pgid == pid**。0.9.1 までは
    `os.kill(pid, SIGTERM)` でグループリーダーだけを止めており、cursor-agent (node) が
    生成した孫プロセスが取り残されて走り続けていた (課金・CPU のリーク)。`_common.subproc`
    が review 系 2 hook で使っている `killpg` と同じ考え方に揃える。

    signal を送る前に **pid が本当に自分が起動した analyzer か** を cmdline で確認する。
    pid ファイルは TTL 超過まで残りうるので、その間に pid が別プロセスへ再利用されている
    ことがある。**判定できないときは送らない側に倒す** — 無関係なプロセスに SIGTERM を
    送る事故のほうが、cursor を 1 つ取り残すより重い。
    """
    if not _is_analyzer(pid):
        log(f"pid {pid} は起動した analyzer と一致しない — signal を送らない")
        return False

    _signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + KILL_GRACE_SEC
    # 猶予中の probe は安い `killpg(pgid, 0)` で回す (リーダーだけでなく孫も数える)。
    # zombie が残る環境ではここが空にならないので、猶予後に zombie を除いた判定
    # (`group_is_stopped`) を 1 回だけ通してから SIGKILL に切り替える。
    while time.monotonic() < deadline and _group_exists(pid):
        time.sleep(KILL_POLL_SEC)
    if not subproc.group_is_stopped(pid):
        _signal_group(pid, signal.SIGKILL)
    return True


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True  # メンバーは居る (signal を送る権限が無いだけ)
    except OSError:
        return False


def _is_analyzer(pid: int) -> bool:
    """pid の cmdline が `cursorcli.readonly_argv` の起動形と一致するか。

    判定不能 (ps が使えない・出力が空) は False = 送らない側。
    """
    cmdline = subproc.pid_command(pid)
    if not cmdline:
        return False
    tokens = cmdline.split()
    return all(t in tokens for t in _SIGNATURE_TOKENS)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    """pgid == pid の process group にまとめて signal を送る。"""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
