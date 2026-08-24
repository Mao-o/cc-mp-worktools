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

# tool_input の必須キー (tool 別)。MultiEdit は CLI 非搭載のため 0.6.0 で除外。
_TOOL_INPUT_KEYS = {
    "read": {"file_path"},
    "bash": {"command"},
    "edit": {"file_path", "old_string", "new_string"},
    "write": {"file_path", "content"},
}

# fixtures/envelopes/README.md の permission_mode 項と core/output.py::LENIENT_MODES
# を突合する
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



class TestLenientModesSubset(unittest.TestCase):
    """``LENIENT_MODES`` が ``_KNOWN_PERMISSION_MODES`` の subset である回帰ガード。

    **本テストは CLI の permission_mode 追加を検出しない。** 見ているのは
    ``LENIENT_MODES - _KNOWN_PERMISSION_MODES`` という、リポジトリ内の静的な定数
    どうしの包含関係だけで、実際に捕捉した envelope は一切参照しない。したがって
    CLI が新しい ``permission_mode`` を返し始めても green のままで、シグナルには
    ならない。CLI 側の変化を知るには、``docs/MAINTAINING.md`` の
    「CLI バージョンアップ時の再実測手順 (Runbook)」に従って probe で採取した値を
    ``_KNOWN_PERMISSION_MODES`` と**人手で**突合すること。

    実際に検出できるのは repo 内の自己矛盾 2 種:
      - ``test_lenient_modes_are_subset_of_known_permission_modes``:
        既知 mode の列挙を更新しないまま ``LENIENT_MODES`` に未登録の値を足した
        とき red
      - ``test_known_modes_contains_six_canonical_entries``:
        ``_KNOWN_PERMISSION_MODES`` の件数を変えたとき red。新 mode を登録した
        **後**に鳴る「更新漏れ検知」であって、CLI 変化の検知ではない

    どちらかが red になったら次を同時に更新する:
      1. ``core/output.py::LENIENT_MODES`` と本ファイルの
         ``_KNOWN_PERMISSION_MODES`` (件数 assert とテスト名にも件数が入る)
      2. ``tests/fixtures/envelopes/README.md`` (``permission_mode`` 項) の列挙
      3. ``docs/DESIGN.md`` の lenient 方針
      4. ``docs/MAINTAINING.md`` の CLI 再実測 Runbook に実測日と CLI version を追記
    """

    def test_lenient_modes_are_subset_of_known_permission_modes(self):
        unknown = LENIENT_MODES - _KNOWN_PERMISSION_MODES
        self.assertFalse(
            unknown,
            msg=(
                f"LENIENT_MODES has unknown values: {sorted(unknown)}. "
                "Update fixtures/envelopes/README.md, docs/DESIGN.md, and "
                "docs/MAINTAINING.md's CLI re-probe Runbook if CLI added a new mode."
            ),
        )

    def test_known_modes_contains_six_canonical_entries(self):
        # regression guard: fixtures README の列挙と対称に 6 値を固定
        self.assertEqual(len(_KNOWN_PERMISSION_MODES), 6)


if __name__ == "__main__":
    unittest.main()
