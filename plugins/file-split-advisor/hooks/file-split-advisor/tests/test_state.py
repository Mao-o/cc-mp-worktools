"""state.py: session_id ベース debounce store のテスト。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import change
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
        # 0.5.0 の書込不能フォールバック先 (XDG キャッシュ) も tmp に向ける。
        # 向けないと、primary が失敗する経路のテストが実行機の
        # ~/.cache/file-split-advisor に state を書きうる。
        self._fallback_dir_patcher = mock.patch.object(
            state, "_fallback_dir", return_value=Path(self.tmp) / "fallback-cache"
        )
        self._fallback_dir_patcher.start()
        self.addCleanup(self._fallback_dir_patcher.stop)
        # 「プロセス内で 1 回だけ stderr に出す」フラグはモジュール global なので
        # テスト間で持ち越さない。
        state._WARNED_UNWRITABLE = False
        self.addCleanup(setattr, state, "_WARNED_UNWRITABLE", False)

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


class TestGrowthAndLineRecording(BaseStateTest):
    """0.2.0: 行数記録と成長判定。"""

    def _record(self, session_id: str, abs_path: str) -> dict:
        raw = json.loads(state._state_path(session_id).read_text())
        return raw[abs_path]

    def test_not_grew_is_suppressed(self):
        self.assertFalse(
            state.try_reserve_emit(
                "session-1", "/repo/foo.py", "strong", 20, growth=change.NOT_GREW
            )
        )

    def test_not_grew_still_records_line_count(self):
        state.try_reserve_emit(
            "session-1", "/repo/foo.py", "strong", 20, line_count=900, growth=change.NOT_GREW
        )
        self.assertEqual(self._record("session-1", "/repo/foo.py")["lines"], 900)

    def test_suppressed_call_does_not_advance_tier(self):
        # 抑制した呼び出しで tier を進めると、その後の本当の成長が恒久的に
        # 抑制されてしまう。
        state.try_reserve_emit(
            "session-1", "/repo/foo.py", "strong", 20, line_count=900, growth=change.NOT_GREW
        )
        self.assertEqual(self._record("session-1", "/repo/foo.py")["tier"], "ok")
        self.assertTrue(
            state.try_reserve_emit(
                "session-1", "/repo/foo.py", "strong", 20, line_count=901, growth=change.GREW
            )
        )

    def test_suppressed_call_does_not_consume_emit_budget(self):
        state.try_reserve_emit(
            "session-1", "/repo/a.py", "warn", 1, line_count=600, growth=change.NOT_GREW
        )
        self.assertTrue(
            state.try_reserve_emit("session-1", "/repo/b.py", "warn", 1, line_count=600)
        )

    def test_unknown_growth_falls_back_to_line_comparison(self):
        state.try_reserve_emit(
            "session-1", "/repo/foo.py", "strong", 20, line_count=900, growth=change.NOT_GREW
        )
        # 縮んだ / 変わらない → 抑制
        self.assertFalse(
            state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20, line_count=850)
        )
        self.assertFalse(
            state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20, line_count=850)
        )
        # 伸びた → 通知
        self.assertTrue(
            state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20, line_count=1000)
        )

    def test_unknown_growth_without_prior_record_is_allowed(self):
        self.assertTrue(
            state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20, line_count=600)
        )

    def test_emit_candidate_false_records_lines_and_returns_false(self):
        self.assertFalse(
            state.try_reserve_emit(
                "session-1", "/repo/foo.py", "review", 20, line_count=350, emit_candidate=False
            )
        )
        record = self._record("session-1", "/repo/foo.py")
        self.assertEqual(record["lines"], 350)
        self.assertEqual(record["tier"], "ok")

    def test_legacy_string_record_is_still_honoured(self):
        # 0.1.0 が書いた state ファイル (tier 文字列) を読んでも壊れない。
        state_file = state._state_path("session-1")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"__emit_count__": 1, "/repo/foo.py": "warn"}))
        self.assertFalse(
            state.try_reserve_emit("session-1", "/repo/foo.py", "review", 20, line_count=400)
        )
        self.assertTrue(
            state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20, line_count=900)
        )

    def test_malformed_record_shapes_are_ignored(self):
        state_file = state._state_path("session-1")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        for bad in [{"tier": 5, "lines": "many"}, {"tier": "nonsense"}, ["warn"], 42, True]:
            with self.subTest(bad=bad):
                state_file.write_text(json.dumps({"/repo/foo.py": bad}))
                self.assertTrue(
                    state.try_reserve_emit(
                        "session-1", "/repo/foo.py", "warn", 20, line_count=600
                    )
                )

    def test_io_failure_suppresses_instead_of_notifying(self):
        # 0.5.0 で反転: 旧版は「記録できないが通知する」(fail-open) で、debounce が
        # 成立しない環境では同一ファイルの編集ごとに同じ memo が出続けていた。
        with mock.patch.object(state.os, "makedirs", side_effect=OSError("boom")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(
                    state.try_reserve_emit(
                        "session-1", "/repo/foo.py", "strong", 20, growth=change.NOT_GREW
                    )
                )
                self.assertFalse(
                    state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20)
                )

    def test_empty_session_id_still_honours_not_grew(self):
        self.assertFalse(
            state.try_reserve_emit("", "/repo/foo.py", "strong", 20, growth=change.NOT_GREW)
        )
        self.assertTrue(state.try_reserve_emit("", "/repo/foo.py", "strong", 20))
        self.assertFalse(state._base_dir().exists())


class TestUnwritablePrimaryDir(BaseStateTest):
    """0.5.0: primary (TMPDIR) が書けないときは XDG キャッシュに退避し、
    それも書けなければ通知を抑制する。"""

    def setUp(self):
        super().setUp()
        # primary の親をファイルにして makedirs を確実に失敗させる (実環境の
        # 読み取り専用 TMPDIR / sandbox 相当)。
        blocked_parent = Path(self.tmp) / "blocked"
        blocked_parent.write_text("not a directory")
        self._blocked_patcher = mock.patch.object(
            state, "_base_dir", return_value=blocked_parent / "file-split-advisor"
        )
        self._blocked_patcher.start()
        self.addCleanup(self._blocked_patcher.stop)

    def test_state_is_written_to_the_fallback_dir(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
        fallback_file = state._state_path("session-1", state._fallback_dir())
        self.assertTrue(fallback_file.exists())

    def test_debounce_still_works_through_the_fallback_dir(self):
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
        self.assertFalse(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20))

    def test_both_dirs_unwritable_suppresses_emit(self):
        self._fallback_dir_patcher.stop()
        blocked = Path(self.tmp) / "blocked" / "cache"
        with mock.patch.object(state, "_fallback_dir", return_value=blocked):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(
                    state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20)
                )
        self._fallback_dir_patcher.start()

    def test_both_dirs_unwritable_reports_once_on_stderr(self):
        self._fallback_dir_patcher.stop()
        blocked = Path(self.tmp) / "blocked" / "cache"
        buffer = io.StringIO()
        with mock.patch.object(state, "_fallback_dir", return_value=blocked):
            with contextlib.redirect_stderr(buffer):
                state.try_reserve_emit("session-1", "/repo/foo.py", "strong", 20)
                state.try_reserve_emit("session-1", "/repo/bar.py", "strong", 20)
        self._fallback_dir_patcher.start()
        self.assertEqual(buffer.getvalue().count("file-split-advisor"), 1)


class TestFallbackDir(unittest.TestCase):
    def test_xdg_cache_home_is_honoured(self):
        with mock.patch.dict(state.os.environ, {"XDG_CACHE_HOME": "/xdg/cache"}):
            self.assertEqual(state._fallback_dir(), Path("/xdg/cache/file-split-advisor"))

    def test_defaults_to_dot_cache_under_home(self):
        with mock.patch.dict(state.os.environ, {"XDG_CACHE_HOME": ""}):
            with mock.patch.object(state.Path, "home", return_value=Path("/home/alice")):
                self.assertEqual(
                    state._fallback_dir(), Path("/home/alice/.cache/file-split-advisor")
                )

    def test_state_dirs_lists_primary_first(self):
        with mock.patch.object(state, "_base_dir", return_value=Path("/tmp/a")):
            with mock.patch.object(state, "_fallback_dir", return_value=Path("/cache/b")):
                self.assertEqual(state._state_dirs(), (Path("/tmp/a"), Path("/cache/b")))

    def test_state_dirs_omits_fallback_when_home_is_unavailable(self):
        # HOME も passwd エントリも無い環境では Path.home() が RuntimeError。
        with mock.patch.object(state, "_base_dir", return_value=Path("/tmp/a")):
            with mock.patch.dict(state.os.environ, {"XDG_CACHE_HOME": ""}):
                with mock.patch.object(state.Path, "home", side_effect=RuntimeError("no home")):
                    self.assertEqual(state._state_dirs(), (Path("/tmp/a"),))

    def test_relative_xdg_cache_home_is_ignored(self):
        # XDG 仕様: 相対値は無効。cwd (編集中の repo) に書かないための回帰。
        with mock.patch.dict(state.os.environ, {"XDG_CACHE_HOME": "relcache"}):
            self.assertTrue(state._fallback_dir().is_absolute())
            self.assertNotIn("relcache", str(state._fallback_dir()))

    def test_state_dirs_deduplicates_identical_candidates(self):
        same = Path("/tmp/same")
        with mock.patch.object(state, "_base_dir", return_value=same):
            with mock.patch.object(state, "_fallback_dir", return_value=same):
                self.assertEqual(state._state_dirs(), (same,))


class TestSweepStaleStates(BaseStateTest):
    """0.5.0: TTL を超えた state ファイルの opportunistic な掃除。"""

    def _write_state(self, name: str, age_seconds: float) -> Path:
        directory = state._base_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("{}")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_files_older_than_the_ttl_are_removed(self):
        stale = self._write_state("stale.json", state.STATE_TTL_SECONDS + 60)
        self.assertEqual(state.sweep_stale_states(state._base_dir()), 1)
        self.assertFalse(stale.exists())

    def test_ttl_is_seven_days(self):
        self.assertEqual(state.STATE_TTL_SECONDS, 7 * 24 * 60 * 60)

    def test_files_within_the_ttl_are_kept(self):
        fresh = self._write_state("fresh.json", state.STATE_TTL_SECONDS - 60)
        self.assertEqual(state.sweep_stale_states(state._base_dir()), 0)
        self.assertTrue(fresh.exists())

    def test_non_json_files_are_left_alone(self):
        directory = state._base_dir()
        directory.mkdir(parents=True, exist_ok=True)
        other = directory / "note.txt"
        other.write_text("keep me")
        stamp = time.time() - state.STATE_TTL_SECONDS - 60
        os.utime(other, (stamp, stamp))
        state.sweep_stale_states(directory)
        self.assertTrue(other.exists())

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(state.sweep_stale_states(Path(self.tmp) / "nope"), 0)

    def test_reserving_a_new_session_sweeps_stale_files(self):
        stale = self._write_state("0000000000000000.json", state.STATE_TTL_SECONDS + 60)
        self.assertTrue(state.try_reserve_emit("session-1", "/repo/foo.py", "warn", 20))
        self.assertFalse(stale.exists())
        # 自分の state ファイルは残る (掃除は自分を作る前に走る)。
        self.assertTrue(state._state_path("session-1").exists())

    def test_live_session_record_survives_even_when_its_file_looks_stale(self):
        # 2 回目以降は「新規作成」ではないので掃除しない。毎回掃除すると、7 日を
        # 超えて生きている session の記録が消え debounce が失われる (ファイル
        # 自体は ``open(..., "a+")`` で作り直されるため、存在だけ見ても気付け
        # ない — 中身の記録で確認する)。
        state.try_reserve_emit("session-1", "/repo/a.py", "warn", 20)
        own = state._state_path("session-1")
        stamp = time.time() - state.STATE_TTL_SECONDS - 60
        os.utime(own, (stamp, stamp))
        state.try_reserve_emit("session-1", "/repo/b.py", "warn", 20)
        self.assertIn("/repo/a.py", json.loads(own.read_text()))


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
