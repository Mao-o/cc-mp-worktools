"""exitplan-review テスト共通のパス設定とフィクスチャ。

post-implementation-review/tests/_testutil.py と同じ方式: TMPDIR を隔離し、
`__main__.py` を `__main__` 以外の名前で読み込んで main() を直接呼ぶ。
cursor / codex は起動せず、`review()` をモックするか PATH 先頭の偽 CLI に差し替える。
"""
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
_HOOKS_DIR = _PKG_DIR.parent  # hooks/_common の解決用 (本番は __main__.py が載せる)
for _p in (_HOOKS_DIR, _PKG_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _common import settings  # noqa: E402  (sys.path 挿入後に import する)

_ENTRY_PATH = _PKG_DIR / "__main__.py"


# cursor の実体は**テストプロセス全体で**偽 CLI 側に固定する。0.11.0 の検出は
# `cursor-agent` も候補にするため、固定しないと開発機に入っている本物を掴んで
# `--version` を起動しうる (外部 AI CLI を起動しないというテストの前提が崩れ、かつ偽
# cursor を PATH 先頭に置いたテストが「argv[0] が cursor-agent」で落ちる)。
# **クラスごとの env パッチに書くだけでは足りない**: 自前で `mock.patch.dict` を張る
# テストクラスが漏れると、そこだけ実機の検出が走る (実際に踏んだ)。モジュール読み込み時に
# 入れておき、`clear_plugin_env` からも必ず除外する。
CURSOR_COMMAND_ENV = {"EXTERNAL_AI_CURSOR_COMMAND": "cursor"}
os.environ.update(CURSOR_COMMAND_ENV)


def clear_plugin_env(keep: dict | None = None) -> None:
    """開発者 shell の `EXTERNAL_AI_*` を外す (`keep` に挙げたものだけ残す)。

    「未設定時は従来どおり」の回帰テストは、開発者が shell で
    `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` 等を export していると嘘になる。個別に列挙する
    方式だと変数が増えるたびに漏れるので接頭辞で一掃する。`mock.patch.dict` は stop 時に
    dict の中身を丸ごと元に戻すので、start した後に消したキーも自動で復元される。
    """
    keep = {**CURSOR_COMMAND_ENV, **(keep or {})}
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


# テストの repo で git に自動 gc / maintenance を起こさせない設定 (key, value)。
# 理由 (git 2.55 の auto maintenance が背景で pack を書き、tempdir の後始末と競合して flaky に
# なる) は post-implementation-review/tests/_testutil.py の `NO_BACKGROUND_GIT_SETTINGS` を参照。
# この suite の `init_repo` は `git init` だけで commit しないが、repo を作るヘルパーの扱いを
# そちらと揃えておく (commit するテストを足したときに復活させないため)。
NO_BACKGROUND_GIT_SETTINGS = (
    ("maintenance.auto", "false"),
    ("maintenance.autoDetach", "false"),
    ("gc.auto", "0"),
    ("gc.autoDetach", "false"),
)


def git_config_env(settings: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """`(key, value)` の並びを `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` にする。

    件数は並びから数える。手で書くと、項目を足し引きしたときに COUNT がずれる: 多ければ git が
    全コマンドで `missing config key` と言って落ち、少なければ末尾の設定が黙って無視される。
    """
    env = {"GIT_CONFIG_COUNT": str(len(settings))}
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# git にグローバル/システム設定を読ませない (post-implementation-review/tests/_testutil.py と同じ配慮)。
# 今は開発者の ~/.gitconfig で結果が変わる経路は無い (この suite の git は `init_repo` の `git init` と、
# hook の `rev-parse --show-toplevel` だけ)。commit するテストを足すと、`core.hooksPath` /
# `init.templateDir` (開発者の hook が走る) が効くので、先に揃えておく。
# global の代わりに読ませるのは tests 配下の fixture で、自動 maintenance を止める設定を持つ。
# env の `GIT_CONFIG_COUNT` に加えて fixture も置く理由 (`receive-pack` に env が届かない) は、
# そちらの `HERMETIC_GIT_ENV` のコメントを参照 (この suite は push しないが、同じ形に揃える)。
# fixture はテストから `git config --global` で書かないこと (tracked の file が書き換わる)。
#
# post-implementation-review の `HERMETIC_GIT_ENV` にある `XDG_CONFIG_HOME` (git の既定の除外ファイルと
# 属性ファイルを開発者のものから切り離す) は、この suite では足さない: git の status / 未追跡 / diff の
# 出力を読むテストが無い (`init_repo` は `git init` だけ、hook が起動する git は
# `rev-parse --show-toplevel` だけ) ので、除外ファイルの有無で結果が変わらない。そういうテストを
# 足すときは、そちらの定数と同じ扱いにすること。
HERMETIC_GIT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermetic.gitconfig")
HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": HERMETIC_GIT_CONFIG,
    "GIT_CONFIG_NOSYSTEM": "1",
    **git_config_env(NO_BACKGROUND_GIT_SETTINGS),
}


# テストが起動する git に、外側の env (開発者の shell、git の hook の中、`git -c` の配下) からそのまま
# 漏れてはいけないもの (post-implementation-review/tests/_testutil.py の `OUTER_GIT_LEAKS` と同じ一覧と理由):
# repo の場所を変えるもの、`GIT_CONFIG_PARAMETERS` (`GIT_CONFIG_COUNT` に勝つ)、旧来の `GIT_CONFIG`、
# `git rev-parse --local-env-vars` が挙げる残り、`GIT_TEMPLATE_DIR` (`git init` が外側の hook を写す)。
# `GIT_CONFIG_COUNT` 系は外さない: `HERMETIC_GIT_ENV` が同じ名前で上書きするので、混ぜる向きを床が見られる。
OUTER_GIT_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_TEMPLATE_DIR",
)


def hermetic_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """テストが git に渡す env。`os.environ` から `OUTER_GIT_LEAKS` を外し、`HERMETIC_GIT_ENV` を重ねる。

    `os.environ` を patch せずに作るので、床が見る「helper が git に渡す前の `os.environ`」は変わらない。
    """
    env = {k: v for k, v in os.environ.items() if k not in OUTER_GIT_LEAKS}
    return {**env, **HERMETIC_GIT_ENV, **(extra or {})}


def scrub_outer_git_env() -> None:
    """`OUTER_GIT_LEAKS` を `os.environ` から外す。基底クラスが `mock.patch.dict(os.environ)` を start した
    後に呼ぶ (stop で元に戻る)。hook (製品コード) の `rev-parse` は env を渡さず `os.environ` を継承する。"""
    for name in OUTER_GIT_LEAKS:
        os.environ.pop(name, None)


def init_repo(path: str) -> str:
    """空の git repo を作る (cursor/codex の起動 cwd 検証用)。realpath を返す。"""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, env=hermetic_env(), check=True)
    return os.path.realpath(path)


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
        self._env = mock.patch.dict(
            os.environ, {"TMPDIR": self.tmpdir, **CURSOR_COMMAND_ENV}
        )
        self._env.start()
        scrub_outer_git_env()
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
        cwd: str | None = None,
    ) -> str:
        """ExitPlanMode hook を 1 回起動する。両レビュアーの `review()` 戻り値を差し替える。

        `cwd` (payload の cwd) は既定で `self.tmpdir` (git 作業ツリーではない)。
        cursor/codex の起動 cwd を検証したいテストは実 git repo のパスを渡す。
        """
        cursor_calls: list[str] = []
        codex_calls: list[str] = []
        cursor_cwds: list[str | None] = []
        codex_cwds: list[str | None] = []

        def fake_cursor(plan_text: str, *, cwd: str | None = None):
            cursor_calls.append(plan_text)
            cursor_cwds.append(cwd)
            return cursor_result

        def fake_codex(plan_text: str, *, cwd: str | None = None):
            codex_calls.append(plan_text)
            codex_cwds.append(cwd)
            return codex_result

        with mock.patch.object(self.cursor, "review", side_effect=fake_cursor), mock.patch.object(
            self.codex, "review", side_effect=fake_codex
        ):
            output = self.run_hook(
                {
                    "session_id": session_id,
                    "tool_name": "ExitPlanMode",
                    "tool_input": {"plan": plan},
                    "cwd": cwd if cwd is not None else self.tmpdir,
                }
            )
        self.cursor_calls = cursor_calls
        self.codex_calls = codex_calls
        self.cursor_cwds = cursor_cwds
        self.codex_cwds = codex_cwds
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
