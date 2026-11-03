"""コマンドの読み取りに期限を付ける。

PreToolUse hook が時間切れになると Claude Code はコマンドをそのまま実行する。読み取り (command.py) の正規表現は
途中で止められず、書き方によっては大きさの 2 乗の時間がかかる。形を個別に閉じても取りこぼすので、大きいコマンドは制限時間つきの
子プロセスで読み、時間切れ・子の異常終了・結果を読めないときは止める。POSIX と Windows で同じ経路にする (SIGALRM は Windows に無い)。

小さいコマンドと、`gh` か `pr` の文字が無いコマンドは読むまでもなく短いので、同じプロセスで読む (全 Bash に起動の時間を足さない)。
"""
from __future__ import annotations

import faulthandler
import json
import subprocess
import sys
from pathlib import Path

# 子はスクリプトとして起動する。PYTHONSAFEPATH=1 だと script の dir が sys.path に入らず、全部の子が異常終了する
sys.path.insert(0, str(Path(__file__).resolve().parent))

from command import _READ_LIMIT, Invocation, _join_continuations, find_invocations, unresolved_reason
from runner import Deadline

# 子プロセスで読む大きさ (文字数)。この手前の最悪の形 (`a=$(` の 255 個 + 空白) は同じプロセスで約 0.8 秒
CHILD_FROM = 4096
# 読み取りの期限 (秒)。手元の実コマンドの読み取りは最大で約 6 ミリ秒。上限ちょうどの最悪の形は約 9 秒で、Windows の 4 倍を見込むと
# 期限を超えるが、その形は止まる側に倒れる
READ_SECONDS = 15
# 親が先に落ちても子が残らないように、子は期限 + この秒数で自分で終わる
_CHILD_GRACE = 1.0
_ADVICE = "PR 操作の位置を確かめられない (本文はファイルに置いて --body-file で渡すか、コマンドを分けて 4096 文字未満にする)"


def _reads_in_child(command: str) -> bool:
    if not CHILD_FROM <= len(command) <= _READ_LIMIT:
        return False  # 上限を超えるものは読まない (command._oversized)
    joined = _join_continuations(command)
    return "gh" in joined and "pr" in joined


def read_command(command: str, budget: Deadline) -> tuple[list[Invocation], str | None]:
    """(見つかった PR 操作, 止める理由) を返す。理由が None でなければ呼び出し側は止める。"""
    if not _reads_in_child(command):
        found = find_invocations(command)
        return found, unresolved_reason(command, found)
    seconds = budget.sub(READ_SECONDS).remaining()
    if seconds <= 0:
        return [], f"コマンドを読む時間が残っていないので{_ADVICE}"
    try:
        # コマンドは stdin で渡す (Windows のコマンドラインは約 32 KB まで)。ASCII の JSON なら単独のサロゲートも壊れない
        r = subprocess.run(
            [sys.executable, __file__, str(seconds + _CHILD_GRACE)],
            input=json.dumps(command).encode("ascii"),
            capture_output=True,
            timeout=seconds,
        )
        if r.returncode != 0:
            return [], f"コマンドの読み取りが異常終了した (exit {r.returncode}) ので{_ADVICE}"
        data = json.loads(r.stdout)
        reason = data["reason"]
        if not isinstance(data["found"], list) or not (reason is None or (isinstance(reason, str) and reason)):
            raise TypeError("found / reason の型が違う")
        return [Invocation(**{**d, "cd_steps": tuple(d["cd_steps"])}) for d in data["found"]], reason
    except subprocess.TimeoutExpired:
        return [], f"コマンドの読み取りが制限時間 ({seconds:.0f} 秒) で終わらないので{_ADVICE}"
    except Exception as e:  # noqa: BLE001 - 読めないものは止める (例外の種類で通す側に倒さない)
        return [], f"コマンドの読み取りの結果を受け取れない ({type(e).__name__}) ので{_ADVICE}"


def _child(lifetime: float) -> None:
    # 正規表現は GIL を握ったまま動くので、スレッドでは止められない。faulthandler の見張りは GIL を使わない
    faulthandler.dump_traceback_later(lifetime, exit=True)
    command = json.loads(sys.stdin.buffer.read())
    found = find_invocations(command)
    reason = unresolved_reason(command, found)
    sys.stdout.write(json.dumps({"found": [vars(inv) for inv in found], "reason": reason}))


if __name__ == "__main__":
    _child(float(sys.argv[1]))
