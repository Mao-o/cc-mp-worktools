"""テスト共通: hook ディレクトリを sys.path に通し、main checkout + linked worktree を作る。"""
from __future__ import annotations

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
# `git commit` は終わりに `git maintenance run --auto --detach` を起動する (起動そのものは
# git 2.50 でも commit のたびに起きる)。git 2.55 は auto maintenance の既定戦略が geometric で、
# `.git/objects/17` に loose object が 2 件あるだけで、小さな repo でも repack が始まりうる。
# しかも `--detach` は背景へ切り離すので、commit は待たずに戻る。その repack が
# `.git/objects/pack` に書いている間に `TemporaryDirectory.cleanup()` が走ると、tearDown が
# `Directory not empty` で落ちる (CI の flaky。object の hash 次第なので偶発的)。git 2.50 は
# 同じ条件でも起きないので、ローカルの実行だけでは気付けない。
#
#   maintenance.auto=false / gc.auto=0: そもそも自動 maintenance を起動しない
#   maintenance.autoDetach=false / gc.autoDetach=false: 何かが走っても背景へ切り離さない
#     (commit が戻る前に終わる)
#
# env で渡す (`GIT_CONFIG_COUNT`。git 2.31 以上) のは、テストが起動する git と hook が起動する git に
# 一括で効かせ、repo 自身の config より優先させるため。ただし `git push` の受け側 (`receive-pack`)
# には届かない: ローカルの path へ送るとき git は repo 用の env (`GIT_CONFIG_COUNT` など) を外して
# 起動する。外されない `GIT_CONFIG_GLOBAL` が指す fixture (`hermetic.gitconfig`) にも同じ設定を置き、
# そちらで止める (`HERMETIC_GIT_ENV`)。この suite の hook とテスト本体は push しない (床の
# `test_hermetic_env.py` だけが push する) が、同じ作りの他の suite と揃えてある。
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


# 開発者の ~/.gitconfig (color.ui=always / diff.external / core.hooksPath 等) でテストが揺れないよう、
# git にグローバル/システム設定を読ませない。global の代わりに読ませるのは tests 配下の fixture で、
# 自動 maintenance を止める設定 (`NO_BACKGROUND_GIT_SETTINGS` と `receive.autogc`) だけを持つ。
#
# 止める経路は 2 本あり、どちらも外さない:
#   - env の `GIT_CONFIG_COUNT`: repo 自身の config より優先される。ただし `receive-pack` には届かない
#   - global の fixture: `receive-pack` にも届く。ただし repo 自身の config には負ける
# fixture はテストから `git config --global` で書かないこと (tracked の file が書き換わる)。
HERMETIC_GIT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermetic.gitconfig")
HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": HERMETIC_GIT_CONFIG,
    "GIT_CONFIG_NOSYSTEM": "1",
    **git_config_env(NO_BACKGROUND_GIT_SETTINGS),
}


def sh(cwd: Path, *args: str) -> str:
    # `HERMETIC_GIT_ENV` は毎回足す。テストクラス側の env patch に頼ると、patch していないクラスが
    # `make_repo` を呼んだ時点で自動 maintenance が復活する (patch 済みなら同じ値の上書き)。
    env = {**os.environ, **HERMETIC_GIT_ENV}
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return r.stdout


def make_repo(base: Path) -> tuple[Path, Path, Path]:
    """(main checkout, worktree A, worktree B) を作る。A は main の中 (.claude/worktrees/a) に置く。"""
    main = base / "repo"
    main.mkdir(parents=True)
    sh(main, "init", "-q", "-b", "main")
    sh(main, "config", "user.name", "Test")
    sh(main, "config", "user.email", "test@example.com")
    sh(main, "config", "commit.gpgsign", "false")
    (main / "README.md").write_text("# demo\n", encoding="utf-8")
    sh(main, "add", "-A")
    sh(main, "commit", "-q", "-m", "init")
    wt_a = main / ".claude" / "worktrees" / "a"
    wt_b = base / "wt-b"
    sh(main, "worktree", "add", "-q", "-b", "lane-a", str(wt_a))
    sh(main, "worktree", "add", "-q", "-b", "lane-b", str(wt_b))
    return main, wt_a, wt_b


class HermeticGitTestCase(unittest.TestCase):
    """テストの間、`HERMETIC_GIT_ENV` を `os.environ` に当てる基底クラス。

    repo を作る `sh()` は毎回この env を自分で足すので、repo の作成はこのクラスに頼らない。ここで
    当てるのは hook (製品コード) が起動する git のため: `family._git` は env を渡さず `os.environ`
    を継承する。
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, HERMETIC_GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
