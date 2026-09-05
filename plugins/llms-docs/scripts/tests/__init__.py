"""テストパッケージ初期化。

単体テスト指定 (`python3 -m unittest scripts.tests.test_x.Class.method` /
`pytest scripts/tests/test_x.py::Class::method`) では `scripts/tests/`
ディレクトリ自体が sys.path に入らないため、各テストファイルの
`import _loader` (トップレベル import、副作用専用で `scripts/` を sys.path に
通す) が `ModuleNotFoundError: No module named '_loader'` になる。ディレクトリ
探索 (`python3 -m unittest discover scripts/tests`、README 記載の実行手順) は
探索先自体を検索パスに入れるため問題にならず、この非対称が「テストが壊れている」
と誤認されやすい (内部バックログで追跡)。

このファイルで `scripts/tests/` を sys.path に足すことで、呼び出し形式によらず
`_loader` を解決できるようにする。呼び出し側 (CI / 共通テストスクリプト /
README の実行手順) は一切変更しない — 呼び出し側を変える対応は他 plugin の
機械ゲートや CI のテストステップと共有する `unittest discover` の起動形を
壊すため不採用 (内部バックログで判断済み)。

このプラグインの floor は Python 3.11+ (README.md 前提条件) のため、
`_loader.py` と同様この初期化ファイルにも `from __future__ import annotations`
は追加しない (3.9 互換化が目的ではない)。
"""

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
