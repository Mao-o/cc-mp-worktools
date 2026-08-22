"""外部 CLI (cursor / codex) の起動ヘルパー。

## なぜ `subprocess.run(capture_output=True, timeout=…)` を使わないか

`run()` は TimeoutExpired 時に **直接の子だけ** kill する。cursor-agent (node) や codex が
生成した孫プロセスが stdout を継承していると、孫は取り残されて走り続ける
(課金・CPU のリーク。Python 3.8+ の `run()` は kill 後 `wait()` しかしないので hook 自体は
timeout で返るが、旧 `run()` や「kill 後に `communicate()` で読み捨てる」実装では孫が
pipe を閉じるまで EOF が来ず、harness の hook timeout (Stop / ExitPlanMode) まで止まる)。

ここでは `Popen(start_new_session=True)` で子を独自の process group (pgid == pid) に
置き、timeout 時は `os.killpg` で **グループごと** SIGTERM → 猶予 → SIGKILL し、残出力を
読み捨ててから返る。hook 自身は別の process group なので巻き込まれない。

「止まった」の判定は pipe の EOF ではなく **process group が空になったこと**
(`killpg(pgid, 0)` が ESRCH) で行う。リーダーが死んで pipe が EOF になっても、SIGTERM を
無視し出力を /dev/null に向けたメンバーが残りうるため、猶予内に空にならなければ
グループに SIGKILL を送る。正常終了した後も同じ probe でメンバーの残存を確認し、
残っていれば同じ手順で止める (CLI が background helper を残すケース)。

SIGKILL 後も EOF が来ない = グループを抜けたプロセス (自分で setsid した孫など) が
pipe を握っている状態。これ以上は待たず、こちらの read 端を閉じて直接の子だけ回収する。
"""
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Sequence

# SIGTERM を送ってから SIGKILL に切り替えるまでの猶予 (秒)。kill 経路の最悪所要時間は
# timeout + 3 * KILL_GRACE_SEC (TERM 待ち / KILL 待ち / 最後の wait)。hooks.json の
# hook timeout はこれを含めて決める (各 hook の tests が式で固定している)。
KILL_GRACE_SEC = 5.0

# グループが空になるのを待つときの probe 間隔 (秒)。
_PROBE_INTERVAL_SEC = 0.05


def cli_available(name: str) -> bool:
    return shutil.which(name) is not None


def run_captured(
    argv: Sequence[str],
    *,
    timeout_sec: float,
    input_text: str | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess | None:
    """argv を独自 process group で起動し、stdout / stderr を回収して返す。

    timeout / 起動失敗 (コマンド不在等) は None。timeout 時は process group ごと停止し、
    孫プロセスが握っていた pipe の残出力も読み捨ててから返る。
    stdin は `input_text` があればそれを渡し、無ければ /dev/null
    (hook 自身の stdin = payload の pipe を子に継承させない)。
    """
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            cwd=cwd,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError):
        return None

    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        kill_process_group(proc, own_group=True)
        return None
    except BaseException:
        kill_process_group(proc, own_group=True)
        raise
    # 正常終了後もグループにメンバー (CLI が残した background helper 等) が居れば止める。
    # 通常は probe 1 回 (ESRCH) で済む
    if _group_alive(proc.pid):
        kill_process_group(proc, own_group=True)
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def run_for_output(
    argv: Sequence[str],
    *,
    timeout_sec: float,
    input_text: str | None = None,
    max_output_chars: int | None = None,
    cwd: str | None = None,
) -> str | None:
    """`run_captured` に「returncode 0 かつ非空 stdout のときだけ本文を返す」を重ねる。

    レビュアー (cursor / codex) の共通契約: timeout・起動失敗・非 0 終了・空出力は
    いずれも None (= fail-open で「結果なし」扱い)。`max_output_chars` は文字数で切る
    (0.2.0 からの `MAX_OUTPUT_BYTES` と同じ挙動)。
    """
    result = run_captured(argv, timeout_sec=timeout_sec, input_text=input_text, cwd=cwd)
    if result is None or result.returncode != 0:
        return None
    output = (result.stdout or "").strip()
    if not output:
        return None
    if max_output_chars is not None:
        output = output[:max_output_chars]
    return output


def kill_process_group(
    proc: subprocess.Popen,
    grace_sec: float | None = None,
    *,
    own_group: bool | None = None,
) -> None:
    """proc の process group に SIGTERM → 猶予 → SIGKILL を送り、残出力を読み捨てて回収する。

    `own_group=True` は proc が `start_new_session=True` で起動されている (pgid == proc.pid)
    ことを呼び出し側が保証する場合に渡す (`run_captured` はそうしている)。このとき
    signal は `os.killpg(proc.pid, sig)` でグループ全体に送る:

    - リーダーが未 reap (`returncode is None`) の間は pid が再利用されないので常に安全
      (リーダーが既に zombie でも、グループに孫が残っていれば届く。`os.getpgid` で確認して
      から送る案は、macOS が zombie に ESRCH を返して `send_signal` → 内部 `poll()` が reap
      → signal 不達 → 孫が残る経路になるため採らない)
    - reap 済みでも、グループにメンバーが残っている限り pgid 番号は新しい pid として
      再割当てされない (Linux / BSD)。`killpg(pgid, 0)` でメンバーの存在を確認してから送り、
      空なら送らない (pid 再利用の誤送信防止)

    「止まった」はグループが空になったことで判定する (pipe の EOF だけでは、SIGTERM を
    無視して出力を /dev/null に向けたメンバーを見逃す)。

    `own_group=None` (外部で作った Popen) は `getpgid` でリーダーかを判定し、リーダーで
    なければ直接の子だけに送る。hook 自身の process group に signal が飛ぶことはない。
    """
    grace = KILL_GRACE_SEC if grace_sec is None else grace_sec
    if own_group is None:
        own_group = _leads_own_group(proc)

    _signal(proc, signal.SIGTERM, own_group)
    if _settle(proc, grace, own_group):
        return
    _signal(proc, signal.SIGKILL, own_group)
    if _settle(proc, grace, own_group):
        return
    # SIGKILL でも EOF が来ない = グループ外のプロセスが pipe を握っている。
    # これ以上待たず、こちらの read 端を閉じて直接の子だけ回収する
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _leads_own_group(proc: subprocess.Popen) -> bool:
    """proc が生存中のグループリーダーか (zombie は macOS で ESRCH になり False)。"""
    try:
        return os.getpgid(proc.pid) == proc.pid
    except OSError:
        return False


def _group_alive(pgid: int) -> bool:
    """process group にメンバーが残っているか (`killpg(pgid, 0)` の成否)。

    EPERM は「居るが signal を送れない」なので残っている扱い。
    """
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _signal(proc: subprocess.Popen, sig: signal.Signals, own_group: bool) -> None:
    try:
        if own_group:
            if proc.returncode is not None and not _group_alive(proc.pid):
                return  # reap 済みでグループも空: pgid 番号が再利用されうるので送らない
            os.killpg(proc.pid, sig)
        elif proc.returncode is None:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _settle(proc: subprocess.Popen, grace: float, own_group: bool) -> bool:
    """猶予内に「残出力を読み捨てて子を回収」かつ「グループが空」になれば True。

    `_drain` (communicate) はリーダーを reap するので、その後の probe はリーダー自身を
    数えない。own_group でなければ回収できた時点で True。
    """
    deadline = time.monotonic() + grace
    drained = False
    while True:
        if not drained:
            drained = _drain(proc, max(0.0, deadline - time.monotonic()))
        if drained and (not own_group or not _group_alive(proc.pid)):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PROBE_INTERVAL_SEC, remaining))


def _drain(proc: subprocess.Popen, timeout: float) -> bool:
    """残出力を読み捨てつつ終了を待つ。timeout 内に回収できれば True。

    `communicate()` は TimeoutExpired 後に再呼び出ししても出力を失わない (公式 docs)。
    """
    try:
        proc.communicate(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except (ValueError, OSError):
        return True  # stream が既に閉じている等。これ以上読むものがない
