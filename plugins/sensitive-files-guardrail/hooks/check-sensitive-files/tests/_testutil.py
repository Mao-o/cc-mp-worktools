"""テスト共通のパス設定と、テストが起動する git の環境。

check-sensitive-files/ と hooks/ (共有 _shared 用) を sys.path に通す。
repo を作る / commit する git は、ここの `git()` / `init_repo()` を通す (理由は
`NO_BACKGROUND_GIT_SETTINGS` のコメント)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

# テストの repo で git に自動 gc / maintenance を起こさせない設定 (key, value)。
#
# `git commit` / `merge` / `fetch` は終わりに `git maintenance run --auto --detach` を起動する。
# git 2.55 は auto maintenance の既定の戦略が geometric で、小さな repo でも `.git/objects/17` に
# loose object が 2 件あるだけで repack を始めうる。しかも `--detach` は repack の自動条件を判定する
# 前に背景へ切り離すので、commit は待たずに戻る。その repack が `.git/objects/pack` に書いている間に
# `TemporaryDirectory` の後始末が走ると、`Directory not empty` で落ちる (CI の flaky。object の
# hash 次第)。この suite で repo を作るテストは後始末が `rmtree(ignore_errors=True)` なので落ちず、
# tmp に残骸が残り、背景の git がテストより長く生きるだけだった。`TemporaryDirectory` を使うのは
# 床 (`test_hermetic_env.py`) で、止めておかないと落ちうる。git 2.50 は戦略が gc でしきい値
# (約 6700 個) が高く、同じ条件でも起きないので、gc 戦略が既定の版 (2.50 など) で流すだけでは
# 気付けない。起動そのものは git 2.50 でも commit のたびに起きる。
#
#   maintenance.auto=false: そもそも起動しない
#   gc.auto=0: 起動された maintenance の gc を走らせない (2.50 では gc.auto=0 だけだと起動は止まらない)
#   maintenance.autoDetach=false / gc.autoDetach=false: 何かが走っても背景へ切り離さない
#     (commit が戻る前に終わる)
#
# env で渡す (`GIT_CONFIG_COUNT`。git 2.31 以上) のは、テストが起動する git、hook が起動する git、
# それらが子として起動する git (`submodule` の clone など) に一括で効かせ、repo 自身の config より
# 優先させるため。ただし `git push` の受け側 (`receive-pack`) には届かない: ローカルの path へ送る
# とき git は repo 用の env (`GIT_CONFIG_COUNT` など) を外して起動する。外されない
# `GIT_CONFIG_GLOBAL` が指す fixture (`hermetic.gitconfig`) にも同じ設定を置き、そちらで止める
# (`HERMETIC_GIT_ENV`)。
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


# 開発者の ~/.gitconfig と system の config でテストが揺れないよう (例: core.excludesFile は hook が
# 読む `git ls-files --others --exclude-standard` の未追跡を変え、core.hooksPath はテストの commit で
# 開発者の hook を走らせる)、git に global / system の設定を読ませない。global の代わりに読ませるのは
# tests 配下の fixture で、自動 maintenance を止める設定 (`NO_BACKGROUND_GIT_SETTINGS` と
# `receive.autogc`) だけを持つ。
#
# 既定の除外ファイル (`$XDG_CONFIG_HOME/git/ignore`、未設定なら `~/.config/git/ignore`) はこの指定では
# 外れない (HOME / XDG_CONFIG_HOME を tmp に向けるのは各テストクラス)。
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

# 外側の env にあると、テストの git が別の repo や別の config、別の template を見てしまう変数。git の hook や
# `git -c` の配下から suite を流すと入る。外さないと、repo を作る helper の `init` / `config` / `commit` が外側の
# repo に書き込む (`GIT_DIR` など)。`GIT_CONFIG_PARAMETERS` は `GIT_CONFIG_COUNT` に勝ち、旧来の
# `GIT_CONFIG` があると `git config` の読み書き先がその file になる。`GIT_TEMPLATE_DIR` は helper の `git init`
# が写す template を差し替える: template の `hooks/pre-commit` が helper の commit で走り、`info/exclude` が
# 除外する file は commit から外れる (いずれも実測)。外せば `git init` は既定の template を使う (global の
# `init.templateDir` は、`GIT_CONFIG_GLOBAL` が指す fixture に無い)。
# `hermetic_env()` と `HermeticGitTestCase` が外す。
OUTER_GIT_LEAK_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_TEMPLATE_DIR",
)


def hermetic_env() -> dict[str, str]:
    """git を起動するときの env。

    `os.environ` から `OUTER_GIT_LEAK_ENV` を外し、`HERMETIC_GIT_ENV` を足す。`HERMETIC_GIT_ENV` を後から
    足すので、外側の env に同じ名前の変数があっても定数が勝つ。
    """
    env = {k: v for k, v in os.environ.items() if k not in OUTER_GIT_LEAK_ENV}
    env.update(HERMETIC_GIT_ENV)
    return env


def git(args: list[str], cwd, *, check: bool = True) -> subprocess.CompletedProcess:
    """テストが起動する git。毎回 `hermetic_env()` (外側の repo / config / template を外し、`HERMETIC_GIT_ENV` を足す) を渡す。

    テストクラス側の env patch (`HermeticGitTestCase`) に頼ると、patch していないクラスが repo を
    作った時点で自動 maintenance が復活する (patch 済みなら同じ値の上書き)。外側の env に `GIT_DIR` などが
    あるときも、patch していないクラスの `init` / `config` / `commit` が外側の repo に書き込む。repo を作る /
    commit する git は `subprocess.run` を直接書かず、必ずこれを通すこと (直接の起動は
    `test_hermetic_env.py` が検出する)。出力は bytes。`check=False` は、設定の問い合わせのように
    非ゼロ終了を assertion で見たいとき用。
    """
    return subprocess.run(["git", *args], cwd=cwd, env=hermetic_env(), check=check, capture_output=True)


def init_repo(cwd) -> None:
    """commit できる状態の空の repo を作る (`git init` と user / gpgsign の設定)。"""
    git(["init", "--initial-branch=main"], cwd)
    git(["config", "user.name", "test"], cwd)
    git(["config", "user.email", "test@example.com"], cwd)
    git(["config", "commit.gpgsign", "false"], cwd)


class HermeticGitTestCase(unittest.TestCase):
    """テストの間、`HERMETIC_GIT_ENV` を `os.environ` に当て、外側の repo / config / template を指す変数
    (`OUTER_GIT_LEAK_ENV`) を外す基底クラス (`mock.patch.dict` の中なので、テストが終われば元に戻る)。

    repo を作る `git()` は毎回 `hermetic_env()` を自分で使うので、repo の作成はこのクラスに頼らない。ここで
    当てるのは hook (製品コード) が起動する git のため: `checker._run_git_raw` は env を渡さず
    `os.environ` を継承する (hook を子プロセスで起動するテストも `dict(os.environ)` から env を作る)。
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, HERMETIC_GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in OUTER_GIT_LEAK_ENV:
            os.environ.pop(name, None)
