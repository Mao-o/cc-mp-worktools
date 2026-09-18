"""Stop hook 全体で共有する時間予算 (0.32.0、内部バックログ)。

## なぜ必要か

`hooks.json` の Stop timeout は **15s**。到達すると Claude Code は hook を
kill して出力を discard するので、**decision なし = 機密ファイルの報告が
1 byte も出ない**「無音の fail-open」になる (公式 hooks docs の Timeouts 節で
確定。`docs/DESIGN.md` の Step 0-c)。つまり 15s を超えることは「検査したが
報告できなかった」ではなく「検査そのものが無かったことになる」に等しい。

にもかかわらず、個々の処理には**呼出単位の上限しか無かった**:

- `checker._run_git_raw` の `subprocess.run(timeout=10)` が 1 回 10s。
  1 回の Stop 実行で `rev-parse` / `ls-files --recurse-submodules` (+ fallback) /
  `ls-files --others` / submodule のネスト段数ぶんの `ls-files --stage` を
  **直列**に呼ぶため、合計は容易に 15s を超えうる
- `is_sensitive` × ファイル数 × rule 数 のマッチングには上限が無い
  (内部バックログのレビュー補正では、こちらが実際の律速だった局面もある)

どちらが律速かに関わらず、**hook 全体の締切を 1 つ持って全経路で共有する**のが
正しい形。本モジュールはそのための最小の道具だけを持つ。

## 方針

- 予算は `DEFAULT_BUDGET_SECONDS` (12s) — hook timeout 15s に対して 3s の余裕。
  余裕を残すのは、予算超過を検出した後に **reason を組んで stdout に書き切る**
  時間が要るため (超過を検出できても出力前に kill されたら意味が無い)。
  `hooks.json` の timeout とは別ファイルなので、
  `tests/test_checker.py::TestSharedGitBudget
  .test_budget_stays_below_the_configured_hook_timeout` が両者を突合する
  (timeout を下げると予算が黙って無意味になり、無音 fail-open に戻るため)
- **トレードオフ**: 12〜15s かかっていた repo では、予算導入前は完走して全件
  報告できていたのに本予算では打ち切って一覧が減る。15s に到達すれば報告が
  丸ごと消える (kill + discard) ので「部分報告 + 打ち切りの明示」を選んだが、
  **報告内容が減る方向の変化であること自体は事実**なので docs / CHANGELOG で
  開示している。12.0 という値は実測に基づく最適値ではなく見積り
- 超過は**必ず可視化する**。`[]` を返して黙ると「機密なし」と区別できない
  (このチケットの元の指摘そのもの)。呼出側は `Deadline.exceeded` を見て
  stderr + `systemMessage` / block reason の注記に反映する
- 判定表 (deny/allow/ask、block/pass) は変えない。変えるのは
  「打ち切ったことを言うか黙るか」だけ
"""
from __future__ import annotations

import time

# hook 全体の予算 (秒)。`hooks.json` の Stop timeout (15s) より小さく取る。
DEFAULT_BUDGET_SECONDS = 12.0

# 1 回の git 呼出に許す上限 (秒)。残予算がこれより少なければ残予算を使う。
PER_CALL_CAP_SECONDS = 10.0

# 残りがこれ未満なら「もう呼ばない」と判断する下限 (秒)。0 にすると
# `subprocess.run(timeout=0)` のような無意味な呼出が発生しうる。
MIN_USEFUL_SECONDS = 0.05


class Deadline:
    """hook 起動時刻からの経過で残り時間を計る締切。

    `time.monotonic` を使う (システム時刻の変更に影響されない)。スレッドは
    使わないので lock は不要。
    """

    def __init__(
        self,
        total: float = DEFAULT_BUDGET_SECONDS,
        per_call_cap: float = PER_CALL_CAP_SECONDS,
    ) -> None:
        self._start = time.monotonic()
        self._total = total
        self._per_call_cap = per_call_cap
        # 一度でも予算超過で打ち切ったら立つ (呼出側が可視化に使う)。
        self.exceeded = False

    def remaining(self) -> float:
        """残り秒数 (負にはしない)。"""
        return max(0.0, self._total - (time.monotonic() - self._start))

    def slice(self) -> float | None:
        """次の外部呼出に渡す timeout を返す。予算切れなら ``None``。

        ``None`` を返したときは `exceeded` が立つので、呼出側はそれを見て
        「打ち切った」ことを報告できる (黙って `[]` を返さない)。
        """
        left = self.remaining()
        if left < MIN_USEFUL_SECONDS:
            self.exceeded = True
            return None
        return min(self._per_call_cap, left)

    def mark_exceeded(self) -> None:
        """外部要因 (呼出が timeout した等) で打ち切ったことを記録する。"""
        self.exceeded = True

    def expired(self) -> bool:
        """予算を使い切ったか (`slice` と違い `exceeded` を立てない照会)。"""
        return self.remaining() < MIN_USEFUL_SECONDS
