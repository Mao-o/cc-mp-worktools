"""__main__.py (PostToolUse hook エントリポイント) の挙動テスト。

sensitive-files-guardrail/hooks/check-sensitive-files/tests/test_main.py のパターン
(importlib.util.spec_from_file_location + stdin/stdout 差し替え) を踏襲する。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import source
import state

_ENTRY_PATH = Path(__file__).resolve().parent.parent / "__main__.py"

# BaseMainTest.setUp が isolation する plugin 環境変数 (P3-4)。これらを
# export した端末/CI では、isolation が漏れていると green の意味が変わる。
_ISOLATED_ENV_KEYS = (
    "FILE_SPLIT_ADVISOR_DISABLED",
    "FILE_SPLIT_ADVISOR_MAX_EMITS",
    "FILE_SPLIT_ADVISOR_CWD_ONLY",
    "FILE_SPLIT_ADVISOR_IGNORE",
    "FILE_SPLIT_ADVISOR_SCALE",
)


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
        # 実行機/CI がこれらを export していても Green の意味が変わらない
        # よう、テスト中は必ず未設定にする (P3-4)。個別テストは
        # mock.patch.dict(os.environ, {...}) で明示的に上書きできる
        # (mock.patch.dict はネストしても内側の変更だけを正しく巻き戻す)。
        self._env_isolation_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_isolation_patcher.start()
        self.addCleanup(self._env_isolation_patcher.stop)
        for key in _ISOLATED_ENV_KEYS:
            os.environ.pop(key, None)
        self._base_dir_patcher = mock.patch.object(
            state, "_base_dir", return_value=Path(self.tmp) / "_state"
        )
        self._base_dir_patcher.start()
        self.addCleanup(self._base_dir_patcher.stop)
        # 実行機の ~/.claude/file-split-advisor/ignore.local.txt に依存しない
        # よう、既定では存在しないパスを指すようにする (個別テストで上書き可能)。
        self._ignore_file_patcher = mock.patch.object(
            source, "_default_ignore_file", return_value=Path(self.tmp) / "_no_ignore_file"
        )
        self._ignore_file_patcher.start()
        self.addCleanup(self._ignore_file_patcher.stop)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _context(self, out: str) -> str:
        """emit された additionalContext を取り出す。無出力なら明示的に失敗する
        (``json.loads("")`` の decode error だと何が壊れたか読めないため)。"""
        self.assertNotEqual(out, "", "通知されるはずが無出力だった")
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

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


class TestSetUpIsolatesAmbientEnv(BaseMainTest):
    """P3-4 回帰: setUp 実行前から export されていた plugin 環境変数 5 つを
    ``BaseMainTest.setUp`` が確実に除去すること。これらを export した
    端末/CI では、isolation が漏れていると green の意味が変わってしまう。
    """

    _AMBIENT_KEYS = (
        "FILE_SPLIT_ADVISOR_DISABLED",
        "FILE_SPLIT_ADVISOR_MAX_EMITS",
        "FILE_SPLIT_ADVISOR_CWD_ONLY",
        "FILE_SPLIT_ADVISOR_IGNORE",
        "FILE_SPLIT_ADVISOR_SCALE",
    )

    def setUp(self):
        # BaseMainTest.setUp による isolation より "前" に、既に export
        # されていた状態を模す。
        self._ambient_patcher = mock.patch.dict(
            os.environ, {key: "ambient-value" for key in self._AMBIENT_KEYS}
        )
        self._ambient_patcher.start()
        self.addCleanup(self._ambient_patcher.stop)
        super().setUp()

    def test_ambient_env_vars_are_removed(self):
        for key in self._AMBIENT_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, os.environ)


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
        text = self._context(out)
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
        self.assertIn("判定: strong", self._context(out))


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


class TestIgnoreGlobEnvVar(BaseMainTest):
    """FILE_SPLIT_ADVISOR_IGNORE / ignore.local.txt による除外設定。"""

    def test_ignored_glob_suppresses_output(self):
        path = self._write("test_helpers.py", _python_lines(900))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_IGNORE": "test_*.py"}):
            out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_non_matching_glob_still_judged(self):
        path = self._write("checkout_flow.py", _python_lines(900))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_IGNORE": "test_*.py"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: strong", self._context(out))

    def test_ignore_local_txt_suppresses_output(self):
        path = self._write("legacy_migration.py", _python_lines(900))
        ignore_file = Path(self.tmp) / "ignore.local.txt"
        ignore_file.write_text("legacy_*.py\n")
        with mock.patch.object(source, "_default_ignore_file", return_value=ignore_file):
            out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_env_and_file_patterns_are_merged(self):
        # env var の glob だけでファイルが一致しなくても、ignore.local.txt 側の
        # glob と併用できることを確認する。
        path = self._write("legacy_migration.py", _python_lines(900))
        ignore_file = Path(self.tmp) / "ignore.local.txt"
        ignore_file.write_text("legacy_*.py\n")
        with mock.patch.object(source, "_default_ignore_file", return_value=ignore_file):
            with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_IGNORE": "unrelated_*.py"}):
                out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")


class TestIgnoreFileInvalidUtf8E2E(unittest.TestCase):
    """P2-2 回帰: 非UTF-8の ignore.local.txt で plugin 全体が無言停止しないこと。

    ``_run_main`` (in-process, ``entry.main()`` を直接呼ぶ) は
    ``if __name__ == "__main__":`` の fail-open ラッパー (末尾の
    ``except Exception`` で exit 0 にしつつ stderr に fatal を出す経路) を
    経由しない (``importlib`` でロードしたモジュールの ``__name__`` は
    ``"__main__"`` にならないため)。レビューが実際に踏んだ経路を再現する
    には実プロセスとして起動する必要があるため、ここだけ subprocess を使う。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_invalid_utf8_ignore_file_does_not_silence_hook(self):
        home = Path(self.tmp) / "home"
        ignore_dir = home / ".claude" / "file-split-advisor"
        ignore_dir.mkdir(parents=True)
        (ignore_dir / "ignore.local.txt").write_bytes(b"\xff\xfe*.min.py\n")

        project = Path(self.tmp) / "project"
        project.mkdir()
        target = project / "checkout_flow.py"
        target.write_text(_python_lines(900))  # strong tier, 初回 Write で必ず emit

        envelope = json.dumps(
            {
                "session_id": "sess-e2e-p2-2",
                "cwd": str(project),
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            }
        )
        env = dict(os.environ)
        env["HOME"] = str(home)
        for key in (
            "FILE_SPLIT_ADVISOR_DISABLED",
            "FILE_SPLIT_ADVISOR_IGNORE",
            "FILE_SPLIT_ADVISOR_SCALE",
            "FILE_SPLIT_ADVISOR_CWD_ONLY",
        ):
            env.pop(key, None)

        result = subprocess.run(
            [sys.executable, str(_ENTRY_PATH)],
            input=envelope,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        # HOME override が効いていることの前提チェック: これが崩れると
        # Path.home() が実ホームを指し、ignore.local.txt が「存在しない」
        # 扱いになって修正前でも本テストが (誤った理由で) 通ってしまう。
        self.assertEqual(
            result.returncode, 0, f"stderr={result.stderr!r}"
        )
        self.assertNotIn("fatal", result.stderr, f"stderr={result.stderr!r}")
        self.assertNotEqual(result.stdout, "", "無出力 (fail-silent) だった")
        payload = json.loads(result.stdout)
        self.assertIn(
            "静的解析メモ",
            payload["hookSpecificOutput"]["additionalContext"],
        )


class TestScaleEnvVar(BaseMainTest):
    """FILE_SPLIT_ADVISOR_SCALE による全閾値の倍率調整。"""

    def test_scale_up_suppresses_output_below_scaled_threshold(self):
        # checkout_flow.py @ 550 行は既定 (scale=1.0) では warn (500 以上) だが、
        # scale=2.0 なら実効閾値が note=300/review=600 になり note tier (シグナル
        # 0 個) に留まり emit しない。
        path = self._write("checkout_flow.py", _python_lines(550))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "2.0"}):
            out, _ = _run_main(self._envelope(path))
        self.assertEqual(out, "")

    def test_scale_down_emits_earlier(self):
        # scale=0.5 なら warn 閾値が 250 相当になり、260 行で warn tier に達し
        # (signal 数によらず emit) 通知される。
        path = self._write("checkout_flow.py", _python_lines(260))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "0.5"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: warn", self._context(out))

    def test_invalid_scale_falls_back_to_default(self):
        path = self._write("checkout_flow.py", _python_lines(550))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "not-a-number"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: warn", self._context(out))

    def test_non_positive_scale_falls_back_to_default(self):
        path = self._write("checkout_flow.py", _python_lines(550))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "0"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: warn", self._context(out))

    def test_nan_scale_falls_back_to_default(self):
        # P2-3: float("nan") は変換に成功し `<= 0` も False を返すため、
        # 修正前は nan がそのまま実効閾値の乗数になり、line_count >= nan が
        # 常に False になって全ファイルが tier=ok (無言) に落ちていた。
        path = self._write("checkout_flow.py", _python_lines(550))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "nan"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: warn", self._context(out))

    def test_inf_scale_falls_back_to_default(self):
        path = self._write("checkout_flow.py", _python_lines(550))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "inf"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: warn", self._context(out))

    def test_overflow_scale_falls_back_to_default(self):
        # float("1e400") は OverflowError にならず inf になる。
        path = self._write("checkout_flow.py", _python_lines(550))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "1e400"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: warn", self._context(out))

    def test_huge_finite_scale_falls_back_to_default(self):
        # P2-1: "1e308" は math.isfinite() を通過する (inf ではない) ため、
        # 修正前は nan/inf フォールバックをすり抜けていた。言語/role/宣言的
        # 緩和の係数と掛け合わさると実効閾値が inf に飽和し、550 行のような
        # 通常サイズのファイルでも tier=ok (無言) に留まり advisor が
        # 無効化されていた。
        path = self._write("checkout_flow.py", _python_lines(550))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "1e308"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: warn", self._context(out))

    def test_scale_note_shown_in_memo_when_scale_applied(self):
        # P2-1 回帰 (レビュー PROBE 3 の再現): SCALE=2.0 のとき、目安に出る
        # 倍率の根拠 (全体 2.0倍) が明示され、printed 係数だけから実効閾値
        # (warn=1000) を導出できることを固定する。
        path = self._write("checkout_flow.py", _python_lines(1200))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_SCALE": "2.0"}):
            out, _ = _run_main(self._envelope(path))
        text = self._context(out)
        self.assertIn("判定: warn", text)
        self.assertIn("warn=1000", text)
        self.assertIn("(全体 2.0倍)", text)


class TestTempDirSkip(BaseMainTest):
    """一時領域配下のファイルは (cwd が一時領域外なら) 常時 skip する。"""

    def test_temp_scratch_file_is_silent_when_cwd_is_a_real_project(self):
        # self.tmp は tempfile.mkdtemp() の実体で OS の一時領域配下にあるため、
        # cwd だけを実プロジェクト風の非一時パスに差し替えれば「Claude が
        # scratchpad に書いた一時ファイル」を再現できる。
        path = self._write("escapes_big.py", _python_lines(900))
        envelope = self._envelope(path)
        envelope["cwd"] = "/Users/example/project"
        out, _ = _run_main(envelope)
        self.assertEqual(out, "")

    def test_temp_file_still_judged_when_cwd_is_also_under_temp(self):
        # 既存フィクスチャ (cwd == self.tmp、実体は OS の一時領域) の回帰確認:
        # session 全体がその場限りの一時プロジェクトであるケースは skip しない。
        path = self._write("checkout_flow.py", _python_lines(900))
        out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: strong", self._context(out))


class TestCwdOnlyOptIn(BaseMainTest):
    """FILE_SPLIT_ADVISOR_CWD_ONLY=1 の opt-in cwd 外 skip。

    常時 on の temp-dir skip と条件が重ならないよう (self.tmp 自体が OS の
    一時領域配下にあるため)、_temp_dir_roots を空にして温存領域スキップを
    無効化し、CWD_ONLY の挙動だけを分離して確認する。
    """

    def setUp(self):
        super().setUp()
        self._no_temp_roots_patcher = mock.patch.object(
            source, "_temp_dir_roots", return_value=()
        )
        self._no_temp_roots_patcher.start()
        self.addCleanup(self._no_temp_roots_patcher.stop)

    def test_outside_cwd_judged_by_default(self):
        # 既定 off: --add-dir 運用を壊さないよう、cwd 外でも通常どおり判定する。
        path = self._write("checkout_flow.py", _python_lines(900))
        envelope = self._envelope(path)
        envelope["cwd"] = "/Users/example/other-project"
        out, _ = _run_main(envelope)
        self.assertIn("判定: strong", self._context(out))

    def test_outside_cwd_silent_when_opted_in(self):
        path = self._write("checkout_flow.py", _python_lines(900))
        envelope = self._envelope(path)
        envelope["cwd"] = "/Users/example/other-project"
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_CWD_ONLY": "1"}):
            out, _ = _run_main(envelope)
        self.assertEqual(out, "")

    def test_inside_cwd_still_judged_when_opted_in(self):
        path = self._write("checkout_flow.py", _python_lines(900))
        with mock.patch.dict(os.environ, {"FILE_SPLIT_ADVISOR_CWD_ONLY": "1"}):
            out, _ = _run_main(self._envelope(path))
        self.assertIn("判定: strong", self._context(out))


class TestDebounce(BaseMainTest):
    def test_same_tier_suppressed_worse_tier_reemits(self):
        # utils.py = vague_filename シグナル 1 個 → review tier でも emit する
        review_path = self._write("utils.py", _python_lines(350))

        out1, _ = _run_main(self._envelope(review_path, session_id="sess-x"))
        self.assertNotEqual(out1, "")
        self.assertIn("判定: review", self._context(out1))

        # 2 回目: 同一 tier (review) の再 Edit → 抑制される
        out2, _ = _run_main(self._envelope(review_path, session_id="sess-x"))
        self.assertEqual(out2, "")

        # 3 回目: warn tier まで悪化 (350 -> 550 行) → 再警告される
        warn_path = self._write("utils.py", _python_lines(550))
        out3, _ = _run_main(self._envelope(warn_path, session_id="sess-x"))
        self.assertNotEqual(out3, "")
        self.assertIn("判定: warn", self._context(out3))


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
        self.assertIn("判定: strong", self._context(out))

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
        self.assertIn("判定: strong", self._context(out2))

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
        self.assertIn("判定: strong", self._context(out3))

    def test_shrink_into_ok_tier_refreshes_the_recorded_line_count(self):
        """ok tier でも行数記録を更新しないと、縮んだ後の再成長を誤って抑制する。"""
        path = self._write("checkout_flow.py", _python_lines(900))
        out1, _ = _run_main(
            self._envelope(path, session_id="sess-s", old_string="teh", new_string="the")
        )
        self.assertEqual(out1, "")  # 900 行が記録される

        # 100 行まで縮む (tier=ok)。ここで記録が 900 のままだと次が抑制される。
        self._write("checkout_flow.py", _python_lines(100))
        out2, _ = _run_main(self._envelope(path, tool_name="Write", session_id="sess-s"))
        self.assertEqual(out2, "")

        # 600 行に成長 → warn。100 行との比較なので通知されるべき。
        self._write("checkout_flow.py", _python_lines(600))
        out3, _ = _run_main(self._envelope(path, tool_name="Write", session_id="sess-s"))
        self.assertIn("判定: warn", self._context(out3))

    def test_appending_a_line_at_eof_emits(self):
        # 末尾に改行なしで 1 行足す編集。改行数の差は 0 だが行数は 1 増える。
        body = _python_lines(900).rstrip("\n")
        path = self._write("checkout_flow.py", body + "\nextra = 1")
        out, _ = _run_main(
            self._envelope(
                path,
                old_string="y899 = 899\n",
                new_string="y899 = 899\nextra = 1",
            )
        )
        self.assertIn("判定: strong", self._context(out))

    def test_adding_a_trailing_newline_at_eof_is_silent(self):
        # 末尾に改行だけを足す整形。改行数は 1 増えるが行数は変わらない。
        path = self._write("checkout_flow.py", _python_lines(900))
        out, _ = _run_main(
            self._envelope(path, old_string="y899 = 899", new_string="y899 = 899\n")
        )
        self.assertEqual(out, "")

    def test_write_without_prior_observation_emits(self):
        path = self._write("checkout_flow.py", _python_lines(900))
        out, _ = _run_main(self._envelope(path, tool_name="Write", session_id="sess-new"))
        self.assertIn("判定: strong", self._context(out))


if __name__ == "__main__":
    unittest.main()
