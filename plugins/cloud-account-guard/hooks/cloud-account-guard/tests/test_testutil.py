"""テスト隔離そのもののテスト (`tests/_testutil.py`)。

隔離は「判定を変えうる実環境の env を落とす」ことで他モジュールの false green /
false red を防ぐが、**その落とす処理自体には誰も負テストを持っていなかった**:
現在のマシンに `GH_TOKEN` 等が無いため、pop ループを消しても全 suite が green の
まま通る (マージ前レビューの mutation で survive を確認)。ここで sentinel を
立てて、落ちること・`stop()` で戻ることを固定する。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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


class TestIsolateGitDiscovery(unittest.TestCase):
    """module-level の `_isolate_git_discovery` (import 時に 1 回走る) の固定。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        # 呼び出しが書き換える env と tempfile.tempdir をテスト後に戻す
        env_patcher = mock.patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        tempdir_patcher = mock.patch.object(tempfile, "tempdir", tempfile.tempdir)
        tempdir_patcher.start()
        self.addCleanup(tempdir_patcher.stop)

    def test_redirecting_git_env_is_dropped(self):
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            with self.subTest(env=name):
                os.environ[name] = str(self.root / "elsewhere")
                _testutil._isolate_git_discovery()
                self.assertNotIn(name, os.environ)

    def test_tempdir_inside_a_work_tree_moves_outside_the_repo(self):
        (self.root / ".git").mkdir()
        inside = self.root / "tmp"
        inside.mkdir()
        tempfile.tempdir = str(inside)
        _testutil._isolate_git_discovery()
        moved = Path(tempfile.gettempdir()).resolve()
        self.assertNotEqual(moved, inside)
        self.assertFalse(
            any((d / ".git").exists() for d in (moved, *moved.parents)),
            f"tempdir がまだ repo の中: {moved}",
        )
        self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], str(moved))

    def test_tempdir_outside_any_repo_is_kept(self):
        tempfile.tempdir = str(self.root)
        _testutil._isolate_git_discovery()
        self.assertEqual(tempfile.tempdir, str(self.root))
        self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], str(self.root))


class TestSanitizedEnv(unittest.TestCase):
    """`test_main` が子プロセス env を組むときに使う除去規則 (共有点)。"""

    def test_drops_the_same_names_without_mutating_the_base(self):
        base = {
            "GH_TOKEN": "x",
            "CLOUD_ACCOUNT_GUARD_MODE": "off",
            "CLOUDSDK_CORE_PROJECT": "p",
            "CLOUDSDK_CONFIG": "/tmp/cfg",
            "PATH": "/usr/bin",
        }
        sanitized = _testutil.sanitized_env(base)
        self.assertNotIn("GH_TOKEN", sanitized)
        self.assertNotIn("CLOUD_ACCOUNT_GUARD_MODE", sanitized)
        self.assertNotIn("CLOUDSDK_CORE_PROJECT", sanitized)
        self.assertEqual(sanitized["CLOUDSDK_CONFIG"], "/tmp/cfg")
        self.assertEqual(sanitized["PATH"], "/usr/bin")
        self.assertIn("GH_TOKEN", base, "引数の dict を書き換えている")


class TestOuterCloudEnvIsDropped(unittest.TestCase):
    """開発者の手元の `AWS_CONFIG_FILE` / `AWS_PROFILE` / `KUBECONFIG` / `CDPATH` などで、テストの
    結果が変わらないこと。隔離が外す env は、各 service が CLI のアカウントを決める env として宣言する
    `IDENTITY_ENV_VARS` / `IDENTITY_ENV_PREFIXES` を (隔離が設定し直すものを除いて) すべて含む。"""

    # 外側に export されていると、実際にテストが落ちた値 (存在しないパス・名前)。
    OUTER = {
        "AWS_CONFIG_FILE": "/nonexistent-cag-test/aws/config",
        "AWS_PROFILE": "prod",
        "KUBECONFIG": "/nonexistent-cag-test/kube/config",
        "CDPATH": "/nonexistent-cag-test-cdpath",
    }

    def _declared_identity_env(self):
        from services import ALL

        reset = set(_testutil.cli_config_env(Path("/x")))
        names, prefixes = set(), set()
        for svc in ALL:
            names |= set(svc.IDENTITY_ENV_VARS) - reset
            prefixes |= set(svc.IDENTITY_ENV_PREFIXES)
        return sorted(names), sorted(prefixes)

    def test_every_declared_identity_env_is_dropped(self):
        names, prefixes = self._declared_identity_env()
        self.assertIn("KUBECONFIG", names)
        self.assertIn("AWS_", prefixes)
        sample = {name: "x" for name in names}
        sample.update({prefix + "CAG_TEST": "x" for prefix in prefixes})
        sanitized = _testutil.sanitized_env({**sample, "PATH": "/usr/bin"})
        for name in sample:
            with self.subTest(name):
                self.assertNotIn(name, sanitized)
        self.assertEqual(sanitized["PATH"], "/usr/bin")

    def test_start_isolation_drops_the_outer_cloud_env(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        with mock.patch.dict(os.environ, self.OUTER):
            isolation = _testutil.start_isolation(root)
            try:
                for name in self.OUTER:
                    with self.subTest(name):
                        self.assertNotIn(name, os.environ)
            finally:
                isolation.stop()
            self.assertEqual(os.environ["AWS_PROFILE"], "prod")

    def _run_in_child(self, args):
        env = {**os.environ, **self.OUTER, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [sys.executable, *args],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    def test_importing_the_helper_drops_the_outer_cloud_env_for_the_process(self):
        """`start_isolation()` を呼ばないモジュール (cd の追跡のテストなど) のために、import の時点で
        プロセスの env から落とす。"""
        res = self._run_in_child(
            ["-c", "import os, _testutil, json; print(json.dumps(sorted(os.environ)))"]
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        present = set(json.loads(res.stdout))
        for name in self.OUTER:
            with self.subTest(name):
                self.assertNotIn(name, present)

    def test_tests_that_read_the_outer_env_pass_with_it_exported(self):
        """外側の env で実際に落ちたテストが、その env を export したまま green になる。"""
        res = self._run_in_child(
            [
                "-m",
                "unittest",
                "test_dispatcher.TestRemediationGuidanceContract.test_aws_mismatch_with_profile_in_config",
                "test_pinning.TestUnregisteredButPinned.test_no_accounts_file_mentions_pinning",
                "test_pinning.TestWorkdir.test_segment_dirs",
                "test_kubectl_workdir.KubectlWorkdirTests.test_sequential_cd_is_still_resolved",
            ]
        )
        self.assertEqual(res.returncode, 0, res.stderr[-3000:])
        self.assertIn("Ran 4 tests", res.stderr)


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


class TestRealCliGuard(unittest.TestCase):
    """テストから本物のクラウド CLI を起動させないガード (`_testutil` の Popen と TestCase.run の包み)。

    「一時 dir の外」を作るため、偽の CLI を置いた dir とは別の dir を `tempfile.tempdir` にする。
    偽の CLI は起動されると印のファイルを作るので、起動されなかったことを印の有無で確かめる。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.marker = self.root / "launched"
        for name in ("gh", "notacli"):
            script = self.bin / name
            script.write_text(f"#!/bin/sh\necho {name} >> '{self.marker}'\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
        self.env = {"PATH": str(self.bin)}
        self.start = len(_testutil.REAL_CLI_LAUNCHES)
        # このテスト自身が記録で落ちないよう、終わりに記録を戻す (登録順で、ガードの確認より先に走る)。
        self.addCleanup(self._forget_records)

    def _forget_records(self):
        del _testutil.REAL_CLI_LAUNCHES[self.start:]

    def _outside(self):
        """`self.bin` を一時 dir の外にする (tempdir を別の dir に向ける)。"""
        inner = self.root / "tmp"
        inner.mkdir(exist_ok=True)
        return mock.patch.object(tempfile, "tempdir", str(inner))

    def _records(self):
        return _testutil.REAL_CLI_LAUNCHES[self.start:]

    def _launched(self) -> str:
        return self.marker.read_text(encoding="utf-8") if self.marker.exists() else ""

    def _run(self, args, **kwargs):
        """起動して結果を返す。FileNotFoundError はそのまま返し、それ以外の例外は失敗にする。"""
        kwargs.setdefault("env", self.env)
        try:
            return subprocess.run(args, capture_output=True, **kwargs)
        except FileNotFoundError as e:
            return e
        except Exception as e:  # noqa: BLE001 — ガードの不具合を ERROR ではなく FAIL として数える
            self.fail(f"起動で想定外の例外: {e!r}")

    def _assert_not_found(self, args, **kwargs):
        got = self._run(args, **kwargs)
        self.assertIsInstance(got, FileNotFoundError, f"起動された: {got!r}")

    def _assert_ran(self, args, **kwargs):
        got = self._run(args, **kwargs)
        self.assertIsInstance(got, subprocess.CompletedProcess, f"起動されなかった: {got!r}")
        self.assertEqual(got.returncode, 0)

    def _assert_blocked(self, args, **kwargs):
        with self._outside():
            self._assert_not_found(args, **kwargs)
        self.assertEqual(self._launched(), "", "本物扱いの CLI が起動された")
        self.assertEqual(len(self._records()), 1, self._records())
        self.assertEqual(self._records()[0][0], os.path.realpath(self.bin / "gh"))

    def test_cli_outside_the_tempdir_is_blocked_and_recorded(self):
        self._assert_blocked(["gh", "auth", "status"])

    def test_cli_named_by_path_is_blocked(self):
        self._assert_blocked([str(self.bin / "gh"), "auth", "status"])

    def test_cli_named_relative_to_cwd_is_blocked(self):
        self._assert_blocked(["./gh"], cwd=str(self.bin))

    def test_cli_given_as_executable_is_blocked(self):
        self._assert_blocked(["anything"], executable=str(self.bin / "gh"))

    def test_cli_given_as_a_single_string_is_blocked(self):
        self._assert_blocked("gh")

    def test_cli_found_through_os_environ_path_is_blocked(self):
        """`env` を渡さない起動は `os.environ` の PATH で引く。"""
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), self._outside():
            self._assert_not_found(["gh"], env=None)
        self.assertEqual(self._launched(), "")
        self.assertEqual(len(self._records()), 1)

    def test_fake_cli_inside_the_tempdir_runs(self):
        """テストが一時 dir に置いた偽の CLI はそのまま走る。"""
        with mock.patch.object(tempfile, "tempdir", str(self.root)):
            self._assert_ran(["gh"])
        self.assertEqual(self._launched(), "gh\n")
        self.assertEqual(self._records(), [])

    def test_other_commands_run(self):
        with self._outside():
            self._assert_ran(["notacli"])
        self.assertEqual(self._launched(), "notacli\n")
        self.assertEqual(self._records(), [])

    def test_missing_cli_is_not_recorded(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self._outside():
            self._assert_not_found(["gh"], env={"PATH": str(empty)})
            self._assert_not_found(["./gh"], cwd=str(empty))
        self.assertEqual(self._records(), [])

    def test_shell_launch_is_out_of_scope(self):
        """シェルの中の起動は見えない (限界の固定。偽の CLI はそのまま走る)。"""
        with self._outside():
            self._assert_ran("gh", shell=True)
        self.assertEqual(self._launched(), "gh\n")
        self.assertEqual(self._records(), [])

    def test_a_test_that_launches_a_real_cli_fails(self):
        """記録が増えたテストは assertion の失敗になる。CLI の失敗を握る経路 (builder の `show`) でも落ちる。"""
        outer = self

        class Inner(unittest.TestCase):
            def test_swallowed(self):
                with outer._outside():
                    try:
                        subprocess.run(["gh"], env=outer.env, capture_output=True)
                    except Exception:  # noqa: BLE001 — 握る経路の再現
                        pass

            def test_clean(self):
                subprocess.run(["notacli"], env=outer.env, capture_output=True)

        result = unittest.TestResult()
        unittest.TestSuite(
            [Inner("test_swallowed"), Inner("test_clean")]
        ).run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual([case.id().rsplit(".", 1)[-1] for case, _tb in result.failures], ["test_swallowed"])
        self.assertIn("本物のクラウド CLI", result.failures[0][1])
        self.assertEqual(self._launched(), "notacli\n")


if __name__ == "__main__":
    unittest.main()
