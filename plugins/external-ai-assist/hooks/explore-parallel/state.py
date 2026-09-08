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

import os
import time
from pathlib import Path

BASE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "explore-parallel"

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
