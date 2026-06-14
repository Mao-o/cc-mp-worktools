"""Bash handler 内部の pure helper / compile-time 定数サブパッケージ (0.3.3)。

このパッケージ配下のモジュールは **副作用なし・plugin 内部状態を持たない**
pure function と compile-time 定数のみを提供する。plugin ステートに依存する処理
(``load_patterns`` / ``is_sensitive`` / envelope 操作 / 判定結果の確定) は
``handlers/bash_handler.py`` 側に残す。

既存テストの patch seam (``handlers.bash_handler.X``) を壊さないため、
``bash_handler.py`` はここで定義された symbol を再 export して従来の import path
を維持する。
"""
