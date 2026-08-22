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

SIGKILL 後も EOF が来ない = グループを抜けたプロセス (自分で setsid した孫など) が
pipe を握っている状態。これ以上は待たず、こちらの read 端を閉じて直接の子だけ回収する。
"""
import os
import shutil
import signal
import subprocess
from collections.abc import Sequence

# SIGTERM を送ってから SIGKILL に切り替えるまでの猶予 (秒)。kill 経路の最悪所要時間は
# timeout + 3 * KILL_GRACE_SEC (TERM 待ち / KILL 待ち / 最後の wait)。hooks.json の
# hook timeout はこれを含めて決める (各 hook の tests が式で固定している)。
KILL_GRACE_SEC = 5.0


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
    **reap 前 (`proc.returncode is None`) の間は無条件に `os.killpg(proc.pid, sig)`** を
    送る。未 reap の pid は再利用されないので別プロセスへの誤送信は起きず、リーダーが
    既に zombie でもグループ (孫) が残っていれば届く。`os.getpgid` で確認しないのは、
    macOS が zombie リーダーに ESRCH を返すため: そこで `Popen.send_signal` に落とすと
    内部の `poll()` が zombie を reap して signal を送らず、孫が取り残される。

    `own_group=None` (外部で作った Popen) は `getpgid` でリーダーかを判定し、リーダーで
    なければ直接の子だけに送る。hook 自身の process group に signal が飛ぶことはない。
    reap 済み (`returncode` 設定済み) の proc には何も送らない (pid 再利用の誤送信防止)。
    """
    grace = KILL_GRACE_SEC if grace_sec is None else grace_sec
    if own_group is None:
        own_group = _leads_own_group(proc)
    _signal(proc, signal.SIGTERM, own_group)
    if _drain(proc, grace):
        return
    _signal(proc, signal.SIGKILL, own_group)
    if _drain(proc, grace):
        return
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


def _signal(proc: subprocess.Popen, sig: signal.Signals, own_group: bool) -> None:
    if proc.returncode is not None:
        return  # reap 済み: pid が再利用されうるので何も送らない
    try:
        if own_group:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


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
