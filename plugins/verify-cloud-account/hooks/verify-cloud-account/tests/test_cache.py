"""cache.get_success / set_success のラウンドトリップと無効化テスト。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import cache  # noqa: E402


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._p = mock.patch.dict(os.environ, {"TMPDIR": self.tmp})
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_miss_returns_false(self):
        self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))

    def test_roundtrip_hit(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        self.assertTrue(cache.get_success("svc", "/p", "exp", 1.0))

    def test_different_expected_miss(self):
        cache.set_success("svc", "/p", "expA", 1.0)
        self.assertFalse(cache.get_success("svc", "/p", "expB", 1.0))

    def test_different_project_miss(self):
        cache.set_success("svc", "/p1", "exp", 1.0)
        self.assertFalse(cache.get_success("svc", "/p2", "exp", 1.0))

    def test_different_service_miss(self):
        cache.set_success("svcA", "/p", "exp", 1.0)
        self.assertFalse(cache.get_success("svcB", "/p", "exp", 1.0))

    def test_mtime_change_miss(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        self.assertFalse(cache.get_success("svc", "/p", "exp", 2.0))

    def test_ttl_expiry(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        self.assertTrue(cache.get_success("svc", "/p", "exp", 1.0))
        with mock.patch.object(cache, "_CACHE_TTL_SEC", 0):
            time.sleep(0.05)
            self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))

    def test_dict_expected_roundtrip(self):
        exp = {"project": "p", "account": "a"}
        cache.set_success("svc", "/p", exp, 1.0)
        self.assertTrue(cache.get_success("svc", "/p", exp, 1.0))

    def test_corrupt_cache_file_miss(self):
        cache.set_success("svc", "/p", "exp", 1.0)
        base = Path(self.tmp) / "cc-mp-verify-cloud-account"
        files = list(base.glob("*.json"))
        self.assertTrue(files)
        files[0].write_text("not json", encoding="utf-8")
        self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))

    def test_different_inline_env_miss(self):
        # profile が異なれば別キー → profile A の成功が profile B で誤 allow されない
        cache.set_success("svc", "/p", "exp", 1.0, {"AWS_PROFILE": "a"})
        self.assertFalse(
            cache.get_success("svc", "/p", "exp", 1.0, {"AWS_PROFILE": "b"})
        )

    def test_same_inline_env_hit(self):
        cache.set_success("svc", "/p", "exp", 1.0, {"AWS_PROFILE": "a"})
        self.assertTrue(
            cache.get_success("svc", "/p", "exp", 1.0, {"AWS_PROFILE": "a"})
        )

    def test_env_vs_no_env_miss(self):
        # env 付き成功キーと env 無しキーは分離される (後方互換のデフォルト None)
        cache.set_success("svc", "/p", "exp", 1.0, {"AWS_PROFILE": "a"})
        self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))

    # --- invalidate (bd_092a232e-629.3: 切替コマンド検出時の service 単位破棄) ---

    def test_invalidate_removes_only_that_service(self):
        cache.set_success("github", "/p", "exp", 1.0)
        cache.set_success("aws", "/p", "exp", 1.0)
        self.assertEqual(cache.invalidate("github"), 1)
        self.assertFalse(cache.get_success("github", "/p", "exp", 1.0))
        self.assertTrue(cache.get_success("aws", "/p", "exp", 1.0))

    def test_invalidate_covers_all_projects_expected_and_envs(self):
        # アカウント状態はマシン全体で共有されるため、project_dir / 期待値 / inline env
        # が異なる entry もまとめて破棄する
        cache.set_success("github", "/p1", "expA", 1.0)
        cache.set_success("github", "/p2", "expB", 2.0)
        cache.set_success("github", "/p1", "expA", 1.0, {"GH_HOST": "ghe.example.com"})
        self.assertEqual(cache.invalidate("github"), 3)
        self.assertFalse(cache.get_success("github", "/p1", "expA", 1.0))
        self.assertFalse(cache.get_success("github", "/p2", "expB", 2.0))
        self.assertFalse(
            cache.get_success("github", "/p1", "expA", 1.0, {"GH_HOST": "ghe.example.com"})
        )

    def test_invalidate_without_entries_is_noop(self):
        self.assertEqual(cache.invalidate("github"), 0)
        # service 名がファイル名に使えない文字を含んでも例外にならない
        cache.set_success("weird/svc name", "/p", "exp", 1.0)
        self.assertTrue(cache.get_success("weird/svc name", "/p", "exp", 1.0))
        self.assertEqual(cache.invalidate("weird/svc name"), 1)
        self.assertFalse(cache.get_success("weird/svc name", "/p", "exp", 1.0))

    def test_invalidate_does_not_match_service_name_prefix(self):
        # "gh" の破棄が "ghx" の entry を巻き込まない (prefix は "<svc>-" で区切る)
        cache.set_success("gh", "/p", "exp", 1.0)
        cache.set_success("ghx", "/p", "exp", 1.0)
        self.assertEqual(cache.invalidate("gh"), 1)
        self.assertTrue(cache.get_success("ghx", "/p", "exp", 1.0))

    # --- epoch / tombstone (PR #43 Codex R2 P1-2: 並行 hook との競合) ---

    def _base(self) -> Path:
        return Path(self.tmp) / "cc-mp-verify-cloud-account"

    def test_invalidate_bumps_epoch_and_stale_epoch_result_is_not_published(self):
        e0 = cache.current_epoch("github")
        cache.set_success("github", "/p", "exp", 1.0)
        self.assertEqual(cache.invalidate("github"), 1)
        e1 = cache.current_epoch("github")
        self.assertGreater(e1, e0)
        # 無効化前 (旧 epoch) に開始した検証の成功は書かれない
        self.assertFalse(cache.set_success("github", "/p", "exp", 1.0, epoch=e0))
        self.assertFalse(cache.get_success("github", "/p", "exp", 1.0))
        # 現在の epoch で開始した検証は書ける
        self.assertTrue(cache.set_success("github", "/p", "exp", 1.0, epoch=e1))
        self.assertTrue(cache.get_success("github", "/p", "exp", 1.0))

    def test_entry_with_old_epoch_is_ignored_even_if_file_survives(self):
        """削除と競合して残った (or 削除後に書かれた) 旧 epoch の entry も無視される (tombstone)。"""
        cache.set_success("github", "/p", "exp", 1.0)
        entry = next(self._base().glob("github-*.json"))
        saved = entry.read_text(encoding="utf-8")
        cache.invalidate("github")
        entry.write_text(saved, encoding="utf-8")
        self.assertTrue(entry.is_file())
        self.assertFalse(cache.get_success("github", "/p", "exp", 1.0))

    def test_epoch_is_monotonic_even_if_epoch_file_is_removed(self):
        cache.invalidate("github")
        e1 = cache.current_epoch("github")
        (self._base() / "github.epoch").unlink()
        self.assertEqual(cache.current_epoch("github"), 0)
        cache.invalidate("github")
        self.assertGreater(cache.current_epoch("github"), e1)

    def test_set_success_without_epoch_uses_current(self):
        cache.invalidate("github")
        self.assertTrue(cache.set_success("github", "/p", "exp", 1.0))
        self.assertTrue(cache.get_success("github", "/p", "exp", 1.0))

    def test_corrupt_epoch_file_is_zero_and_recovers(self):
        self._base().mkdir(exist_ok=True)
        (self._base() / "github.epoch").write_text("not json", encoding="utf-8")
        self.assertEqual(cache.current_epoch("github"), 0)
        cache.invalidate("github")
        self.assertGreater(cache.current_epoch("github"), 0)

    def test_epoch_is_per_service(self):
        cache.set_success("aws", "/p", "exp", 1.0)
        cache.invalidate("github")
        self.assertEqual(cache.current_epoch("aws"), 0)
        self.assertTrue(cache.get_success("aws", "/p", "exp", 1.0))

    def test_writes_leave_no_tmp_files(self):
        cache.set_success("github", "/p", "exp", 1.0)
        cache.invalidate("github")
        self.assertEqual([p.name for p in self._base().glob("*.tmp")], [])


if __name__ == "__main__":
    unittest.main()
