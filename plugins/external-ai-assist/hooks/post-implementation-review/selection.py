"""差分レビューをどの外部 AI backend に送るか (0.12.0)。

## 不変条件: 既定の送信先は増やさない

**`DEFAULT_BACKENDS` は cursor のみ** = 0.11.0 までと同じ送信先。`_common/backends` の
registry に backend を足しても、この plugin を更新しただけの利用者の差分が新しい
サービスへ送られることは無い。送信先を増やすのは
`EXTERNAL_AI_POST_REVIEW_BACKENDS` を**自分で書いたとき**だけ。

同じ理由で**設定は環境変数のみ**にしてある。プロジェクト側の設定ファイル
(`<repo>/.claude/external-ai-assist/config.json` のようなもの) を読む経路を作ると、
clone してきた repo が「この repo では差分を外部サービス X にも送る」と宣言できて
しまう。env であれば `~/.claude/settings.json` か利用者自身の shell / プロジェクトの
`settings.json` を **利用者が trust した後** にしか効かない。

床テスト: `tests/test_selection.py::TestDefaultDestination`
(`test_codex_available_but_unset_backends_never_sends_to_codex`)。

## 設定

| 変数 | 既定 | 意味 |
|---|---|---|
| `EXTERNAL_AI_POST_REVIEW_BACKENDS` | `cursor` | 送信先の候補をカンマ区切りで列挙 |
| `EXTERNAL_AI_POST_REVIEW_STRATEGY` | 1 つなら `fixed` / 2 つ以上なら `alternate` | 候補の中からどれを先に試すか |

戦略:

| 値 | 試す順 |
|---|---|
| `fixed` | 列挙順そのまま (先頭が常に第一候補) |
| `available` | 列挙順のうち「今使えるもの」を先に、残りを後ろに |
| `alternate` | **このセッションで前回レビューを返した backend 以外**を先に |
| `random` | 列挙順をシャッフル |

`all` (同時送信) は**提供しない**。差分レビューは Stop のたびに走るので、同時送信は
送信量と課金を backend の数だけ倍にする (プランレビューと違い回数上限も無い)。

## フォールバックと待ち時間の上限

第一候補が結果を返せなければ**列挙した集合の中で**次の候補へ回す。集合の外へは出ない
(未知の名前は `backends.select` が落として通知に回す)。

待ち時間は `MAX_TOTAL_TIMEOUT_SEC` (= 全 backend の timeout 上限の最大 = 600 秒) を
基準に抑える。**2 つ目以降は「その backend の timeout + 停止処理の猶予」が残り予算に
収まるときだけ起動する**ので、1 回の Stop がレビューに費やす時間は
`worst_case_wall_sec()` (= 600 + 15 秒) を超えない — これは 0.11.0 の単一 backend
の最悪ケースと同じ値で、`hooks.json` の Stop timeout (690 秒) の予算計算は変わらない
(`tests/test_review_set.py::TestTimeoutBudgets`)。

帰結として、**第一候補が timeout いっぱい待って失敗した場合は次へ回らない**
(既定 300 秒なら 315 + 300 + 15 = 630 > 600)。フォールバックが効くのは
「候補が速く失敗したとき」(未インストール / 即エラー / 利用上限) で、そこがこの機能の
狙いでもある。全滅したターンは 0.11.0 と同じく**何も送らず pending に戻す**。

## 前回の backend の記録

`alternate` のために state に **1 値だけ** 持つ (`state.last_backend`)。パス単位の
記録も「レビュー済み hunk」の重複除去 state も作らない — 同じ行が Stop と後続の
commit レビューで 2 回見られるのは許容し、その 2 回目を別の backend に振って
クロスチェックにするのがこの値の目的。
"""
from __future__ import annotations

import random
import time

from _common import backends, settings, subproc

import codex
import cursor

#: この hook が使う backend。**registry の全件ではなく明示列挙**なので、
#: `_common/backends/registry.py` に backend を足してもここに書くまで候補にならない。
REVIEWERS = (cursor, codex)

#: `EXTERNAL_AI_POST_REVIEW_BACKENDS` 未設定時の送信先 (= 0.11.0 と同じ)。
#: **ここを広げることは全利用者の送信先を広げること**なので、変更は
#: `TestDefaultDestination` が落ちることで必ず目に入る。
DEFAULT_BACKENDS = (cursor.NAME,)

ENV_BACKENDS = "EXTERNAL_AI_POST_REVIEW_BACKENDS"
ENV_STRATEGY = "EXTERNAL_AI_POST_REVIEW_STRATEGY"

STRATEGY_FIXED = "fixed"
STRATEGY_AVAILABLE = "available"
STRATEGY_ALTERNATE = "alternate"
STRATEGY_RANDOM = "random"
STRATEGIES = (STRATEGY_FIXED, STRATEGY_AVAILABLE, STRATEGY_ALTERNATE, STRATEGY_RANDOM)

#: 1 回の Stop がレビューに使ってよい合計時間の基準 (秒)。**全 backend の timeout 上限の
#: 最大**から導出する — 特定 backend の定数を直に書くと、上限の違う backend が増えたとき
#: に `state.IN_FLIGHT_TTL_SEC` と hooks.json の予算が静かにずれる。
MAX_TOTAL_TIMEOUT_SEC = max(r.MAX_TIMEOUT_SEC for r in REVIEWERS)

#: 1 回の起動を timeout で打ち切るときの停止処理の最悪所要時間
#: (SIGTERM 待ち / SIGKILL 待ち / 最後の wait = 3 × `KILL_GRACE_SEC`)。
KILL_OVERHEAD_SEC = 3 * subproc.KILL_GRACE_SEC


def worst_case_wall_sec() -> float:
    """1 回の Stop がレビューに費やしうる最悪の実時間 (秒)。

    第一候補は残り予算を見ずに起動するので `MAX_TOTAL_TIMEOUT_SEC + KILL_OVERHEAD_SEC`
    まで伸びうる。2 つ目以降は起動前に `_fits_in_budget()` を通るため、合計が
    `MAX_TOTAL_TIMEOUT_SEC` を超えてから新しい起動が始まることはない。
    """
    return MAX_TOTAL_TIMEOUT_SEC + KILL_OVERHEAD_SEC


def configured() -> tuple[list, list[str]]:
    """`(送信先の候補, 未知の名前)`。未設定なら `DEFAULT_BACKENDS` (= cursor のみ)。

    未知の名前だけを書いた場合は候補が空になる (既定へ fallback しない)。タイプミスで
    「外したはずの backend が黙って走る」= 送信先が増える方向に倒さないため。
    """
    wanted = settings.names(ENV_BACKENDS)
    if wanted is None:
        wanted = list(DEFAULT_BACKENDS)
    return backends.select(REVIEWERS, wanted)


def available(chosen, deadline: float | None = None) -> list:
    """`chosen` のうち今起動できるもの (列挙順を保つ)。"""
    return [b for b in chosen if b.is_available(deadline)]


def any_available(deadline: float | None = None) -> bool:
    """設定された候補のうち 1 つでも起動できるか (pre-tool / post-tool の門番)。

    最初に見つかった時点で打ち切る。未設定なら cursor だけを見るので、0.11.0 までの
    `cursor.is_available(deadline)` と完全に同じ呼び出しになる。
    """
    chosen, _ = configured()
    return any(b.is_available(deadline) for b in chosen)


def strategy(count: int) -> str:
    """実効の戦略名。未設定・未知の値は候補数から決める (1 つなら fixed、2 つ以上なら alternate)。

    未知の値を既定へ倒すのは他の env と同じ方針 (タイプミスで機能が黙って止まらない)。
    """
    raw = settings.raw(ENV_STRATEGY).lower()
    if raw in STRATEGIES:
        return raw
    return STRATEGY_ALTERNATE if count >= 2 else STRATEGY_FIXED


def order(
    chosen,
    *,
    strategy_name: str,
    last: str | None = None,
    deadline: float | None = None,
) -> list:
    """試す順に並べ替える。**集合そのものは変えない** (候補を落とさない)。

    落とさないのは意図的で、`available` 戦略でも「今は使えないと判定された backend」を
    末尾に残す。検出は probe の予算切れ等で保留に倒れることがあり、そこで候補ごと
    捨てるとレビューが黙って止まる経路になる (送信先が増えるわけではないので、
    残す側が安全側)。
    """
    if strategy_name == STRATEGY_RANDOM:
        shuffled = list(chosen)
        random.shuffle(shuffled)
        return shuffled
    if strategy_name == STRATEGY_AVAILABLE:
        usable = {b.NAME for b in available(chosen, deadline)}
        return [b for b in chosen if b.NAME in usable] + [
            b for b in chosen if b.NAME not in usable
        ]
    if strategy_name == STRATEGY_ALTERNATE and last:
        return [b for b in chosen if b.NAME != last] + [
            b for b in chosen if b.NAME == last
        ]
    return list(chosen)


class Outcome:
    """`run_review()` の結果。

    dataclass にしていないのは、テストが hook のモジュール群を `sys.modules` 未登録の
    まま読む経路があるため (`__main__.ReviewBatch` と同じ理由)。
    """

    def __init__(self, strategy_name: str = STRATEGY_FIXED) -> None:
        self.strategy = strategy_name
        self.text: str | None = None
        self.backend: str | None = None
        #: 実際に起動した backend と結果 `[(name, STATUS_*)]` (起動順)
        self.attempts: list[tuple[str, str]] = []
        #: 残り予算が足りず起動しなかった backend 名
        self.skipped: list[str] = []
        #: 並べ替えた後の候補順 (ログ・テスト用)
        self.order: list[str] = []

    @property
    def is_ok(self) -> bool:
        return self.text is not None


def _fits_in_budget(backend, elapsed: float) -> bool:
    return elapsed + backend.timeout_sec() + KILL_OVERHEAD_SEC <= MAX_TOTAL_TIMEOUT_SEC


def run_review(
    diff_text: str,
    *,
    cwd: str | None,
    chosen,
    strategy_name: str,
    last: str | None = None,
    log=None,
    manifest: str = "",
    now=time.monotonic,
) -> Outcome:
    """候補を順に試し、最初に結果を返した backend の本文を持つ Outcome を返す。

    `manifest` は「何を送ったか」(パス名とバイト数) の一覧で、`log` が渡されていれば
    **起動するたびに backend 名と一緒に記録する**。送信先ごとの送信内容を後から追える
    ようにするためで、レビュー本文も diff 本文も書かない (`_common/notify.py` の線引きと
    同じ)。

    `now` はテストが時計を差し替えるための注入点 (予算の打ち切りを実時間を使わずに
    検証する)。
    """
    outcome = Outcome(strategy_name)
    candidates = order(chosen, strategy_name=strategy_name, last=last)
    outcome.order = [b.NAME for b in candidates]

    started = now()
    for index, backend in enumerate(candidates):
        if index > 0 and not _fits_in_budget(backend, now() - started):
            # 残り予算で起動すると Stop の hook timeout を踏み越えうる。踏み越えると
            # ハーネスの kill が先に来て claim を pending へ戻せなくなる (= このターンの
            # 変更が TTL まで沈黙する) ので、起動しない側に倒す
            outcome.skipped.append(backend.NAME)
            continue
        if log is not None:
            log(
                f"{backend.NAME} に差分を送信 ({len(diff_text.encode())} bytes)"
                + (f": {manifest}" if manifest else "")
            )
        text = backend.review(diff_text, cwd=cwd)
        if text:
            outcome.attempts.append((backend.NAME, backends.STATUS_OK))
            outcome.text = text
            outcome.backend = backend.NAME
            return outcome
        outcome.attempts.append((backend.NAME, backends.STATUS_FAILED))
        if log is not None:
            log(f"{backend.NAME}: 結果なし (timeout / 失敗)")
    return outcome
