"""hook 1 回分の実時間予算 (総予算) を管理する。

**なぜ必要か**: hook は `hooks/hooks.json` の `timeout` (秒) を超えると
Claude Code 側で打ち切られ、**出力が破棄されて tool call がそのまま進む**
(公式仕様上の fail-open)。CLI 未検出も CLI timeout も deny に倒している本 plugin
にとって、ここだけ無音で通るのは判定表の穴になる。

ところが個々の subprocess timeout (gh 10s / firebase 10s / aws 15s /
gcloud 10s×2 / kubectl 10s) は **1 コマンドあたり** の上限でしかない。
`gh ... && aws ... && gcloud ...` のような複合コマンドは service ごとに直列で
verify するため、最悪ケースの合計は hook timeout を大きく超える (内部バックログ)。

**やり方**: dispatcher が verify を始める前に `start()` で締切を置き、各 service は
subprocess を起動する直前に `call_timeout(<既定>)` を呼んで「残り予算」に丸めた
timeout を使う。残りが尽きたら dispatcher が `expired()` を見て、その service を
CLI 呼び出しなしで deny に集約する。

**並列化しない理由**: 並列 (ThreadPoolExecutor) は総所要を縮められるが、
`concurrent.futures` の worker は非 daemon スレッドで、インタプリタ終了時に
atexit で join される。ハングした subprocess を抱えたまま「予算切れ」を返しても
**プロセスが終了できず hook timeout に落ちる**ため、fail-open を塞ぐ目的には
そのままでは効かない。締切の伝播なら追加のスレッドなしで上限を保証できる。

**上限の見積り**: 予算切れの判定は verify 単位なので、判定直前に始まった呼び出し
と、その verify が内部で行う追加呼び出し (現状の最大は gcloud dict の 2 回) の分だけ
超過しうる。超過は最大 `MAX_CALLS_PER_VERIFY * MIN_CALL_TIMEOUT_SECONDS` 秒。
`TOTAL_BUDGET_SECONDS` との合計が hook timeout を下回ることは
`tests/test_budget.py` が hooks.json を読んで機械的に固定する。
"""
from __future__ import annotations

import time

# hook 1 回分の総予算 (秒)。hooks.json の timeout (20s) から、超過分と
# hook 起動・JSON 出力のオーバーヘッドを引いた値。
TOTAL_BUDGET_SECONDS = 15.0

# 予算が残っていても subprocess に渡す timeout をこれ未満にはしない。
# 0 秒近い timeout は「起動した瞬間に必ず TimeoutExpired」= 実質的に
# 検証不能な deny を量産するため、下限を切って「短いが意味のある試行」にする。
MIN_CALL_TIMEOUT_SECONDS = 1.0

# 1 回の verify() が行う subprocess 呼び出しの最大数 (gcloud の dict 期待値が
# project / account の 2 回)。予算超過の上限見積りに使う。
MAX_CALLS_PER_VERIFY = 2

_deadline: float | None = None


def start(total: float = TOTAL_BUDGET_SECONDS) -> None:
    """総予算の締切を「今から `total` 秒後」に設定する (再呼び出しで上書き)。"""
    global _deadline
    _deadline = time.monotonic() + total


def clear() -> None:
    """予算を解除する。以後 `call_timeout()` は既定値をそのまま返す。

    builder (`scripts/accounts_builder.py`) のように hook timeout の制約が
    無い経路では予算を張らない。
    """
    global _deadline
    _deadline = None


def _remaining() -> float | None:
    """残り予算 (秒)。予算未設定なら None。"""
    if _deadline is None:
        return None
    return _deadline - time.monotonic()


def remaining() -> float | None:
    """残り予算 (秒)。予算未設定なら None (負値もそのまま返す)。"""
    return _remaining()


def expired() -> bool:
    """予算を使い切っていれば True (予算未設定なら常に False)。"""
    r = _remaining()
    if r is None:
        return False
    return r <= 0.0


def call_timeout(default: float) -> float:
    """subprocess に渡す timeout を返す。

    予算未設定なら `default` そのまま。予算があれば「残り予算」で頭打ちにし、
    `MIN_CALL_TIMEOUT_SECONDS` を下限にする (0 秒 timeout を渡さない)。
    """
    remaining_sec = _remaining()
    if remaining_sec is None:
        return default
    return max(MIN_CALL_TIMEOUT_SECONDS, min(default, remaining_sec))


def worst_case_seconds(total: float = TOTAL_BUDGET_SECONDS) -> float:
    """総予算 `total` のときに hook が消費しうる実時間の上限 (秒)。

    予算切れの判定は verify の**手前**でしか行わないため、締切直前に始まった
    verify の分だけ超過する。1 回の verify が行う subprocess 呼び出しは最大
    `MAX_CALLS_PER_VERIFY` 回で、各呼び出しの timeout は
    `MIN_CALL_TIMEOUT_SECONDS` まで丸められる。
    """
    return total + MAX_CALLS_PER_VERIFY * MIN_CALL_TIMEOUT_SECONDS
