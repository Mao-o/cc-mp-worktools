"""テストが作る git repo で、自動 gc / maintenance が止まっていること。

理由は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント (post-implementation-review の同名の
テストと同じ床。そちらは commit するので実害が出る側で、こちらは `init_repo` を揃えるだけ)。
期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config --get` は未設定のキーで exit 1 に
なるので、「無い」は例外ではなく値の不一致 (assertion) として出る。
"""
import os
import subprocess
import tempfile
import unittest

import _testutil

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}


class TestInitRepoSettings(unittest.TestCase):
    def test_git_sees_the_settings_init_repo_passes(self):
        """`init_repo` が git に渡す env (`HERMETIC_GIT_ENV`) を、git がそのまま解釈すること。"""
        env = {**os.environ, **_testutil.HERMETIC_GIT_ENV}
        with tempfile.TemporaryDirectory() as tmp:
            repo = _testutil.init_repo(os.path.join(tmp, "repo"))
            for key, expected in EXPECTED.items():
                with self.subTest(key=key):
                    res = subprocess.run(
                        ["git", "config", "--get", key],
                        cwd=repo,
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual((res.returncode, res.stdout.strip()), (0, expected))


if __name__ == "__main__":
    unittest.main()
