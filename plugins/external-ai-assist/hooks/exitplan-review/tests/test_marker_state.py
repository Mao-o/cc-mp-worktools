"""マーカーの状態機械 (JSON 化・仮予約の TTL 回収・エントリ cap・GC) の単体テスト。

block/clean の判定は test_sentinel_flow.py、env var の解釈は
test_settings_flow.py が担当する。ここはマーカーファイルそのものの構造と
TTL/cap/GC の境界を直接検証する。
"""
import json
import os
import time
import unittest

from _testutil import _PKG_DIR, FINDINGS, PLAN, HookTestCase

SESSION = "sess-marker-state"


class MarkerStateTestCase(HookTestCase):
    def marker_file(self, session_id: str = SESSION) -> str:
        path = os.path.join(
            self.tmpdir, "plan-review-markers", f"{session_id}.exitplan.marker"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def write_marker(self, session_id: str, data: dict) -> None:
        with open(self.marker_file(session_id), "w") as f:
            json.dump(data, f)

    def read_marker(self, session_id: str = SESSION) -> dict:
        with open(self.marker_file(session_id)) as f:
            return json.load(f)


class TestReservationTtl(MarkerStateTestCase):
    """RESERVATION_TTL_SEC の境界は「正しさの制約」であって推奨値ではない。"""

    def test_ttl_exceeds_hooks_json_timeout(self):
        """ExitPlanMode の hook timeout (1560s) を必ず超えること。

        下回ると、まだ走っているレビューの仮予約を、別の ExitPlanMode 呼び出しが
        「kill された」と誤認して横取りする。
        """
        hooks = json.loads((_PKG_DIR.parent / "hooks.json").read_text())
        timeout = None
        for entry in hooks["hooks"]["PreToolUse"]:
            if entry.get("matcher") == "ExitPlanMode":
                timeout = entry["hooks"][0]["timeout"]
        self.assertIsNotNone(timeout, "hooks.json に ExitPlanMode の hook が無い")
        self.assertGreater(self.entry.RESERVATION_TTL_SEC, timeout)

    def test_ttl_derives_from_reviewer_timeout_ceilings(self):
        """既定値ではなく上限 (MAX_TIMEOUT_SEC) から導出すること。

        `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` で timeout が env 可変なため、既定値から
        導くと「短く設定したセッションが、長く設定した別セッションの仮予約を
        TTL 超過とみなして横取りする」経路ができる。
        """
        self.assertGreater(self.entry.RESERVATION_TTL_SEC, self.cursor.MAX_TIMEOUT_SEC)
        self.assertGreater(self.entry.RESERVATION_TTL_SEC, self.codex.MAX_TIMEOUT_SEC)


class TestStaleReservationReclaim(MarkerStateTestCase):
    """kill されて確定も解放もされなかった仮予約を TTL 経過後に回収する。

    正当に確定した (reserved_at=None) block は sweep が触れないこと。
    """

    def test_unsettled_reservation_is_reclaimed_after_ttl(self):
        h = self.entry.plan_hash("plan-x")
        self.write_marker(
            SESSION,
            {
                "v": 1,
                "last": h,
                "plans": {
                    h: {
                        "count": 1,
                        "reserved_at": time.time() - self.entry.RESERVATION_TTL_SEC - 60,
                    }
                },
            },
        )

        # 別プランで reserve_slot を叩いて sweep を発火させる
        reserved, _ = self.entry.reserve_slot(self.marker_file(SESSION), "other-hash", 2)
        self.assertTrue(reserved)

        data = self.read_marker(SESSION)
        self.assertNotIn(h, data["plans"], "TTL 超過の未確定予約が回収されていない")

    def test_confirmed_block_is_not_reclaimed_even_if_marker_is_old(self):
        """reserved_at=None (confirm_slot 済み) のエントリは sweep が触れない。"""
        h = self.entry.plan_hash("plan-x")
        self.write_marker(
            SESSION, {"v": 1, "last": "", "plans": {h: {"count": 1, "reserved_at": None}}}
        )

        reserved, _ = self.entry.reserve_slot(self.marker_file(SESSION), "other-hash", 2)
        self.assertTrue(reserved)

        data = self.read_marker(SESSION)
        self.assertEqual(
            data["plans"][h]["count"], 1, "確定済み block の count が変わってはいけない"
        )

    def test_fresh_unsettled_reservation_is_not_reclaimed(self):
        """TTL 未経過の仮予約は横取りしない (走行中のレビューを守る)。"""
        h = self.entry.plan_hash("plan-x")
        self.write_marker(
            SESSION, {"v": 1, "last": h, "plans": {h: {"count": 1, "reserved_at": time.time()}}}
        )

        reserved, _ = self.entry.reserve_slot(self.marker_file(SESSION), "other-hash", 2)
        self.assertTrue(reserved)

        data = self.read_marker(SESSION)
        self.assertEqual(
            data["plans"][h]["count"], 1, "TTL 未経過の予約を横取りしてはいけない"
        )
        self.assertIsNotNone(data["plans"][h]["reserved_at"])


class TestEndToEndConfirmSlot(HookTestCase):
    """confirm_slot が実際の block / context 経路で呼ばれること。

    advisor 指摘の「最も疑わしいバグ」: 呼び忘れると、正当に確定した block でも
    RESERVATION_TTL_SEC 経過後に「kill された仮予約」と誤認されて count が
    静かにロールバックされ、上限が実質緩んでしまう。
    """

    def _marker_path(self, session_id: str) -> str:
        return os.path.join(
            self.tmpdir, "plan-review-markers", f"{session_id}.exitplan.marker"
        )

    def test_block_settles_reserved_at_to_none(self):
        self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, "REVIEW_CLEAN"))
        with open(self._marker_path(SESSION)) as f:
            data = json.load(f)
        h = self.entry.plan_hash(PLAN.strip())
        self.assertIsNone(
            data["plans"][h]["reserved_at"],
            "block 確定後も reserved_at が残っている (TTL 回収で静かにロールバックされる)",
        )
        self.assertEqual(data["plans"][h]["count"], 1)

    def test_context_mode_settles_reserved_at_to_none(self):
        import os as _os

        _os.environ["EXTERNAL_AI_PLAN_REVIEW_MODE"] = "context"
        self.exitplan(SESSION, PLAN, FINDINGS, "REVIEW_CLEAN")
        with open(self._marker_path(SESSION)) as f:
            data = json.load(f)
        h = self.entry.plan_hash(PLAN.strip())
        self.assertIsNone(
            data["plans"][h]["reserved_at"], "context モードでも確定は同じく必要"
        )


class TestPlanEntryCap(MarkerStateTestCase):
    def test_cap_limits_total_plan_entries(self):
        marker_file = self.marker_file(SESSION)
        overflow = self.entry.MAX_PLAN_ENTRIES + 5
        for i in range(overflow):
            # release しない (count を残したまま溜め続けて cap に到達させる)
            self.entry.reserve_slot(marker_file, f"hash-{i}", 100)

        with open(marker_file) as f:
            data = json.load(f)
        self.assertEqual(len(data["plans"]), self.entry.MAX_PLAN_ENTRIES)
        self.assertNotIn("hash-0", data["plans"], "古い順に捨てられていない")
        self.assertIn(f"hash-{overflow - 1}", data["plans"], "新しいエントリが残っていない")


class TestMarkerGc(MarkerStateTestCase):
    def _touch(self, path: str, age_sec: float) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        stamp = time.time() - age_sec
        os.utime(path, (stamp, stamp))

    def test_stale_marker_is_removed_fresh_kept(self):
        stale = os.path.join(self.tmpdir, "plan-review-markers", "old.exitplan.marker")
        fresh = os.path.join(self.tmpdir, "plan-review-markers", "new.exitplan.marker")
        self._touch(stale, self.entry.MARKER_STATE_TTL_SEC + 60)
        self._touch(fresh, 0)

        self.entry._gc_stale_markers()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    def test_stale_review_copy_txt_is_removed(self):
        stale = os.path.join(self.tmpdir, "plan-review-abc12345.txt")
        fresh = os.path.join(self.tmpdir, "plan-review-def67890.txt")
        self._touch(stale, self.entry.MARKER_STATE_TTL_SEC + 60)
        self._touch(fresh, 0)

        self.entry._gc_stale_markers()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    def test_gc_on_missing_dirs_is_noop(self):
        self.entry._gc_stale_markers()  # 例外を出さないことのみ確認


if __name__ == "__main__":
    unittest.main()
