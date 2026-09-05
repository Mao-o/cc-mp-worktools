"""tests パッケージの初期化。

各テストモジュールは共有ヘルパー ``_testutil`` を ``import _testutil`` という
トップレベル名で読み込む（sys.path 整備の副作用のみが目的、``# noqa: F401``）。
``python3 -m unittest discover tests`` はディレクトリ探索が ``tests/`` 自体を
sys.path に追加するため解決できるが、``python3 -m unittest tests.test_cli`` の
ようなモジュールパス指定や ``pytest`` 実行では ``tests/`` 自体は sys.path に
乗らず ``ModuleNotFoundError: No module named '_testutil'`` になる。

ここで自分自身のディレクトリ (``tests/``) を sys.path に足すことで、
どの起動経路でも ``import _testutil`` を解決できるようにする。
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
