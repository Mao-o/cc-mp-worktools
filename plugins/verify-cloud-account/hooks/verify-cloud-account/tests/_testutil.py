"""テスト共通のパス設定と実環境からの隔離。

パス設定: verify-cloud-account/ と hooks/ を sys.path に通す。

隔離: v0.13.0 以降、`verify()` は CLI を起動する前に **ローカル設定ファイル**
(`~/.config/gh/hosts.yml` / `~/.config/gcloud/configurations/config_<name>`) を読み、
dispatcher は accounts.local.json が無いとき `$HOME` のグローバル既定を見る。
何もしないと**開発者の実環境が verdict を変える** (実際に、開発者の hosts.yml の
アクティブアカウントが fixture の期待値と一致して「CLI を呼ばずに allow」になり、
CLI モックを前提にしたテストが壊れた)。`start_isolation()` で設定ディレクトリと
`$HOME` を使い捨て dir に固定し、判定を変えうる env を落とす。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


# 実環境から漏れるとローカル読取 / モード解決の結果が変わる env。
LEAKY_ENV_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "VERIFY_CLOUD_ACCOUNT_MODE",
    "VERIFY_CLOUD_ACCOUNT_DEBUG",
)

_CLOUDSDK_PREFIX = "CLOUDSDK_"


def cli_config_env(root: Path) -> dict[str, str]:
    """gh / gcloud のローカル設定 dir を `root` 配下 (= 存在しない) に向ける env。"""
    return {
        "GH_CONFIG_DIR": str(root / "gh"),
        "XDG_CONFIG_HOME": str(root / "xdg"),
        "CLOUDSDK_CONFIG": str(root / "gcloud"),
    }


# 隔離中の `$HOME`。**実在しない絶対パス**を使う:
# - 実 HOME を読まない / 書かない
# - fixture (tempfile 配下) と親子関係を持たないので、親遡及の停止条件
#   (`core/paths._crosses_home`) を実 HOME のときと同じ形に保てる
#   (tmp 配下に置くと共通の親で遡及が止まり、既存テストの前提が変わる)
ISOLATED_HOME = Path("/nonexistent-home-verify-cloud-account")


class _Isolation:
    def __init__(self, patchers, home: Path):
        self._patchers = patchers
        self.home = home

    def stop(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()


def start_isolation(root: Path, home: Path = ISOLATED_HOME) -> _Isolation:
    """実環境の CLI 設定 / `$HOME` / env から切り離す。

    `setUpModule()` か `setUp()` から呼び、返り値の `stop()` で解除する
    (`addCleanup` / `tearDownModule` に登録すること)。
    """
    patchers = [
        mock.patch.dict(os.environ, cli_config_env(Path(root))),
        mock.patch.object(Path, "home", staticmethod(lambda: home)),
    ]
    for patcher in patchers:
        patcher.start()
    # patch.dict は stop() で元の内容を復元するため、開始後の pop は安全。
    for name in LEAKY_ENV_VARS:
        os.environ.pop(name, None)
    for name in [
        n
        for n in os.environ
        if n.startswith(_CLOUDSDK_PREFIX) and n != "CLOUDSDK_CONFIG"
    ]:
        os.environ.pop(name, None)
    return _Isolation(patchers, home)
