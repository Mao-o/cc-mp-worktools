"""PreToolUse envelope fixture の shape 契約テスト。

Phase 0 実測で得た envelope 構造が維持されているかを確認する。
値は比較しない (Claude Code CLI バージョン依存)、必須キー存在のみ検査。
"""
from __future__ import annotations

import json
import unittest

from _testutil import FIXTURES

ENVELOPES_DIR = FIXTURES / "envelopes"

# 共通キー (全 tool)
_COMMON_KEYS = {"hook_event_name", "tool_name", "tool_input", "cwd", "permission_mode"}

# tool_input の必須キー (tool 別)
_TOOL_INPUT_KEYS = {
    "read": {"file_path"},
    "bash": {"command"},
    "edit": {"file_path", "old_string", "new_string"},
    "write": {"file_path", "content"},
    "multiedit": {"file_path", "edits"},
}


class TestEnvelopeShapes(unittest.TestCase):
    def _load(self, name: str) -> dict:
        path = ENVELOPES_DIR / f"{name}.json"
        with path.open() as f:
            return json.load(f)

    def _assert_common(self, env: dict) -> None:
        for k in _COMMON_KEYS:
            self.assertIn(k, env, f"missing common key: {k}")
        self.assertEqual(env["hook_event_name"], "PreToolUse")
        self.assertIsInstance(env["tool_input"], dict)

    def test_read_envelope(self):
        env = self._load("read")
        self._assert_common(env)
        self.assertEqual(env["tool_name"], "Read")
        for k in _TOOL_INPUT_KEYS["read"]:
            self.assertIn(k, env["tool_input"])

    def test_bash_envelope(self):
        env = self._load("bash")
        self._assert_common(env)
        self.assertEqual(env["tool_name"], "Bash")
        for k in _TOOL_INPUT_KEYS["bash"]:
            self.assertIn(k, env["tool_input"])

    def test_edit_envelope(self):
        env = self._load("edit")
        self._assert_common(env)
        self.assertEqual(env["tool_name"], "Edit")
        for k in _TOOL_INPUT_KEYS["edit"]:
            self.assertIn(k, env["tool_input"])

    def test_write_envelope(self):
        env = self._load("write")
        self._assert_common(env)
        self.assertEqual(env["tool_name"], "Write")
        for k in _TOOL_INPUT_KEYS["write"]:
            self.assertIn(k, env["tool_input"])

    def test_multiedit_envelope(self):
        env = self._load("multiedit")
        self._assert_common(env)
        self.assertEqual(env["tool_name"], "MultiEdit")
        for k in _TOOL_INPUT_KEYS["multiedit"]:
            self.assertIn(k, env["tool_input"])
        self.assertIsInstance(env["tool_input"]["edits"], list)
        self.assertGreaterEqual(len(env["tool_input"]["edits"]), 1)
        first = env["tool_input"]["edits"][0]
        self.assertIn("old_string", first)
        self.assertIn("new_string", first)


if __name__ == "__main__":
    unittest.main()
