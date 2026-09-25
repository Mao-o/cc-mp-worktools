"""テストパッケージ初期化。

単体テスト指定 (`python3 -m unittest tests.test_x`) では `tests/` 自体が
sys.path に入らず `import _testutil` が解決できないため、ここで足す。
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
