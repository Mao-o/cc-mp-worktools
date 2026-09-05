"""``python -m unittest tests.<module>`` (単体指定) でも共有ヘルパーが
解決されるようにする sys.path 整備。

各テストファイルは先頭で ``import _testutil`` する慣例だが、``_testutil``
は ``tests/`` 直下にある単なるモジュールで、bare import (``import _testutil``)
で解決するには ``tests/`` ディレクトリ自体が sys.path に載っている必要がある。
``unittest discover tests`` はディレクトリ探索の top-level directory として
``tests/`` 自体を検索パスに足すため問題にならないが、
``python -m unittest tests.test_checker`` のようにモジュールパスを直接
指定すると通常の import 機構 (``tests`` パッケージ経由) が使われ、``tests/``
自体は検索パスに入らず ``ModuleNotFoundError: _testutil`` になる
(内部バックログで実測済み)。

パッケージの ``__init__.py`` はモジュールパス指定でも import 経路の最初に
必ず実行されるため、ここで ``tests/`` 自体 (``_testutil`` 解決用) と
``check-sensitive-files/`` / ``hooks/`` (``_testutil.py`` が追加で挿入する
のと同じパス、``from core import ...`` 等の解決用) を sys.path に載せる。
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PKG_DIR = _TESTS_DIR.parent
_HOOKS_DIR = _PKG_DIR.parent
for _p in (_TESTS_DIR, _PKG_DIR, _HOOKS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
