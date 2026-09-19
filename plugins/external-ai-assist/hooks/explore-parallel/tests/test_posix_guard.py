"""Windows (`os.name != "posix"`) では他モジュールの import より前に exit 0 すること。

0.11.0 で `state.py` が起動枠の直列化に `fcntl` を使うようになった。`fcntl` は Windows に
存在しないモジュールなので、`import state` をそのまま実行すると ImportError になり、
**Agent ツール呼び出しのたびに hook error 通知**が出る (review 系 2 hook が 0.9.0 で
同じ穴を塞いでいる)。`__main__.py` は `os` / `sys` 以外の import より前に `os.name` を
判定して抜ける。

なお停止処理は 0.10.0 から `os.killpg` + `ps` の POSIX 前提なので、この hook は
それ以前から機能としては POSIX 専用だった (import が落ちるかどうかが変わった)。
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)

_ENTRY_PATH = Path(__file__).resolve().parent.parent / "__main__.py"

# ガードの条件は `os.name != "posix"` なので、値そのものは "posix" 以外なら何でもよい。
# 実際の Windows 値 "nt" は使わない: Python 3.12+ の `pathlib.Path()` は生成時に
# `os.name` を見て `WindowsPath` / `PosixPath` を選ぶため、"nt" を mock すると
# ガードとは無関係な module レベルの `Path(...)` が POSIX 機上で落ちる
# (exitplan-review / post-implementation-review の同名テストと同じ理由)。
_NON_POSIX = "java"


class TestPosixGuard(unittest.TestCase):
    def _load_under(self, os_name: str):
        spec = importlib.util.spec_from_file_location(
            "explore_parallel_posix_probe", _ENTRY_PATH
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        with mock.patch("os.name", os_name):
            spec.loader.exec_module(mod)
        return mod

    def test_non_posix_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            self._load_under(_NON_POSIX)
        self.assertEqual(ctx.exception.code, 0, "非 POSIX では exit 0 で抜けること")

    def test_guard_runs_before_state_module_is_imported(self):
        """ガードが `import state` (= `fcntl` を引き込む側) より前で発火していること。"""
        sys.modules.pop("state", None)
        sys.modules.pop("cursor", None)
        with self.assertRaises(SystemExit):
            self._load_under(_NON_POSIX)
        self.assertNotIn("state", sys.modules, "state.py が import されてしまっている")
        self.assertNotIn("cursor", sys.modules, "cursor.py が import されてしまっている")

    def test_posix_still_loads_normally(self):
        """回帰: POSIX (テスト環境) では従来どおり最後まで import できること。"""
        sys.modules.pop("state", None)
        sys.modules.pop("cursor", None)
        mod = self._load_under("posix")
        self.assertTrue(hasattr(mod, "_main"))
        self.assertIn("state", sys.modules)
        self.assertIn("cursor", sys.modules)


if __name__ == "__main__":
    unittest.main()
