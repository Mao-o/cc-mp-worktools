"""PreToolUse envelope fixture の shape 契約テスト。

Phase 0 実測で得た envelope 構造が維持されているかを確認する。
値は比較しない (Claude Code CLI バージョン依存)、必須キー存在のみ検査。
"""
from __future__ import annotations

import json
import unittest

from _testutil import FIXTURES

from core.output import LENIENT_MODES

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

# fixtures/envelopes/README.md:22 と core/output.py::LENIENT_MODES を突合する
# 既知 permission_mode の完全列挙。CLI 2.1.x 系の実測に基づく。
_KNOWN_PERMISSION_MODES = {
    "default",
    "plan",
    "acceptEdits",
    "auto",
    "dontAsk",
    "bypassPermissions",
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


class TestLenientModesSubset(unittest.TestCase):
    """LENIENT_MODES と fixtures/envelopes/README.md の列挙が乖離していないか。

    CLI 側が permission_mode の新しい値を追加したとき、docs と実動が乖離する前に
    気付けるようにする。本テストが red になったら:
      1. 実 envelope を採取して permission_mode の値を確認
         (``hooks/_debug/capture_envelope.py`` を作成して hooks.json 経由で取る)
      2. `core/output.py::LENIENT_MODES` と
         `tests/fixtures/envelopes/README.md:22` の列挙を同時更新
      3. `docs/DESIGN.md` の lenient 方針も更新
      4. `CLAUDE.md` の CLI 再実測 Runbook に実測日を追記
    """

    def test_lenient_modes_are_subset_of_known_permission_modes(self):
        unknown = LENIENT_MODES - _KNOWN_PERMISSION_MODES
        self.assertFalse(
            unknown,
            msg=(
                f"LENIENT_MODES has unknown values: {sorted(unknown)}. "
                "Update fixtures/envelopes/README.md, docs/DESIGN.md, and "
                "CLAUDE.md's CLI re-probe Runbook if CLI added a new mode."
            ),
        )

    def test_known_modes_contains_six_canonical_entries(self):
        # regression guard: fixtures README の列挙と対称に 6 値を固定
        self.assertEqual(len(_KNOWN_PERMISSION_MODES), 6)


if __name__ == "__main__":
    unittest.main()
