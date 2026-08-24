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


def duration(name: str, default: float, maximum: float) -> float:
    """秒数。未設定・非数値・0 以下は `default`、`maximum` を超える値は clamp する。

    `0` を「無効化」として解釈しないのは意図的で、無効化は `flag()` のスイッチの仕事。
    `EXTERNAL_AI_POST_REVIEW_TIMEOUT=0` が「即 timeout」と「無効」のどちらにも読めると、
    設定した本人にしか意図が分からない状態になる。
    """
    value = raw(name)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return min(parsed, maximum)


def count(name: str, default: int = 0) -> int:
    """非負整数。未設定・非数値は `default`、負数は 0。`0` は「無効」を意味させてよい。

    `int()` ではなく `float()` を通してから丸める。`int("600.0")` / `int("6e2")` は
    `ValueError` になるため、`int()` だけだと `COOLDOWN_SEC=1800.0` のような書き方が
    `default` (= 0 = 無効) に落ちて **設定したつもりの抑制が黙って効かない**。
    `duration()` は同じ文字列を受け付けるので、揃えないと変数ごとに解釈が変わる。
    """
    value = raw(name)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
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
