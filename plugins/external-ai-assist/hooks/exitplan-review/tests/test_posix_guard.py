"""Windows (`os.name != "posix"`) では他モジュールの import より前に exit 0 すること。

`_common/flock.py` が `fcntl` を直接 import しており、Windows には `fcntl` が存在しない
ため `from _common import flock` をそのまま実行すると ImportError で hook error 通知に
なる (内部バックログ)。`__main__.py` は `os` / `sys` 以外の import より前に `os.name`
を判定して抜ける。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_ENTRY_PATH = Path(__file__).resolve().parent.parent / "__main__.py"

# ガードの条件は `os.name != "posix"` なので、値そのものは "posix" 以外なら何でもよい。
# 実際の Windows 値である "nt" は使わない: Python 3.12+ の `pathlib.Path()` は生成時に
# `os.name` を見て `WindowsPath` / `PosixPath` を選ぶため、"nt" を mock すると
# `cursor.py` の module レベル `Path(__file__)` (ガードとは無関係) が `WindowsPath` を
# 選んでしまい、Python 3.14 ではこの POSIX 機上での `WindowsPath` インスタンス化が
# `UnsupportedOperation` で落ちる (未修正コードでの負テスト実行時に実測)。"java"
# (Jython の伝統的な `os.name` 値) は pathlib の特別扱いに触れないため、ガードの
# 条件だけを狙い撃ちできる。
_NON_POSIX = "java"


class TestPosixGuard(unittest.TestCase):
    def _load_under(self, os_name: str):
        spec = importlib.util.spec_from_file_location("exitplan_review_posix_probe", _ENTRY_PATH)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        with mock.patch("os.name", os_name):
            spec.loader.exec_module(mod)
        return mod

    def test_non_posix_exits_cleanly_before_flock_import(self):
        with self.assertRaises(SystemExit) as ctx:
            self._load_under(_NON_POSIX)
        self.assertEqual(ctx.exception.code, 0, "非 POSIX では exit 0 で抜けること")

    def test_non_posix_guard_runs_before_flock_module_is_imported(self):
        """ガードが `from _common import flock` より前で発火していることを直接確認する。

        `_common.flock` 自体の sys.modules 登録は `from pkg import name` 形の import が
        `pkg` オブジェクトの既存属性を再利用するため (一度でも import された後は
        `sys.modules` から pop しても再登録されない) 検証に使えない。代わりに、
        `from _common import flock` の**後**にある `import cursor` / `import codex`
        (トップレベル import で pop の効果が確実に効く) が実行されていないことを見る。
        """
        sys.modules.pop("cursor", None)
        sys.modules.pop("codex", None)
        with self.assertRaises(SystemExit):
            self._load_under(_NON_POSIX)
        self.assertNotIn("cursor", sys.modules, "cursor.py が import されてしまっている")
        self.assertNotIn("codex", sys.modules, "codex.py が import されてしまっている")

    def test_posix_still_loads_normally(self):
        """回帰: POSIX (テスト環境) では従来どおり最後まで import できること。"""
        sys.modules.pop("cursor", None)
        sys.modules.pop("codex", None)
        mod = self._load_under("posix")
        self.assertTrue(hasattr(mod, "main"))
        self.assertIn("cursor", sys.modules)
        self.assertIn("codex", sys.modules)


if __name__ == "__main__":
    unittest.main()
