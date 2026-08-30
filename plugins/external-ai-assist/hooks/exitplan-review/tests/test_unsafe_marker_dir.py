"""共有 $TMPDIR で他ユーザーが先回りして marker ディレクトリを
作った場合、使わずにプランレビューを拒否すること。

`_common/flock.py` の `ensure_private_root` は post-implementation-review の
`state_root()` と exitplan-review の `marker_dir` の両方で使う共通の検査
(`tests/test_flock.py::TestEnsurePrivateRoot` が単体レベルを担当)。ここでは
exitplan-review 側の `main()` が実際に拒否・通知することを end-to-end で確認する。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from _testutil import FINDINGS, PLAN, HookTestCase

SESSION = "sess-unsafe-marker-dir"


class TestUnsafeMarkerDir(HookTestCase):
    def _marker_dir(self) -> str:
        return os.path.join(self.tmpdir, "plan-review-markers")

    def test_world_writable_marker_dir_is_refused(self):
        """regression: 誰でも書けるディレクトリが先回りして存在していても、
        黙って受け入れずプランレビューを拒否し、その旨を 1 行通知する。

        `os.mkdir(mode=...)` は umask でマスクされうるため、`os.chmod` で
        明示的に 0o777 にしてから検証する (verifying-changes-empirically の
        負テスト作法)。
        """
        marker_dir = self._marker_dir()
        os.makedirs(marker_dir)
        os.chmod(marker_dir, 0o777)
        self.assertEqual(
            os.stat(marker_dir).st_mode & 0o777, 0o777, "umask でマスクされていないこと"
        )

        data = self.assertNotBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))

        self.assertEqual(self.cursor_calls, [], "安全でないディレクトリのままレビューが走った")
        self.assertEqual(self.codex_calls, [])
        self.assertIn("状態ディレクトリ", data.get("systemMessage", ""))
        self.assertEqual(self.marker(SESSION), ("", 0))

    def test_dir_owned_by_other_user_is_refused(self):
        """所有者違いは締め直しようがないため、恒久的に拒否し続ける。root 権限
        なしで実際に別所有者のディレクトリを作ることはできないため、
        `os.geteuid` を偽装して再現する (`test_flock.py` と同じ手法)。"""
        marker_dir = self._marker_dir()
        os.makedirs(marker_dir, exist_ok=True)
        os.chmod(marker_dir, 0o700)

        with mock.patch("os.geteuid", return_value=os.geteuid() + 12345):
            data = self.assertNotBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))

        self.assertEqual(self.cursor_calls, [])
        self.assertEqual(self.codex_calls, [])
        self.assertIn("状態ディレクトリ", data.get("systemMessage", ""))

    def test_safe_dir_is_unaffected(self):
        """回帰: 通常時 (新規作成、自分所有の 0o700) は従来どおりプランレビューが走る。"""
        self.assertBlocked(self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS))
        self.assertEqual(len(self.cursor_calls), 1)
        self.assertEqual(len(self.codex_calls), 1)

    def test_disabled_switch_suppresses_notice_even_with_hostile_dir(self):
        """regression (マージ前レビューの指摘): 機能を無効化している利用者には、marker_dir
        が安全でなくても通知を出さない。`ensure_private_root` の検査を
        `review_enabled()` 等より前に置くと、この機能を使っていない利用者にまで
        毎回無関係な通知が出てしまう。"""
        marker_dir = self._marker_dir()
        os.makedirs(marker_dir)
        os.chmod(marker_dir, 0o777)

        os.environ["EXTERNAL_AI_PLAN_REVIEW"] = "0"
        output = self.exitplan(SESSION, PLAN, FINDINGS, FINDINGS)

        self.assertEqual(self.cursor_calls, [])
        self.assertEqual(self.codex_calls, [])
        self.assertEqual(output, "", "無効化中は無出力のはず (通知も出さない)")


if __name__ == "__main__":
    unittest.main()
