"""テストが作る git repo で、自動 gc / maintenance が止まっていること。

理由は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント (post-implementation-review の同名の
テストと同じ床。そちらは commit するので実害が出る側で、こちらは `init_repo` を揃えるだけ)。

見るのは `init_repo` が**実際に git へ渡した env** (`subprocess.run` を spy で包んで捕まえる)。
テストが自分で組んだ env で確かめると、`init_repo` が env を渡し忘れても気付けない。止める経路
(env の `GIT_CONFIG_COUNT` / global の fixture) は同じ値を持つので、有効値だけを見ると片方が欠けても
もう片方が埋めて通ってしまう。1 本ずつ単独で見る。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config --get` は未設定のキーで exit 1 に
なるので、「無い」は例外ではなく値の不一致 (assertion) として出る。
"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import _testutil

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
# global の fixture が持つ設定 (`receive.autogc` は push の受け側 `receive-pack` 用)
EXPECTED_WITH_RECEIVE = {**EXPECTED, "receive.autogc": "false"}


class TestInitRepoSettings(unittest.TestCase):
    def _envs_passed_by_init_repo(self) -> list[tuple[str, dict]]:
        """`init_repo` が起動した git ごとの (サブコマンド, 渡した env)。

        env を渡していなければ、そのとき継いだ `os.environ` を返す。「patch していない」状態は
        `GIT_CONFIG_*` をこのテストの中で外して作る (開発者の shell が export している値に
        左右されないため)。
        """
        passed: list[tuple[str, dict]] = []
        real_run = subprocess.run

        def spy(argv, *args, **kwargs):
            if argv[:1] == ["git"]:
                passed.append((argv[1], dict(kwargs.get("env") or os.environ)))
            return real_run(argv, *args, **kwargs)

        with mock.patch.dict(os.environ):
            for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
                del os.environ[name]
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                subprocess, "run", side_effect=spy
            ):
                _testutil.init_repo(os.path.join(tmp, "repo"))
        self.assertTrue(passed, "前提: init_repo が git を起動している (空の床にしない)")
        return passed

    def _git_config(self, env: dict, *args: str) -> tuple[int, str]:
        """repo の外 (一時ディレクトリ) で `git config <args>` を `env` で実行する。"""
        with tempfile.TemporaryDirectory() as tmp:
            res = subprocess.run(
                ["git", "config", *args], cwd=tmp, env=env, capture_output=True, text=True
            )
        return res.returncode, res.stdout.strip()

    def test_the_env_alone_has_the_four_settings(self):
        """global の fixture を外しても、渡した env の `GIT_CONFIG_COUNT` だけで 4 設定が効くこと。"""
        for sub, passed in self._envs_passed_by_init_repo():
            env = {**passed, "GIT_CONFIG_GLOBAL": os.devnull}
            for key, expected in EXPECTED.items():
                with self.subTest(git=sub, key=key):
                    self.assertEqual(self._git_config(env, "--get", key), (0, expected))

    def test_the_global_fixture_has_the_five_settings(self):
        """渡した env の `GIT_CONFIG_GLOBAL` が指す fixture が、5 設定を持つこと。"""
        for sub, passed in self._envs_passed_by_init_repo():
            for key, expected in EXPECTED_WITH_RECEIVE.items():
                with self.subTest(git=sub, key=key):
                    self.assertEqual(self._git_config(passed, "--global", "--get", key), (0, expected))


if __name__ == "__main__":
    unittest.main()
