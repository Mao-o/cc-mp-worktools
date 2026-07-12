"""state.py: session_id ベース debounce store のテスト。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import state


class BaseStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._base_dir_patcher = mock.patch.object(
            state, "_base_dir", return_value=Path(self.tmp) / "file-split-advisor"
        )
        self._base_dir_patcher.start()
        self.addCleanup(self._base_dir_patcher.stop)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTryReserveEmit(BaseStateTest):
    def test_first_call_succeeds(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))

    def test_same_tier_reservation_is_suppressed(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
        self.assertFalse(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))

    def test_lower_tier_reservation_is_suppressed(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
        self.assertFalse(state.try_reserve_emit("session-1", "/repo/foo.py", "review", 20))

    def test_worse_tier_reservation_succeeds(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "review", 20))
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))

    def test_shrink_then_regrow_same_tier_is_suppressed(self):
        # warn まで警告済み → shrink して note 相当に戻る (再警告なし、記録も変えない)
        # → 再び warn に regrow したとき、既に warn を通知済みなので再警告しない。
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
        self.assertFalse(state.try_reserve_emit("session-1", "/repo/foo.py", "note", 20))
        self.assertFalse(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))

    def test_emit_count_limit_enforced(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/a.py", "warn", 1))
        self.assertFalse(state.try_reserve_emit("session-1", "/repo/b.py", "warn", 1))

    def test_different_paths_tracked_independently(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/a.py", "warn", 20))
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/b.py", "warn", 20))

    def test_different_sessions_tracked_independently(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/a.py", "warn", 1))
        self.assertTrue(state.try_reserve_emit("session-2", "/repo/a.py", "warn", 1))

    def test_corrupted_json_treated_as_empty_state(self):
        state_file = state._state_path("session-1")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{not valid json")
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))

    def test_empty_session_id_always_true_and_no_state_written(self):
        self.assertTrue(state.try_reserve_emit("", "/repo/foo.py", "warn", 20))
        self.assertTrue(state.try_reserve_emit("", "/repo/foo.py", "warn", 20))
        self.assertFalse(state._base_dir().exists())

    def test_repeated_reservations_increment_emit_count_by_one_each(self):
        state.try_reserve_emit("session-1", "/repo/a.py", "review", 20)
        state.try_reserve_emit("session-1", "/repo/b.py", "review", 20)
        raw = json.loads(state._state_path("session-1").read_text())
        self.assertEqual(raw["__emit_count__"], 2)


class TestSessionIdHashing(BaseStateTest):
    def test_session_id_with_path_traversal_stays_within_base_dir(self):
        malicious_ids = ["../../etc/passwd", "a/b/c", "..", "/etc/passwd"]
        for session_id in malicious_ids:
            with self.subTest(session_id=session_id):
                state.try_reserve_emit(session_id, "/repo/foo.py", "warn", 20)
                state_path = state._state_path(session_id)
                self.assertEqual(state_path.parent, state._base_dir())
                self.assertTrue(state_path.is_relative_to(state._base_dir()))

    def test_state_path_is_deterministic(self):
        self.assertEqual(state._state_path("abc"), state._state_path("abc"))


class TestWithoutFlock(BaseStateTest):
    def test_reservation_logic_unchanged_when_flock_unavailable(self):
        with mock.patch.object(state, "HAVE_FLOCK", False):
            self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
            self.assertFalse(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
            self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20))


class TestTierRank(unittest.TestCase):
    def test_known_tiers_ordered(self):
        self.assertLess(state.tier_rank("ok"), state.tier_rank("note"))
        self.assertLess(state.tier_rank("note"), state.tier_rank("review"))
        self.assertLess(state.tier_rank("review"), state.tier_rank("warn"))
        self.assertLess(state.tier_rank("warn"), state.tier_rank("strong"))

    def test_unknown_tier_defaults_to_zero(self):
        self.assertEqual(state.tier_rank("nonsense"), 0)


if __name__ == "__main__":
    unittest.main()
