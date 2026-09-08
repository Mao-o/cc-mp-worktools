"""Cursor Agent 並走アナライザ。

Explore の prompt を受け取って cursor agent を読み取り専用 (`--mode plan`) でバックグラウンド
起動し、post フェーズで結果を取得して additionalContext 用文字列として返す。起動 argv は
review 系 2 hook と同じ `_common.cursorcli.readonly_argv` (調査用途で作業ツリーを書き換えない)。

停止は **process group ごと** (`killpg`) 行う。`pre` は `start_new_session=True` で起動する
ので pgid == pid で、リーダーだけを止めると cursor-agent (node) の孫プロセスが残る。
signal を送る前に **cmdline の署名と開始時刻**の両方で pid の同一性を確認する
(PID 再利用対策)。リーダーが先に死んだ後も group に孫が残ることがあるので、その場合は
**group の生存メンバー**を見て停止する。この group 側の判定は `post()` と GC
(`reap_orphan`) の**両方**が通る — post はリーダーの生死しか見ていなかったため、
リーダーが先に死んだ経路で孫を残したまま pid 記録を消していた。

**zombie のリーダーは「走っていない」扱い**にする。PID 1 が孤児を reap しないコンテナでは
処理を終えたリーダーが zombie として残り `os.kill(pid, 0)` が成功し続けるが、zombie の
`ps` は cmdline を返さない (`<defunct>`) ので同一性を確認できず、停止経路が永久に
未確定のまま回り続ける。zombie は停止済みとして group 側の判定に進める。

停止できたかどうかは呼び出し側に返す。`post()` も GC (`reap_orphan`) も、**停止を確認
できなかったときは pid / 結果ファイルを残す** — pid ファイルはその孤児を追える唯一の記録
なので、確認できていない状態で消すと以後どの経路も再試行できない。

GC 経路 (`reap_orphan`) は **残り予算 (`deadline`) を停止処理の内部まで持ち回る**。
`ps` の timeout と TERM の猶予をその残りで cap し、尽きたら `REAP_UNCONFIRMED` で戻して
記録を残す。GC は 5 秒の同期 PreToolUse hook の中で回るため、1 エントリの停止処理が
`ps` (各 2 秒) と猶予 (2 秒) を積み上げると hook 自体が kill され、現在の analyzer を
起動できないまま次回も同じ孤児で同じところに嵌まる。`post()` は async hook で
harness の timeout が掛からないので予算なし (`deadline=None`) で回す。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from pathlib import Path

from _common import cursorcli, hooklog, subproc

from state import REAP_SIGNALED, REAP_STOPPED, REAP_UNCONFIRMED, cleanup, paths

NAME = cursorcli.NAME
TIMEOUT_SEC = 60
POLL_INTERVAL_SEC = 3
MAX_OUTPUT_BYTES = 8000

#: SIGTERM を送ってから SIGKILL に切り替えるまでの猶予 (秒)。
KILL_GRACE_SEC = 2.0

#: 停止待ちの probe 間隔 (秒)。
KILL_POLL_SEC = 0.05

#: 生死・同一性判定に使う `ps` の実行上限 (秒)。`_common.subproc` の ps 呼び出しと同じ値。
#: GC 経路では `deadline` の残り予算がこれより短ければそちらが上限になる (`_ps_budget`)。
_PS_TIMEOUT_SEC = 2.0

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

#: プロセスの開始時刻が pid ファイルの mtime (= 起動時刻) より後に見えても許す幅 (秒)。
#:
#: `pre` は Popen した直後に pid ファイルを書くので、正しい analyzer の開始時刻は
#: 常に mtime 以前になる。ただし `ps -o etime=` は秒未満を切り捨てるため開始時刻が
#: 最大 1 秒ぶん後ろにずれて見え、mtime 側にもファイルシステムの時刻粒度がある。
#: 誤って「別プロセス」と判定して停止をあきらめないよう、数秒の余裕を持たせる。
_START_SKEW_SEC = 5.0

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
    """cursor agent を最大 TIMEOUT_SEC 秒待ち、結果を整形して返す。

    **timeout 後に停止を確認できなかったときは pid / 結果ファイルを残す**
    (マージ前レビューの指摘)。`terminate()` は同一性を確認できない (`ps` が一時的に
    使えない・cmdline が切り詰められた・pid ファイルの mtime が読めない) 場合や、
    signal を送出できなかった場合に False を返す。そこで無条件に掃除すると、まだ走って
    いる analyzer を追える唯一の記録である pid ファイルが消え、`__main__.gc_orphans()`
    も再試行できなくなる (ハングした cursor が走り続けて課金され続ける)。GC 側と同じく
    **pid / 結果ファイルを対で残し**、TTL 超過後の GC に委ねる。

    **リーダーが走っていない場合も掃除の前に group を見る** (マージ前レビューの指摘)。
    リーダーが post の完了前に exit / crash しても、`pre` が作った独立 process group には
    孫が残りうる。リーダーの生死だけで「掃除してよい」と決めると、走り続ける group を
    追える唯一の記録 (pid / pgid) をここで消してしまい、GC の leaderless-group 経路も
    以後その group に手が届かない。`reap_orphan` と同じ `_reap_leaderless_group` を通し、
    生存メンバーが居れば停止を試み、未確定なら両ファイルを残す。

    結果の読み取り自体は best-effort で続ける (書きかけでも読めたぶんは返す)。掃除しない
    だけなので、次に読む主体は GC (中身を見ずに消す) しか居らず二重注入にはならない。
    """
    result_file, pid_file = paths(NAME, tool_use_id)
    unconfirmed = False

    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid = None
        # mtime は待機の前に読む (掃除後には取れない)。取れなければ停止をあきらめる側。
        started_at = _started_at(pid_file)

        # pid <= 0 は group 判定の宛先にしない (`killpg(0, sig)` は**呼び出し側自身の
        # process group** = hook プロセスを撃つ)。`reap_orphan` 側と同じガード。
        if pid is not None and pid > 0:
            waited = 0
            while waited < TIMEOUT_SEC and _is_running(pid):
                time.sleep(POLL_INTERVAL_SEC)
                waited += POLL_INTERVAL_SEC

            if _is_running(pid):
                if terminate(pid, started_at):
                    log(f"timeout ({TIMEOUT_SEC}s) — killed")
                else:
                    unconfirmed = True
                    log(
                        f"timeout ({TIMEOUT_SEC}s) — 停止を確認できない。"
                        "pid / 結果ファイルを残して GC に委ねる"
                    )
            elif _reap_leaderless_group(pid, started_at) == REAP_UNCONFIRMED:
                unconfirmed = True
                log(
                    "リーダー亡き後の残存 group を停止できない。"
                    "pid / 結果ファイルを残して GC に委ねる"
                )

        if not unconfirmed:
            cleanup(pid_file)

    if not result_file.is_file():
        return None

    try:
        raw = result_file.read_bytes()[:MAX_OUTPUT_BYTES]
        data = raw.decode("utf-8", errors="replace").strip()
    except OSError:
        data = ""
    finally:
        if not unconfirmed:
            cleanup(result_file)

    if not data:
        return None

    return _CONTEXT_HEADER + data


def reap_orphan(pid_file: Path, deadline: float | None = None) -> str:
    """TTL 超過の pid ファイルが指す analyzer を停止し、**停止の確度**を返す (残骸 GC 用)。

    post が来なかった経路 (Agent ツールの失敗・ユーザー中断・セッション終了、および
    `async` hook が `claude -p` の teardown で kill された場合) では停止も掃除も
    走らないため、`state.stale_entries` が拾った残骸をここで止める。

    戻り値は `state.REAP_*` (マージ前レビューの指摘):

    - `REAP_STOPPED`: 走っていない / pid 記録が壊れていて止める対象を特定できない
    - `REAP_SIGNALED`: 停止 signal の送出を実際に試みた
    - `REAP_UNCONFIRMED`: **まだ走っている** (リーダー or group のメンバー) のに、
      同一性を確認できない / signal を送出できなかった

    呼び出し側 (`__main__.gc_orphans`) は `REAP_UNCONFIRMED` のとき pid ファイルを残す。
    ここで戻り値を持たせるまでは「停止できなくても無条件に掃除」していたため、`ps` が
    一時的に使えない・cmdline が切り詰められた等で同一性を確認できなかった孤児は、
    唯一の追跡手段である pid 記録ごと消えて二度と GC の対象にならなかった。

    **リーダーが死んでいても終わりではない** (マージ前レビューの指摘)。`pre` が作った
    独立 process group には孫が残りうるので、`_reap_leaderless_group` で group 側を見る。

    リーダーの生死は `_is_running` (**zombie は死んだ扱い**) で見る。zombie を走行中と
    読むと `terminate()` へ進み、`ps` が `<defunct>` しか返さないので同一性を確認できず、
    毎回 `REAP_UNCONFIRMED` を返して pid / 結果ファイルが永遠に残る。

    **`deadline` は GC の残り予算** (time.monotonic 基準。None = 予算なし) で、
    停止処理の内部まで持ち回る (マージ前レビューの指摘)。GC 側の予算チェックは
    エントリ**間**にしか無かったため、1 エントリの停止処理が `ps` の timeout (各 2 秒を
    複数回) と TERM の猶予 (2 秒) を積み上げ、`hooks.json` の同期 PreToolUse timeout
    (5 秒) を超えて hook 自体が kill されうる — そうなると現在の analyzer を起動できず、
    次回もまた同じ孤児から処理して同じところで死ぬ。予算が尽きたら
    **`REAP_UNCONFIRMED` で戻して記録を残す** (掃除しない側 = 次回に再試行できる側)。
    """
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return REAP_STOPPED
    if pid <= 0:
        return REAP_STOPPED
    if _out_of_budget(deadline):
        log(f"GC の残り予算が尽きた (pid {pid}) — 未確定として記録を残す")
        return REAP_UNCONFIRMED
    started_at = _started_at(pid_file)
    if _is_running(pid, deadline):
        if not terminate(pid, started_at, deadline):
            return REAP_UNCONFIRMED
        log(f"孤児 analyzer (pid {pid}) を停止")
        return REAP_SIGNALED
    return _reap_leaderless_group(pid, started_at, deadline)


def _reap_leaderless_group(
    pgid: int, started_at: float | None, deadline: float | None = None
) -> str:
    """リーダーが exit / crash した後も残っている process group を停止する。

    `pre` は `start_new_session=True` で起動するので pgid == リーダーの pid。リーダーが
    先に死んでも、cursor-agent (node) の孫は同じ group に残って走り続けることがある。
    リーダーだけを見て `REAP_STOPPED` を返すと、GC が pid 記録を消した時点で group を
    撃つ機会が永久に失われる (課金が続く)。

    リーダーの cmdline はもう読めないので、同一性は 2 つで見る:

    1. group に**生きた (非 zombie) メンバーが居る**こと。pgid の番号はメンバー
       (zombie 含む) が残っている限り新しい pid として再割当てされない
       (`_common.subproc` の kill 経路と同じ前提) ので、生存メンバーが居る group は
       起動時に作った group とみなせる
    2. 生きたメンバーの**開始時刻がいずれも記録した起動時刻より前でない**こと。
       メンバーは analyzer の子孫なので、起動時刻 (pid ファイルの mtime) 以降に
       生まれているはず

    判定不能 (`ps` が使えない・開始時刻を読めるメンバーが居ない・mtime が取れない・
    `deadline` の残り予算が尽きた) はいずれも `REAP_UNCONFIRMED` = 送らない側に倒し、
    記録を残して次回の GC に委ねる。

    **限界**: group が一度完全に空になってから pgid の番号が再利用され、その新しい
    リーダーも既に死んでいる、という二重の偶然までは弾けない (リーダーが生きている経路と
    違って cmdline を照合できないため)。2. で「記録より前から居る group」は落とせる。
    """
    if subproc.group_is_stopped(pgid, timeout_sec=_ps_budget(deadline)):
        return REAP_STOPPED  # メンバーが居ない / zombie だけ = 止めるものが無い
    if not _group_is_analyzer(pgid, started_at, deadline):
        log(f"pgid {pgid} の残存 group を analyzer と確認できない — signal を送らない")
        return REAP_UNCONFIRMED
    if not _stop_group(pgid, deadline):
        return REAP_UNCONFIRMED
    log(f"孤児 analyzer の残存 group (pgid {pgid}) を停止")
    return REAP_SIGNALED


def terminate(pid: int, started_at: float | None, deadline: float | None = None) -> bool:
    """analyzer の process group を停止する (SIGTERM → 猶予 → SIGKILL)。

    戻り値は **停止に漕ぎ着けたか** (group が止まった / SIGKILL まで送出できた /
    送出前に group が空になっていた)。同一性を確認できないとき、および group が
    生きたまま SIGKILL を送出できなかったときは False で、呼び出し側は
    「停止未確定」として pid / 結果ファイルを残す。

    `pre` は `start_new_session=True` で起動しているので **pgid == pid**。0.9.1 までは
    `os.kill(pid, SIGTERM)` でグループリーダーだけを止めており、cursor-agent (node) が
    生成した孫プロセスが取り残されて走り続けていた (課金・CPU のリーク)。`_common.subproc`
    が review 系 2 hook で使っている `killpg` と同じ考え方に揃える。

    signal を送る前に **pid が本当に自分が起動した analyzer か** を cmdline の署名と
    プロセスの開始時刻で確認する。pid ファイルは TTL 超過まで残りうるので、その間に pid が
    別プロセスへ再利用されていることがある。**判定できないときは送らない側に倒す** —
    無関係なプロセスに SIGTERM を送る事故のほうが、cursor を 1 つ取り残すより重い。

    `started_at` には **pid ファイルの mtime (= 起動時刻)** を渡す (`_started_at`)。
    None (mtime が取れない) は送らない側。

    `deadline` (time.monotonic 基準。None = 予算なし) を渡すと、同一性確認の `ps` と
    TERM の猶予をその残り予算で cap する (`reap_orphan` 参照)。予算切れは「判定不能」=
    送らない側に倒れるので、hook timeout を食い潰す方向には決して倒れない。
    """
    if not _is_analyzer(pid, started_at, deadline):
        log(f"pid {pid} は起動した analyzer と一致しない — signal を送らない")
        return False
    return _stop_group(pid, deadline)


def _stop_group(pgid: int, deadline: float | None = None) -> bool:
    """process group に SIGTERM → 猶予 → SIGKILL を送る。**停止に漕ぎ着けたら True**。

    判定は「signal を送れたか」ではなく **「group が止まったか、または SIGKILL まで
    送出できたか」**:

    - 猶予後に group が停止していれば True (TERM で止まった / 元から空だった)
    - 止まっていなければ SIGKILL を送り、**送出できたときだけ** True
    - SIGKILL を送出できず group がまだ生きていれば **False** (= 停止未確定)

    0.10.0 の途中までは `os.killpg` の例外を握りつぶして無条件に True を返しており、
    TERM も KILL も送れていない (権限が無い等) のに呼び出し側が `REAP_SIGNALED` と読み、
    まだ走っている group の pid 記録を消していた。それを直した後も **TERM の送出成功が
    OR で残る**ため、「TERM は届いたが無視され、SIGKILL が `PermissionError` 等で
    送出できなかった」経路が True のまま報告されていた (マージ前レビューの指摘) —
    group は生きているのに pid 記録が消え、以後どの GC もその孤児に手が届かない。

    `deadline` (time.monotonic 基準) を渡すと **TERM の猶予と生死判定の `ps` を残り予算で
    cap する**。GC は 5 秒の PreToolUse hook の中で回るため、猶予 2 秒 + `ps` 2 秒を
    そのまま消費すると hook 自体が kill されて次の analyzer を起動できない。
    """
    _signal_group(pgid, signal.SIGTERM)
    grace_until = time.monotonic() + _remaining(deadline, KILL_GRACE_SEC)
    # 猶予中の probe は安い `killpg(pgid, 0)` で回す (リーダーだけでなく孫も数える)。
    # zombie が残る環境ではここが空にならないので、猶予後に zombie を除いた判定
    # (`group_is_stopped`) を 1 回だけ通してから SIGKILL に切り替える。
    while time.monotonic() < grace_until and _group_exists(pgid):
        time.sleep(min(KILL_POLL_SEC, max(0.0, grace_until - time.monotonic())))
    if subproc.group_is_stopped(pgid, timeout_sec=_ps_budget(deadline)):
        return True
    return _signal_group(pgid, signal.SIGKILL)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True  # メンバーは居る (signal を送る権限が無いだけ)
    except OSError:
        return False


def _started_at(pid_file: Path) -> float | None:
    """pid ファイルの mtime (= analyzer の起動時刻)。取れなければ None。"""
    try:
        return pid_file.stat().st_mtime
    except OSError:
        return None


def _remaining(deadline: float | None, cap: float) -> float:
    """`deadline` (time.monotonic 基準) までの残り時間を `cap` で頭打ちにして返す。

    `deadline` が None (予算なし = `post()` 経路。async hook なので harness の timeout が
    掛からない) は `cap` をそのまま返す。残りが無ければ 0 以下を返し、呼び出し側は
    「待たない / `ps` を起動しない」= 判定不能側に倒す。
    """
    if deadline is None:
        return cap
    return min(cap, deadline - time.monotonic())


def _out_of_budget(deadline: float | None) -> bool:
    """`deadline` に達しているか (None は予算なしなので常に False)。"""
    return deadline is not None and time.monotonic() >= deadline


def _ps_budget(deadline: float | None) -> float:
    """`ps` 1 回に許す秒数 (残り予算を既定の上限 `_PS_TIMEOUT_SEC` で頭打ちにする)。"""
    return _remaining(deadline, _PS_TIMEOUT_SEC)


def _is_analyzer(
    pid: int, started_at: float | None, deadline: float | None = None
) -> bool:
    """pid が「その時起動した自分の analyzer」か。

    2 段で見る:

    1. cmdline が `cursorcli.readonly_argv` の起動形と一致するか
    2. プロセスの開始時刻が `started_at` (pid ファイルの mtime) 以前か

    署名だけでは足りない。同じ `readonly_argv` で cursor を起動する hook が本 plugin 内に
    他にもあり (プラン / 実装後のレビュー)、TTL 超過まで残った pid ファイルの pid が
    それらに再利用されていると、署名照合を素通りして無関係なレビューを `killpg` で撃つ。
    `pre` は Popen 直後に pid ファイルを書くので、自分の analyzer なら開始時刻は必ず
    mtime 以前になる。再利用された pid は mtime より後に起動しているので弾ける。

    判定不能 (ps が使えない・出力が空・mtime が取れない・`deadline` の残り予算が尽きた)
    はいずれも False = 送らない側。
    """
    cmdline = subproc.pid_command(pid, timeout_sec=_ps_budget(deadline))
    if not cmdline:
        return False
    tokens = cmdline.split()
    if not all(t in tokens for t in _SIGNATURE_TOKENS):
        return False
    return _started_before(pid, started_at, deadline)


def _group_is_analyzer(
    pgid: int, started_at: float | None, deadline: float | None = None
) -> bool:
    """リーダー亡き後の残存 group が「その時起動した自分の analyzer」の残りか。

    メンバーは analyzer の子孫なので、**開始時刻は記録した起動時刻 (pid ファイルの
    mtime) 以降**になる。それより前から居るメンバーが 1 つでもあれば、pgid の番号が
    別の group に使われている (= 撃ってはいけない)。判定不能はすべて False。
    """
    if started_at is None:
        return False
    starts = _live_group_start_times(pgid, deadline)
    if not starts:
        # None (`ps` が使えない / 残り予算が尽きた) / 空 (開始時刻を読めるメンバーが
        # 居ない) — どちらも「メンバーが自分の子孫だと確認できていない」ので送らない側
        return False
    return all(s >= started_at - _START_SKEW_SEC for s in starts)


def _live_group_start_times(
    pgid: int, deadline: float | None = None
) -> list[float] | None:
    """pgid に属する非 zombie メンバーの開始時刻 (epoch 秒) 一覧。取得できなければ None。

    メンバー**単位**の開始時刻が要るので、`subproc` の group 生死判定 (bool) ではなく
    `ps` を 1 回呼んで pid / pgid / stat / etime をまとめて取る。`/proc` からも開始時刻は
    出せるが clock tick と boot 時刻の換算が要るうえ、`ps` が無い環境では結局どの
    メンバーの開始時刻も読めない (= 未確定側に倒れる) ので ps 一本にしてある。

    `etime` を解析できなかったメンバーは一覧に含めない (走査中に exit した等)。

    `deadline` を渡すと `ps` の timeout を残り予算で cap する。予算切れなら `ps` を
    起動せず None (判定不能 = 送らない側)。
    """
    timeout = _ps_budget(deadline)
    if timeout <= 0:
        return None
    try:
        res = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat=,etime="],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    now = time.time()
    starts: list[float] = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            member_pgid = int(parts[1])
        except ValueError:
            continue
        if member_pgid != pgid or parts[2].startswith("Z"):
            continue
        elapsed = subproc.parse_etime(parts[3])
        if elapsed is not None:
            starts.append(now - elapsed)
    return starts


def _started_before(
    pid: int, started_at: float | None, deadline: float | None = None
) -> bool:
    """pid の開始時刻が `started_at` (+ 許容ずれ) 以前か。判定不能は False。"""
    if started_at is None:
        return False
    elapsed = subproc.pid_elapsed_sec(pid, timeout_sec=_ps_budget(deadline))
    if elapsed is None:
        return False
    return (time.time() - elapsed) <= started_at + _START_SKEW_SEC


def _signal_group(pid: int, sig: signal.Signals) -> bool:
    """pgid == pid の process group にまとめて signal を送る。**送出できたら True**。

    - 送出成功 → True
    - `ProcessLookupError` (group にメンバーが居ない) → True。止めるものが無い =
      目的は達成されている
    - `PermissionError` / その他の `OSError` → **False**。送れていないので、呼び出し側は
      停止未確定として扱う
    """
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False


def _is_running(pid: int, deadline: float | None = None) -> bool:
    """pid が「止める対象として走っている」か。**zombie は走っていない扱い**。

    `os.kill(pid, 0)` だけでは足りない (マージ前レビューの指摘)。PID 1 が孤児を reap
    しないコンテナでは、処理を終えたリーダーが zombie として残り `os.kill(pid, 0)` が
    成功し続ける。それを「走行中」と読むと停止経路 (`terminate`) に進むが、zombie の
    `ps` は cmdline を返さない (`<defunct>`) ため同一性を確認できず、`post()` も GC も
    毎回 `REAP_UNCONFIRMED` に倒れて pid / 結果ファイルが永遠に残る (孤児は既に居ないのに
    GC が一生収束しない)。zombie は**停止済み**として group 側の判定
    (`_reap_leaderless_group`) に進める — 止めるべき孫が group に残っていればそこで撃てる。

    判定不能 (`pid_is_zombie` が None = `/proc` も `ps` も使えない・`deadline` の残り予算が
    尽きた) は `os.kill` の結果に従う = 走行中側に倒す (同一性を確認したうえで停止を試みる
    側。予算切れならその同一性確認が判定不能になり、記録を残して次回へ送られる)。
    テストヘルパー (`tests/test_orphan_gc.py` の `_alive`) と同じ契約。
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return subproc.pid_is_zombie(pid, timeout_sec=_ps_budget(deadline)) is not True
