"""`EXTERNAL_AI_*` パーサと利用者向け通知の組み立て (0.6.0)。

3 hook が同じ解釈規則を共有するので、規則の網羅はここで押さえ、各 hook の tests は
「その hook で意図どおり効くか」だけを見る。
"""
import os
import unittest
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)

from _common import notify, settings

VAR = "EXTERNAL_AI_TEST_VALUE"


class SettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {})
        self._env.start()
        for key in [k for k in os.environ if k.startswith(settings.ENV_PREFIX)]:
            del os.environ[key]

    def tearDown(self) -> None:
        self._env.stop()

    def set(self, value: str) -> None:
        os.environ[VAR] = value


class TestFlag(SettingsTestCase):
    def test_unset_uses_default(self):
        self.assertTrue(settings.flag(VAR, default=True))
        self.assertFalse(settings.flag(VAR, default=False))

    def test_falsy_values(self):
        for value in ("0", "false", "FALSE", "off", "no", " no "):
            with self.subTest(value=value):
                self.set(value)
                self.assertFalse(settings.flag(VAR, default=True))

    def test_truthy_values(self):
        for value in ("1", "true", "ON", "yes"):
            with self.subTest(value=value):
                self.set(value)
                self.assertTrue(settings.flag(VAR, default=False))

    def test_unparsable_falls_back_to_default(self):
        """fail-open: タイプミスで機能が黙って止まったり勝手に走ったりしない。"""
        self.set("maybe")
        self.assertTrue(settings.flag(VAR, default=True))
        self.assertFalse(settings.flag(VAR, default=False))


class TestDuration(SettingsTestCase):
    def test_unset_uses_default(self):
        self.assertEqual(settings.duration(VAR, 300, 600), 300)

    def test_value_within_range(self):
        self.set("120")
        self.assertEqual(settings.duration(VAR, 300, 600), 120)

    def test_value_above_ceiling_is_clamped(self):
        """hooks.json の hook timeout は静的なので、超える値を許すと後始末に到達しない。"""
        self.set("99999")
        self.assertEqual(settings.duration(VAR, 300, 600), 600)

    def test_zero_and_negative_fall_back(self):
        """`0` を「無効化」と読ませない (無効化は on/off スイッチの仕事)。"""
        for value in ("0", "-1"):
            with self.subTest(value=value):
                self.set(value)
                self.assertEqual(settings.duration(VAR, 300, 600), 300)

    def test_non_numeric_falls_back(self):
        self.set("5 minutes")
        self.assertEqual(settings.duration(VAR, 300, 600), 300)

    def test_default_is_returned_unchanged_for_float(self):
        """テストが TIMEOUT_SEC を float に差し替えてもそのまま通ること。"""
        self.assertEqual(settings.duration(VAR, 1.0, 600), 1.0)


class TestCount(SettingsTestCase):
    def test_unset_uses_default(self):
        self.assertEqual(settings.count(VAR, 2), 2)

    def test_zero_is_kept(self):
        """`count` の 0 は「無効」として意味を持たせてよい (`EXTERNAL_AI_REVIEW_MAX=0`)。"""
        self.set("0")
        self.assertEqual(settings.count(VAR, 2), 0)

    def test_negative_becomes_zero(self):
        self.set("-3")
        self.assertEqual(settings.count(VAR, 2), 0)

    def test_non_numeric_falls_back(self):
        self.set("many")
        self.assertEqual(settings.count(VAR, 2), 2)

    def test_float_spellings_are_accepted_like_duration(self):
        """`int()` だけだと `1800.0` が既定 (= 0 = 無効) に落ち、抑制が黙って効かない。"""
        for value, expected in (("1800.0", 1800), ("6e2", 600), ("20.9", 20)):
            with self.subTest(value=value):
                self.set(value)
                self.assertEqual(settings.count(VAR, 2), expected)


class TestNames(SettingsTestCase):
    def test_unset_is_none(self):
        self.assertIsNone(settings.names(VAR))

    def test_split_strip_and_lowercase(self):
        self.set(" Cursor , CODEX ")
        self.assertEqual(settings.names(VAR), ["cursor", "codex"])

    def test_empty_items_are_dropped(self):
        self.set("cursor,,")
        self.assertEqual(settings.names(VAR), ["cursor"])

    def test_separators_only_is_none(self):
        self.set(",,")
        self.assertIsNone(settings.names(VAR))


class TestNotify(unittest.TestCase):
    def test_format_elapsed(self):
        self.assertEqual(notify.format_elapsed(0), "0秒")
        self.assertEqual(notify.format_elapsed(-5), "0秒")
        self.assertEqual(notify.format_elapsed(59.9), "59秒")
        self.assertEqual(notify.format_elapsed(60), "1分00秒")
        self.assertEqual(notify.format_elapsed(252), "4分12秒")
        self.assertEqual(notify.format_elapsed(3600), "60分00秒")

    def test_compose_prefixes_and_joins(self):
        self.assertEqual(notify.compose("hook", ["a", "b"]), "[hook] a\nb")

    def test_compose_drops_empty_lines(self):
        self.assertEqual(notify.compose("hook", ["", "a", ""]), "[hook] a")

    def test_compose_returns_none_when_nothing_to_say(self):
        self.assertIsNone(notify.compose("hook", []))
        self.assertIsNone(notify.compose("hook", ["", ""]))


if __name__ == "__main__":
    unittest.main()
