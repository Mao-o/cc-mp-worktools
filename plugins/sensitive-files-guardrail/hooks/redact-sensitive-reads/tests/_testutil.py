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

# Stop hook (check-sensitive-files) 側のモジュール (``checker`` / ``stop_ack``)
# を import したいテストが使う。
_CHECKER_DIR = _HOOKS_DIR / "check-sensitive-files"


def checker_dir_on_path() -> Path:
    """``check-sensitive-files`` を ``sys.path`` の **末尾** に足して返す。

    **``sys.path.insert(0, ...)`` にしてはいけない** (0.31.0、内部バックログ)。
    両 hook はどちらも ``tests`` という名前のパッケージを持つため、Stop 側の
    ディレクトリが先頭に入ると **プロセス全体で ``tests`` の解決先が入れ替わる**。
    ``sys.path`` の変更はプロセス終了まで残るので、以降に走るテストが巻き込まれる:

    - ``tests/test_logging.py`` の並行ローテーションテストは ``multiprocessing``
      の **spawn** 子プロセスを使う。spawn は target 関数を「モジュール名 +
      関数名」で pickle して子で import し直すため、子が ``tests.test_logging``
      を import できず ``ModuleNotFoundError`` になる (親の ``sys.path`` を
      そのまま受け継ぐので、汚染も受け継ぐ)
    - 実測 (0.30.0): ``python3 -m unittest tests.test_shared_import
      tests.test_logging`` で **100% 再現**。``unittest discover`` では
      アルファベット順に ``test_logging`` が先に走るので再現せず、flaky に見えていた

    末尾に足せば、自 hook の ``tests`` が常に先に解決される。``checker`` /
    ``stop_ack`` は名前が一意なので import には影響しない。
    hook 本体の ``sys.path`` 操作 (``check-sensitive-files/__main__.py``) は
    別プロセスで動く本番経路なので触らない。
    """
    if str(_CHECKER_DIR) not in sys.path:
        sys.path.append(str(_CHECKER_DIR))
    return _CHECKER_DIR

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ---------------------------------------------------------------------------
# エンコーディング形状の fixture (0.31.0、内部バックログ)
#
# BOM / UTF-16 のテストが 1 件も無かったため、BOM 付き ``.env`` で先頭 1 鍵が
# 黙って消える / UTF-16 ``.env`` が「空ファイル」と報告される不具合が
# 1,200 件超のテストを通過していた。**バイト列は commit せず、1 本の UTF-8
# ソースからテスト実行時に再エンコードする** — `fixtures/keys/README.md` と
# 同じ方針で、クレデンシャル形状のバイナリを公開 repo に置かないため
# (`fixtures/encodings/README.md` 参照)。
# ---------------------------------------------------------------------------

# 3 鍵の dotenv (値はすべて明示的なダミー)。先頭鍵が BOM の影響を受けるので、
# 「1 鍵目が消えたか」を検証できるように 1 鍵目にも意味のある名前を置く。
ENCODING_SAMPLE_DOTENV = (
    "DATABASE_URL=postgres://dummy-user:dummy-pass@127.0.0.1/dummydb\n"
    "JWT_SECRET=dummydummydummydummydummydummydummy\n"
    "STRIPE_KEY=dummy_0000000000000000000000\n"
)
ENCODING_SAMPLE_DOTENV_KEYS = ("DATABASE_URL", "JWT_SECRET", "STRIPE_KEY")

# 同じ鍵名を持つ YAML / 構造不明形式 (dotenv 以外のパーサを通す用)。
ENCODING_SAMPLE_YAML = (
    "DATABASE_URL: postgres://dummy\n"
    "JWT_SECRET: dummy\n"
    "STRIPE_KEY: dummy\n"
)
# keys-only scan (構造不明形式) 用。0.31.0 の初版は ``ENCODING_SAMPLE_DOTENV``
# の別名で、マトリクスが dotenv と**同一入力**を 2 回通すだけだった
# (隔離内レビュー P3-6)。``[section]`` 見出しと ``KEY : value`` (コロン + 空白)
# という dotenv パーサが扱わない形にして、``keyonly_scan`` の
# ``^\s*(?:export\s+)?KEY\s*[:=]`` 側の経路を実際に通す。鍵名は 3 形式で
# 揃えておく (同じ assert を使い回すため)。
ENCODING_SAMPLE_OPAQUE = (
    "[section]\n"
    "DATABASE_URL : postgres://dummy\n"
    "JWT_SECRET : dummy\n"
    "STRIPE_KEY : dummy\n"
)

# テスト対象のエンコーディング一覧 (``bytes.encode`` に渡せる名前)。
# ``latin-1`` は「BOM も NUL も無いので推定できない」側の代表で、
# 「取りこぼしを開示する」ことだけを期待する。
ENCODINGS_WITH_DETECTION = ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be")


def encode_sample(text: str, encoding: str) -> bytes:
    """``text`` を ``encoding`` で符号化する (BOM 有無はコーデックに委ねる)。

    ``utf-16`` は Python が BOM を付け、``utf-16-le`` / ``utf-16-be`` は付けない
    (= BOM 無し UTF-16 の検出経路を通す)。
    """
    return text.encode(encoding)

# 内部バックログ: unittest 実行が実ログ (~/.claude/logs/redact-hook.log) を
# 汚染し計測値を誤らせる問題への対処。``core.logging`` はモジュール import 時に
# ``SFG_LOG_PATH`` を読んで書込み先を解決する (``core/logging.py`` 参照) ため、
# 各テストファイルが ``core`` 配下を import するより前に、sys.path bootstrap と
# 同じこの場所で 1 回だけ設定する。全 30 テストファイルが先頭で
# ``from _testutil import FIXTURES`` (または副作用目的の ``import _testutil``)
# する慣例になっており (sys.path 整備が無いと ``from core import ...`` が
# 解決できないため)、``unittest discover`` はモジュールをアルファベット順に
# import するので先頭の ``test_bash_handler.py`` が最初にこのモジュールを
# import し、以降の全モジュールより前に ``SFG_LOG_PATH`` が確定する。
# ``-p <pattern>`` で 1 ファイルだけを対象にする単独実行や、この慣例に沿わない
# モジュールを最初に import する実行順では守られない — 全モジュールが個別に
# この import を持つことで単独実行でも保護が効くようにしている。
# pytest 実行時は ``tests/conftest.py`` が同じガードを持つ (collection が
# テスト module の import より先に走るため、そちらが先に効く)。
if "SFG_LOG_PATH" not in os.environ:
    _tmp_log_dir = tempfile.mkdtemp(prefix="sfg-test-logs-")
    os.environ["SFG_LOG_PATH"] = str(Path(_tmp_log_dir) / "redact-hook.log")
    atexit.register(shutil.rmtree, _tmp_log_dir, ignore_errors=True)
