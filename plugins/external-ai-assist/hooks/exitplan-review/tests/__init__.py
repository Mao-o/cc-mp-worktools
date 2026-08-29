"""単体テスト指定でも共有ヘルパー (`_testutil` 等) を解決できるようにする。

`python3 -m unittest discover tests` はディレクトリ探索の起点である `tests/` 自体を
sys.path に足すが、`python3 -m unittest tests.test_x.Class.method` のようなドット
区切りのモジュール指定や pytest のノード ID (`tests/test_x.py::Class::method`) で
1 件だけ実行すると `tests/` 自体は sys.path に乗らない。`_testutil` をトップレベル
名で import しているテストモジュールはこの場合 `ModuleNotFoundError` になる
(bd_092a232e-zh5.33。marketplace 内の hooks/*/tests/ 全 10 ディレクトリで同一の
構成・同一の症状を確認済み)。

パッケージ初期化時に自分のディレクトリを sys.path へ足すことで、呼び出し形に
関係なく解決させる (呼び出し側 — 消化ループの機械ゲートや CI の
`python3 -m unittest discover tests` 起動形 — の変更は不要)。
"""
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
