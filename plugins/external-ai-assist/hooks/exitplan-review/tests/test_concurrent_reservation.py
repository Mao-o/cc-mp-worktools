"""reserve_slot の flock 排他が並行呼び出しでも単一勝者を保証すること。

内部バックログの指摘: 同一プランに対する複数スレッドからの reserve_slot 呼び出しが
race しても、成功 (True) は 1 件だけであること。`_common/flock.locked_file` が
`fcntl.flock` (per-open-file-description ロック。同一プロセス内の別スレッドが
別々に `open()` したファイル記述子同士でも排他が効く) で read-modify-write を
直列化しているのがこれを保証する。ロックが外れる、または check → act が
アトミックでなくなると、count の lost update で複数スレッドが成功してしまう。
"""
import json
import os
import threading
import unittest

from _testutil import HookTestCase

SESSION = "sess-concurrent"


class TestConcurrentReserveSlot(HookTestCase):
    def test_five_threads_race_same_plan_only_one_succeeds(self):
        marker_file = os.path.join(
            self.tmpdir, "plan-review-markers", f"{SESSION}.exitplan.marker"
        )
        os.makedirs(os.path.dirname(marker_file), exist_ok=True)
        # max_reviews は高めにして、上限判定ではなく dedup (last==hash) の
        # 単一勝者性だけを見る (上限判定の挙動は test_settings_flow.py が別途担当)。
        current_hash = self.entry.plan_hash("racing plan")
        max_reviews = 10

        barrier = threading.Barrier(5)
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            reserved, _ = self.entry.reserve_slot(marker_file, current_hash, max_reviews)
            with results_lock:
                results.append(reserved)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "reserve_slot がデッドロックしている")

        self.assertEqual(len(results), 5, "スレッドが完走していない")
        self.assertEqual(
            results.count(True), 1, "同一プランへの並行 reserve_slot が複数成功している"
        )

        with open(marker_file) as f:
            data = json.load(f)
        self.assertEqual(
            data["plans"][current_hash]["count"], 1, "count が二重に加算されている"
        )


if __name__ == "__main__":
    unittest.main()
