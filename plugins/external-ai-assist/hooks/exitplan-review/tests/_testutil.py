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

from _common import settings  # noqa: E402  (sys.path 挿入後に import する)

_ENTRY_PATH = _PKG_DIR / "__main__.py"


def clear_plugin_env(keep: dict | None = None) -> None:
    """開発者 shell の `EXTERNAL_AI_*` を外す (`keep` に挙げたものだけ残す)。

    「未設定時は従来どおり」の回帰テストは、開発者が shell で
    `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` 等を export していると嘘になる。個別に列挙する
    方式だと変数が増えるたびに漏れるので接頭辞で一掃する。`mock.patch.dict` は stop 時に
    dict の中身を丸ごと元に戻すので、start した後に消したキーも自動で復元される。
    """
    keep = keep or {}
    for key in [k for k in os.environ if k.startswith(settings.ENV_PREFIX)]:
        if key not in keep:
            del os.environ[key]

# 2026-08-20 の Stop hook 実出力相当: 前置き 1 文 + フェンス付き sentinel
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
        clear_plugin_env()

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

    def _marker_path(self, session_id: str) -> str:
        return os.path.join(self.tmpdir, "plan-review-markers", f"{session_id}.exitplan.marker")

    def marker_raw(self, session_id: str) -> dict:
        """マーカー本文 (JSON) をそのまま dict で返す。無ければ空マーカー相当。"""
        path = self._marker_path(session_id)
        if not os.path.exists(path):
            return {"v": 1, "last": "", "plans": {}}
        with open(path) as f:
            return json.load(f)

    def marker(self, session_id: str) -> tuple[str, int]:
        """(last, 全プラン合算 count)。マーカー未作成なら ("", 0)。

        0.7.0 でマーカーが JSON (プラン単位の {hash: count}) になった。
        「特定 1 プランの count」を見たいテストは
        `marker_count_for()` を使うこと。合算は「何も予約が残っていない」
        ことを確認する用途 (clean/失敗後の後始末確認) に使う。
        """
        data = self.marker_raw(session_id)
        plans = data.get("plans", {})
        total = sum(entry.get("count", 0) for entry in plans.values())
        return data.get("last", ""), total

    def marker_count_for(self, session_id: str, plan_text: str) -> int:
        """特定のプラン本文 (hash 化前のテキスト) の count。記録が無ければ 0。"""
        data = self.marker_raw(session_id)
        h = self.entry.plan_hash(plan_text.strip())
        return data.get("plans", {}).get(h, {}).get("count", 0)

    def review_copy(self, session_id: str) -> str | None:
        path = os.path.join(self.tmpdir, f"plan-review-{session_id[:8]}.txt")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    # -- assertion ヘルパー ------------------------------------------------

    def assertBlocked(self, output: str) -> dict:
        """既定 (`MODE=block`) の差し戻し出力を検証する。

        0.8.0 で top-level `decision: "block"` から
        `hookSpecificOutput.permissionDecision: "deny"` + `permissionDecisionReason` へ
        移行した (公式 docs: PreToolUse の top-level decision/reason は deprecated、
        "block" -> "deny" のマッピングが明記されている)。内容だけを見たい既存テストの
        ために、`permissionDecisionReason` を `data["reason"]` のエイリアスとしても
        返す (実際の wire format は `hookSpecificOutput` 側であり、この検証はここで
        一度だけ行う)。
        """
        self.assertTrue(output, "block の JSON が出力されていない")
        data = json.loads(output)
        self.assertNotIn(
            "decision", data, "廃止済みの top-level decision を使っている (deprecated)"
        )
        specific = data.get("hookSpecificOutput", {})
        self.assertEqual(specific.get("hookEventName"), "PreToolUse")
        self.assertEqual(specific.get("permissionDecision"), "deny")
        reason = specific.get("permissionDecisionReason", "")
        self.assertIn("## クロスレビュー結果 (ExitPlanMode)", reason)
        data["reason"] = reason
        return data

    def assertNotBlocked(self, output: str) -> dict:
        """block も所見注入もしていないこと。返り値は出力 JSON (無出力なら {})。

        0.6.0 から、ブロックしないターンでも所要時間と結果の要約を `systemMessage`
        だけの JSON で出す。「出力が空」を非 block の判定基準にはできない。
        """
        if not output:
            return {}
        data = json.loads(output)
        self.assertNotIn("decision", data, "block してはいけない出力に decision がある")
        self.assertNotIn(
            "hookSpecificOutput", data, "block してはいけない出力に所見注入がある"
        )
        return data
