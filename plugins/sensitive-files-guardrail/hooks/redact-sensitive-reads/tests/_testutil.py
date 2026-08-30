"""テスト共通のパス設定とヘルパ。"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# 内部バックログ: unittest 実行が実ログ (~/.claude/logs/redact-hook.log) を
# 汚染し計測値を誤らせる問題への対処。``core.logging`` はモジュール import 時に
# ``SFG_LOG_PATH`` を読んで書込み先を解決する (``core/logging.py`` 参照) ため、
# 各テストファイルが ``core`` 配下を import するより前に、sys.path bootstrap と
# 同じこの場所で 1 回だけ設定する。``unittest discover`` は全テストファイルが
# 先頭で ``from _testutil import FIXTURES`` する慣例 (sys.path 整備が無いと
# ``from core import ...`` が解決できないため) に依存しており、その慣例により
# このモジュールは他のどの hook パッケージ import よりも先に実行される。
# pytest 実行時は ``tests/conftest.py`` が同じガードを持つ (collection が
# テスト module の import より先に走るため、そちらが先に効く)。
if "SFG_LOG_PATH" not in os.environ:
    _tmp_log_dir = tempfile.mkdtemp(prefix="sfg-test-logs-")
    os.environ["SFG_LOG_PATH"] = str(Path(_tmp_log_dir) / "redact-hook.log")
    atexit.register(shutil.rmtree, _tmp_log_dir, ignore_errors=True)
