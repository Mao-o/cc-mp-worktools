"""テストが作る git repo で、自動 gc / maintenance が止まっていること。

理由は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント (post-implementation-review の同名の
テストと同じ床。そちらは commit するので実害が出る側で、こちらは `init_repo` を揃えるだけ)。

見るのは `init_repo` が**実際に git へ渡した env** (`subprocess.run` を spy で包んで捕まえる)。
テストが自分で組んだ env で確かめると、`init_repo` が env を渡し忘れても気付けない。止める経路
(env の `GIT_CONFIG_COUNT` / global の fixture) は同じ値を持つので、有効値だけを見ると片方が欠けても
もう片方が埋めて通ってしまう。1 本ずつ単独で見る: env は 4 設定を 1 項目ずつ、fixture は内容の
完全一致 (5 設定だけを持つこと。余計な設定が増えると、git を起動する全テストに効く)。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config` は未設定 / 読めないとき exit 1 などに
なるので、`check=True` を使わず、「無い」は例外ではなく値の不一致 (assertion) として出す。
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
        `GIT_CONFIG_*` を外し、global / system の config も空にして作る。開発者の shell や
        `~/.gitconfig` の値に左右されないため (env を渡し損ねた git が開発者の設定を読むと、
        有効値が揃って床が黙って通ってしまう)。
        """
        passed: list[tuple[str, dict]] = []
        real_run = subprocess.run

        def spy(argv, *args, **kwargs):
            if argv[:1] == ["git"]:
                passed.append((argv[1], dict(kwargs.get("env") or os.environ)))
            return real_run(argv, *args, **kwargs)

        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
                del os.environ[name]
            os.environ.update({"HOME": tmp, "XDG_CONFIG_HOME": tmp, "GIT_CONFIG_NOSYSTEM": "1"})
            with mock.patch.object(subprocess, "run", side_effect=spy):
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

    def test_the_global_fixture_holds_only_the_five_settings(self):
        """渡した env の `GIT_CONFIG_GLOBAL` が指す fixture が、5 設定だけを持つこと (完全一致)。

        キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
        将来足された `diff.noprefix` など) が増えても通る。`git config --list` はキーを小文字で出す。
        """
        expected = sorted(f"{key.lower()}={value}" for key, value in EXPECTED_WITH_RECEIVE.items())
        for sub, passed in self._envs_passed_by_init_repo():
            code, out = self._git_config(passed, "--global", "--list")
            with self.subTest(git=sub):
                self.assertEqual((code, sorted(out.splitlines())), (0, expected))


if __name__ == "__main__":
    unittest.main()
