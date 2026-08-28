"""__main__.py (PostToolUse hook エントリポイント) の挙動テスト。

sensitive-files-guardrail/hooks/check-sensitive-files/tests/test_main.py のパターン
(importlib.util.spec_from_file_location + stdin/stdout 差し替え) を踏襲する。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import state

_ENTRY_PATH = Path(__file__).resolve().parent.parent / "__main__.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("file_split_advisor_entry", _ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main_raw(stdin_text: str) -> tuple[str, str]:
    entry = _load_entry()
    old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
    try:
        sys.stdin = io.StringIO(stdin_text)
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        entry.main()
        return sys.stdout.getvalue(), sys.stderr.getvalue()
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def _run_main(envelope: dict) -> tuple[str, str]:
    return _run_main_raw(json.dumps(envelope))


def _python_lines(n: int) -> str:
    """control_flow_density がおよそ 10% (宣言的緩和も高密度シグナルも発火しない
    範囲) になるよう if 文を混ぜた python ソースを生成する。"""
    lines = []
    for i in range(n):
        if i % 10 == 0:
            lines.append(f"if x == {i}:")
        else:
            lines.append(f"    y{i} = {i}")
    return "\n".join(lines) + "\n"


class BaseMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._base_dir_patcher = mock.patch.object(
            state, "_base_dir", return_value=Path(self.tmp) / "_state"
        )
        self._base_dir_patcher.start()
        self.addCleanup(self._base_dir_patcher.stop)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: str) -> Path:
        path = Path(self.tmp) / name
        path.write_text(content)
        return path

    def _envelope(
        self,
        file_path: Path,
        tool_name: str = "Edit",
        session_id: str = "sess-1",
        **tool_input_extra,
    ) -> dict:
        tool_input = {"file_path": str(file_path)}
        tool_input.update(tool_input_extra)
        return {
            "session_id": session_id,
            "cwd": self.tmp,
            "tool_name": tool_name,
            "tool_input": tool_input,
        }


class TestBelowThreshold(BaseMainTest):
    def test_small_file_no_output(self):
        path = self._write("small.py", _python_lines(50))
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")


class TestAboveThreshold(BaseMainTest):
    def test_review_tier_with_signal_emits_additional_context(self):
        # python 係数 1.0, control_flow_density ~10% (緩和・高密度シグナルなし)
        # → review しきい値 = 300。ファイル名 utils.py で vague_filename シグナル
        # が 1 個立つため review tier でも emit する。
        path = self._write("utils.py", _python_lines(350))
        out, _ = _run_main(self._envelope(path))
        self.assertNotEqual(out, "")
        payload = json.loads(out)
        text = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("file-split-advisor", text)
        self.assertIn("判定: review", text)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_review_tier_without_signal_is_silent(self):
        # 同じ 350 行でもシグナルが立たないファイル名なら通知しない (0.2.0)。
        path = self._write("checkout_flow.py", _python_lines(350))
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_warn_tier_emits_without_signal(self):
        path = self._write("checkout_flow.py", _python_lines(550))
        out, _ = _run_main(self._envelope(path))
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("判定: warn", text)


class TestNonCodeFiles(BaseMainTest):
    """0.2.0: 拡張子 allowlist に無いファイルは判定対象外。"""

    def test_markdown_not_judged_even_if_huge(self):
        path = self._write("CHANGELOG.md", _python_lines(2000))
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_json_not_judged_even_if_huge(self):
        path = self._write("translations.json", _python_lines(2000))
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_extensionless_script_not_judged(self):
        path = self._write("deploy", "#!/usr/bin/env python3\n" + _python_lines(2000))
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_newly_allowlisted_extension_is_judged(self):
        # 0.2.0 で allowlist に追加した拡張子は従来どおり判定される。
        path = self._write("handlers.sh", _python_lines(900))
        out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: strong", json.loads(out)["hookSpecificOutput"]["additionalContext"])


class TestSkipPatterns(BaseMainTest):
    def test_lockfile_skipped_even_if_huge(self):
        path = self._write("package-lock.json", _python_lines(2000))
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_generated_marker_skipped(self):
        content = "# Code generated by protoc. DO NOT EDIT.\n" + _python_lines(600)
        path = self._write("big_generated.py", content)
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_generated_path_pattern_skipped(self):
        path = self._write("foo_pb2.py", _python_lines(2000))
        out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")


class TestFailOpen(BaseMainTest):
    def test_missing_file_path_noop(self):
        envelope = {
            "session_id": "sess-1",
            "cwd": self.tmp,
            "tool_name": "Edit",
            "tool_input": {},
        }
        out, _ = _run_main(envelope)
        self.assertEqual(out, "")

    def test_nonexistent_file_noop(self):
        envelope = self._envelope(Path(self.tmp) / "does-not-exist.py")
        out, _ = _run_main(envelope)
        self.assertEqual(out, "")

    def test_invalid_json_stdin_no_exception(self):
        out, err = _run_main_raw("not valid json{")
        self.assertEqual(out, "")

    def test_empty_stdin_no_exception(self):
        out, err = _run_main_raw("")
        self.assertEqual(out, "")

    def test_non_write_edit_tool_name_noop(self):
        path = self._write("big.py", _python_lines(250))
        envelope = self._envelope(path, tool_name="Read")
        out, _ = _run_main(envelope)
        self.assertEqual(out, "")


class TestDisableEnvVar(BaseMainTest):
    def test_disabled_env_var_suppresses_output(self):
        path = self._write("big.py", _python_lines(250))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_DISABLED": "1"}):
            out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")


class TestDebounce(BaseMainTest):
    def test_same_tier_suppressed_worse_tier_reemits(self):
        # utils.py = vague_filename シグナル 1 個 → review tier でも emit する
        review_path = self._write("utils.py", _python_lines(350))

        out1, _ = _run_main(self._envelope(review_path, session_id="sess-x"))
        self.assertNotEqual(out1, "")
        self.assertIn("判定: review", json.loads(out1)["hookSpecificOutput"]["additionalContext"])

        # 2 回目: 同一 tier (review) の再 Edit → 抑制される
        out2, _ = _run_main(self._envelope(review_path, session_id="sess-x"))
        self.assertEqual(out2, "")

        # 3 回目: warn tier まで悪化 (350 -> 550 行) → 再警告される
        warn_path = self._write("utils.py", _python_lines(550))
        out3, _ = _run_main(self._envelope(warn_path, session_id="sess-x"))
        self.assertNotEqual(out3, "")
        self.assertIn("判定: warn", json.loads(out3)["hookSpecificOutput"]["additionalContext"])


class TestGrowthGate(BaseMainTest):
    """0.2.0: ファイルを大きくしない編集では通知しない。"""

    def test_typo_edit_on_huge_file_is_silent(self):
        path = self._write("checkout_flow.py", _python_lines(900))  # strong tier
        out, _ = _run_main(
            self._envelope(path, old_string="teh value", new_string="the value")
        )
        self.assertEqual(out, "")

    def test_shrinking_edit_is_silent(self):
        path = self._write("checkout_flow.py", _python_lines(900))
        out, _ = _run_main(
            self._envelope(path, old_string="a\nb\nc", new_string="abc")
        )
        self.assertEqual(out, "")

    def test_growing_edit_emits(self):
        path = self._write("checkout_flow.py", _python_lines(900))
        out, _ = _run_main(
            self._envelope(path, old_string="abc", new_string="a\nb\nc")
        )
        self.assertIn("判定: strong", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_suppressed_edit_does_not_consume_the_tier_high_water_mark(self):
        """抑制した編集で tier を進めてしまうと、その後の本当の成長が恒久的に
        抑制される。行数だけを記録し tier は据え置くことをここで固定する。"""
        path = self._write("checkout_flow.py", _python_lines(900))
        out1, _ = _run_main(
            self._envelope(path, session_id="sess-g", old_string="teh", new_string="the")
        )
        self.assertEqual(out1, "")

        out2, _ = _run_main(
            self._envelope(path, session_id="sess-g", old_string="x", new_string="x\ny")
        )
        self.assertIn("判定: strong", json.loads(out2)["hookSpecificOutput"]["additionalContext"])

    def test_write_shrinking_below_last_seen_line_count_is_silent(self):
        # 1) typo Edit で行数だけ記録させる (tier は据え置かれる)
        path = self._write("checkout_flow.py", _python_lines(900))
        out1, _ = _run_main(
            self._envelope(path, session_id="sess-w", old_string="teh", new_string="the")
        )
        self.assertEqual(out1, "")

        # 2) Write で縮む → 直近 900 行との比較で抑制 (tier は未通知のままなので
        #    tier 判定では抑制されない = 行数比較の効果を分離できる)
        self._write("checkout_flow.py", _python_lines(850))
        out2, _ = _run_main(self._envelope(path, tool_name="Write", session_id="sess-w"))
        self.assertEqual(out2, "")

        # 3) Write で伸びる → 通知される
        self._write("checkout_flow.py", _python_lines(1000))
        out3, _ = _run_main(self._envelope(path, tool_name="Write", session_id="sess-w"))
        self.assertIn("判定: strong", json.loads(out3)["hookSpecificOutput"]["additionalContext"])

    def test_write_without_prior_observation_emits(self):
        path = self._write("checkout_flow.py", _python_lines(900))
        out, _ = _run_main(self._envelope(path, tool_name="Write", session_id="sess-new"))
        self.assertIn("判定: strong", json.loads(out)["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
