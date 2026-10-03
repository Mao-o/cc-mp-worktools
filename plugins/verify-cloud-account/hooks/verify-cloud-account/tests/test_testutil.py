"""テスト隔離そのもののテスト (`tests/_testutil.py`)。

隔離は「判定を変えうる実環境の env を落とす」ことで他モジュールの false green /
false red を防ぐが、**その落とす処理自体には誰も負テストを持っていなかった**:
現在のマシンに `GH_TOKEN` 等が無いため、pop ループを消しても全 suite が green の
まま通る (マージ前レビューの mutation で survive を確認)。ここで sentinel を
立てて、落ちること・`stop()` で戻ることを固定する。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil


class TestStartIsolation(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def test_leaky_env_is_dropped_and_restored(self):
        """実環境の `GH_TOKEN` は隔離中は見えず、`stop()` で元に戻る。

        落ちていないと、gh のローカル読取が「token env があるので CLI に委ねる」
        経路に入り、ローカル読取を検証しているテストが**別の理由で green** になる。
        """
        with mock.patch.dict(os.environ, {"GH_TOKEN": "sentinel-token"}):
            isolation = _testutil.start_isolation(self.root)
            try:
                self.assertNotIn("GH_TOKEN", os.environ)
            finally:
                isolation.stop()
            self.assertEqual(os.environ["GH_TOKEN"], "sentinel-token")

    def test_cloudsdk_prefix_env_is_dropped_except_the_config_dir(self):
        with mock.patch.dict(
            os.environ,
            {"CLOUDSDK_CORE_PROJECT": "leaked-proj", "CLOUDSDK_PYTHON": "python3"},
        ):
            isolation = _testutil.start_isolation(self.root)
            try:
                self.assertNotIn("CLOUDSDK_CORE_PROJECT", os.environ)
                self.assertNotIn("CLOUDSDK_PYTHON", os.environ)
                self.assertEqual(
                    os.environ["CLOUDSDK_CONFIG"], str(self.root / "gcloud")
                )
            finally:
                isolation.stop()
            self.assertEqual(os.environ["CLOUDSDK_CORE_PROJECT"], "leaked-proj")

    def test_home_env_matches_the_patched_path_home(self):
        """`os.environ["HOME"]` と `Path.home()` が隔離中に食い違わないこと。

        食い違うと `cli_config.home_overridden()` の基準 (`os.environ["HOME"]`) が
        実環境の `$HOME` になり、隔離の内側で実環境が判定に混ざる。
        """
        isolation = _testutil.start_isolation(self.root)
        try:
            self.assertEqual(
                os.environ["HOME"], str(_testutil.ISOLATED_HOME)
            )
            self.assertEqual(Path.home(), _testutil.ISOLATED_HOME)
        finally:
            isolation.stop()


class TestSanitizedEnv(unittest.TestCase):
    """`test_main` が子プロセス env を組むときに使う除去規則 (共有点)。"""

    def test_drops_the_same_names_without_mutating_the_base(self):
        base = {
            "GH_TOKEN": "x",
            "VERIFY_CLOUD_ACCOUNT_MODE": "off",
            "CLOUDSDK_CORE_PROJECT": "p",
            "CLOUDSDK_CONFIG": "/tmp/cfg",
            "PATH": "/usr/bin",
        }
        sanitized = _testutil.sanitized_env(base)
        self.assertNotIn("GH_TOKEN", sanitized)
        self.assertNotIn("VERIFY_CLOUD_ACCOUNT_MODE", sanitized)
        self.assertNotIn("CLOUDSDK_CORE_PROJECT", sanitized)
        self.assertEqual(sanitized["CLOUDSDK_CONFIG"], "/tmp/cfg")
        self.assertEqual(sanitized["PATH"], "/usr/bin")
        self.assertIn("GH_TOKEN", base, "引数の dict を書き換えている")


class TestIsFileLikePy313(unittest.TestCase):
    """`patch_is_file_like_py313` の差し替えは、Python 3.13 までの本物の `Path.is_file` と同じに
    振る舞う。違ってよいのは、3.14 で本物が変わった「stat できない path」(本物は False、差し替えは
    3.13 までと同じく OSError) だけ (マージ前レビューの指摘: パスに NUL を含むとき・
    `follow_symlinks=False` のときに食い違っていた)。"""

    @staticmethod
    def _result_of(fn, path, **kwargs):
        # 例外は型の名前にして比べる (食い違いを ERROR ではなく assertion の失敗にする)
        try:
            return fn(path, **kwargs)
        except (OSError, ValueError) as e:
            return "OSError" if isinstance(e, OSError) else type(e).__name__

    def test_matches_the_real_one(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        unstattable = tmp / "long"
        os.symlink("a" * 300, unstattable)  # 1 要素が 255 バイトを超える → stat が ENAMETOOLONG
        loop = tmp / "loop"
        os.symlink(loop, loop)
        regular = tmp / "regular"
        regular.write_text("x", encoding="utf-8")
        cases = {
            "regular file": (regular, {}),
            "missing": (tmp / "missing", {}),
            "directory": (tmp, {}),
            "symlink loop": (loop, {}),
            "NUL in the path": (Path(f"{tmp}/a\0b"), {}),
            "unstattable": (unstattable, {}),
        }
        if sys.version_info >= (3, 13):  # follow_symlinks は 3.13 から
            cases["unstattable, follow_symlinks=False"] = (unstattable, {"follow_symlinks": False})
        for name, (path, kwargs) in cases.items():
            with self.subTest(name):
                real = self._result_of(_testutil._REAL_IS_FILE, path, **kwargs)
                substitute = self._result_of(_testutil._is_file_like_py313, path, **kwargs)
                if name == "unstattable":
                    # 3.14 未満は本物も投げることまで見る (fixture が stat できてしまう環境では、
                    # 両方 False で一致して通ってしまう)
                    expected_real = "OSError" if _testutil.REAL_IS_FILE_RAISES else False
                    self.assertEqual((real, substitute), (expected_real, "OSError"))
                else:
                    self.assertEqual(substitute, real)


class TestAssertRealIsFileOnThisVersion(unittest.TestCase):
    """前提の helper (`assert_real_is_file_on_this_version`) 自身のテスト。

    helper が何も確かめなくなっても (no-op)、使う側のテストは green のままになる (マージ前
    レビューの指摘)。前提と食い違う path を渡すと、どの版でも assertion で落ちることを見る。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fails_for_a_path_that_contradicts_the_premise(self):
        regular = self.tmp / "regular"  # stat できるので、3.13 までも投げず、3.14 からも True
        regular.write_text("x", encoding="utf-8")
        with self.assertRaises(AssertionError):
            _testutil.assert_real_is_file_on_this_version(self, regular)

    def test_passes_for_a_path_that_cannot_be_statted(self):
        unstattable = self.tmp / "long"
        os.symlink("a" * 300, unstattable)  # 1 要素が 255 バイトを超える → stat が ENAMETOOLONG
        _testutil.assert_real_is_file_on_this_version(self, unstattable)


if __name__ == "__main__":
    unittest.main()
