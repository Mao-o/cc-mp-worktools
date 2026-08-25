"""state.py / stategc.py の状態機械・ロック・GC の単体テスト。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 設定)

import state
import stategc

SESSION = "sess-unit"


class StateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"TMPDIR": self._tmp.name})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def read_state(self, session_id: str = SESSION) -> dict:
        path = os.path.join(state.state_root(), "state", f"{session_id}.json")
        with open(path) as f:
            return json.load(f)


class TestPending(StateTestCase):
    def test_record_and_claim(self):
        state.record_pending(SESSION, ["/repo/a.py", "/repo/b.py"])
        claim = state.claim_pending(SESSION)
        self.assertIsNotNone(claim)
        claim_id, paths = claim
        self.assertEqual(sorted(paths), ["/repo/a.py", "/repo/b.py"])
        self.assertEqual(self.read_state()["pending"], {})
        self.assertIn(claim_id, self.read_state()["in_flight"])

    def test_claim_returns_none_when_empty(self):
        self.assertIsNone(state.claim_pending(SESSION))

    def test_duplicate_paths_are_deduped(self):
        state.record_pending(SESSION, ["/repo/a.py"])
        state.record_pending(SESSION, ["/repo/a.py"])
        _, paths = state.claim_pending(SESSION)
        self.assertEqual(paths, ["/repo/a.py"])

    def test_pending_is_capped(self):
        state.record_pending(SESSION, [f"/repo/f{i}.py" for i in range(300)])
        _, paths = state.claim_pending(SESSION)
        self.assertEqual(paths, [f"/repo/f{i}.py" for i in range(state.MAX_PENDING_PATHS)])

    def test_cap_drops_newest_not_carried_over(self):
        """先頭 (前回 Stop の繰り越し分) を守り、末尾 (新しい編集) から落とす。"""
        state.record_pending(SESSION, ["/repo/carried.py"])
        state.record_pending(SESSION, [f"/repo/new{i}.py" for i in range(state.MAX_PENDING_PATHS + 50)])
        _, paths = state.claim_pending(SESSION)
        self.assertEqual(paths[0], "/repo/carried.py")
        self.assertEqual(len(paths), state.MAX_PENDING_PATHS)
        self.assertNotIn(f"/repo/new{state.MAX_PENDING_PATHS + 49}.py", paths)

    def test_sessions_do_not_share_pending(self):
        state.record_pending("sess-a", ["/repo/a.py"])
        self.assertIsNone(state.claim_pending("sess-b"))


class TestClaimLifecycle(StateTestCase):
    def test_complete_clears_in_flight_and_records_hashes(self):
        state.record_pending(SESSION, ["/repo/a.py"])
        claim_id, _ = state.claim_pending(SESSION)
        state.complete_claim(SESSION, claim_id, {"/repo/a.py": "deadbeef"})

        data = self.read_state()
        self.assertEqual(data["in_flight"], {})
        self.assertEqual(data["pending"], {})
        self.assertEqual(data["reviewed"], {"/repo/a.py": "deadbeef"})

    def test_restore_puts_back_only_submitted_paths(self):
        state.record_pending(SESSION, ["/repo/a.py", "/repo/skipped.py"])
        claim_id, _ = state.claim_pending(SESSION)
        state.restore_claim(SESSION, claim_id, ["/repo/a.py"])

        data = self.read_state()
        self.assertEqual(list(data["pending"]), ["/repo/a.py"])
        self.assertEqual(data["in_flight"], {})
        self.assertEqual(data["reviewed"], {}, "失敗時に hash を記録してはいけない")

    def test_reviewed_is_lru_trimmed(self):
        state.record_pending(SESSION, ["/repo/a.py"])
        claim_id, _ = state.claim_pending(SESSION)
        hashes = {f"/repo/f{i}.py": f"h{i}" for i in range(state.MAX_REVIEWED_ENTRIES + 50)}
        state.complete_claim(SESSION, claim_id, hashes)

        reviewed = self.read_state()["reviewed"]
        self.assertEqual(len(reviewed), state.MAX_REVIEWED_ENTRIES)
        self.assertIn(f"/repo/f{state.MAX_REVIEWED_ENTRIES + 49}.py", reviewed)
        self.assertNotIn("/repo/f0.py", reviewed)


class TestInFlightTtl(StateTestCase):
    def test_fresh_in_flight_is_not_stolen(self):
        state.record_pending(SESSION, ["/repo/a.py"])
        state.claim_pending(SESSION)
        self.assertIsNone(state.claim_pending(SESSION))

    def test_expired_in_flight_is_reclaimed(self):
        state.record_pending(SESSION, ["/repo/a.py"])
        state.claim_pending(SESSION)

        path = os.path.join(state.state_root(), "state", f"{SESSION}.json")
        data = self.read_state()
        for entry in data["in_flight"].values():
            entry["at"] = time.time() - state.IN_FLIGHT_TTL_SEC - 1
        with open(path, "w") as f:
            json.dump(data, f)

        claim = state.claim_pending(SESSION)
        self.assertIsNotNone(claim)
        self.assertEqual(claim[1], ["/repo/a.py"])

    def test_ttl_derives_from_cursor_timeout_ceiling(self):
        """TTL は **上限** から導出すること (0.6.0 で timeout が env 可変になった)。

        既定値から導くと、`EXTERNAL_AI_POST_REVIEW_TIMEOUT` を短くしたセッションが、
        長く設定した別セッションの in-flight を「TTL 超過」とみなして横取りする。
        TTL は全セッションで同一でなければならない。
        """
        import cursor

        self.assertGreater(state.IN_FLIGHT_TTL_SEC, cursor.MAX_TIMEOUT_SEC)
        self.assertGreater(state.IN_FLIGHT_TTL_SEC, cursor.TIMEOUT_SEC)


class TestCorruptState(StateTestCase):
    def test_garbage_state_file_is_reset(self):
        path = os.path.join(state.state_root(), "state", f"{SESSION}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")

        state.record_pending(SESSION, ["/repo/a.py"])
        self.assertEqual(list(self.read_state()["pending"]), ["/repo/a.py"])


class TestBashSnapshot(StateTestCase):
    def test_roundtrip_and_single_consumption(self):
        state.save_bash_snapshot(SESSION, "tu_1", {"a.py": ["M", 1, 2]})
        self.assertEqual(state.pop_bash_snapshot(SESSION, "tu_1"), {"a.py": ["M", 1, 2]})
        self.assertIsNone(state.pop_bash_snapshot(SESSION, "tu_1"))

    def test_missing_snapshot_returns_none(self):
        self.assertIsNone(state.pop_bash_snapshot(SESSION, "nope"))


class TestCursorLock(StateTestCase):
    def test_same_worktree_is_exclusive(self):
        with state.cursor_lock("/repo") as first:
            self.assertTrue(first)
            # 同一プロセスでも fd が別なら flock は競合する
            with state.cursor_lock("/repo") as second:
                self.assertFalse(second)

    def test_lock_is_released_after_block(self):
        with state.cursor_lock("/repo"):
            pass
        with state.cursor_lock("/repo") as again:
            self.assertTrue(again)

    def test_exception_in_body_propagates_and_releases(self):
        """with 本体の OSError が RuntimeError にすり替わらず、ロックも解放されること。"""
        with self.assertRaises(BrokenPipeError):
            with state.cursor_lock("/repo"):
                raise BrokenPipeError("stdout closed")

        with state.cursor_lock("/repo") as again:
            self.assertTrue(again, "例外経路でもロックが解放されていること")

    def test_symlinked_cwd_maps_to_same_lock(self):
        real = os.path.join(self._tmp.name, "real")
        link = os.path.join(self._tmp.name, "link")
        os.makedirs(real, exist_ok=True)
        os.symlink(real, link)
        with state.cursor_lock(real) as first:
            self.assertTrue(first)
            with state.cursor_lock(link) as second:
                self.assertFalse(second, "symlink 経由でも同一ロックとみなすこと")


class TestGc(StateTestCase):
    def _touch(self, path: str, age_sec: float) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        stamp = time.time() - age_sec
        os.utime(path, (stamp, stamp))
        return path

    def test_stale_state_removed_fresh_kept(self):
        stale = self._touch(
            os.path.join(state.state_root(), "state", "old.json"),
            state.STATE_TTL_SEC + 60,
        )
        fresh = self._touch(os.path.join(state.state_root(), "state", "new.json"), 0)

        stategc.gc_stale()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    def test_legacy_markers_are_cleaned(self):
        legacy = self._touch(
            os.path.join(self._tmp.name, "post-review-markers", "sess.post.marker"),
            state.STATE_TTL_SEC + 60,
        )
        legacy_txt = self._touch(
            os.path.join(self._tmp.name, "post-review-abc12345.txt"),
            state.STATE_TTL_SEC + 60,
        )
        fresh_legacy = self._touch(
            os.path.join(self._tmp.name, "post-review-markers", "live.post.marker"), 0
        )

        stategc.gc_stale()
        self.assertFalse(os.path.exists(legacy))
        self.assertFalse(os.path.exists(legacy_txt))
        self.assertTrue(os.path.exists(fresh_legacy), "稼働中の旧版マーカーは消さない")

    def test_orphaned_bash_snapshots_expire_early(self):
        """Bash が実行されず PostToolUse が来なかった snapshot を長く抱えない。

        permission 拒否 / 別 hook の block / 中断で PreToolUse だけが走ると孤児になる。
        """
        orphan = self._touch(
            os.path.join(state.state_root(), "bashsnap", "sess__tu_old.json"),
            state.BASH_SNAPSHOT_TTL_SEC + 60,
        )
        fresh = self._touch(
            os.path.join(state.state_root(), "bashsnap", "sess__tu_live.json"), 0
        )
        # 同じ古さでも state ファイルは残る (TTL が別)
        state_file = self._touch(
            os.path.join(state.state_root(), "state", "sess.json"),
            state.BASH_SNAPSHOT_TTL_SEC + 60,
        )

        stategc.gc_stale()
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(fresh), "実行中の snapshot を消してはいけない")
        self.assertTrue(
            os.path.exists(state_file), "state に bashsnap の短い TTL を適用しないこと"
        )

    def test_snapshot_ttl_is_shorter_than_state_ttl(self):
        self.assertLess(state.BASH_SNAPSHOT_TTL_SEC, state.STATE_TTL_SEC)

    def test_gc_on_missing_root_is_noop(self):
        self.assertEqual(stategc.gc_stale(), 0)

    def test_held_lock_file_survives_gc(self):
        """GC がロック保持中のファイルを消すと inode が分岐して排他が壊れる。"""
        lock_path = os.path.join(
            state.state_root(), "locks", f"cursor-{state._safe_cwd_key('/repo')}.lock"
        )
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w"):
            pass
        stamp = time.time() - state.STATE_TTL_SEC - 60
        os.utime(lock_path, (stamp, stamp))

        with state.cursor_lock("/repo") as acquired:
            self.assertTrue(acquired)
            stategc.gc_stale()
            self.assertTrue(
                os.path.exists(lock_path), "保持中のロックは mtime 更新で GC されないこと"
            )


if __name__ == "__main__":
    unittest.main()
