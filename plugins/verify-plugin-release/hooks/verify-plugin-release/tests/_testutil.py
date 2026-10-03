"""テスト共通: plugin の hook ディレクトリを sys.path に通し、使い捨て git repo を作る。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

# テストの repo で git に自動 gc / maintenance を起こさせない設定 (key, value)。
#
# `git commit` / `fetch` は終わりに `git maintenance run --auto --detach` を起動する。git 2.55 は
# auto maintenance の既定戦略が geometric で、`.git/objects/17` に loose object が 2 件以上あると
# (loose object 数の見積もり = `objects/17` の件数 × 256 が、しきい値 100 を 256 単位に切り上げた
# 256 を超えると) 小さな repo でも repack が走る。しかも `--detach` は repack の自動条件を判定する前に
# 背景へ切り離すので、commit は待たずに戻る。その repack が `.git/objects/pack` に書いている間に
# `TemporaryDirectory.cleanup()` が走ると、tearDown が `Directory not empty` で落ちる (CI の flaky。
# object の hash 次第なので偶発的)。git 2.50 は gc 戦略でしきい値 (約 6700 個) が高く、同じ条件でも
# 起きないので、gc 戦略が既定の版 (2.50 など) で流すだけでは気付けない。起動そのものは版によらず
# 起きる: 設定で止めない限り、commit のたびに `git maintenance run --auto` が子として起動する。
#
#   maintenance.auto=false / gc.auto=0: そもそも自動 maintenance を起動しない
#   maintenance.autoDetach=false / gc.autoDetach=false: 何かが走っても背景へ切り離さない
#     (commit が戻る前に終わる)
#
# env で渡す (`GIT_CONFIG_COUNT`。git 2.31 以上) のは、テストが起動する git と、ゲート (製品コード)
# が起動する git に一括で効かせるため。ただし `git push` の受け側 (`receive-pack`) には届かない:
# ローカルの path へ送るとき git は repo 用の env (`GIT_CONFIG_COUNT` など) を外して起動する。
# 外されない `GIT_CONFIG_GLOBAL` が指す fixture (`hermetic.gitconfig`) にも同じ設定を置き、
# そちらで止める (`HERMETIC_GIT_ENV`)。
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


# 開発者の ~/.gitconfig でテストが揺れないよう、git にグローバル/システム設定を読ませない
# (例: diff.renames=false だとゲートが読む `diff --name-only` の一覧が、status.showUntrackedFiles=no
# だと `status --porcelain` の未追跡が変わる。ゲートはこの出力で判定する)。global の代わりに読ませる
# のは tests 配下の fixture で、自動 maintenance を止める設定 (`NO_BACKGROUND_GIT_SETTINGS` と
# `receive.autogc`) だけを持つ。既定の excludes (`XDG_CONFIG_HOME/git/ignore`) はこの指定では外れない
# (`XDG_CONFIG_HOME` を空にしているのは `test_main.run_hook` と、`test_hermetic_env` の床だけ)。
#
# 止める経路は 2 本あり、どちらも外さない:
#   - env の `GIT_CONFIG_COUNT`: repo 自身の config より優先される。ただし `receive-pack` には届かない
#   - global の fixture: `receive-pack` にも届く。ただし repo 自身の config には負ける
# fixture はテストから `git config --global` で書かないこと (tracked の file が書き換わる)。
HERMETIC_GIT_CONFIG = str(Path(__file__).resolve().parent / "hermetic.gitconfig")
HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": HERMETIC_GIT_CONFIG,
    "GIT_CONFIG_NOSYSTEM": "1",
    **git_config_env(NO_BACKGROUND_GIT_SETTINGS),
}


class HermeticGitTestCase(unittest.TestCase):
    """ゲート (製品コード) を in-process で動かすテストの基底クラス。

    `runner.run` は env を渡さず `os.environ` を継ぐので、ゲートが起動する git に設定を届けるには
    テスト側で `os.environ` に張る。repo を作る `sh` / `init_bare_origin` は自分で env を足すので
    この基底クラスに依存しない (patch していないクラスが repo を作っても止まる)。ゲートを
    subprocess で起動する側は、起動のたびに env へ `HERMETIC_GIT_ENV` を足す (`test_main.run_hook`)。
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, HERMETIC_GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)


def sh(cwd: Path, *args: str) -> str:
    # `HERMETIC_GIT_ENV` はここでも足す。テストクラス側の env patch に頼ると、patch していない
    # クラスが repo を作った時点で自動 maintenance が復活する (patch 済みなら同じ値の上書き)。
    env = {**os.environ, **HERMETIC_GIT_ENV}
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return r.stdout


def write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def write_json(root: Path, rel: str, data: object) -> None:
    write(root, rel, json.dumps(data, indent=2) + "\n")


def commit_all(root: Path, msg: str) -> None:
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", msg)


PASSING_TEST = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
FAILING_TEST = "import unittest\n\nclass T(unittest.TestCase):\n    def test_ng(self):\n        self.fail('boom')\n"


def add_plugin(root: Path, name: str, version: str | None = "0.1.0", tests: str | None = PASSING_TEST) -> None:
    manifest: dict[str, object] = {"name": name, "description": f"{name} plugin"}
    if version is not None:
        manifest["version"] = version
    write_json(root, f"plugins/{name}/.claude-plugin/plugin.json", manifest)
    write(root, f"plugins/{name}/CHANGELOG.md", "# Changelog\n")
    write(root, f"plugins/{name}/hooks/{name}/__main__.py", "print('hi')\n")
    if tests is not None:
        write(root, f"plugins/{name}/hooks/{name}/tests/__init__.py", "")
        write(root, f"plugins/{name}/hooks/{name}/tests/test_x.py", tests)


def make_marketplace(root: Path, plugins: list[str]) -> Path:
    """main に 1 commit ある marketplace repo を作る。"""
    root.mkdir(parents=True, exist_ok=True)
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "user.name", "Test")
    sh(root, "config", "user.email", "test@example.com")
    sh(root, "config", "commit.gpgsign", "false")
    sh(root, "config", "core.autocrlf", "false")
    write_json(
        root,
        ".claude-plugin/marketplace.json",
        {
            "name": "demo-market",
            "owner": {"name": "Demo"},
            "metadata": {"description": "demo"},
            "plugins": [{"name": p, "source": f"./plugins/{p}"} for p in plugins],
        },
    )
    for p in plugins:
        add_plugin(root, p)
    commit_all(root, "init")
    return root


def init_bare_origin(parent: Path, name: str = "origin.git") -> Path:
    """push 先の bare repo を作り、そのパス (`parent/name`) を返す。

    `git push` がローカルの path へ送るとき、受け側の `receive-pack` は `GIT_CONFIG_COUNT` など
    repo 用の env を外されて起動する (git が意図的にそうする)。受け取った後に走る自動 maintenance
    (`receive.autogc`) は、外されない `GIT_CONFIG_GLOBAL` の fixture が止めるので、`git init --bare`
    を直接呼んだ bare repo でも止まる。ここでは同じ設定を repo 自身の config にも書く (二重の備え):
    global の指定が外れても、この helper で作った bare repo は止まる。
    """
    sh(parent, "init", "--bare", "-q", name)
    path = parent / name
    for key, value in (*NO_BACKGROUND_GIT_SETTINGS, ("receive.autogc", "false")):
        sh(path, "config", key, value)
    return path


def bump(root: Path, name: str, version: str) -> None:
    rel = f"plugins/{name}/.claude-plugin/plugin.json"
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    data["version"] = version
    write_json(root, rel, data)
