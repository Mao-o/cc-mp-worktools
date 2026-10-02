"""テストが作る git repo (push 先の bare repo を含む) と、hook が起動する git で、自動 gc /
maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に tempdir の後始末が走ると、tearDown が
`Directory not empty` で落ちる。この flaky は直接は検出できない (object の hash 次第で偶発的)
ので、原因の側 = **git が実際に見ている設定**を床にする。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config --get` は未設定のキーで exit 1 になる
ので、「無い」は例外ではなく値の不一致 (assertion) として出す。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}


def configured(repo: str, key: str, *, local: bool = False) -> str | None:
    """`_testutil.git` 経由で git が repo で見ている `key` の値。未設定なら None。

    `local=True` なら repo 自身の config ファイルだけを読む (env で渡した設定を含めない)。
    """
    scope = ["--local"] if local else []
    try:
        return _testutil.git(repo, "config", *scope, "--get", key).stdout.strip()
    except subprocess.CalledProcessError:
        return None


class TestHelpersStopBackgroundMaintenance(unittest.TestCase):
    """テストクラスが env を patch していなくても、repo を作るヘルパー自身が止める。

    「patch していない」状態は、`GIT_CONFIG_*` をこのテストの中で明示的に外して作る。
    他のテストの patch 漏れや、開発者の shell が export している値に左右されないため。
    """

    def test_repo_made_without_any_env_patch(self):
        with mock.patch.dict(os.environ):
            for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
                del os.environ[name]
            with tempfile.TemporaryDirectory() as tmp:
                repo = _testutil.init_repo(os.path.join(tmp, "repo"))
                for key, expected in EXPECTED.items():
                    with self.subTest(key=key):
                        self.assertEqual(configured(repo, key), expected)


class TestBareOriginKeepsTheSettingsInItsOwnConfig(unittest.TestCase):
    """push 先の bare repo は、env ではなく **repo 自身の config** で止めていること。

    `git push` がローカルの path へ送るとき、受け側の `receive-pack` は repo 用の env
    (`GIT_CONFIG_COUNT` など) を外されて起動するので、env の設定は届かない。env を渡した
    クライアントから push しても、origin 側に設定が無いと `receive-pack` が自動 maintenance を
    起動する (実測)。そのため `--local` で config ファイルだけを見る。
    """

    def test_bare_origin_has_the_settings_in_its_own_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = _testutil.init_bare_origin(tmp)
            for key, expected in {**EXPECTED, "receive.autogc": "false"}.items():
                with self.subTest(key=key):
                    self.assertEqual(configured(bare, key, local=True), expected)


class TestHookLaunchedGitInheritsTheSettings(HookTestCase):
    """hook (製品コード) が起動する git にも、同じ設定が届くこと。

    `gitscan._git` は env を渡さず `os.environ` を継承するので、基底クラスが patch した env が
    そのまま見える。ここが外れると hook の git だけ自動 maintenance が復活する。
    """

    def test_git_launched_by_the_hook_sees_the_settings(self):
        for key, expected in EXPECTED.items():
            with self.subTest(key=key):
                res = self.gitscan._git(self.repo, ["config", "--get", key])
                self.assertEqual(
                    (res.returncode, res.stdout.decode().strip()), (0, expected)
                )

    def test_helper_git_sees_the_settings_in_the_test_repo(self):
        for key, expected in EXPECTED.items():
            with self.subTest(key=key):
                self.assertEqual(configured(self.repo, key), expected)


if __name__ == "__main__":
    unittest.main()
