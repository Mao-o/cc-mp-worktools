"""pytest 実行時の sys.path 整備 (unittest discover 経由でも invariant として動く)。

unittest では各テストファイルが ``from _testutil import FIXTURES`` を先頭で呼ぶため
``_testutil.py`` 側でも同じ sys.path 挿入を行っている。このファイルは pytest 実行時
(`pytest hooks/redact-sensitive-reads/tests/`) に対応するための重複セーフティ。

内部バックログ: ``SFG_LOG_PATH`` の設定 (``core.logging`` がテスト実行で実ログを
汚染しないための差し替え口、``core/logging.py`` / ``_testutil.py`` 参照) も同じ
理由でここに重複させる。pytest は conftest.py をテスト module の import より先に
読むため、``_testutil.py`` より確実に早く効く。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
for p in (_PKG_DIR, _HOOKS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

if "SFG_LOG_PATH" not in os.environ:
    _tmp_log_dir = tempfile.mkdtemp(prefix="sfg-test-logs-")
    os.environ["SFG_LOG_PATH"] = str(Path(_tmp_log_dir) / "redact-hook.log")
    atexit.register(shutil.rmtree, _tmp_log_dir, ignore_errors=True)
