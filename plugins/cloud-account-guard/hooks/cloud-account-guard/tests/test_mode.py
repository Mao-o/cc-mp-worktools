"""`core/mode.py` (検証モードの解決) のテスト。

既定が `enforce` であること / 不正値を enforce に倒すこと (fail-closed) /
env が `"$mode"` より優先されることを固定する。
"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from core import mode  # noqa: E402


class TestFromEnv(unittest.TestCase):
    def test_unset_is_none(self):
        self.assertEqual(mode.from_env({}), (None, None))

    def test_valid_values(self):
        for value in mode.VALID_MODES:
            with self.subTest(value=value):
                self.assertEqual(
                    mode.from_env({mode.ENV_VAR: value}), (value, None)
                )

    def test_case_and_space_insensitive(self):
        self.assertEqual(
            mode.from_env({mode.ENV_VAR: "  WARN "}), (mode.WARN, None)
        )

    def test_empty_value_is_unset(self):
        """`VAR=` (シェルで空を渡す形) は「未設定」と同じ扱い。"""
        self.assertEqual(mode.from_env({mode.ENV_VAR: ""}), (None, None))
        self.assertEqual(mode.from_env({mode.ENV_VAR: "   "}), (None, None))

    def test_invalid_value_falls_back_to_enforce_with_note(self):
        found, note = mode.from_env({mode.ENV_VAR: "yes"})
        self.assertIsNone(found)
        self.assertIn("不正な値", note)
        self.assertIn(mode.ENV_VAR, note)


class TestFromAccounts(unittest.TestCase):
    def test_absent_key_is_none(self):
        self.assertEqual(mode.from_accounts({"github": "u"}), (None, None))

    def test_valid_value(self):
        self.assertEqual(
            mode.from_accounts({mode.MODE_KEY: "off"}), (mode.OFF, None)
        )

    def test_invalid_value_has_note(self):
        found, note = mode.from_accounts({mode.MODE_KEY: 1})
        self.assertIsNone(found)
        self.assertIn(mode.MODE_KEY, note)

    def test_non_dict_is_none(self):
        self.assertEqual(mode.from_accounts(["github"]), (None, None))


class TestEffective(unittest.TestCase):
    def test_default_is_enforce(self):
        self.assertEqual(mode.effective(None, None), mode.ENFORCE)

    def test_env_wins_over_file(self):
        self.assertEqual(mode.effective(mode.ENFORCE, mode.OFF), mode.ENFORCE)
        self.assertEqual(mode.effective(mode.OFF, mode.WARN), mode.OFF)

    def test_file_used_when_env_absent(self):
        self.assertEqual(mode.effective(None, mode.WARN), mode.WARN)


class TestReservedKeyDoesNotCollideWithServices(unittest.TestCase):
    def test_mode_key_is_not_a_service_key(self):
        from services import ALL as SERVICES

        self.assertNotIn(mode.MODE_KEY, [svc.ACCOUNT_KEY for svc in SERVICES])
        self.assertTrue(mode.MODE_KEY.startswith("$"))


if __name__ == "__main__":
    unittest.main()
