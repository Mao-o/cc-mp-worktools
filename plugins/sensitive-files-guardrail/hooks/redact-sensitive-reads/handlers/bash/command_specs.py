"""Bash operand scan 用のコマンド別 option 知識 (0.22.0 新設)。

``operand_lexer._find_path_candidates`` が token 列を「option / option の値 /
positional / redirect」に字句分けするときの **宣言的なテーブル** だけを置く。
仕組み (字句分け・pattern 枠・prefix 省略の解決) は ``operand_lexer.py`` 側。
設計の経緯と「spec に無いものは 0.21.x の規則のまま」の原則も同モジュールの
docstring を参照。

このモジュールは副作用なし・plugin 状態非依存。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from handlers.bash.grep_extract import _GREP_FIRST_TOKENS
from handlers.bash.interpreters import _AWK_FIRST_TOKENS, _SED_FIRST_TOKENS


# option の値の種別 (``_CmdSpec.values`` の値文字列の 1 文字 = 値 1 token)。
_VALUE_NON_PATH = "n"   # pattern / 正規表現 / 数値 / 書式 / glob … → 候補から外す
_VALUE_PATH = "p"       # patterns file / ignore file / archive … → 候補に残す
# 種別列の先頭 ``=`` は「密着形 (``--opt=V`` / ``-XV``) のときだけ値を取る」印。
# 値省略可の option (``git log --pretty[=<fmt>]`` / GNU diff ``-U[NUM]`` / ag
# ``-C [LINES]`` / BSD grep ``-C[num]``) に使う。これらを「分離形でも値を取る」と
# 登録すると ``git log --pretty .env -p`` の ``.env`` を値として読み飛ばし、
# 本当の file operand が候補から消える (= 保護の穴) ため。
_ATTACHED_ONLY = "="


def _split_kinds(kinds: str) -> tuple[bool, str]:
    """値の種別列を ``(密着形のみか, 種別列)`` に分ける (``"=n"`` → ``(True, "n")``)。"""
    if kinds.startswith(_ATTACHED_ONLY):
        return True, kinds[1:]
    return False, kinds


@dataclass(frozen=True)
class _CmdSpec:
    """コマンド 1 つ分の option 知識。

    values: option 名 → 値の種別列。1 文字が値 1 token に対応し ``n`` = path では
        ない値 (候補から外す)、``p`` = path の値 (候補に残す)。先頭 ``=`` は
        密着形のみ値を取る印 (``_ATTACHED_ONLY``)。短形 (``-e``) は束ね
        (``-rne PAT``) と密着 (``-ePAT``)、長形 (``--regexp``) は分離と ``=`` 結合、
        および GNU getopt_long の一意な prefix 省略 (``--reg=PAT``) を解釈する。
        **ここに無い option は値を取らない扱い** (0.21.x までの規則)。
    pattern_slot: 第 1 positional が pattern / filter / script であって path では
        ないコマンド (grep 系 / jq / awk / sed) なら True。
    pattern_opts: pattern を option 経由で与える option。token 列のどこかに 1 つ
        でも現れたら positional は全て path (``grep -e PAT file`` /
        ``grep -f FILE file`` / ``sed -e SCRIPT file``)。
    """

    values: dict[str, str] = field(default_factory=dict)
    pattern_slot: bool = False
    pattern_opts: frozenset[str] = frozenset()


def _spec(values: dict[str, str] | None = None, *,
          pattern_opts: tuple[str, ...] | None = None) -> _CmdSpec:
    """``_CmdSpec`` の短い組み立て。

    ``pattern_opts`` を渡す (空 tuple 可) と「第 1 positional は pattern」の
    コマンド、None なら positional は全て path のコマンド。
    """
    return _CmdSpec(
        values=dict(values or {}),
        pattern_slot=pattern_opts is not None,
        pattern_opts=frozenset(pattern_opts or ()),
    )


# 登録規則 (誤登録だけが保護の穴になるため):
# - 「全形で必ず値を取る」option だけを ``n`` / ``p`` で登録する
# - 値省略可の option は ``=n`` (密着形のみ) にするか登録しない
# - GNU / BSD で値の有無が異なる option (BSD grep ``-C[num]`` / BSD sed ``-l``) は
#   BSD 側 (値なし) に合わせる
# - 値の中身を読んで出力・利用する option (patterns file / files-from /
#   password-file / 出力先) は ``p`` (候補に残す)
# 実測: git 2.50 / BSD grep 2.6 / bsdtar 3.5 / jq 1.7 / openrsync / zip 3.0 /
# BSD awk・sed (2026-08-28)。

# --- 検索系 (第 1 positional = pattern) ---
# grep / egrep / fgrep。``-e`` / ``--regexp`` の値は pattern、``-f`` / ``--file``
# の値は patterns file (path、``grep -f .env x.txt`` は .env の中身を pattern
# として読むので deny 維持)。どちらかがあれば positional は全て file。
# ``--include`` / ``--exclude`` / ``--exclude-dir`` の値は glob、``-A`` / ``-B`` /
# ``-m`` は数値、``-d`` / ``-D`` / ``--binary-files`` は ACTION / TYPE、``--label``
# は表示名。``-C`` / ``--context`` は BSD grep で値省略可 (``-C[num]``) なので
# 密着形のみ。``--exclude-from FILE`` (GNU) は path。
_GREP_SPEC = _spec(
    {
        "-e": "n", "--regexp": "n", "-f": "p", "--file": "p",
        "--include": "n", "--exclude": "n", "--exclude-dir": "n",
        "--exclude-from": "p",
        "-A": "n", "--after-context": "n", "-B": "n", "--before-context": "n",
        "-C": "=n", "--context": "=n", "-m": "n", "--max-count": "n",
        "-d": "n", "--directories": "n", "-D": "n", "--devices": "n",
        "--binary-files": "n", "--label": "n",
    },
    pattern_opts=("-e", "--regexp", "-f", "--file"),
)
# ripgrep (clap parser、登録 option は全て必須値)。``--pre COMMAND`` は前処理
# コマンドで path でも pattern でもないため未登録 (``interpreters`` が委譲として
# 別途 ask に倒す)。
_RG_SPEC = _spec(
    {
        "-e": "n", "--regexp": "n", "-f": "p", "--file": "p",
        "-g": "n", "--glob": "n", "--iglob": "n",
        "-t": "n", "--type": "n", "-T": "n", "--type-not": "n",
        "--type-add": "n", "--type-clear": "n",
        "-A": "n", "--after-context": "n", "-B": "n", "--before-context": "n",
        "-C": "n", "--context": "n", "-m": "n", "--max-count": "n",
        "-M": "n", "--max-columns": "n", "-d": "n", "--max-depth": "n",
        "--max-filesize": "n", "-j": "n", "--threads": "n",
        "-E": "n", "--encoding": "n", "-r": "n", "--replace": "n",
        "--color": "n", "--colors": "n", "--sort": "n", "--sortr": "n",
        "--engine": "n", "--context-separator": "n",
        "--field-context-separator": "n", "--field-match-separator": "n",
        "--path-separator": "n", "--regex-size-limit": "n",
        "--dfa-size-limit": "n", "--pre-glob": "n",
        "--ignore-file": "p",
    },
    pattern_opts=("-e", "--regexp", "-f", "--file"),
)
# ag (the silver searcher): pattern は positional のみ (``-e`` 無し、``-f`` は
# ``--follow`` で値を取らない)。``-A`` / ``-B`` / ``-C`` は ``[LINES]`` 省略可
# なので密着形のみ。``-p`` / ``--path-to-ignore`` は ignore file (path)。
_AG_SPEC = _spec(
    {
        "-A": "=n", "--after": "=n", "-B": "=n", "--before": "=n",
        "-C": "=n", "--context": "=n",
        "-G": "n", "--file-search-regex": "n", "-g": "n",
        "--ignore": "n", "--ignore-dir": "n", "-m": "n", "--max-count": "n",
        "--depth": "n", "--pager": "n", "-p": "p", "--path-to-ignore": "p",
        "--color-line-number": "n", "--color-match": "n", "--color-path": "n",
    },
    pattern_opts=(),
)
# ack: ``--match PAT`` で pattern を与える。``-f`` は「PATTERN 無しでファイル名
# だけ出す」flag (値なし) で、あれば positional は全て path。``-C`` /
# ``--context`` は ``[NUM]`` 省略可。``--ignore-file`` の値は filter 式で path
# ではない。``--files-from FILE`` は path。
_ACK_SPEC = _spec(
    {
        "--match": "n",
        "-A": "n", "--after-context": "n", "-B": "n", "--before-context": "n",
        "-C": "=n", "--context": "=n", "-m": "n", "--max-count": "n",
        "-t": "n", "--type": "n", "--type-add": "n", "--type-set": "n",
        "--ignore-dir": "n", "--ignore-file": "n", "--pager": "n",
        "--output": "n", "--color-filename": "n", "--color-match": "n",
        "--color-lineno": "n", "--files-from": "p",
    },
    pattern_opts=("--match", "-f"),
)
# git grep: ``-e PAT`` / ``-f FILE`` は grep と同じ。``-O[<pager>]`` は値省略可
# なので未登録。
_GIT_GREP_SPEC = _spec(
    {
        "-e": "n", "-f": "p",
        "-A": "n", "--after-context": "n", "-B": "n", "--before-context": "n",
        "-C": "n", "--context": "n", "--max-depth": "n", "--threads": "n",
    },
    pattern_opts=("-e", "-f"),
)
# jq: 第 1 positional は filter。``-f`` / ``--from-file FILE`` なら filter は
# ファイルから読み positional は全て入力 JSON (path)。``--arg NAME VALUE`` /
# ``--argjson NAME JSON`` は値 2 つとも non-path、``--slurpfile NAME FILE`` /
# ``--rawfile NAME FILE`` は 2 つ目が path (中身を変数に読む)。
_JQ_SPEC = _spec(
    {
        "-f": "p", "--from-file": "p",
        "--arg": "nn", "--argjson": "nn",
        "--slurpfile": "np", "--rawfile": "np",
        "--indent": "n", "-L": "p",
    },
    pattern_opts=("-f", "--from-file"),
)
# awk 系: 第 1 positional は program。``-f`` / ``--file`` (gawk ``-E`` /
# ``--exec``) なら program はファイルから読み positional は全て入力 file。
# ``-F fs`` / ``-v var=val`` (gawk ``--field-separator`` / ``--assign``) は
# 必須値の option (値は path ではない)。gawk ``-e`` / ``--source`` は program
# text の供給。
_AWK_SPEC = _spec(
    {
        "-f": "p", "--file": "p", "-E": "p", "--exec": "p",
        "-e": "n", "--source": "n",
        "-F": "n", "--field-separator": "n", "-v": "n", "--assign": "n",
    },
    pattern_opts=("-f", "--file", "-E", "--exec", "-e", "--source"),
)
# sed 系: 第 1 positional は script。``-e`` / ``--expression`` / ``-f`` /
# ``--file`` があれば positional は全て入力 file。GNU / BSD で値の有無が異なる
# ``-i`` / ``-l`` は **登録しない** (BSD の値なし ``-l`` を値ありと誤認すると
# script を値として消費し、本当の file operand が script 枠に落ちて allow に
# なる = 保護の穴)。GNU 専用の ``--line-length N`` は BSD に長形が無く誤認
# しようがないので登録する。
_SED_SPEC = _spec(
    {
        "-e": "n", "--expression": "n", "-f": "p", "--file": "p",
        "--line-length": "n",
    },
    pattern_opts=("-e", "--expression", "-f", "--file"),
)

# --- git (サブコマンド単位。positional = rev / pathspec = path) ---
# log / show / whatchanged / rev-list: pickaxe ``-S<string>`` / ``-G<regex>``、
# commit フィルタ (``--grep`` / ``--author`` / ``--committer`` / ``--since`` 等)、
# 出力書式 (``--date`` / ``--encoding``) と diff option。``--format`` は ``=``
# 必須 (``git log --format oneline`` は "unrecognized argument")、``--pretty`` は
# 値省略可 (``git log --pretty oneline`` は oneline を path 扱い) なので密着形
# のみ。``-L <range>:<file>`` は値に file を含み行範囲の履歴 (= 内容) を出す、
# ``-O <orderfile>`` / ``--output <file>`` は path、なので候補に残す。``-m`` は
# log では値を取らない (commit の ``-m`` と別物) ので登録しない。
_GIT_LOG_VALUES: dict[str, str] = {
    "-S": "n", "-G": "n", "--grep": "n", "--author": "n", "--committer": "n",
    "--format": "=n", "--pretty": "=n", "--date": "n", "--encoding": "n",
    "--since": "n", "--until": "n", "--after": "n", "--before": "n",
    "--since-as-filter": "n", "-n": "n", "--max-count": "n", "--skip": "n",
    "--min-parents": "n", "--max-parents": "n", "--grep-reflog": "n",
    "--diff-filter": "n", "-I": "n", "--word-diff-regex": "n", "--anchored": "n",
    "--src-prefix": "n", "--dst-prefix": "n", "--line-prefix": "n",
    "--output-indicator-new": "n", "--output-indicator-old": "n",
    "--output-indicator-context": "n",
    "-L": "p", "-O": "p", "--output": "p",
}
_GIT_LOG_SPEC = _spec(_GIT_LOG_VALUES)
# diff 系: log の diff option 部分のみ (``-n`` / ``--grep`` 等は無い)。
_GIT_DIFF_SPEC = _spec({
    k: v for k, v in _GIT_LOG_VALUES.items()
    if k in {
        "-S", "-G", "-I", "--diff-filter", "--word-diff-regex", "--anchored",
        "--src-prefix", "--dst-prefix", "--line-prefix",
        "--output-indicator-new", "--output-indicator-old",
        "--output-indicator-context", "-O", "--output",
    }
})
# shortlog: ``-n`` は ``--numbered`` (flag) なので log と別。``--format`` は省略可。
_GIT_SHORTLOG_SPEC = _spec({
    "-S": "n", "-G": "n", "--grep": "n", "--author": "n", "--committer": "n",
    "--since": "n", "--until": "n", "--after": "n", "--before": "n",
    "--format": "=n",
})
# commit / tag / merge / stash: message 系 (``-m`` / ``--trailer`` / ``--author``
# / ``--date``)。``-F FILE`` / ``-t FILE`` / ``--pathspec-from-file`` は中身を
# 使う path。``-S[<keyid>]`` / ``--gpg-sign[=<keyid>]`` は値省略可。
_GIT_COMMIT_SPEC = _spec({
    "-m": "n", "--message": "n", "--author": "n", "--date": "n", "--trailer": "n",
    "-C": "n", "--reuse-message": "n", "-c": "n", "--reedit-message": "n",
    "--fixup": "n", "--squash": "n", "--cleanup": "n",
    "-S": "=n", "--gpg-sign": "=n",
    "-F": "p", "--file": "p", "-t": "p", "--template": "p",
    "--pathspec-from-file": "p",
})
_GIT_TAG_SPEC = _spec({
    "-m": "n", "--message": "n", "--format": "n", "--sort": "n",
    "-u": "n", "--local-user": "n", "--cleanup": "n",
    "-F": "p", "--file": "p",
})
_GIT_BRANCH_SPEC = _spec({
    "--format": "n", "--sort": "n", "-u": "n", "--set-upstream-to": "n",
})
_GIT_MERGE_SPEC = _spec({
    "-m": "n", "--message": "n", "-s": "n", "--strategy": "n",
    "-X": "n", "--strategy-option": "n", "--into-name": "n", "--cleanup": "n",
    "-S": "=n", "--gpg-sign": "=n", "-F": "p", "--file": "p",
})
_GIT_STASH_SPEC = _spec({
    "-m": "n", "--message": "n", "--pathspec-from-file": "p",
})
_GIT_FOR_EACH_REF_SPEC = _spec({
    "--format": "n", "--sort": "n", "--count": "n", "--points-at": "n",
})

# --- archive / sync / diff (positional = path) ---
# tar: ``--exclude PATTERN`` は glob、``-X`` / ``-T`` (exclude-from / files-from)
# と ``-f`` (archive) / ``-C`` (directory) は path。``-s`` は bsdtar (置換
# regex) と GNU (``--preserve-order`` flag) で異なるので未登録。
_TAR_SPEC = _spec({
    "--exclude": "n", "-X": "p", "--exclude-from": "p",
    "-T": "p", "--files-from": "p",
    "--strip-components": "n", "--transform": "n", "--xform": "n",
    "--mode": "n", "--owner": "n", "--group": "n", "--mtime": "n",
    "--exclude-ignore": "n", "--exclude-ignore-recursive": "n",
    "-f": "p", "--file": "p", "-C": "p", "--directory": "p",
})
# rsync: filter 系 (``--exclude`` / ``--include`` / ``-f`` / ``--filter``) は
# pattern / rule、``-e`` / ``--rsh`` / ``--rsync-path`` はコマンド、
# ``*-from`` / ``--log-file`` / ``--password-file`` / ``*-dest`` / ``*-dir`` は path。
_RSYNC_SPEC = _spec({
    "--exclude": "n", "--include": "n", "-f": "n", "--filter": "n",
    "--chmod": "n", "--chown": "n", "--timeout": "n", "--contimeout": "n",
    "--bwlimit": "n", "-e": "n", "--rsh": "n", "--rsync-path": "n",
    "-M": "n", "--remote-option": "n", "--out-format": "n",
    "--log-file-format": "n", "--suffix": "n", "-B": "n", "--block-size": "n",
    "--max-size": "n", "--min-size": "n", "--max-delete": "n", "--port": "n",
    "--exclude-from": "p", "--include-from": "p", "--files-from": "p",
    "--log-file": "p", "--password-file": "p", "-T": "p", "--temp-dir": "p",
    "--compare-dest": "p", "--copy-dest": "p", "--link-dest": "p",
    "--partial-dir": "p", "--backup-dir": "p",
})
# zip / unzip: ``-x`` / ``-i`` は名前 pattern (zip は複数取るが 1 つ目だけ
# 消費。2 つ目以降は glob なら ask_or_allow、literal なら候補に残る = 保守側)。
# ``-b`` / ``-O`` / ``-d`` は path、``-P`` は password 文字列。
_ZIP_SPEC = _spec({
    "-x": "n", "-i": "n", "-b": "p", "-O": "p", "--output-file": "p",
    "-P": "n", "--password": "n", "-n": "n", "-t": "n",
})
_UNZIP_SPEC = _spec({"-x": "n", "-d": "p", "-P": "n"})
# diff: ``-I`` / ``-x`` / ``-S`` / ``-F`` は regex / pattern / 名前、``-X`` /
# ``--from-file`` / ``--to-file`` は path。``-U`` / ``-C`` は GNU で ``[NUM]``
# 省略可 (BSD は必須) なので密着形のみ。
_DIFF_SPEC = _spec({
    "-I": "n", "--ignore-matching-lines": "n", "-x": "n", "--exclude": "n",
    "-X": "p", "--exclude-from": "p", "-S": "n", "--starting-file": "n",
    "-F": "n", "--show-function-line": "n", "--from-file": "p", "--to-file": "p",
    "-W": "n", "--width": "n", "--tabsize": "n", "--label": "n",
    "--horizon-lines": "n", "--line-format": "n", "--old-line-format": "n",
    "--new-line-format": "n", "--unchanged-line-format": "n",
    "-U": "=n", "--unified": "=n", "-C": "=n", "--context": "=n",
})

_SPECS: dict[str, _CmdSpec] = {}
for _tok in _GREP_FIRST_TOKENS:
    _SPECS[_tok] = _GREP_SPEC
for _tok in _AWK_FIRST_TOKENS:
    _SPECS[_tok] = _AWK_SPEC
for _tok in _SED_FIRST_TOKENS:
    _SPECS[_tok] = _SED_SPEC
del _tok
_SPECS.update({
    "rg": _RG_SPEC, "ag": _AG_SPEC, "ack": _ACK_SPEC, "jq": _JQ_SPEC,
    "tar": _TAR_SPEC, "bsdtar": _TAR_SPEC, "gtar": _TAR_SPEC,
    "rsync": _RSYNC_SPEC, "zip": _ZIP_SPEC, "unzip": _UNZIP_SPEC,
    "diff": _DIFF_SPEC,
    # git はサブコマンド単位 (``git <sub>`` を key にする)
    "git grep": _GIT_GREP_SPEC,
    "git log": _GIT_LOG_SPEC, "git show": _GIT_LOG_SPEC,
    "git whatchanged": _GIT_LOG_SPEC, "git rev-list": _GIT_LOG_SPEC,
    "git diff": _GIT_DIFF_SPEC, "git diff-tree": _GIT_DIFF_SPEC,
    "git diff-index": _GIT_DIFF_SPEC, "git diff-files": _GIT_DIFF_SPEC,
    "git shortlog": _GIT_SHORTLOG_SPEC, "git commit": _GIT_COMMIT_SPEC,
    "git tag": _GIT_TAG_SPEC, "git branch": _GIT_BRANCH_SPEC,
    "git merge": _GIT_MERGE_SPEC, "git stash": _GIT_STASH_SPEC,
    "git for-each-ref": _GIT_FOR_EACH_REF_SPEC,
})
