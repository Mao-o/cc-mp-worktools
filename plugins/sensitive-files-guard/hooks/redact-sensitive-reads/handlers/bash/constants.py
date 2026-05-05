"""Bash handler 用 compile-time 定数 (0.3.3 分解)。

このモジュールは副作用なし・plugin 状態非依存。regex / frozenset / 文字列定数のみ。
定義は ``bash_handler.py`` から import される。
"""
from __future__ import annotations

import re

# 0.3.1 以降、通常コマンドと未知コマンドを区別せず全て同じ operand 判定に通す
# ため、このセットは **ドキュメント目的** でのみ保持している。処理ロジックからは
# 参照しない。
_SAFE_READ_CMDS = frozenset({
    "cat", "less", "more", "head", "tail", "bat", "view",
    "nl", "tac",
})
_SOURCE_CMDS = frozenset({"source", "."})

# hard-stop: 動的評価 / 入力リダイレクト / グループ化 — 静的に結果を決められない。
# ``<`` は target 抽出を試みた上で残りを ``ask_or_allow`` に倒す。
_HARD_STOP_CHARS = frozenset("$`(){}<\r")

# セグメント内に剥がしきれずに残ると ``ask_or_allow`` する metachar セット。
_SEGMENT_RESIDUAL_METACHARS = frozenset("&|<>")

# 安全リダイレクト: ``/dev/null`` / ``/dev/stderr`` / ``/dev/stdout`` / fd 複製。
# 1 トークン化されたもの (``2>/dev/null`` 等) に一致。
_SAFE_REDIRECT_RE = re.compile(
    r"^(?:&|[0-9]+)?>(?:&[0-9]+|/dev/null|/dev/stderr|/dev/stdout)$"
)
# 空白区切りで分割されたリダイレクト前半 (``2>`` + ``/dev/null`` 等) を扱うための受け皿。
_REDIRECT_OP_TOKENS = frozenset({">", "1>", "2>", "&>"})
_SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "/dev/stderr", "/dev/stdout"})

# opaque wrapper: 静的解析不能。``ask_or_allow`` (default=ask, auto/bypass=allow)。
# ``time`` ``!`` ``exec`` は 0.3.2 で _SHELL_KEYWORDS から移動 (shell 文法要素 /
# プロセス置換挙動として opaque 扱いに統一)。
# 0.8.0 で ``env`` / ``command`` / ``builtin`` / ``nohup`` (透過 prefix だった
# もの) もここに統合し、prefix normalize 経路を撤廃した。``FOO=1 cat .env``
# のような env-assignment prefix は ``_ENV_PREFIX_RE`` で別途検出する (思想 1
# = うっかり露出予防、敵対的防御は非目的)。
_OPAQUE_WRAPPERS = frozenset({
    "bash", "sh", "zsh", "ksh", "fish", "dash",
    "eval",
    "python", "python3", "node", "ruby", "perl",
    "awk", "sed",
    "xargs", "parallel",
    "sudo", "doas",
    "exec",   # ``exec -a name cmd`` 等プロセス置換系
    "time",   # pipeline 前置 / shell keyword 的挙動
    "!",      # 否定: ``! cat .env`` で後続を実行
    "env",    # 0.8.0: option/assignment 含む形を一律 opaque
    "command",  # 0.8.0: option 含む形を一律 opaque
    "builtin",  # 0.8.0
    "nohup",    # 0.8.0
})

# シェル予約語 / 制御構文: 第 1 トークンがこれらなら ``ask_or_allow``。
# segment split を挟むと ``do cat .env`` ``then cat .env`` のような制御構文本体
# セグメントが未知コマンド扱いで allow される bypass を塞ぐ。
# ``time`` / ``!`` / ``exec`` は ``_OPAQUE_WRAPPERS`` 側に移動 (0.3.2)。
_SHELL_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "fi",
    "for", "while", "until", "do", "done",
    "case", "esac", "select",
    "function", "coproc",
    "[[", "]]", "[", "]",
})

# glob 文字: operand にこれらが含まれると bash の pathname expansion 対象。
_GLOB_CHARS = frozenset("*?[")

# 環境変数プレフィクス: ``FOO=1 cmd`` 形式の第 1 トークン検出用 (0.8.0 で
# 透過剥がしを撤廃したため、この regex は「第一トークンが env-assignment 形式
# なら opaque 扱い」の判定で 1 回だけ使う)。
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
