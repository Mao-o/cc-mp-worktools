"""テスト共通のパス設定とフィクスチャ。

CI は `python3 -m unittest discover tests` をパッケージ root で回すため、
import は post-implementation-review/ 直下を sys.path に載せて解決する。
`hooks/_common` の解決用に hooks/ も載せる (本番は `__main__.py` が同じ挿入を行う)。
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

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
for _p in (_HOOKS_DIR, _PKG_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_ENTRY_PATH = _PKG_DIR / "__main__.py"


def load_entry():
    """`__main__.py` を `__main__` 以外の名前で読み込む (main() の自動実行を避ける)。"""
    spec = importlib.util.spec_from_file_location("post_review_entry", _ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def init_repo(path: str) -> str:
    """初期コミット済みの git repo を作る。realpath を返す (macOS の /tmp 対策)。"""
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "test")
    write(path, "seed.txt", "alpha\nbeta\ngamma\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return os.path.realpath(path)


def write(repo: str, rel: str, content: str) -> str:
    full = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


class HookTestCase(unittest.TestCase):
    """TMPDIR を隔離し、git repo と entry module を用意する基底クラス。

    本番の $TMPDIR を汚さないよう、必ず TMPDIR を差し替えてから hook を起動する。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.tmpdir = os.path.join(base, "tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        self._env = mock.patch.dict(
            os.environ,
            {
                "TMPDIR": self.tmpdir,
                "EXTERNAL_AI_POST_REVIEW": "1",
                "EXTERNAL_AI_POST_REVIEW_BASH_TRACKING": "1",
            },
        )
        self._env.start()

        self.repo = init_repo(os.path.join(base, "repo"))
        self.entry = load_entry()
        self.state = sys.modules["state"]
        self.gitscan = sys.modules["gitscan"]
        self.cursor = sys.modules["cursor"]

        self.review_calls: list[str] = []
        self._patches = [
            mock.patch.object(self.cursor, "is_available", return_value=True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    # -- hook 起動 --------------------------------------------------------

    def run_hook(self, phase: str, payload: dict) -> str:
        stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(
            sys, "stdout", out
        ), mock.patch.object(sys, "stderr", err):
            self.entry.main(["--phase", phase])
        self.last_stderr = err.getvalue()
        return out.getvalue()

    def stop(self, session_id: str, review_result: str | None) -> str:
        """Stop hook を 1 回起動する。cursor.review() の戻り値を差し替える。"""
        calls: list[str] = []

        def fake_review(diff_text: str):
            calls.append(diff_text)
            return review_result

        with mock.patch.object(self.cursor, "review", side_effect=fake_review):
            output = self.run_hook(
                "stop",
                {"session_id": session_id, "cwd": self.repo, "stop_hook_active": False},
            )
        self.review_calls = calls
        return output

    def edit(self, session_id: str, rel: str, content: str) -> str:
        """Write ツール相当: ファイルを書いて PostToolUse を発火させる。"""
        full = write(self.repo, rel, content)
        self.run_hook(
            "post-tool",
            {
                "session_id": session_id,
                "cwd": self.repo,
                "tool_name": "Write",
                "tool_use_id": f"tu_{rel}_{len(content)}",
                "tool_input": {"file_path": full, "content": content},
            },
        )
        return full

    def bash(self, session_id: str, tool_use_id: str, mutate) -> None:
        """Bash ツール相当: PreToolUse -> 実処理 -> PostToolUse を順に発火させる。"""
        payload = {
            "session_id": session_id,
            "cwd": self.repo,
            "tool_name": "Bash",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": "mutate"},
        }
        self.run_hook("pre-tool", payload)
        mutate()
        self.run_hook("post-tool", payload)

    # -- assertion ヘルパー ------------------------------------------------

    def assertReviewed(self, *rels: str) -> None:
        self.assertEqual(len(self.review_calls), 1, "cursor.review() が 1 回呼ばれていない")
        diff = self.review_calls[0]
        for rel in rels:
            self.assertIn(rel, diff, f"{rel} がレビュー対象に含まれていない")

    def assertNotReviewed(self) -> None:
        self.assertEqual(self.review_calls, [], "cursor.review() が呼ばれてしまった")

    def pending(self, session_id: str) -> list[str]:
        path = os.path.join(self.tmpdir, "post-implementation-review", "state", f"{session_id}.json")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return list(json.load(f).get("pending", {}).keys())
