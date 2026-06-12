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

# 0.12.0: read-only first_token allow-list。
# 「副作用なしの見る・数える系」だけを集める。これらが segment の first_token に
# 出現したときは ``_segment_has_residual_metachar`` (= ``>`` ``&`` 等の剥がし残り)
# / ``_OPAQUE_WRAPPERS`` / ``_SHELL_KEYWORDS`` による ask 経路を **スキップして
# operand scan に直行** する (ask_or_allow ではなく operand scan 結果のみで決定)。
#
# 思想 1 (うっかり露出予防が目的、敵対的防御は非目的) を維持しつつ、ユーザーが
# 日常的に使う調査用ワンライナー (``grep foo > /tmp/out``, ``ls > listing.txt``,
# ``grep foo file | wc -l`` 等) を ask に倒さないための allow-list。
# operand 機密一致は依然 **deny 固定** (例: ``grep foo > .env`` は ``.env`` が
# operand に拾われて deny)。hard-stop (``$(...)`` / backtick / heredoc / ``<``)
# は依然 ``ask_or_allow`` (segment 全体が静的解析不能なため)。
#
# 入れないコマンド (副作用持つ可能性):
# - ``awk``: ``print > "/p"`` で redirect、``-f`` で任意 script 実行
# - ``sed``: ``-i`` で in-place 書換
# - ``find``: ``-delete`` / ``-exec`` で副作用
# - ``xargs`` / ``parallel``: 任意コマンド実行 (opaque wrapper 経路維持)
# - ``cut`` / ``sort`` / ``uniq`` / ``tr``: read-only だが副作用判別 (`tee` のような書込み
#   経路) が ambiguous なため一旦保留
#
# 注意: ``_OPAQUE_WRAPPERS`` / ``_SHELL_KEYWORDS`` とは **disjoint** (両方に含まれる
# ことはない)。``_SAFE_READ_FIRST_TOKENS`` ヒットなら opaque / keyword 判定は不要
# (短絡)。
_SAFE_READ_FIRST_TOKENS = frozenset({
    "ls",
    "cat", "head", "tail", "nl", "tac",
    "bat", "less", "more", "view",
    "wc",
    "file", "stat", "du", "df", "tree",
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "od", "xxd", "hexdump",
})

# 0.14.0: metadata-only first_token allow-list (離脱分析 G2)。
# 「operand の **内容** を stdout に出さない」コマンド群。機密 path が operand に
# 居ても、出力されるのはファイル名・属性・件数・パス文字列だけで値は LLM
# コンテキストに載らないため、operand scan を **スキップして allow** に倒す。
#
# 2026-05 の離脱分析 (transcript 実測) で、実 deny 15 件のうち
# ``find -name X`` / ``ls -la X`` / ``git check-ignore X`` のような所在・属性
# 確認が 1/3 を占めた。これらは思想 1 (うっかり **露出** 予防) の射程外 —
# 値がコンテキストに載らない操作を止めても予防効果がなく、ユーザー離脱
# (plugin 無効化) だけが起きる。
#
# 含めないもの:
# - ``cat`` / ``head`` / ``tail`` / ``grep`` / ``od`` 等: 内容を出力する (deny 維持)
# - ``cp`` / ``mv``: 内容は出ないが別 path への複製で漏洩面が広がる (deny 維持)
# - ``md5`` / ``shasum`` 等: 値の fingerprint が出る (保守的に対象外)
# - ``find``: 単体集合には **入れない**。``-exec cat`` 等で内容を出力できるため
#   条件付き判定 (``_is_metadata_only`` で ``_FIND_DANGEROUS_ACTIONS`` を検査)。
#
# ``echo`` / ``printf`` は引数文字列をそのまま出すだけでファイルを開かない
# (``echo .env`` は ".env" という 4 文字を出力するだけ)。``echo KEY=val > .env``
# のような書込み形は ``>`` が residual metachar として **この判定より先に**
# ask_or_allow へ倒れるため (echo / printf は ``_SAFE_READ_FIRST_TOKENS`` 外)、
# metadata-only 扱いにしても書込み経路は緩まない。
# 判定順序: opaque → residual metachar (非 safe_read のみ) → shell keyword
# → metadata-only → operand scan。
_METADATA_ONLY_FIRST_TOKENS = frozenset({
    # ファイル一覧 / 存在・属性確認 (出力は名前・サイズ・時刻・型のみ)
    "ls", "tree", "stat", "file", "du", "df", "test",
    # 計数のみ (内容そのものは出ない)
    "wc",
    # パス文字列の操作 / 解決 (出力は path 文字列のみ)
    "basename", "dirname", "realpath", "readlink",
    # 引数をそのまま表示 (ファイルを開かない)
    "echo", "printf",
})

# find のうち「内容出力・副作用」を伴うアクション (0.14.0, Codex P1 対応)。
# これらを **1 つでも含む** find は metadata-only から除外し、operand scan に
# 倒す (機密 operand があれば deny)。``find . -name .env -exec cat .env ';'`` は
# ``;`` がクォートされ segment 分割も hard-stop も回避して単一 segment で
# ここに到達するが、``cat`` を実行して .env の内容を stdout に出すため危険。
# - ``-exec`` / ``-execdir`` / ``-ok`` / ``-okdir``: 任意コマンド実行 (cat で露出)
# - ``-delete``: 破壊的
# - ``-fprint`` / ``-fprint0`` / ``-fprintf`` / ``-fls``: ファイル書込み (副作用)
# stdout への metadata 出力 (``-print`` / ``-print0`` / ``-printf`` / ``-ls``、
# find の ``%`` 書式はパス・サイズ・時刻のみで内容を含まない) は安全なので除外しない。
_FIND_DANGEROUS_ACTIONS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir",
    "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})

# metadata-only コマンドのうち「operand ファイルの **中身** を別パスのリストと
# して読み、その名前 (= 中身) を stdout / stderr に echo する」オプション
# (0.14.0, Codex P2 第2弾)。これらを含むコマンドは metadata-only から除外して
# operand scan → deny に倒す。find の ``-exec`` と同じ「オプションで内容露出に
# 化ける」クラス。
# - ``file -f FILE`` / ``--files-from FILE``: namefile の各行をファイル名扱いし、
#   ``<行>: cannot open`` 等のエラーに行内容を echo する
# - ``wc --files0-from=F`` / ``du --files0-from=F``: NUL 区切り名を F から読む。
#   dotenv は NUL 区切りでないため全内容を 1 名前として読みエラーに echo
# - ``tree --fromfile``: ディレクトリ一覧をファイルから読み tree 表示で echo
# 値結合形 (``--files0-from=.env`` / ``-f.env``) と分離形 (``-f .env``) 両対応。
# 除外後は operand scan が値を拾って deny する (``_find_path_candidates`` が
# ``--opt=val`` / ``-Xval`` / 分離 operand いずれも候補化するため)。
_METADATA_CONTENT_READING_OPTS: dict[str, frozenset[str]] = {
    "file": frozenset({"-f", "--files-from"}),
    "wc": frozenset({"--files0-from"}),
    "du": frozenset({"--files0-from"}),
    "tree": frozenset({"--fromfile"}),
}

# git の metadata-only subcommand (``git <sub>`` 直書き形のみ認識)。
# ``git -C dir check-ignore`` のような global option 前置形は対象外 (従来通り
# operand scan → deny。保守側に倒す)。``show`` / ``diff`` / ``log`` /
# ``cat-file`` 等の内容出力系は history カテゴリの deny を維持。
#
# ``status`` は **含めない** (0.14.0, Codex P1 第2弾)。``git status -v`` /
# ``--verbose`` が staged 変更の diff (= 機密の旧値/新値) を出力するため、
# ``git status -v -- .env`` で .env の実値が漏れる。``check-ignore -v`` は
# gitignore ルール (source:line:pattern + path) を出すだけでオプションに関わらず
# 内容露出は無いので維持。``ls-files`` は plain 形 (名前のみ) は安全だが
# ``-s`` / ``--stage`` / ``--format`` が blob object name を出すため条件付き
# (``_is_metadata_only`` で ``_git_ls_files_exposes_object`` を検査、後述)。
# ``status`` は ``-v`` という頻出オプションが diff を出し plain 形の価値も低い
# (operand scan で裸 ``git status`` は allow) ため option-gate より allowlist
# 除外が単純。``git status -- .env`` 等 operand 明示形のみ deny。
_GIT_METADATA_SUBCOMMANDS = frozenset({"check-ignore", "ls-files"})

# git ls-files のうち blob object name (= 内容の指紋) を出力するオプション
# (0.14.0, Codex P2 第3弾)。これらを含む ``git ls-files`` は metadata-only から
# 除外して operand scan → deny。``md5`` / ``shasum`` を allowlist 外にしている
# のと同じ「fingerprint は出さない」方針との整合。
# - ``-s`` / ``--stage``: mode + objectname(blob hash) + stage を出力
# - ``--format`` (任意書式): ``%(objectname)`` 等で hash を埋め込めるため一律除外
# plain ``git ls-files .env`` (名前のみ) は allow 維持 (Codex 明示要望、思想 1)。
_GIT_LS_FILES_OBJECT_OPTS = frozenset({"-s", "--stage", "--format"})
# git ls-files の「値を取らない」短縮フラグ (``-sz`` 等の束ね検出用)。
# ``-x`` / ``-X`` は値を取るため除外 (``-x s.env`` を誤検出しないため)。
_GIT_LS_FILES_SHORT_FLAGS = frozenset("cdikmostuvz")

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
