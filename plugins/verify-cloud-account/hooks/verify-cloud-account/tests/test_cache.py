"""cache.get_success / set_success のラウンドトリップと無効化テスト。"""
from __future__ import annotations

import json
import os
import shutil
import stat
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

    def test_unreadable_cache_file_is_a_miss_not_an_exception(self):
        """UTF-8 でない / 入れ子が深い entry も cache miss (検証し直す。v0.18.0)。

        旧版は JSONDecodeError と OSError だけを捕まえ、UnicodeDecodeError / RecursionError が
        dispatch() の外まで抜けて __main__ の最終防波堤が検証をスキップしていた。
        """
        base = Path(self.tmp) / "cc-mp-verify-cloud-account"
        for name, payload in (
            ("not UTF-8", b'{"success": true, "x": "\xff"}'),
            ("deep", b"[" * 100000 + b"]" * 100000),
        ):
            with self.subTest(name):
                cache.set_success("svc", "/p", "exp", 1.0)
                files = list(base.glob("svc-*.json"))
                self.assertTrue(files)
                for path in files:
                    path.write_bytes(payload)
                try:
                    hit = cache.get_success("svc", "/p", "exp", 1.0)
                except (UnicodeDecodeError, RecursionError) as e:
                    self.fail(f"読めない cache entry で get_success が {type(e).__name__} を投げた")
                self.assertFalse(hit)

    def test_entry_values_of_an_unexpected_type_are_a_miss(self):
        """値が期待した型でない entry も cache miss (検証し直す。v0.19.0)。

        旧版は timestamp が数値でない値の TypeError と、float に収まらない整数の
        OverflowError を捕まえず、dispatch() の外まで抜けて __main__ の最終防波堤が検証を
        スキップしていた。NaN・無限大・未来の時刻は期限が切れず、`"false"` のような success を
        真に数えていた。
        """
        base = Path(self.tmp) / "cc-mp-verify-cloud-account"
        cases = {
            "control: as written": ({}, True),
            "timestamp string": ({"timestamp": "now"}, False),
            "timestamp null": ({"timestamp": None}, False),
            "timestamp array": ({"timestamp": [1]}, False),
            "timestamp object": ({"timestamp": {}}, False),
            "timestamp true": ({"timestamp": True}, False),
            "timestamp too large for a float": ({"timestamp": 10 ** 400}, False),
            "timestamp NaN": ({"timestamp": float("nan")}, False),
            "timestamp Infinity": ({"timestamp": float("inf")}, False),
            "timestamp in the future": ({"timestamp": time.time() + 3600}, False),
            "success string": ({"success": "false"}, False),
            "success 1": ({"success": 1}, False),
        }
        for name, (fields, want) in cases.items():
            with self.subTest(name):
                cache.set_success("svc", "/p", "exp", 1.0)
                (entry,) = base.glob("svc-*.json")
                data = {**json.loads(entry.read_text(encoding="utf-8")), **fields}
                entry.write_text(json.dumps(data), encoding="utf-8")
                try:
                    hit = cache.get_success("svc", "/p", "exp", 1.0)
                except (TypeError, OverflowError, ValueError) as e:
                    self.fail(f"get_success が {type(e).__name__} を投げた")
                self.assertIs(hit, want)

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

    # --- invalidate (内部バックログ: 切替コマンド検出時の service 単位破棄) ---

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

    # --- epoch / in-flight 窓 (PR #43 Codex R2 P1-2: 並行 hook との競合) ---

    def _base(self) -> Path:
        return Path(self.tmp) / "cc-mp-verify-cloud-account"

    def _no_in_flight_window(self):
        """in-flight 窓を 0 にして epoch だけの挙動を観察する。"""
        p = mock.patch.object(cache, "IN_FLIGHT_SEC", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_invalidate_bumps_epoch_and_stale_epoch_result_is_not_published(self):
        self._no_in_flight_window()
        e0 = cache.current_epoch("github")
        cache.set_success("github", "/p", "exp", 1.0)
        self.assertEqual(cache.invalidate("github"), 1)
        e1 = cache.current_epoch("github")
        self.assertGreater(e1, e0)
        # 無効化前 (旧 epoch) に開始した検証の成功は書かれない
        self.assertFalse(cache.set_success("github", "/p", "exp", 1.0, epoch=e0))
        self.assertFalse(cache.get_success("github", "/p", "exp", 1.0))
        # 現在の epoch で開始した検証は書ける (窓の外)
        self.assertTrue(cache.set_success("github", "/p", "exp", 1.0, epoch=e1))
        self.assertTrue(cache.get_success("github", "/p", "exp", 1.0))

    def test_set_success_refused_within_in_flight_window(self):
        """無効化直後 (切替の実行中とみなす窓内) は現在の epoch でも成功を書かない。"""
        cache.invalidate("github")
        e1 = cache.current_epoch("github")
        self.assertFalse(cache.set_success("github", "/p", "exp", 1.0, epoch=e1))
        self.assertFalse(cache.set_success("github", "/p", "exp", 1.0))
        self.assertFalse(cache.get_success("github", "/p", "exp", 1.0))
        self.assertEqual(list(self._base().glob("github-*.json")), [])

    def test_set_success_allowed_after_in_flight_window(self):
        cache.invalidate("github")
        e1 = cache.current_epoch("github")
        self.assertFalse(cache.set_success("github", "/p", "exp", 1.0, epoch=e1))
        self._no_in_flight_window()  # 窓が過ぎた
        self.assertTrue(cache.set_success("github", "/p", "exp", 1.0, epoch=e1))
        self.assertTrue(cache.get_success("github", "/p", "exp", 1.0))

    def test_in_flight_window_is_per_service(self):
        cache.invalidate("github")
        self.assertTrue(cache.set_success("aws", "/p", "exp", 1.0))
        self.assertTrue(cache.get_success("aws", "/p", "exp", 1.0))

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
        self._no_in_flight_window()
        cache.invalidate("github")
        self.assertTrue(cache.set_success("github", "/p", "exp", 1.0))
        self.assertTrue(cache.get_success("github", "/p", "exp", 1.0))

    def test_corrupt_epoch_file_is_zero_and_recovers(self):
        # 0700: cache dir は他のユーザーが書ける mode なら使われない (umask 002 でも同じ結果に)
        self._base().mkdir(mode=0o700, exist_ok=True)
        (self._base() / "github.epoch").write_text("not json", encoding="utf-8")
        self.assertEqual(cache.current_epoch("github"), 0)
        cache.invalidate("github")
        self.assertGreater(cache.current_epoch("github"), 0)

    def test_epoch_values_that_are_not_ns_integers_are_zero(self):
        """epoch / tombstone が int64 に収まる非負の整数でない epoch ファイルも、無効化の記録が
        無いのと同じ 0 (v0.19.0)。旧版は `int()` で変換し、JSON として読める `Infinity` の
        OverflowError が dispatch() の外まで抜け、__main__ の最終防波堤が検証をスキップしていた。
        """
        self._base().mkdir(mode=0o700, exist_ok=True)
        epoch_file = self._base() / "github.epoch"
        cases = {
            "control: integers": ('{"epoch": 5, "at_ns": 7}', 5),
            "epoch Infinity": ('{"epoch": Infinity, "at_ns": 0}', 0),
            "at_ns Infinity": ('{"epoch": 5, "at_ns": Infinity}', 0),
            "epoch string": ('{"epoch": "5", "at_ns": 0}', 0),
            "epoch true": ('{"epoch": true, "at_ns": 0}', 0),
            "epoch float": ('{"epoch": 5.0, "at_ns": 0}', 0),
            "epoch negative": ('{"epoch": -1, "at_ns": 0}', 0),
            "epoch beyond int64": ('{"epoch": 9223372036854775808, "at_ns": 0}', 0),
        }
        for name, (text, want) in cases.items():
            with self.subTest(name):
                epoch_file.write_text(text, encoding="utf-8")
                try:
                    epoch = cache.current_epoch("github")
                except (OverflowError, ValueError, TypeError) as e:
                    self.fail(f"current_epoch が {type(e).__name__} を投げた")
                self.assertEqual(epoch, want)

    def test_deeply_nested_epoch_file_is_zero_not_an_exception(self):
        """入れ子の深い epoch ファイルも「読めない」と同じ 0 (v0.18.0)。旧版は RecursionError が
        dispatch() の外まで抜け、__main__ の最終防波堤が検証をスキップしていた。"""
        self._base().mkdir(mode=0o700, exist_ok=True)
        (self._base() / "github.epoch").write_bytes(b"[" * 100000 + b"]" * 100000)
        try:
            epoch = cache.current_epoch("github")
        except RecursionError:
            self.fail("入れ子の深い epoch ファイルで current_epoch が RecursionError を投げた")
        self.assertEqual(epoch, 0)
        cache.invalidate("github")
        self.assertGreater(cache.current_epoch("github"), 0)

    def test_epoch_is_per_service(self):
        cache.set_success("aws", "/p", "exp", 1.0)
        cache.invalidate("github")
        self.assertEqual(cache.current_epoch("aws"), 0)
        self.assertTrue(cache.get_success("aws", "/p", "exp", 1.0))

    # --- stat できない epoch / entry (v0.18.0) ---
    #
    # pathlib の `Path.is_file()` は Python 3.13 まで、ENOENT など以外の OSError (長すぎる名前を
    # 指す symlink の ENAMETOOLONG・EACCES) をそのまま投げる (3.14 から False)。try の外で呼んで
    # いたので、`$TMPDIR` にその symlink を置くだけで例外が dispatch() の外まで抜け、__main__ の
    # 最終防波堤がその service の検証をスキップしていた (マージ前レビューの指摘)。3.14 以降でも
    # 同じ失敗を再現するため、`Path.is_file` を 3.13 までの挙動に差し替える。

    _LONG = "a" * 300  # 1 要素が 255 バイトを超える → stat が ENAMETOOLONG

    def _unstattable(self, path: Path) -> None:
        """path を stat できない symlink にし、差し替えた `Path.is_file` が投げることを確かめる。"""
        os.symlink(self._LONG, path)
        patcher = _testutil.patch_is_file_like_py313()
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(OSError):  # 前提: 3.13 までの失敗を再現できている
            path.is_file()
        _testutil.assert_real_is_file_on_this_version(self, path)

    def test_epoch_file_that_cannot_be_statted_is_zero(self):
        self._unstattable(cache._epoch_path("firebase"))
        try:
            epoch = cache.current_epoch("firebase")
        except OSError as e:
            self.fail(f"stat できない epoch ファイルで current_epoch が {type(e).__name__} を投げた")
        self.assertEqual(epoch, 0)

    def test_entry_that_cannot_be_statted_is_a_miss(self):
        self._unstattable(
            cache._cache_path(cache._cache_key("firebase", "/p", "exp", None, None, None))
        )
        try:
            hit = cache.get_success("firebase", "/p", "exp", 1.0)
        except OSError as e:
            self.fail(f"stat できない cache entry で get_success が {type(e).__name__} を投げた")
        self.assertFalse(hit)

    def test_writes_leave_no_tmp_files(self):
        cache.set_success("github", "/p", "exp", 1.0)
        cache.invalidate("github")
        self.assertEqual([p.name for p in self._base().glob("*.tmp")], [])


@unittest.skipUnless(hasattr(os, "geteuid"), "所有者と mode は POSIX のもの")
class TestCacheDirOwnership(unittest.TestCase):
    """cache dir は自分の所有で、他のユーザーが書けない実ディレクトリのときだけ使う (v0.19.0)。

    TMPDIR の無い Linux などでは共有の `/tmp` に置かれ、別のユーザーが先に同じ名前の dir を
    作れる。旧版は所有者も mode も確かめずに使い、置かれた entry で検証を省きえた。使わない
    ときは成功 cache も epoch も補助ファイルも読まず書かない (毎回、通常の照合をする)。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"TMPDIR": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = self.tmp / "cc-mp-verify-cloud-account"

    def _seed(self):
        """使える dir に正しい entry を書いておく (dir が使われれば hit する)。"""
        self.assertTrue(cache.set_success("svc", "/p", "exp", 1.0))
        self.assertTrue(cache.get_success("svc", "/p", "exp", 1.0))

    def _assert_not_used(self):
        self.assertIsNone(cache.state_dir())
        self.assertIsNone(cache.service_state_path("svc", ".x.json"))
        self.assertFalse(cache.get_success("svc", "/p", "exp", 1.0))
        self.assertFalse(cache.set_success("svc", "/p", "exp", 1.0))
        self.assertEqual(cache.invalidate("svc"), 0)
        self.assertFalse(os.path.lexists(self.base / "svc.epoch"))

    def test_new_dir_is_private(self):
        self._seed()
        self.assertEqual(stat.S_IMODE(os.lstat(self.base).st_mode), 0o700)

    def test_dir_that_other_users_can_write_is_not_used(self):
        self._seed()
        self.addCleanup(os.chmod, self.base, 0o700)
        for mode in (0o777, 0o770, 0o703):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.base, mode)
                self._assert_not_used()

    def test_dir_owned_by_another_user_is_not_used(self):
        self._seed()
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            self._assert_not_used()

    def test_symlink_to_a_private_dir_is_not_used(self):
        """自分の dir を指す symlink も使わない (置いた人の選んだ場所に書かされる)。"""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        os.symlink(elsewhere, self.base)
        self._assert_not_used()
        self.assertEqual(list(elsewhere.iterdir()), [])


class TestContextInCacheKey(unittest.TestCase):
    """context option も cache キーに含める。

    含めないと `aws --profile other s3 rm` が既定 profile の成功 entry を hit し、
    未検証のまま allow される (TTL の 30 秒間、別アカウントへの write が通る)。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._p = mock.patch.dict(os.environ, {"TMPDIR": self.tmp})
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_different_context_is_a_different_entry(self):
        cache.set_success("aws", "/p", "exp", 1.0, None, {"profile": "a"})
        self.assertFalse(
            cache.get_success("aws", "/p", "exp", 1.0, None, {"profile": "b"})
        )

    def test_same_context_round_trips(self):
        cache.set_success("aws", "/p", "exp", 1.0, None, {"profile": "a"})
        self.assertTrue(
            cache.get_success("aws", "/p", "exp", 1.0, None, {"profile": "a"})
        )

    def test_context_entry_does_not_satisfy_default_lookup(self):
        cache.set_success("aws", "/p", "exp", 1.0, None, {"profile": "a"})
        self.assertFalse(cache.get_success("aws", "/p", "exp", 1.0))

    def test_default_entry_does_not_satisfy_context_lookup(self):
        cache.set_success("aws", "/p", "exp", 1.0)
        self.assertFalse(
            cache.get_success("aws", "/p", "exp", 1.0, None, {"profile": "a"})
        )


class TestIdentityEnvInCacheKey(unittest.TestCase):
    """アカウントを決める環境変数 (identity env) も cache キーに含める (v0.17.0)。

    settings の `env` は保存した時点で起動中のセッションに反映される。キーが行頭の
    inline env だけだと、`AWS_PROFILE` を変えた直後の TTL (30 秒) の間、前の profile
    での成功 entry を hit して未検証のまま通る。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._p = mock.patch.dict(os.environ, {"TMPDIR": self.tmp})
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_different_identity_env_is_a_different_entry(self):
        cache.set_success("aws", "/p", "exp", 1.0, identity_env={"AWS_PROFILE": "a"})
        self.assertFalse(
            cache.get_success("aws", "/p", "exp", 1.0, identity_env={"AWS_PROFILE": "b"})
        )
        self.assertTrue(
            cache.get_success("aws", "/p", "exp", 1.0, identity_env={"AWS_PROFILE": "a"})
        )

    def test_pinned_entry_does_not_satisfy_unpinned_lookup(self):
        cache.set_success("aws", "/p", "exp", 1.0, identity_env={"AWS_PROFILE": "a"})
        self.assertFalse(cache.get_success("aws", "/p", "exp", 1.0))
        cache.set_success("aws", "/p", "exp", 1.0)
        self.assertFalse(
            cache.get_success("aws", "/p", "exp", 1.0, identity_env={"AWS_PROFILE": "b"})
        )

    def test_identity_values_are_not_written_to_cache_files(self):
        secret = "vca-identity-secret-value-0123456789"
        self.assertTrue(
            cache.set_success("github", "/p", "exp", 1.0, identity_env={"GH_TOKEN": secret})
        )
        base = Path(self.tmp) / "cc-mp-verify-cloud-account"
        files = [p for p in base.iterdir() if p.is_file()]
        self.assertTrue(files)
        for p in files:
            self.assertNotIn(secret, p.name)
            self.assertNotIn(secret, p.read_text(encoding="utf-8"))

    def test_identity_env_picks_declared_names_and_prefixes(self):
        import types

        svc = types.SimpleNamespace(
            IDENTITY_ENV_VARS=frozenset({"KUBECONFIG"}),
            IDENTITY_ENV_PREFIXES=("AWS_",),
        )
        env = {
            "KUBECONFIG": "/k",
            "AWS_PROFILE": "p",
            "AWS_REGION": "r",
            "PATH": "/bin",
            "KUBECONFIG_EXTRA": "x",
        }
        self.assertEqual(
            cache.identity_env(svc, env),
            {"KUBECONFIG": "/k", "AWS_PROFILE": "p", "AWS_REGION": "r"},
        )


if __name__ == "__main__":
    unittest.main()
