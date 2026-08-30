"""秘密非混入ログ。

呼出側は第二引数に path / 値 / basename / command 文字列を絶対に渡してはならない。
渡してよいのはエラー種別・関数名・処理時間・classify 結果などの
「公開しても安全な情報」のみ。

0.4.3 で **detail に文字種ホワイトリスト** を導入 (L1)。設計コメントだけで
依存していた呼出側責任の最終防御層として、コード変更時の意図せぬ秘密混入
(path / 値 / basename) を実行時に止める。違反は ``_BAD`` placeholder に
置換してログする。category 側は固定文字列 (caller がハードコード) なので
sanitize 対象外。

0.27.0 (内部バックログ): unittest 実行が実ログを汚染し計測値を誤らせていた
問題への対処として、``SFG_LOG_PATH`` 環境変数で書込み先を差し替えられるように
した。テスト側 (``tests/_testutil.py`` / ``tests/conftest.py``) がこの環境変数を
プロセス起動時に一度だけ tmpdir へ設定する。``LOG_PATH`` はモジュール import
時に 1 回だけ解決するので、環境変数はそれより前 (import 前) に設定されている
必要がある — テスト側の sys.path bootstrap (``_testutil`` を最初に import する
慣例) がこの前提を自然に満たす。``LOG_PATH`` 自体は従来通りモジュール属性の
ままなので、``mock.patch.object(L, "LOG_PATH", ...)`` による個別テストの差し替え
(``tests/test_logging.py``) は影響を受けない。

同じく 0.27.0 (内部バックログ): ログの無制限増加を防ぐ 1 世代ローテーションを
追加した (``MAX_LOG_BYTES`` / ``_rotate_if_needed``)。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path


def _resolve_log_path() -> Path:
    """``LOG_PATH`` の実体を解決する (内部バックログ)。

    ``SFG_LOG_PATH`` 環境変数が設定されていればそれを使う (テスト実行が実ログ
    ``~/.claude/logs/redact-hook.log`` を汚染しないための差し替え口)。未設定なら
    既定のパス。モジュール import 時に 1 回だけ呼ばれる。
    """
    override = os.environ.get("SFG_LOG_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "logs" / "redact-hook.log"


LOG_PATH = _resolve_log_path()

# ログファイルの 1 世代ローテーション閾値 (内部バックログ)。この byte 数を
# 超えて書き込む**前**に ``<LOG_PATH>.1`` へ rename する (直近世代のみ保持)。
# テストが差し替えやすいようモジュール定数にしてある。
MAX_LOG_BYTES = 5 * 1024 * 1024

# detail に許可する文字種 (0.4.3, L1)。
# 既存使用例の最大は 33 文字 (``segment_residual_metachar_lenient``)。
# `:` は ``f"shell_keyword_lenient:{first}"`` のような identifier 連結用。
# `[` `]` は ``_SHELL_KEYWORDS`` の ``[[`` / ``]]`` / ``[`` / ``]`` 用。
# `!` は ``_OPAQUE_WRAPPERS`` の ``!`` (否定) 用 (現状ログに来ないが将来拡張)。
# 長さ 64 で打ち切り (path 文字列等が誤って入ったときの被害を抑える)。
_DETAIL_RE = re.compile(r"^[A-Za-z0-9_:.\-\[\]!]{0,64}$")
_DETAIL_PLACEHOLDER = "_BAD"


def _sanitize_detail(detail: str) -> str:
    """detail を文字種ホワイトリストで通す。違反は ``_BAD`` に置換 (L1)。

    str 以外、長さ超過、許可外文字混入のいずれでも placeholder を返す。
    呼出側の契約 (公開可情報のみ) を破った場合の最終防御。
    """
    if not isinstance(detail, str):
        return _DETAIL_PLACEHOLDER
    if _DETAIL_RE.match(detail):
        return detail
    return _DETAIL_PLACEHOLDER


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _rotate_if_needed(path: Path) -> None:
    """``path`` が ``MAX_LOG_BYTES`` を超えていたら 1 世代ローテーションする
    (内部バックログ)。

    ``<path>.1`` への rename のみ (2 世代目以降は保持せず上書き)。stat / rename
    のいずれの失敗も握りつぶし、そのまま追記を続ける (ログ機構の不具合で hook
    本体の判定を止めない — ``log_error`` / ``log_info`` 全体の契約と同じ)。
    rename 後も同じ file descriptor を開いたまま追記し続ける並行プロセスが
    あれば、その分の行は rename 済みの旧 world (= ``.1`` 側) に残ることがある
    (数行が世代をまたいで混ざる程度で内容は壊れない) — hook は 1 プロセス
    1 呼出で短命なため lock は導入しない。
    """
    try:
        if path.stat().st_size < MAX_LOG_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(path, Path(str(path) + ".1"))
    except OSError:
        pass


def _append(line: str) -> None:
    """1 行をログファイルに追記する (ローテーション込み、内部バックログ)。

    ディレクトリ作成 / ローテーション / 書込みのいずれの失敗も握りつぶす
    (hook の責務ではない)。
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(LOG_PATH)
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass


def log_error(category: str, detail: str = "") -> None:
    """エラーログを記録する。detail は公開可情報のみを想定 (L1 で sanitize)。

    stderr にも category を出力 (Claude Code UI で可視化される)。
    ファイル書込失敗は握りつぶす (hook の責務ではない)。
    """
    safe_detail = _sanitize_detail(detail)
    line = f"{_now()} ERROR {category} {safe_detail}\n".rstrip() + "\n"
    try:
        sys.stderr.write(f"[redact-hook] {category}\n")
    except OSError:
        pass
    _append(line)


def log_info(category: str, detail: str = "") -> None:
    """INFO ログ (stderr には出さない)。detail は公開可情報のみ (L1 で sanitize)。"""
    safe_detail = _sanitize_detail(detail)
    line = f"{_now()} INFO  {category} {safe_detail}\n".rstrip() + "\n"
    _append(line)
