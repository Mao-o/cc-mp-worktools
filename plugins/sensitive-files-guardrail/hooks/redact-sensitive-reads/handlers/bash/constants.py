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
# 出現したときは residual metachar (= ``>`` ``&`` 等の剥がし残り、0.25.0 から
# ``_live_operator_metachars`` の quote-aware 判定)
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
    # 0.19.0 (2026-08 精査): 属性 / タイムスタンプ操作。出力は無し、または
    # ``-v`` / ``-c`` での変更前後の mode / owner のみで内容は出ない。
    # ``--reference=RFILE`` / ``touch -r RFILE`` は RFILE の mode / owner /
    # timestamp (= metadata) を読むだけで、内容を読む option は存在しない。
    # 両 hook の deny / block reason が次善策として提示する ``chmod 600 .env``
    # / ``touch .env`` を自分で deny していた自己矛盾を解消する。書込み形
    # (``chmod 600 x > .env``) は safe_read 外なので residual metachar が先に
    # 効き従来通り ask_or_allow (echo と同じ)。
    "chmod", "chown", "chgrp", "touch",
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

# git のサブコマンド前 global option のうち、**次の token を値として取る** もの
# (``-C <path>`` / ``-c <name>=<value>`` / ``--git-dir <path>`` /
# ``--work-tree <path>`` / ``--namespace <name>`` / ``--config-env <name>=<envvar>``)。
# ``=`` 結合形 (``--git-dir=x``) と密着形 (``-Cdir`` / ``-ck=v``) は 1 token で
# 完結する。``interpreters._git_global_options`` (サブコマンド特定) と
# ``operand_lexer._git_subcommand_index`` (コマンド別 option 知識の適用位置) が
# 同じ規則でサブコマンドを探すための共有定義 (0.22.0)。
_GIT_GLOBAL_VALUE_OPTS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
})

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
# ``rm`` はこの集合には **入れない**。``--cached`` 付きのみ metadata-only と
# する条件付き判定 (``_GIT_RM_INDEX_ONLY_FLAG``、0.19.0)。
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

# git rm の「index からの除去のみ」flag (0.19.0, 2026-08 精査)。
# ``git rm --cached <path>`` は作業ツリーの実ファイルを消さず内容も出力しない
# (出力は ``rm '<path>'`` の path 文字列のみ) ため metadata-only。両 hook の
# reason が「tracked なら ``git rm --cached`` で untrack」と案内しているのに
# 自分で deny していた自己矛盾を解消する。``--cached`` 無しの plain ``git rm``
# は作業ツリー削除 (破壊操作) のため operand scan → deny 維持。**完全一致のみ**
# 認識 (git 自身は ``--cache`` / ``--cac`` の一意接頭辞も受理するが、省略形の
# 展開は自前実装しない — 保守側に倒れるだけ)。``--`` 以降は pathspec なので
# flag として数えない (``git rm .env -- --cached`` は deny)。
_GIT_RM_INDEX_ONLY_FLAG = "--cached"
# ``--cached`` と共存しても index-only のままで内容も出さない **既知の** long
# option (完全一致)。fail-closed 規則 (Codex review P1): git は long option の
# **一意な接頭辞** を受理する (``--no-cach`` = ``--no-cached`` で後勝ちにより
# 作業ツリーも削除、``--pathspec-from-fil`` = ``--pathspec-from-file`` で operand
# の中身を pathspec として読み ``fatal: pathspec '<行>'`` に echo) ため、危険な
# option を exact-token で deny-list しても省略形がすり抜ける。よって逆に
# 「ここに無い ``--xxx`` が 1 つでもあれば index-only と見なさない」(→ 通常の
# operand scan → 機密 operand なら deny) とする。``--no-*`` 否定形 /
# ``--pathspec-from-file[=<f>]`` / ``--pathspec-file-nul`` / それらの省略形 /
# ``--cached`` の省略形はすべて未知扱い。``-r`` (long form 無し) は
# ``_GIT_RM_SAFE_SHORT_FLAGS`` 側。
_GIT_RM_KNOWN_LONG_OPTS = frozenset({
    "--force", "--dry-run", "--quiet", "--ignore-unmatch", "--sparse",
})
# ``--cached`` と共存しても安全な短縮 flag (束ね ``-rf`` 可)。short option は
# 1 文字なので接頭辞問題は無いが、未知の文字 (``-h`` / ``-x`` 等) を含む束ねは
# 同じく index-only と見なさない (fail-closed)。git rm の短縮 flag は値を取らない。
_GIT_RM_SAFE_SHORT_FLAGS = frozenset("fnrq")

# hard-stop: 動的評価 / 入力リダイレクト / グループ化 — 静的に結果を決められない。
# ``<`` は target 抽出を試みた上で残りを ``ask_or_allow`` に倒す。
# 0.18.0: 判定 (``_has_hard_stop``) は quote-aware になり、**シングルクォート内**
# の該当 char は展開されないため無視する (``\r`` のみクォート内でも hard-stop)。
# 0.25.0: ダブルクォート内は ``_DQ_LIVE_HARD_STOPS`` のみ hard-stop (下記)。
_HARD_STOP_CHARS = frozenset("$`(){}<\r")

# ダブルクォート内でも **展開が生きる** hard-stop char (0.25.0)。
# Bash はダブルクォート内で ``$`` (変数 / コマンド / 算術展開) とバッククォート
# (コマンド置換) だけを解釈し、``(`` ``)`` ``{`` ``}`` ``<`` は literal になる
# (bash 5 実測: ``echo "A%(b)A"`` は literal 出力、無クォートは syntax error)。
# ``--format="%(objectname)"`` / ``"HEAD@{1}"`` のような日常形をシングル
# クォート形 (0.18.0 で緩和済み) と同じ経路に載せるための絞り込み。
_DQ_LIVE_HARD_STOPS = frozenset("$`")

# セグメント内に剥がしきれずに残ると ``ask_or_allow`` する metachar セット。
# 0.25.0: 判定は ``redirects._live_operator_metachars`` が raw segment を
# quote-aware に走査する (クォート内 / エスケープ済みの文字は演算子になれない)。
_SEGMENT_RESIDUAL_METACHARS = frozenset("&|<>")

# 安全リダイレクト: ``/dev/null`` / ``/dev/stderr`` / ``/dev/stdout`` / fd 複製。
# 1 トークン化されたもの (``2>/dev/null`` 等) に一致。
#
# 0.25.0: **演算子部と target 部をフラグメントに分けて 1 か所で定義する**。
# quote-aware 走査 (``redirects._live_operator_metachars``) は「演算子が live
# (クォート外・非エスケープ) か」と「quote removal 後の target が安全か」を
# 別々に判定する必要がある (bash は ``2>"/dev/null"`` を無クォート形と同じ
# リダイレクトとして扱う) が、そこで regex をもう 1 本手書きすると 2 つの文法が
# 必ず drift する。演算子の形 (``>>`` / ``>|`` を **含まない**) はここが唯一の
# 定義で、フラグメントを足し引きすると両経路が同時に変わる。
_SAFE_REDIRECT_OP = r"(?:&|[0-9]+)?>"
_SAFE_REDIRECT_TARGET = r"(?:&[0-9]+|/dev/null|/dev/stderr|/dev/stdout)"
_SAFE_REDIRECT_OP_RE = re.compile(_SAFE_REDIRECT_OP)
_SAFE_REDIRECT_TARGET_RE = re.compile(_SAFE_REDIRECT_TARGET)
_SAFE_REDIRECT_RE = re.compile(
    f"^{_SAFE_REDIRECT_OP}{_SAFE_REDIRECT_TARGET}$"
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
#
# 0.17.0: ``awk`` / ``sed`` を **除外** した。ここに入れる基準は「**operand が
# 静的に file path と判らない**」こと (``bash -c "..."`` / ``eval`` /
# ``python -c`` の引数はコマンド文字列であって path ではない)。awk / sed は
# script 引数の後ろの positional が素直に file operand なので、この基準に
# 当てはまらない。opaque のままだと ``sed -n 1,5p .env`` が autonomous で素通り
# し、DESIGN.md が定める確信 deny 条件 (機密 operand 確定 × 内容出力) と実装が
# 食い違っていた (内部バックログ)。
#
# 副作用 (``sed -i`` の in-place / ``awk 'print > "f"'`` の redirect) への慎重さは
# ``_SAFE_READ_FIRST_TOKENS`` に **入れない**ことで維持する — ``sed s/x/y/ f > out``
# は residual metachar 経由で従来どおり ask_or_allow に倒れる。
#
# 0.18.0 (内部バックログ): ``awk '{print}' .env`` は ``{`` ``}`` ``$`` が
# ``_HARD_STOP_CHARS`` に該当し opaque 判定より **前** の hard-stop で ask に
# 倒れていた (awk 最頻形の穴)。``_has_hard_stop`` を quote-aware
# (シングルクォート内を無視) にして operand scan に到達させ、この穴を塞いだ。
_OPAQUE_WRAPPERS = frozenset({
    "bash", "sh", "zsh", "ksh", "fish", "dash",
    "eval",
    "python", "python3", "node", "ruby", "perl",
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

# 同一コマンド内で「glob が dotfile にも展開される」状態を作る形の検出 (0.22.0、
# Codex R1)。bash の ``shopt -s dotglob`` / ``GLOBIGNORE=<非空>`` (設定されると
# dotglob が暗黙に有効になる)、zsh の ``setopt globdots`` (option 名は大文字小文字
# と ``_`` を無視するので ``GLOB_DOTS`` / ``glob_dots`` も)。shell option は Bash
# tool の呼び出しごとに初期化されるため、同じコマンド文字列に現れる形だけが
# 対象 (profile で常時有効な環境は hook から見えない — docs で開示)。検出は
# 保守的 (``shopt -u dotglob`` や単なる言及でも一致) で、効果は
# ``_glob_operand_is_dotenv_match`` が fnmatch の意味論 (先頭ドットも一致) に
# 戻る = deny 寄りにしか倒れない。
_DOTGLOB_HINT_RE = re.compile(r"dotglob|glob_?dots|GLOBIGNORE=", re.IGNORECASE)
