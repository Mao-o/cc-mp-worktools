"""テスト共通のパス設定とフィクスチャ。

CI は `python3 -m unittest discover tests` をパッケージ root で回すため、
import は post-implementation-review/ 直下を sys.path に載せて解決する。
`hooks/_common` の解決用に hooks/ も載せる (本番は `__main__.py` が同じ挿入を行う)。
"""
from __future__ import annotations

import atexit
import importlib.util
import io
import json
import os
import shutil
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

from _common import settings  # noqa: E402  (sys.path 挿入後に import する)

_ENTRY_PATH = _PKG_DIR / "__main__.py"


# cursor の実体は**テストプロセス全体で**偽 CLI 側に固定する。0.11.0 の検出は
# `cursor-agent` も候補にするため、固定しないと開発機に入っている本物を掴んで
# `--version` を起動しうる (外部 AI CLI を起動しないというテストの前提が崩れる)。
# **クラスごとの env パッチに書くだけでは足りない**: 自前で `mock.patch.dict` を張る
# テストクラスが漏れると、そこだけ実機の検出が走る (実際に踏んだ)。モジュール読み込み時に
# 入れておき、`clear_plugin_env` からも必ず除外する。
CURSOR_COMMAND_ENV = {"EXTERNAL_AI_CURSOR_COMMAND": "cursor"}
os.environ.update(CURSOR_COMMAND_ENV)


def clear_plugin_env(keep: dict | None = None) -> None:
    """開発者 shell の `EXTERNAL_AI_*` を外す (`keep` に挙げたものだけ残す)。

    「未設定時は従来どおり」の回帰テストは、開発者が shell で
    `EXTERNAL_AI_POST_REVIEW_TIMEOUT` 等を export していると嘘になる。個別に列挙する
    方式だと変数が増えるたびに漏れるので接頭辞で一掃する。`mock.patch.dict` は stop 時に
    dict の中身を丸ごと元に戻すので、start した後に消したキーも自動で復元される。
    """
    keep = {**CURSOR_COMMAND_ENV, **(keep or {})}
    for key in [k for k in os.environ if k.startswith(settings.ENV_PREFIX)]:
        if key not in keep:
            del os.environ[key]

# テストの repo で git に自動 gc / maintenance を起こさせない設定 (key, value)。
#
# `git commit` / `merge` / `fetch` は終わりに `git maintenance run --auto --detach` を起動する。
# git 2.55 は auto maintenance の既定戦略が geometric で、`.git/objects/17` に loose object が
# 2 件以上あると (loose object 数の見積もり = `objects/17` の件数 × 256 が、しきい値 100 を
# 256 単位に切り上げた 256 を超えると) 小さな repo でも repack が走る。しかも
# `--detach` は repack の自動条件を判定する前に背景へ切り離すので、commit は待たずに戻る。
# その repack が `.git/objects/pack` に書いている間に `TemporaryDirectory.cleanup()` が走ると、
# tearDown が `Directory not empty` で落ちる (CI の flaky。object の hash 次第なので偶発的)。
# git 2.50 は gc 戦略でしきい値 (約 6700 個) が高く、同じ条件でも起きないので、gc 戦略が既定の
# 版 (2.50 など) で流すだけでは気付けない。
#
#   maintenance.auto=false: そもそも自動 maintenance を起動しない
#   gc.auto=0: 起動された maintenance の gc を走らせない。git 2.50 では gc.auto=0 だけだと
#     `git maintenance run --auto` の起動は止まらない (実測)
#   maintenance.autoDetach=false / gc.autoDetach=false: 何かが走っても背景へ切り離さない
#     (commit が戻る前に終わる)
#
# env で渡す (`GIT_CONFIG_COUNT`。git 2.31 以上) のは、テストが起動する git、hook が起動する
# git、それらが子として起動する git (`pull` の fetch / merge など) に一括で効かせるため。
# ただし `git push` の受け側 (`receive-pack`) には届かない: ローカルの path へ送るとき git は
# repo 用の env (`GIT_CONFIG_COUNT` など) を外して起動する。外されない `GIT_CONFIG_GLOBAL` が
# 指す fixture (`hermetic.gitconfig`) にも同じ設定を置き、そちらで止める (`HERMETIC_GIT_ENV`)。
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


# 開発者の ~/.gitconfig (`diff.renames=false` / `diff.external` / `diff.noprefix` / `core.excludesFile`
# 等。hook が読む変更ファイル名の一覧・patch・未追跡の一覧を変える) でテストが揺れないよう、git に
# グローバル/システム設定を読ませない。global の代わりに読ませるのは tests 配下の fixture で、
# 自動 maintenance を止める設定 (`NO_BACKGROUND_GIT_SETTINGS` と `receive.autogc`) だけを持つ。
#
# 止める経路は 2 本あり、どちらも外さない:
#   - env の `GIT_CONFIG_COUNT`: repo 自身の config より優先される。ただし `receive-pack` には届かない
#   - global の fixture: `receive-pack` にも届く。ただし repo 自身の config には負ける
# fixture はテストから `git config --global` で書かないこと (tracked の file が書き換わる)。
#
# git の既定の除外ファイル (`$XDG_CONFIG_HOME/git/ignore`。`XDG_CONFIG_HOME` が未設定か空なら
# `~/.config/git/ignore`) と属性ファイル (同じ場所の `attributes`) は、`GIT_CONFIG_GLOBAL` を指しても
# 外れない (config ではなく環境変数と HOME で決まる)。実測: 除外ファイルに載せた名前が
# `ls-files --others --exclude-standard` と `status` から消え、`.env` を載せていると `git add -A` が
# `.env` を拾わず、続く commit が失敗するテストが出る。hook が読む git の出力 (未追跡の判定) も変わる。
# 何も置かない dir に `XDG_CONFIG_HOME` を向けて切り離す。空でない `XDG_CONFIG_HOME` があれば git は
# `~/.config/git/ignore` を読まない (実測) ので、`HOME` は差し替えない。
HERMETIC_XDG_CONFIG_HOME = tempfile.mkdtemp(prefix="hermetic-xdg-")
atexit.register(shutil.rmtree, HERMETIC_XDG_CONFIG_HOME, ignore_errors=True)
HERMETIC_GIT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermetic.gitconfig")
HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": HERMETIC_GIT_CONFIG,
    "GIT_CONFIG_NOSYSTEM": "1",
    "XDG_CONFIG_HOME": HERMETIC_XDG_CONFIG_HOME,
    **git_config_env(NO_BACKGROUND_GIT_SETTINGS),
}

# 開発者 shell の除外設定 (CODE_ONLY=1 等) で `.txt` を使う既存テストが落ちないよう pin する
NEUTRAL_EXCLUSION_ENV = {
    "EXTERNAL_AI_POST_REVIEW_CODE_ONLY": "0",
    "EXTERNAL_AI_POST_REVIEW_EXCLUDE": "",
    "EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS": "1",
}

# `CLAUDE_CODE_VERSION` は `_claude_code_version()` の検出順で最優先に見られる値
# (`__main__.py` モジュール docstring「未対応 CLI での自動 fail-closed」節参照)。ここで
# 固定しないと、開発者の実行環境で実在する `CLAUDE_CODE_EXECPATH` (ローカルインストール)
# や実際の `claude --version` の結果に頼ってしまい、「auto モード = additionalContext」を
# 前提にした既存テストが「今この端末で動いている Claude Code の版数」次第で揺れる
# (かつ実機の `claude` を起動しかねない — 禁止事項)。値は下限 2.1.163 より十分大きい
# "9.9.9" を使い、既定を「常に additionalContext 対応」に固定する。auto の block
# フォールバックを明示的にテストしたいケースは、各テストが `_claude_code_version` を
# 直接 mock する (`test_throttle_flow.py::TestVersionAwareMode`)。版数検出そのもの
# (env var / EXECPATH / subprocess の 3 段) の単体テストは `test_version_detect.py`
# (こちらは `_claude_code_version` を mock せず `subprocess.run` を mock する)。
PINNED_VERSION_ENV = {"CLAUDE_CODE_VERSION": "9.9.9"}


def load_entry():
    """`__main__.py` を `__main__` 以外の名前で読み込む (main() の自動実行を避ける)。"""
    spec = importlib.util.spec_from_file_location("post_review_entry", _ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    # `HERMETIC_GIT_ENV` はここでも足す。テストクラス側の env patch に頼ると、patch していない
    # クラスが `init_repo` を呼んだ時点で自動 maintenance が復活する (patch 済みなら同じ値の上書き)。
    # ここで足していても、テストクラス側の patch は要る: hook の関数は env を渡さず `os.environ` を
    # 継承するので、hook が起動する git に届くのはクラス側の patch だけ (`test_hermetic_env` の
    # `_ProductGitChecks` が見る)。
    env = {**os.environ, **HERMETIC_GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )


# git を 5 回起動する本物の初期化 (1 回約 54 ms) はプロセスで 1 回だけ行い、各テストには
# そのコピー (約 5 ms) を渡す。置き場所はモジュール読み込み時に決める: `HookTestCase.setUp`
# は TMPDIR を差し替えてから `init_repo` を呼ぶので、そこで決めるとテストの一時ディレクトリ
# ごと消されうる。
_TEMPLATE_DIR = tempfile.mkdtemp(prefix="pir-template-")
atexit.register(shutil.rmtree, _TEMPLATE_DIR, True)
_TEMPLATE_REPO: str | None = None


def _build_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "test")
    write(path, "seed.txt", "alpha\nbeta\ngamma\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")


def init_repo(path: str) -> str:
    """初期コミット済みの git repo を作る。realpath を返す (macOS の /tmp 対策)。"""
    global _TEMPLATE_REPO
    if _TEMPLATE_REPO is None:
        _build_repo(os.path.join(_TEMPLATE_DIR, "repo"))
        _TEMPLATE_REPO = os.path.join(_TEMPLATE_DIR, "repo")
    shutil.copytree(_TEMPLATE_REPO, path, symlinks=True, dirs_exist_ok=True)
    return os.path.realpath(path)


def init_bare_origin(parent: str, name: str = "origin.git") -> str:
    """push 先の bare repo を作り、そのパス (`parent/name`) を返す。

    `git push` がローカルの path へ送るとき、受け側の `receive-pack` は `GIT_CONFIG_COUNT` など
    repo 用の env を外されて起動する (git が意図的にそうする)。受け取った後に走る自動 maintenance
    (`receive.autogc`) は、外されない `GIT_CONFIG_GLOBAL` の fixture が止めるので、`git init --bare`
    を直接呼んだ bare repo でも止まる。ここでは同じ設定を repo 自身の config にも書く (二重の備え):
    global の指定が外れても、この helper で作った bare repo は止まる。
    """
    git(parent, "init", "--bare", "-q", name)
    path = os.path.join(parent, name)
    for key, value in (*NO_BACKGROUND_GIT_SETTINGS, ("receive.autogc", "false")):
        git(path, "config", key, value)
    return path


def write(repo: str, rel: str, content: str) -> str:
    full = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


def content_kib(kib: int, seed: str = "x") -> str:
    """kib KiB ちょうどのテキスト (1 行 64 バイト、行ごとに連番)。予算テストのサイズ制御用。

    新規ファイルの diff は本文 + 行ごとの `+` + ヘッダ約 110 バイトになる。
    """
    head = (seed * 58)[:58]
    return "".join(f"{head}{i:05d}\n" for i in range(kib * 16))


class HookTestCase(unittest.TestCase):
    """TMPDIR を隔離し、git repo と entry module を用意する基底クラス。

    本番の $TMPDIR を汚さないよう、必ず TMPDIR を差し替えてから hook を起動する。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.tmpdir = os.path.join(base, "tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        pinned = {
            "EXTERNAL_AI_POST_REVIEW": "1",
            "EXTERNAL_AI_POST_REVIEW_BASH_TRACKING": "1",
            **CURSOR_COMMAND_ENV,  # cursor の実体の固定 (モジュール冒頭のコメント参照)
            **NEUTRAL_EXCLUSION_ENV,
        }
        self._env = mock.patch.dict(
            os.environ,
            {"TMPDIR": self.tmpdir, **pinned, **HERMETIC_GIT_ENV, **PINNED_VERSION_ENV},
        )
        self._env.start()
        clear_plugin_env(keep=pinned)

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
        cwds: list[str | None] = []

        def fake_review(diff_text: str, *, cwd: str | None = None):
            calls.append(diff_text)
            cwds.append(cwd)
            return review_result

        with mock.patch.object(self.cursor, "review", side_effect=fake_review):
            output = self.run_hook(
                "stop",
                {"session_id": session_id, "cwd": self.repo, "stop_hook_active": False},
            )
        self.review_calls = calls
        self.review_cwds = cwds
        return output

    def stop_raising(self, session_id: str, error: Exception) -> str:
        """Stop hook を起動し、`cursor.review()` が例外を投げる状況を作る。

        外部 CLI ラッパは OSError / タイムアウトを内部で吸うが、それ以外
        (不正な timeout 値による ValueError 等) はそのまま上がってくる。
        """
        with mock.patch.object(self.cursor, "review", side_effect=error):
            return self.run_hook(
                "stop",
                {"session_id": session_id, "cwd": self.repo, "stop_hook_active": False},
            )

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

    def assertNotBlocked(self, output: str) -> str:
        """block していないこと。返り値は `systemMessage` 本文 (無ければ "")。

        0.6.0 から、ブロックしないターンでも所要時間と結果を `systemMessage` で出す
        ようになったので、「出力が空」は非 block の判定基準として使えない。
        """
        if not output:
            return ""
        data = json.loads(output)
        self.assertNotIn("decision", data, "block してはいけない出力に decision がある")
        self.assertNotIn(
            "hookSpecificOutput", data, "block してはいけない出力に所見注入がある"
        )
        return data.get("systemMessage", "")

    def assertBlocked(self, output: str, backend: str = "Cursor") -> dict:
        """指摘ありでレビュー結果を返したことを検証する。

        `backend` はレビュー本文のヘッダに出る backend の表示名 (0.12.0)。既定は
        `Cursor` = `EXTERNAL_AI_POST_REVIEW_BACKENDS` 未設定時の唯一の送信先なので、
        既存の呼び出し側は **cursor に送られたことまで込みで**従来どおり固定される。
        別の backend に送った結果を検証するテストだけが明示的に渡す。

        0.8.0 から既定は `hookSpecificOutput.additionalContext` (hookEventName: "Stop")。
        `EXTERNAL_AI_POST_REVIEW_MODE=block` なら 0.7.0 までの top-level
        `decision: "block"` + `reason` に戻る。どちらのモードでも Claude に届く本文は
        同じなので、呼び出し側の便宜のために `data["reason"]` へエイリアスして返す
        (実際の wire format の検証はここで一度だけ行う)。公式 docs
        (`Stop decision control` 節) は `additionalContext` について
        "It keeps the conversation going through the same loop protections as
        decision: block" と明記しており、モードによって Claude 側の効果は変わらない。

        **どちらの envelope も受理するのは意図的**。既定 (`context`) を明示的に
        固定したいテストは `test_throttle_flow.py::TestOutputMode` を見ること — ここは
        「内容 (reason 相当の本文) がどちらのモードでも一貫していること」だけを保証し、
        個々の block/繰り越し系テストは env を切り替えなくても両モードで動くようにする
        (=このメソッドを寛容にすることで、内容アサーションを重複させない)。
        """
        self.assertTrue(output, "レビュー結果の JSON が出力されていない")
        data = json.loads(output)
        if "decision" in data:
            self.assertEqual(data["decision"], "block")
            reason = data.get("reason", "")
        else:
            specific = data.get("hookSpecificOutput", {})
            self.assertEqual(specific.get("hookEventName"), "Stop")
            self.assertNotIn("decision", data)
            reason = specific.get("additionalContext", "")
            data["reason"] = reason
        self.assertIn(f"## 実装直後レビュー結果 ({backend}, 差分レビュー)", reason)
        return data

    def pending(self, session_id: str) -> list[str]:
        path = os.path.join(self.tmpdir, "post-implementation-review", "state", f"{session_id}.json")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return list(json.load(f).get("pending", {}).keys())
