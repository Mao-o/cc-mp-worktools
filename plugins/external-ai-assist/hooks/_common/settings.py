"""`EXTERNAL_AI_*` 環境変数の共通パーサ (3 hook で命名規則と解釈を揃える)。

## 命名規則

    EXTERNAL_AI_<FEATURE>            機能そのものの on/off
    EXTERNAL_AI_<FEATURE>_<SETTING>  その機能の設定

`<FEATURE>` は hook ディレクトリ名に 1:1 で対応する:

| hook | FEATURE |
|---|---|
| `explore-parallel` | `EXPLORE_PARALLEL` |
| `exitplan-review` | `PLAN_REVIEW` |
| `post-implementation-review` | `POST_REVIEW` |

`EXTERNAL_AI_REVIEW_MAX` (exitplan-review のブロック回数) だけはこの規則より前からある
名前で、`0` を無効化スイッチとして使う運用が定着しているため改名しない。0.6.0 以降は
`EXTERNAL_AI_PLAN_REVIEW=0` が正規のスイッチで、両者は **AND** で効く。

## 解釈の方針

**不正な値は既定値に倒す** (hook は絶対に失敗させない / fail-open)。タイプミスで
レビューが黙って全停止するより、既定で動き続けるほうが事故が小さい。ただし
`duration()` の上限だけは clamp する — hooks.json の hook timeout は静的なので、
それを超える値を許すとハーネスの kill が先に来て後始末 (枠を戻す / pending を戻す) に
到達しない。
"""
from __future__ import annotations

import math
import os

_FALSY = frozenset(("0", "false", "off", "no"))
_TRUTHY = frozenset(("1", "true", "on", "yes"))

#: 各 hook のテストが「開発者 shell の設定でテストが揺れない」ことを保証するための接頭辞
ENV_PREFIX = "EXTERNAL_AI_"


def raw(name: str) -> str:
    return os.environ.get(name, "").strip()


def flag(name: str, default: bool = True) -> bool:
    """on/off スイッチ。`0` `false` `off` `no` が偽、`1` `true` `on` `yes` が真。

    未設定・解釈できない値は `default`。
    """
    value = raw(name).lower()
    if not value:
        return default
    if value in _FALSY:
        return False
    if value in _TRUTHY:
        return True
    return default


def _finite(value: str) -> float | None:
    """文字列を**有限の** float として読む。数値でない / 非有限なら None。

    `float()` は `nan` / `inf` / `-inf` / `Infinity` を大文字小文字を問わず受理する。
    これを素通しすると、環境変数のタイプミス 1 つで hook が壊れる:

    - **`nan`**: NaN との比較は常に False なので `parsed <= 0` の下限チェックを
      すり抜け、`min(nan, maximum)` も NaN のまま残る。これが
      `Popen.communicate(timeout=nan)` に渡ると `ValueError` になり、Stop hook が
      **claim を握ったまま**落ちて TTL (900s) まで pending が戻らない
    - **`inf` / `-inf`**: `int(inf)` は `OverflowError`、`int(nan)` は `ValueError`。
      `count()` は `float()` だけを try で囲っているのでこれらが外へ抜ける

    どちらも「設定を間違えた人が hook の故障で気付く」形なので、既定値に倒す。
    """
    try:
        parsed = float(value)
    except (ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def duration(name: str, default: float, maximum: float) -> float:
    """秒数。未設定・非数値・非有限・0 以下は `default`、`maximum` を超える値は clamp。

    `0` を「無効化」として解釈しないのは意図的で、無効化は `flag()` のスイッチの仕事。
    `EXTERNAL_AI_POST_REVIEW_TIMEOUT=0` が「即 timeout」と「無効」のどちらにも読めると、
    設定した本人にしか意図が分からない状態になる。
    """
    value = raw(name)
    if not value:
        return default
    parsed = _finite(value)
    # 小数はそのまま通す (timeout は秒の小数指定に意味がある)。整数を要求するのは
    # `count()` 側だけ — あちらは 0 が「無効化」なので切り捨てが機能の消失になる
    if parsed is None or parsed <= 0:
        return default
    return min(parsed, maximum)


def count(name: str, default: int = 0) -> int:
    """非負整数。`0` は「無効」を意味させてよい (`EXTERNAL_AI_REVIEW_MAX=0` 等)。

    検証は 3 段で、**先に落ちたものが勝つ**:

    | 段 | 弾くもの | 結果 |
    |---|---|---|
    | 1. 有限か (`_finite`) | 未設定 / 非数値 / `nan` / `inf` | `default` |
    | 2. 整数か (`is_integer`) | `0.5` `1.5` `-0.5` などの小数 | `default` |
    | 3. 範囲 (`max(0, ...)`) | 負数 | `0` |

    **2 段目は「値が整数か」であって「表記が整数か」ではない**。`600.0` / `6e2` /
    `2.0` は整数値なので受理する (`int("600.0")` は `ValueError` なので、`int()` 直呼び
    だと `COOLDOWN_SEC=1800.0` が `default` = 0 = 無効に落ちて抑制が黙って効かない)。

    小数を切り捨てずに `default` へ倒すのは、**この関数の 0 が「無効化」という特別な
    意味を持つ**から。`EXTERNAL_AI_REVIEW_MAX=0.5` を切り捨てると 0 = 無効になり、
    打ち間違いが「既定で動く」でも「エラーで気付く」でもなく **黙って機能が消える** に
    着地する。`duration()` が小数を受理する (2 段目を持たない) のは、timeout には
    秒の小数指定に意味があり、かつ 0 以下を既定へ倒すので同種の事故が起きないため。
    """
    value = raw(name)
    if not value:
        return default
    parsed = _finite(value)
    if parsed is None or not parsed.is_integer():
        return default
    return max(0, int(parsed))


def names(name: str) -> list[str] | None:
    """カンマ区切りの識別子リスト (小文字化)。未設定・空要素のみなら None。

    None は「絞り込み指定なし」= 呼び出し側の既定全件。区切り文字だけを書いて
    「全部外す」を表す経路は作らない (無効化は on/off スイッチの仕事)。
    """
    value = raw(name)
    if not value:
        return None
    parsed = [item.strip().lower() for item in value.split(",") if item.strip()]
    return parsed or None
