"""テストパッケージ初期化。

単体テスト指定 (`python3 -m unittest tests.test_x.Class.method` /
`pytest tests/test_x.py::Class::method`) では `tests/` ディレクトリ自体が
sys.path に入らないため、各テストファイルの `import _testutil` (トップレベル
import、副作用専用で plugin dir を sys.path に通す) が `ModuleNotFoundError`
になる。ディレクトリ探索 (`python3 -m unittest discover tests`) は探索先自体を
検索パスに入れるため問題にならず、この非対称が「テストが壊れている」と誤認
されやすい (内部バックログ)。

このファイルで `tests/` を sys.path に足すことで、呼び出し形式によらず
`_testutil` を解決できるようにする。呼び出し側 (CI / 共通テストスクリプト /
README の手順) は一切変更しない (呼び出し側を変える対応は他 lane の機械ゲート
や CI のテストステップと共有の起動形を壊すため不採用 — 詳細は内部バックログ)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
