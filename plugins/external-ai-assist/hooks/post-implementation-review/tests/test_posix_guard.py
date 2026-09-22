"""Windows (`os.name != "posix"`) では他モジュールの import より前に exit 0 すること。

`state.py` が `fcntl` を直接 import しており、Windows には `fcntl` が存在しないため
そのまま import すると ImportError で hook error 通知になる (内部バックログ)。
`__main__.py` は `os` / `sys` 以外の import より前に `os.name` を判定して抜ける。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_PKG_DIR = Path(__file__).resolve().parent.parent
_ENTRY_PATH = _PKG_DIR / "__main__.py"


def _purge_hook_modules() -> None:
    """hook ディレクトリ由来の top-level module を `sys.modules` から外す。

    **名前で列挙してはいけない。** hook 内 module が増えたときに漏れ、漏れた module は
    「外し損ねた古い実体」への参照を抱えたまま生き残る。後続のテストが
    `sys.modules["cursor"]` を patch しても、古い module 経由の呼び出しはその patch を
    通らない ので、**モックのはずの箇所で実機の外部 AI CLI が起動する** (0.12.0 で
    `selection` を足したときに実際に踏んだ: `selection.cursor` が旧 module を指したまま
    残り、`cursor.review` の patch が効かずに本物の cursor CLI を起動しかけた)。

    ここではパッケージディレクトリ直下のファイルから読まれた module を機械的に全部
    落とすので、module が増えてもこの関数を直す必要が無い。
    """
    pkg_dir = str(_PKG_DIR)
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and os.path.dirname(os.path.abspath(path)) == pkg_dir:
            del sys.modules[name]

# ガードの条件は `os.name != "posix"` なので、値そのものは "posix" 以外なら何でもよい。
# 実際の Windows 値である "nt" は使わない: Python 3.12+ の `pathlib.Path()` は生成時に
# `os.name` を見て `WindowsPath` / `PosixPath` を選ぶため、"nt" を mock すると
# `cursor.py` の module レベル `Path(__file__)` (ガードとは無関係) が
# `WindowsPath` を選んでしまい、Python 3.14 ではこの POSIX 機上での `WindowsPath`
# インスタンス化が `UnsupportedOperation` で落ちる (未修正コードでの負テスト実行時に
# 実測)。"java" (Jython の伝統的な `os.name` 値) は pathlib の特別扱いに触れないため、
# ガードの条件だけを狙い撃ちできる。
_NON_POSIX = "java"


class TestPosixGuard(unittest.TestCase):
    def _load_under(self, os_name: str):
        """`os.name` を差し替えた状態で `__main__.py` を新規 exec する。"""
        spec = importlib.util.spec_from_file_location("post_review_posix_probe", _ENTRY_PATH)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        with mock.patch("os.name", os_name):
            spec.loader.exec_module(mod)
        return mod

    def test_non_posix_exits_cleanly_before_fcntl_dependent_imports(self):
        with self.assertRaises(SystemExit) as ctx:
            self._load_under(_NON_POSIX)
        self.assertEqual(ctx.exception.code, 0, "非 POSIX では exit 0 で抜けること")

    def test_non_posix_guard_runs_before_state_module_is_imported(self):
        """ガードが `state` (fcntl 依存) の import より前で発火していることを直接確認する。

        `os.name` を差し替えずに単に例外を捕まえるだけだと、guard が
        `from _common import ...` の後ろに紛れ込んでいても偶然 SystemExit で
        気付かないケースを見逃す。`sys.modules` に state 由来のモジュールが
        一切登録されていないことまで見る。
        """
        _purge_hook_modules()
        with self.assertRaises(SystemExit):
            self._load_under(_NON_POSIX)
        self.assertNotIn(
            "state", sys.modules, "state (fcntl 依存) が import されてしまっている"
        )

    def test_posix_still_loads_normally(self):
        """回帰: POSIX (テスト環境) では従来どおり最後まで import できること。"""
        mod = self._load_under("posix")
        self.assertTrue(hasattr(mod, "main"))
        self.assertIn("state", sys.modules)


if __name__ == "__main__":
    unittest.main()
