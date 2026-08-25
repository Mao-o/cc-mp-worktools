"""`EXTERNAL_AI_EXPLORE_PARALLEL=0` で並走を止められること。

0.5.0 まではスイッチが皆無で、`cursor` を PATH から外す以外に止める手段が無かった。
他 2 hook (`EXTERNAL_AI_REVIEW_MAX` / `EXTERNAL_AI_POST_REVIEW`) とは独立に効く。
"""
import os
import unittest

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import HookTestCase, explore_payload


class TestExploreParallelSwitch(HookTestCase):
    def test_unset_launches_cursor_as_before(self):
        """回帰: 未設定なら 0.5.0 と同じく並走を起動する。"""
        argv_file = self.fake_cursor()
        self.run_hook("pre", explore_payload("tu-on"))
        self.reap_cursor("tu-on")
        self.assertTrue(os.path.exists(argv_file), "cursor が起動していない")

    def test_zero_skips_launch(self):
        argv_file = self.fake_cursor()
        os.environ["EXTERNAL_AI_EXPLORE_PARALLEL"] = "0"

        self.assertEqual(self.run_hook("pre", explore_payload("tu-off")), "")
        self.assertFalse(os.path.exists(argv_file), "無効化したのに cursor が起動した")
        _, pid_file = self.state.paths(self.cursor.NAME, "tu-off")
        self.assertFalse(pid_file.is_file(), "起動していないのに pid ファイルがある")
        self.assertIn("EXTERNAL_AI_EXPLORE_PARALLEL=0", self.last_stderr)

    def test_falsy_spellings(self):
        for value in ("0", "false", "off", "no", "OFF"):
            with self.subTest(value=value):
                os.environ["EXTERNAL_AI_EXPLORE_PARALLEL"] = value
                self.assertFalse(self.entry.enabled())

    def test_unparsable_value_keeps_default(self):
        """fail-open: 解釈できない値で並走が黙って止まらない。"""
        os.environ["EXTERNAL_AI_EXPLORE_PARALLEL"] = "sometimes"
        self.assertTrue(self.entry.enabled())

    def test_post_still_reaps_when_disabled_midway(self):
        """pre で起動済みのアナライザは、post を止めずに必ず回収する。

        post も止めると、直前のターンで起動した cursor と pid / 結果ファイルが
        孤児になる (無効化した瞬間だけ発生し、気付きにくい)。
        """
        self.fake_cursor("SEMANTIC-RESULT\n")
        self.run_hook("pre", explore_payload("tu-mid"))
        self.reap_cursor("tu-mid")

        os.environ["EXTERNAL_AI_EXPLORE_PARALLEL"] = "0"
        output = self.run_hook("post", explore_payload("tu-mid"))

        self.assertIn("SEMANTIC-RESULT", output, "起動済みの結果が捨てられている")
        result_file, pid_file = self.state.paths(self.cursor.NAME, "tu-mid")
        self.assertFalse(result_file.is_file(), "結果ファイルが掃除されていない")
        self.assertFalse(pid_file.is_file(), "pid ファイルが掃除されていない")

    def test_post_is_noop_when_nothing_was_launched(self):
        """無効化して pre を通していないターンでは post も何も出さない。"""
        self.fake_cursor()
        os.environ["EXTERNAL_AI_EXPLORE_PARALLEL"] = "0"
        self.run_hook("pre", explore_payload("tu-none"))
        self.assertEqual(self.run_hook("post", explore_payload("tu-none")), "")


if __name__ == "__main__":
    unittest.main()
