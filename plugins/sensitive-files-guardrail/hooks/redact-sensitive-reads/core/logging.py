"""秘密非混入ログ。

呼出側は第二引数に path / 値 / basename / command 文字列を絶対に渡してはならない。
渡してよいのはエラー種別・関数名・処理時間・classify 結果などの
「公開しても安全な情報」のみ。

0.4.3 で **detail に文字種ホワイトリスト** を導入 (L1)。設計コメントだけで
依存していた呼出側責任の最終防御層として、コード変更時の意図せぬ秘密混入
(path / 値 / basename) を実行時に止める。違反は ``_BAD`` placeholder に
置換してログする。category 側は固定文字列 (caller がハードコード) なので
sanitize 対象外。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

LOG_PATH = Path.home() / ".claude" / "logs" / "redact-hook.log"

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
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass


def log_info(category: str, detail: str = "") -> None:
    """INFO ログ (stderr には出さない)。detail は公開可情報のみ (L1 で sanitize)。"""
    safe_detail = _sanitize_detail(detail)
    line = f"{_now()} INFO  {category} {safe_detail}\n".rstrip() + "\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass
