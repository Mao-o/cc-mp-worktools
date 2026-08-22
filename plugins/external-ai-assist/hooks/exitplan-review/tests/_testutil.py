"""exitplan-review テスト共通のパス設定とフィクスチャ。

post-implementation-review/tests/_testutil.py と同じ方式: TMPDIR を隔離し、
`__main__.py` を `__main__` 以外の名前で読み込んで main() を直接呼ぶ。
cursor / codex は起動せず、`review()` をモックするか PATH 先頭の偽 CLI に差し替える。
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent  # hooks/_common の解決用 (本番は __main__.py が載せる)
for _p in (_HOOKS_DIR, _PKG_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_ENTRY_PATH = _PKG_DIR / "__main__.py"

# 2026-08-20 の Stop hook 実出力相当 (zh5.1): 前置き 1 文 + フェンス付き sentinel
FENCED_CLEAN_WITH_PREAMBLE = "critical 指摘はない\n\n```\nREVIEW_CLEAN\n```\n"
FENCED_CLEAN = "```\nREVIEW_CLEAN\n```"
FINDINGS = (
    "1. **前提として不足している確認事項** — 認可境界が未定義\n"
    "2. **既存コードとの衝突候補** — services/auth.py に同名の関数がある"
)
PLAN = "## 目的\n\nログイン API を追加する\n\n## 手順\n\n1. ルータ追加\n2. テスト追加\n"


def load_entry():
    """`__main__.py` を `__main__` 以外の名前で読み込む (main() の自動実行を避ける)。"""
    spec = importlib.util.spec_from_file_location("exitplan_review_entry", _ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HookTestCase(unittest.TestCase):
    """TMPDIR を隔離し、entry module と両レビュアーのモックを用意する基底クラス。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.tmpdir = os.path.join(base, "tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"TMPDIR": self.tmpdir})
        self._env.start()
        os.environ.pop("EXTERNAL_AI_REVIEW_MAX", None)

        self.entry = load_entry()
        self.cursor = sys.modules["cursor"]
        self.codex = sys.modules["codex"]

        self.cursor_calls: list[str] = []
        self.codex_calls: list[str] = []
        self._patches = [
            mock.patch.object(self.cursor, "is_available", return_value=True),
            mock.patch.object(self.codex, "is_available", return_value=True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    # -- hook 起動 --------------------------------------------------------

    def run_hook(self, payload: dict) -> str:
        stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(
            sys, "stdout", out
        ), mock.patch.object(sys, "stderr", err):
            try:
                self.entry.main()
            except SystemExit:
                pass
        self.last_stderr = err.getvalue()
        return out.getvalue()

    def exitplan(
        self,
        session_id: str,
        plan: str,
        cursor_result: str | None,
        codex_result: str | None,
    ) -> str:
        """ExitPlanMode hook を 1 回起動する。両レビュアーの `review()` 戻り値を差し替える。"""
        cursor_calls: list[str] = []
        codex_calls: list[str] = []

        def fake_cursor(plan_text: str):
            cursor_calls.append(plan_text)
            return cursor_result

        def fake_codex(plan_text: str):
            codex_calls.append(plan_text)
            return codex_result

        with mock.patch.object(self.cursor, "review", side_effect=fake_cursor), mock.patch.object(
            self.codex, "review", side_effect=fake_codex
        ):
            output = self.run_hook(
                {
                    "session_id": session_id,
                    "tool_name": "ExitPlanMode",
                    "tool_input": {"plan": plan},
                    "cwd": self.tmpdir,
                }
            )
        self.cursor_calls = cursor_calls
        self.codex_calls = codex_calls
        return output

    # -- 状態の読み出し ----------------------------------------------------

    def marker(self, session_id: str) -> tuple[str, int]:
        """(hash, count)。マーカー未作成なら ("", 0)。"""
        path = os.path.join(self.tmpdir, "plan-review-markers", f"{session_id}.exitplan.marker")
        if not os.path.exists(path):
            return "", 0
        with open(path) as f:
            lines = [line.strip() for line in f.read().split("\n")]
        return lines[0], int(lines[1]) if len(lines) > 1 and lines[1] else 0

    def review_copy(self, session_id: str) -> str | None:
        path = os.path.join(self.tmpdir, f"plan-review-{session_id[:8]}.txt")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    # -- assertion ヘルパー ------------------------------------------------

    def assertBlocked(self, output: str) -> dict:
        self.assertTrue(output, "block の JSON が出力されていない")
        data = json.loads(output)
        self.assertEqual(data.get("decision"), "block")
        self.assertIn("## クロスレビュー結果 (ExitPlanMode)", data.get("reason", ""))
        return data

    def assertNotBlocked(self, output: str) -> None:
        self.assertEqual(output, "", "block してはいけない出力が出ている")
