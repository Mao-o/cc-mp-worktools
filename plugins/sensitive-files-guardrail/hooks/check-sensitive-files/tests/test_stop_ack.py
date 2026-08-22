"""stop_ack.py (0.19.0, session 単位 once-only の状態管理) の単体テスト。

- session_id の検証 (path traversal / dotfile / 型違い)
- digest の安定性と status 感度
- state の読み書き (HOME 隔離、壊れた行の無視、失敗時の黙殺)
- 古い session state の GC
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import stop_ack

_DAY = 24 * 60 * 60


class BaseStopAck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _state_dir(self) -> Path:
        return self.home / ".claude" / "sensitive-files-guardrail" / "stop-ack"


class TestResolveStateDir(BaseStopAck):
    def test_under_home_like_patterns_local(self):
        self.assertEqual(stop_ack.resolve_state_dir(), self._state_dir())


class TestSanitizeSessionId(BaseStopAck):
    def test_accepts_uuid_like(self):
        sid = "0f3c2a9e-4b1d-4e6a-9c8f-123456789abc"
        self.assertEqual(stop_ack.sanitize_session_id(sid), sid)

    def test_accepts_max_length(self):
        sid = "a" * 128
        self.assertEqual(stop_ack.sanitize_session_id(sid), sid)

    def test_rejects_unsafe_values(self):
        for bad in (
            "../x", "a/b", "a\\b", ".hidden", "..", "", " ", "a b",
            "x" * 129, None, 5, ["a"],
        ):
            self.assertIsNone(stop_ack.sanitize_session_id(bad), msg=repr(bad))


class TestDigestEntries(BaseStopAck):
    def test_stable_and_status_sensitive(self):
        a = stop_ack.digest_entries([{"path": ".env", "status": "tracked"}])
        b = stop_ack.digest_entries([{"path": ".env", "status": "tracked"}])
        c = stop_ack.digest_entries([{"path": ".env", "status": "untracked"}])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        for d in a | c:
            self.assertRegex(d, r"^[0-9a-f]{64}$")

    def test_empty(self):
        self.assertEqual(stop_ack.digest_entries([]), set())

    def test_scope_sensitive(self):
        # 同一 session で別 repo に cd しても同じ相対 path を報告済み扱いしない
        entry = [{"path": ".env", "status": "tracked"}]
        a = stop_ack.digest_entries(entry, scope="/work/repo-a")
        b = stop_ack.digest_entries(entry, scope="/work/repo-b")
        self.assertNotEqual(a, b)
        self.assertEqual(a, stop_ack.digest_entries(entry, scope="/work/repo-a"))

    def test_set_semantics_over_multiple_entries(self):
        entries = [
            {"path": ".env", "status": "tracked"},
            {"path": "a.pem", "status": "untracked"},
        ]
        digests = stop_ack.digest_entries(entries)
        self.assertEqual(len(digests), 2)
        self.assertTrue(
            stop_ack.digest_entries(entries[:1]) < digests  # 真部分集合
        )


class TestLoadSave(BaseStopAck):
    def test_roundtrip_under_home(self):
        digests = {"a" * 64, "b" * 64}
        stop_ack.save_acked("s1", digests)
        self.assertTrue((self._state_dir() / "s1").is_file())
        self.assertEqual(stop_ack.load_acked("s1"), digests)

    def test_missing_is_empty(self):
        self.assertEqual(stop_ack.load_acked("nope"), set())

    def test_corrupt_lines_ignored(self):
        self._state_dir().mkdir(parents=True)
        (self._state_dir() / "s2").write_text(
            "garbage\n" + "f" * 64 + "\n/Users/x/.env\n" + "0" * 63 + "\n"
        )
        self.assertEqual(stop_ack.load_acked("s2"), {"f" * 64})

    def test_undecodable_file_is_empty(self):
        self._state_dir().mkdir(parents=True)
        (self._state_dir() / "s3").write_bytes(b"\xff\xfe\x00garbage")
        self.assertEqual(stop_ack.load_acked("s3"), set())

    def test_save_failure_is_silent(self):
        # stop-ack がファイルとして存在 → mkdir 失敗 → 例外を出さず何も書かない
        self._state_dir().parent.mkdir(parents=True)
        self._state_dir().write_text("not a dir\n")
        stop_ack.save_acked("s4", {"a" * 64})
        self.assertEqual(stop_ack.load_acked("s4"), set())

    def test_no_tmp_left_behind(self):
        stop_ack.save_acked("s5", {"a" * 64})
        names = sorted(p.name for p in self._state_dir().iterdir())
        self.assertEqual(names, ["s5"])

    def test_overwrite_replaces_content(self):
        stop_ack.save_acked("s6", {"a" * 64})
        stop_ack.save_acked("s6", {"b" * 64})
        self.assertEqual(stop_ack.load_acked("s6"), {"b" * 64})


class TestGc(BaseStopAck):
    def _touch(self, name: str, age_seconds: float) -> Path:
        p = self._state_dir() / name
        p.write_text("")
        stamp = time.time() - age_seconds
        os.utime(p, (stamp, stamp))
        return p

    def test_gc_removes_stale_keeps_recent_and_current(self):
        stop_ack.save_acked("cur", {"a" * 64})
        old = self._touch("old", 8 * _DAY)
        recent = self._touch("recent", 1 * _DAY)
        stop_ack._gc_stale(self._state_dir(), keep="cur")
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue((self._state_dir() / "cur").exists())

    def test_gc_keeps_current_even_if_stale(self):
        stop_ack.save_acked("cur", {"a" * 64})
        cur = self._state_dir() / "cur"
        stamp = time.time() - 30 * _DAY
        os.utime(cur, (stamp, stamp))
        stop_ack._gc_stale(self._state_dir(), keep="cur")
        self.assertTrue(cur.exists())

    def test_gc_explicit_now(self):
        self._state_dir().mkdir(parents=True)
        p = self._touch("x", 0)
        stop_ack._gc_stale(
            self._state_dir(), keep="other", now=time.time() + 8 * _DAY
        )
        self.assertFalse(p.exists())

    def test_save_triggers_gc(self):
        self._state_dir().mkdir(parents=True)
        old = self._touch("old", 8 * _DAY)
        stop_ack.save_acked("new", {"a" * 64})
        self.assertFalse(old.exists())
        self.assertTrue((self._state_dir() / "new").exists())

    def test_gc_missing_dir_is_silent(self):
        stop_ack._gc_stale(self._state_dir() / "missing", keep="x")


if __name__ == "__main__":
    unittest.main()
