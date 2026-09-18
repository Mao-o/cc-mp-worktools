"""tool_use_id ベースの一時ファイルパス管理と、残骸の TTL GC。

パス命名は `<name>-<tool_use_id>.{txt,pid}` とし、複数アナライザが同時実行されても
衝突しないようにする。

0.9.1 までは `post()` が正常に来たときの後始末しか無く、post が来ない経路
(Agent ツールの失敗・ユーザー中断・セッション終了、および `async` hook が
`claude -p` の teardown で kill される経路) では pid / 結果ファイルが無期限に
残り、バックグラウンドの cursor も自然完了まで走り続けていた (課金)。
`stale_entries` が TTL 超過の残骸を拾い、`__main__.py` が pre / post 双方で掃除する。
"""
from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "explore-parallel"

#: 同時に走らせる analyzer の既定上限 (`__main__.max_concurrent()` が env で上書きする)。
#:
#: Claude は 1 メッセージで複数の Explore を並列起動するのが通常なので、上限が無いと
#: その数だけ cursor が同時に走り、CPU と利用量がターンごとに線形に増える。既定を 2 に
#: したのは、1 だと「2 本目以降の Explore には補助調査が一切付かない」= 並走の価値が
#: ほぼ消えるのに対し、2 なら主要な 2 本には付きつつ増え方を頭打ちにできるため。
#: 注入量も同時起動数で決まる (`cursor.max_output_bytes()` の docstring)。
DEFAULT_MAX_CONCURRENT = 2

#: `launch_gate()` がロックを取れるまで待つ上限 (秒) と probe 間隔。
#:
#: pre の hook timeout は 5 秒で、GC (`GC_BUDGET_SEC` = 2 秒) と同じ呼び出しの中で回る。
#: 起動処理そのものは Popen + pid ファイル書込だけなので待つのは一瞬で足り、待ちきれ
#: なければ起動を諦める (= 同時起動数を超えない側に倒す)。
LAUNCH_GATE_WAIT_SEC = 0.5
LAUNCH_GATE_POLL_SEC = 0.02

#: 残骸とみなすまでの経過時間 (秒)。**pid ファイルの mtime = 起動時刻** で測る。
#:
#: 下限の根拠: analyzer の待機上限 (`cursor.TIMEOUT_SEC`) は 60 秒だが、post hook 自体が
#: `async` で走るため `claude -p` の teardown で kill されうる (公式 docs: outcome
#: `cancelled`)。その場合 cursor は自然完了まで走るので、TTL は「まだ書いている最中の
#: analyzer を消さない」余裕を持たせる必要がある。60 秒予算の 15 倍を取る。
ORPHAN_TTL_SEC = 900

#: GC 1 回あたりの打ち切り時間 (秒)。pre の hook timeout は 5 秒しかないので、
#: 残骸が大量にあっても起動を遅らせない。取りこぼしは次回の GC が拾う。
GC_BUDGET_SEC = 2.0

#: analyzer の `reap_orphan()` が返す停止の確度。GC が「掃除してよいか」を決めるのに使う。
#:
#: pid ファイルは**その孤児を追える唯一の記録**なので、停止を確認できていない状態で消すと
#: 次回以降の GC が再試行できなくなる (走り続ける cursor が課金され続ける)。
#: 確認できた場合だけ消し、`REAP_UNCONFIRMED` は残して次回の GC に委ねる。
REAP_STOPPED = "stopped"  #: 走っていない (または pid 記録が壊れていて止める対象が無い)
REAP_SIGNALED = "signaled"  #: 停止 signal の送出を実際に試みた
REAP_UNCONFIRMED = "unconfirmed"  #: まだ走っているが同一性を確認できない / 停止を試みられない


def paths(name: str, tool_use_id: str) -> tuple[Path, Path]:
    """(result_file, pid_file) のタプルを返す。親ディレクトリも作成する。"""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    result_file = BASE_DIR / f"{name}-{tool_use_id}.txt"
    pid_file = BASE_DIR / f"{name}-{tool_use_id}.pid"
    return result_file, pid_file


def cleanup(*files: Path) -> None:
    """ファイルを削除する。存在しない場合は無視。"""
    for f in files:
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def live_analyzer_count() -> int:
    """pid ファイルが指すプロセスのうち、まだ生きているものの件数 (同時起動数)。

    判定は `os.kill(pid, 0)` **だけ**で行う (`ps` を起動しない)。pre の hook timeout は
    5 秒しかなく、同じ呼び出しで GC も回るため subprocess を増やしたくない。

    **zombie は「生きている」側に数える** (`os.kill(pid, 0)` が成功する)。枠を 1 つ
    余分に塞ぐだけで、倒れる方向は「起動しない」= 利用量が増えない側。zombie を残す
    pid ファイルは post / TTL GC が掃除するので恒久的には詰まらない。

    signal を送る権限が無いプロセス (`PermissionError`) も生きている扱い。共有
    `$TMPDIR` で他ユーザーの pid ファイルを読んだ場合に起きうるが、「上限に達している」
    と読んで起動を控える方向なので安全側。

    **現在の tool_use_id を除外しない**のは意図的: 同じ tool_use_id で pre が二重に
    呼ばれたとき、`cursor.pre()` は pid ファイルを上書きして前のプロセスを追えなくする
    (孤児化する)。数に入れておけば上限側で二重起動を止められる。
    """
    try:
        names = os.listdir(BASE_DIR)
    except OSError:
        return 0

    alive = 0
    for filename in names:
        if not filename.endswith(".pid"):
            continue
        try:
            pid = int((BASE_DIR / filename).read_text().strip())
        except (OSError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            alive += 1
        except OSError:
            continue  # 既に終了している (pid ファイルは GC / post が掃除する)
        else:
            alive += 1
    return alive


@contextmanager
def launch_gate() -> Iterator[bool]:
    """同時起動数の数え上げと起動を直列化する (取れなければ False を yield)。

    Claude は 1 メッセージで複数の Explore を並列起動するため、PreToolUse hook 自体が
    同時に複数走りうる。数え上げと `pre()` を排他にしないと、どちらも「まだ枠がある」と
    読んで上限を超えて起動する。

    - **取れなかった場合は False** = この回は起動しない。別の pre が今まさに起動処理を
      しているので、待って数え直すより諦めるほうが上限を超えない側に倒れる
      (`LAUNCH_GATE_WAIT_SEC` だけは待つ — 起動処理は Popen + pid 書込だけなので
      通常はすぐ空く)
    - **ロックファイルを作れない環境では True** (直列化を諦めて進む。post-implementation
      -review の `cursor_lock` と同じ fail-open。ロックが作れないことで並走機能そのものが
      止まるほうが利用者にとって驚きが大きい)

    flock はプロセス終了時にカーネルが解放するので、hook が kill されてもロックは残らない
    (TTL は不要)。`Popen` は既定で fd を子に渡さないため、起動した cursor がロックを
    握り続けることもない。

    **共有 `$TMPDIR` で他ユーザーが先に `launch.lock` を作れる**点は塞いでいない
    (review 系 2 hook の `ensure_private_root` に相当する検査を explore-parallel は
    まだ持っていない)。悪用されても起きるのは「並走が起動しない」だけで、外部への
    送信範囲は広がらない。
    """
    path = BASE_DIR / "launch.lock"
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield True
        return

    try:
        acquired = _acquire_with_deadline(fd)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _acquire_with_deadline(fd: int) -> bool:
    deadline = time.monotonic() + LAUNCH_GATE_WAIT_SEC
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LAUNCH_GATE_POLL_SEC)


def stale_entries(
    ttl_sec: float | None = None,
    *,
    exclude_tool_use_id: str = "",
    now: float | None = None,
) -> list[tuple[str, Path, Path]]:
    """TTL を超えた残骸を `(analyzer 名, result_file, pid_file)` の一覧で返す。

    経過時間は **pid ファイルの mtime (= 起動時刻)** で測る。結果ファイルの mtime は
    analyzer が書くたびに更新されるので、走り続けている孤児ほど新しく見えてしまい
    「止めたい対象ほど残る」逆転が起きる。pid ファイルが無い (結果だけ残った) 場合は
    結果ファイルの mtime で代用する。

    `exclude_tool_use_id` には実行中の tool_use_id を渡す。TTL があるので通常は
    掛からないが、自分が今起動したばかりのプロセスを GC が撃つ経路を構造的に潰す。

    `ttl_sec` 省略時は `ORPHAN_TTL_SEC` を**呼び出しのたびに**読む (既定引数に束縛
    しない)。テストが TTL を短縮して「本当に走っている孤児」を扱えるようにするため。
    pid ファイルの mtime を過去へずらす方式では、プロセスの開始時刻が mtime より後に
    なり `cursor` 側の PID 同一性判定 (再利用ガード) から見て別プロセスに見えてしまう。
    """
    ttl_sec = ORPHAN_TTL_SEC if ttl_sec is None else ttl_sec
    now = time.time() if now is None else now
    try:
        names = os.listdir(BASE_DIR)
    except OSError:
        return []

    entries: list[tuple[str, Path, Path]] = []
    seen: set[str] = set()
    for filename in sorted(names):
        stem, ext = os.path.splitext(filename)
        if ext not in (".pid", ".txt") or stem in seen:
            continue
        analyzer, sep, tool_use_id = stem.partition("-")
        if not sep or not analyzer or tool_use_id == exclude_tool_use_id:
            continue
        seen.add(stem)
        result_file, pid_file = BASE_DIR / f"{stem}.txt", BASE_DIR / f"{stem}.pid"
        age_source = pid_file if pid_file.is_file() else result_file
        try:
            if now - age_source.stat().st_mtime <= ttl_sec:
                continue
        except OSError:
            continue
        entries.append((analyzer, result_file, pid_file))
    return entries
