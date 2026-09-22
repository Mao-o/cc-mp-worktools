"""backend の実行結果 (`Result`) と、`subproc` の戻り値からの分類。

## なぜ bool / `str | None` ではなく 3 値なのか

レビュー backend が結果を返さなかったとき、呼び出し側が知りたいのは「次の候補へ
回すべきか」だけでなく「なぜ回すのか」である:

| status | 意味 | 呼び出し側の扱い |
|---|---|---|
| `ok` | レビュー本文を取得できた | そのまま使う |
| `failed` | timeout / 起動失敗 / 非 0 終了 / 空出力 | 次の候補へ |
| `limit` | その CLI の利用上限に達していると **出力から判定できた** | 次の候補へ |

`failed` と `limit` はどちらも「次の候補へ」なので分岐そのものは同じだが、利用者への
通知と hooklog で区別できると「CLI が壊れている」のか「今月の枠を使い切った」のかが
分かる。

## `limit` を推測で作らない

**現時点で `from_captured()` は `limit` を返さない。** cursor / codex いずれについても、
利用上限に達したときの実際の出力 (終了コード・文言) を実機で確認できていないため、
パターンを推測で書くと 2 方向に壊れる:

- 正常なレビュー本文に「limit」「quota」等の語が含まれるだけで `limit` と誤判定し、
  **取得済みのレビューを捨てて別の backend へ送り直す** (送信量が倍になる)
- 実際の上限文言と違うパターンを書けば、そもそも検出できず `failed` と変わらない

したがって現状は**分からないものは `failed` に倒す**。実機で文言を確認できた backend
から `run()` 側で `limit()` を返すようにする (この module を変える必要は無い)。
"""
from __future__ import annotations

import subprocess

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_LIMIT = "limit"


class Result:
    """backend 1 回分の実行結果。`text` は `ok` のときだけ非 None。"""

    __slots__ = ("status", "text")

    def __init__(self, status: str, text: str | None = None) -> None:
        self.status = status
        self.text = text

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def is_limit(self) -> bool:
        return self.status == STATUS_LIMIT

    @property
    def is_failed(self) -> bool:
        return self.status == STATUS_FAILED

    def truncated(self, max_output_chars: int | None) -> Result:
        """本文を `max_output_chars` で切り詰めた新しい Result を返す。

        出力整形は hook 固有 (レビュー本文の上限は hook ごとの予算で決まる) なので、
        registry 側では切らずに呼び出し側がこれを使う。
        """
        if not self.is_ok or max_output_chars is None or self.text is None:
            return self
        return Result(self.status, self.text[:max_output_chars])

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        size = len(self.text) if self.text is not None else 0
        return f"Result({self.status!r}, {size} chars)"


def ok(text: str) -> Result:
    return Result(STATUS_OK, text)


def failed() -> Result:
    return Result(STATUS_FAILED)


def limit() -> Result:
    return Result(STATUS_LIMIT)


def from_captured(completed: subprocess.CompletedProcess | None) -> Result:
    """`subproc.run_captured()` の戻り値を Result に分類する。

    `subproc.run_for_output()` と同じ判定 (timeout / 起動失敗 / 非 0 終了 / 空出力は
    いずれも失敗) を、`str | None` ではなく Result で返す。**上限の検出は行わない**
    (module docstring「`limit` を推測で作らない」)。
    """
    if completed is None:
        return failed()  # timeout / 起動失敗 (コマンド不在等)
    if completed.returncode != 0:
        return failed()
    text = (completed.stdout or "").strip()
    if not text:
        return failed()
    return ok(text)
