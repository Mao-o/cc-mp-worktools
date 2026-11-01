"""Bash コマンド文字列を検証対象候補セグメントに分解するユーティリティ。

- split_on_operators: `&&` / `||` / `;` / `|` / `|&` / 単独の `&` / `\\n` で分割。
  quote / $() / ${} / バッククォート内は保護 (subshell 内の quote も追跡)。
  `2>&1` / `>&` / `&>` は区切りではない。区切りの演算子も要るときは
  split_with_separators
  heredoc (`<<EOF` 〜 `EOF`) の本文は 1 セグメントに取り込んで分割しない。
  bash コメント (unquoted `#` 以降行末まで) は無視
- strip_leading_env: 先頭の `FOO=bar` 形式環境変数割当を剥がす
- strip_transparent_wrappers: 透過的 wrapper (`sudo`, `time`, `nohup`,
  `env`, `command`, `builtin`, `exec`, `npx`, `timeout`, `nice`, `stdbuf`,
  `setsid`, `caffeinate`, `watch`, `xargs`, `pnpm exec`, `pnpm dlx`,
  `mise exec --`, `bun x`) とその直後のフラグ (`sudo -u USER` 等) を剥がす
- extract_candidates: 上記に加えてシェル構文プレフィックス (`(` / `{` /
  `!` / `if`〜`do` 等の予約語 / 先頭リダイレクト)、末尾の `)` `;` `}` `&`、
  コマンド名のパス・`\\` エスケープを正規化し、「コマンドマッチにかける候補
  断片」と、その断片の先頭に書かれていたインライン環境変数
  (`AWS_PROFILE=prod` 等) の dict を返す。検証 subprocess に同じ env を渡して
  コマンド実行時と同条件でアカウント検証するため (剥がすだけで使わない非対称を解消)

参考: liberzon/claude-hooks の smart_approve.py のアプローチをベースに独自実装。
"""
from __future__ import annotations

import contextlib
import re
import shlex

_WRAPPERS_SINGLE = {
    "sudo", "time", "nohup", "command", "builtin", "exec", "npx",
    # v0.9.0 追加。いずれも「後続コマンドを継承 env のまま起動する」実行ラッパで、
    # 剥がさないと mutating コマンド (`timeout 30 gh pr create` 等) が
    # service に match せず検証をすり抜けていた。
    "timeout", "nice", "stdbuf", "setsid", "caffeinate", "watch", "xargs",
}
_WRAPPERS_TWO = {("pnpm", "exec"), ("pnpm", "dlx"), ("bun", "x")}
_WRAPPERS_THREE = {("mise", "exec", "--")}

# --- 透過 wrapper の env 伝播クラス分類 (D16) ---
#
# 各透過 wrapper を「pre-wrapper のインライン env が後続コマンドの実行環境に
# **実際に届くか**」で分類する。届くもの (passthrough) は剥がして env を収集・
# 伝播してよい。届かないもの (conditional_scrub) は誤った env を検証に渡すと
# 「検証 env ≠ 実行 env」の非対称 (= 未承認 profile での false-allow) になるため、
# 個別の補正ロジックが要る。
#
# この dict は **コードの振る舞いそのものではなく分類の宣言**。実際の剥がし/
# 収集は `_strip_one_wrapper` / `_normalize_segment` / `_sudo_preserves_env`
# が行う。dict の役割は (1) 将来 wrapper を `_WRAPPERS_*` に追加する人へ
# 「env 挙動を必ず分類せよ」と促す checklist、(2) テストが
# 「`_WRAPPERS_*` の全要素がこの dict のキーに存在する」ことを assert して
# 未分類 wrapper の混入を検出する guard、の 2 つ。分類の根拠と将来 wrapper
# 追加時のチェックリストは `docs/wrapper-env-audit.md` (D16) を正本とする。
#
# クラス:
#   "passthrough" — 継承 env を素通しする。pre-wrapper env を収集・伝播してよい
#                   (time / nohup / command / exec / npx / pnpm exec /
#                    mise exec -- / bun x)。`command` は外部 CLI を起動する場合も
#                    env を素通すので passthrough。`builtin` は外部 CLI 自体を
#                    起動しない (= service が match しない) ため env 伝播の有無は
#                    無害だが、分類上は素通し側に置く。
#   "conditional_scrub" — 既定では継承 env を scrub するが、フラグ次第で保持する。
#                    pre-wrapper env を無条件には伝播できない。現状 `sudo` のみ
#                    (`-E` / `--preserve-env[=LIST]` があれば保持、無ければ scrub)。
#                    `_sudo_preserves_env` が判定し、scrub 時は pre-sudo env を破棄する。
#
# **`env` は意図的にここに含めない**。`env` は `_strip_one_wrapper` で特別扱い
# され、オプション無し (`env FOO=bar cmd`) のときだけ剥がす。`env -i` (環境リセット)
# / `env -u NAME` (個別 unset) / `env --` は剥がさず opaque のまま残す
# (= セグメントが service に match せず検証もスキップ = 安全側)。env のリセット系を
# wrapper として剥がしてしまうと「実行は空/縮小環境 / 検証は親環境」の非対称に
# なるため、剥がさないことが正しい。詳細は D16 ノートの「env の扱い」節参照。
_WRAPPER_ENV_CLASS = {
    "sudo": "conditional_scrub",
    "time": "passthrough",
    "nohup": "passthrough",
    "command": "passthrough",
    "builtin": "passthrough",
    "exec": "passthrough",
    "npx": "passthrough",
    # v0.9.0 追加分。いずれも「継承 env をそのまま子プロセスへ渡す」実行ラッパ。
    # `timeout` / `xargs` / `stdbuf` / `setsid` は docs/wrapper-env-audit.md の
    # 「特に注意が要る将来 wrapper」表で env 継承として整理済み。`nice` /
    # `caffeinate` はローカル man page (nice(1) / caffeinate(8)) が
    # 「utility を実行する」ラッパであることを明記しており env をフィルタしない。
    # `watch` はコマンドを `sh -c` 経由で繰り返し起動するだけで env は継承する。
    "timeout": "passthrough",
    "nice": "passthrough",
    "stdbuf": "passthrough",
    "setsid": "passthrough",
    "caffeinate": "passthrough",
    "watch": "passthrough",
    "xargs": "passthrough",
    ("pnpm", "exec"): "passthrough",
    ("pnpm", "dlx"): "passthrough",
    ("bun", "x"): "passthrough",
    ("mise", "exec", "--"): "passthrough",
}

# wrapper ごとの flag 分類。**3 分類**を厳密に区別する (PR #48 Codex P1 x2 の教訓):
#
#   1. 値を取らない       — 次の token を消費しない
#   2. 必須引数を取る     — 分離形 (`-u deploy`) でも `=` 形でも値を取る
#                          → `_WRAPPER_FLAGS_WITH_VALUE`
#   3. optional 引数を取る — GNU の long option 規約により **`=` 形でのみ**値を取る。
#                          bare 形 (`--replace`) は次の token を消費しない
#                          → `_WRAPPER_FLAGS_OPTIONAL_VALUE`
#
# 3 を 2 として登録すると `xargs --replace gh pr close {}` が `pr close {}` に
# 正規化され、実際には実行される `gh` が**検証をすり抜ける**。逆に 2 を 1 として
# 扱うと値 token でループが止まり、やはりコマンドを見失う。**どちらの取り違えも
# 「検証が消える」方向に倒れる**ため、分類は一次情報 (man page / --help) で確定させ、
# 根拠を docs/wrapper-env-audit.md に記録すること。
#
# 短縮の連結形 (`stdbuf -oL` / `xargs -I{}` / `xargs -i{}` / `timeout -k10`) は
# 「値を含む 1 トークン」としてそのまま消費されるため、登録は**分離形が正当な
# flag だけ**でよい。同様に long の `--key=value` 形は登録の有無に関わらず
# 1 トークンで消費される。
#
# 出所の凡例:
#   [local]  開発機にインストール済みの man page を逐語確認
#   [ref]    上流の公式リファレンス記述のみ (開発機に man page が無い)
_WRAPPER_FLAGS_WITH_VALUE = {
    # [local] sudo(8): `-C num` `-D directory` `-g group` `-h host` `-p prompt`
    # `-R directory` `-T timeout` `-u user` `-U user`。`-a`/`-r`/`-t` は Linux 版の
    # auth type / SELinux role / type (macOS の synopsis には無い)。
    "sudo": {
        "-u", "-g", "-U", "-p", "-C", "-D", "-h", "-r", "-t", "-T", "-R", "-a",
        "--user", "--group", "--other-user", "--prompt", "--close-from",
        "--chdir", "--host", "--role", "--type", "--command-timeout",
        "--chroot", "--auth-type",
    },
    # [local] time(1): `time [-al] [-h | -p] [-o file] utility`。`-f/--format` は GNU 版。
    "time": {"-o", "-f", "--output", "--format"},
    # [ref] npx: `-p, --package <spec>` / `-c, --call <cmd>` ほか。開発機に npx が
    # 無く man も無い。**v0.8.0 から存在するエントリ**なので削除すると
    # `npx -p pkg firebase deploy` の検証が新たに失われるため据え置く。
    # [local 実機] `npx --help` 逐語: `[--package <package-spec> ...]`
    # `[-c|--call <call>]` `[-w|--workspace <workspace-name> ...]`。
    # いずれも**非数値**なので登録必須。`--node-options` / `--node-arg` は
    # v0.8.0 から存在するエントリなので据え置く。
    # npm のグローバル option (`--registry` 等) は開集合で列挙不能 — 未登録のものは
    # 値が非数値なら検証が消えうる (報告に開示)。
    "npx": {
        "-p", "--package", "-c", "--call", "-w", "--workspace",
        "--node-options", "--node-arg",
    },
    # [local] `man 1 bash`: `exec [-cl] [-a name] [command [arguments]]`。
    "exec": {"-a"},
    # [ref] GNU coreutils timeout(1): `-s, --signal=SIGNAL` / `-k, --kill-after=DURATION`。
    # SIGNAL は数値とは限らない (`KILL`) ため登録が要る。DURATION は下の
    # arg-like ネットでも拾えるが、明示しておく。
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    # [local] nice(1): `nice [-n increment] utility`。GNU の長形式も同義。
    # 値は数値なので arg-like ネットでも拾えるが、一次情報があるので明示する。
    "nice": {"-n", "--adjustment"},
    # [local] stdbuf(1): `stdbuf [-e bufdef] [-i bufdef] [-o bufdef] [command]`。
    # bufdef は `L` / `B` など**非数値**を取りうるので登録が必須。
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    # [local] caffeinate(8): `caffeinate [-disu] [-t timeout] [-w pid] [utility ...]`。
    "caffeinate": {"-t", "-w"},
    # xargs は GNU / BSD の**全 option を列挙して分類済み** (docs の表を参照)。
    # [local] xargs(1) (BSD/macOS): `-E -I -J -L -n -P -R -S -s`
    # [ref] GNU findutils + GNU `xargs --help` 逐語 (外部レビュー環境で確認):
    #   `-a, --arg-file=FILE` / `-d, --delimiter=CHARACTER` /
    #   `--process-slot-var=VAR` はいずれも**必須引数かつ非数値**。
    #   非数値は下の arg-like ネットが効かないので登録が必須
    #   (登録漏れだと `xargs -a input gh pr close 1` の `gh` を見失い検証が消える)。
    # 数値を取る長形式 (`--max-args` / `--max-procs` / `--max-chars`) も
    # 分離形が正当なので明示する (ネットでも拾えるが二重の保険)。
    "xargs": {
        "-E", "-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "-a", "-d",
        "--arg-file", "--delimiter", "--process-slot-var",
        "--max-args", "--max-procs", "--max-chars",
    },
    # `watch` は開発機に man page が無く一次情報を取れない。値を取る option
    # (`-n/--interval` / `-q/--equexit` 等) の値は**すべて数値**なので、
    # 表を空にしたうえで下の arg-like ネットに任せる。こうすると
    # 「その flag が値を取るか」の推測が不要になり、`watch -q 5 gh pr create` の
    # ように bool か値付きか判らない形でも正しくコマンドに到達できる。
    "watch": set(),
    # setsid(1) (util-linux) は `-c/--ctty` `-f/--fork` `-w/--wait` のみで
    # 値を取る flag が無い (= 既定の bool 扱いで足りる)。
}

# **`=` 形でのみ**値を取る flag (GNU の optional argument 規約)。bare 形は
# 次の token を消費してはいけない。
# [ref] GNU findutils xargs: `--replace[=R]` / `--eof[=eof-str]` /
# `--max-lines[=max-lines]`。短縮の `-i[R]` / `-l[N]` は引っ付け形でのみ値を取るので
# bool 扱い (連結形は 1 トークンで消費される) で正しく、ここには載せない。
_WRAPPER_FLAGS_OPTIONAL_VALUE = {
    "xargs": {"--replace", "--eof", "--max-lines"},
}

# 「表示して終了する」終端 option。これが付いた wrapper は**後続コマンドを実行しない**
# ため、剥がしてはいけない (剥がすと `watch --help gh pr create` が `gh pr create` に
# なり、実行されないコマンドで誤 deny する = 離脱要因そのもの)。
#
# 実機確認 (開発機にインストール済みのものを `--help` / `--version` で実行):
#   nice(1)   : `nice: -help: invalid nice value` (BSD。エラー終了し utility を実行しない)
#   stdbuf(1) : `illegal option -- -` + usage (同上)
#   xargs(1)  : `unrecognized option '--help'` + usage (同上)
#   npx       : help を表示して終了
#   sudo(8)   : man に `-h, --help` / `-V, --version` (第 1 synopsis 形が終端形)
# GNU 版は「help を表示して終了」、BSD 版は「不正 option でエラー終了」と挙動は違うが、
# **どちらも後続コマンドを実行しない**点は同じ。timeout / watch / setsid は開発機に
# 無く実機確認できないが、同じ POSIX/GNU 慣行に従う (報告に明記)。
#
# 短縮形 (`-h` / `-v` / `-V`) は**既定では終端扱いしない**。`sudo -h host` のように
# 値を取る別 option と衝突し、終端と誤判定すると検証が消えるため (過剰検証側に倒す)。
# 曖昧さの無い `sudo -V` だけ個別に登録する。
_TERMINATING_FLAGS = frozenset({"--help", "--version"})
_WRAPPER_EXTRA_TERMINATING = {
    "sudo": frozenset({"-V"}),
}


# wrapper が引数列を **シェル経由 (`sh -c "..."`) で実行するか、直接 exec するか**。
# シェル経由の場合、引数はクォートされた**コマンド文字列**として渡されることがあり、
# クォートを剥がして中身を解析しないと `watch 'gh pr create'` が
# `'gh pr create'` のまま残り、行頭 anchored な PATTERNS に一致せず検証が消える。
#
#   "shell_trailing" — flag の後ろの残り全体がシェルコマンド文字列
#   "shell_flag"     — 特定 flag が付いているときだけ上記になる
#   "shell_value"    — 特定 flag の**値**がシェルコマンド文字列
#   (未掲載の wrapper はすべて直接 exec = `utility [argument ...]` 形)
#
# 根拠:
#   watch : [ref] procps-ng `watch --help` の `-x, --exec` が
#           「pass command to exec instead of 'sh -c'」= **既定は sh -c 経由**
#   sudo  : [local man] sudo(8) `-s, --shell` /「If a command is specified, it is
#           passed to the shell for execution via the shell's -c option」。
#           `-i, --login` も同様
#   npx   : [local 実機] `npx --help` の usage 行 `npm exec -c '<cmd> [args...]'`
#           = `-c/--call` の値がシェルコマンド文字列
_WRAPPER_SHELL_EXEC = {
    "watch": ("shell_trailing", frozenset({"--exec", "-x"})),
    "sudo": ("shell_flag", frozenset({"-s", "--shell", "-i", "--login"})),
    "npx": ("shell_value", frozenset({"-c", "--call"})),
}


# 「コマンド名ではありえない」引数トークン: 純粋な数値 (+ 任意の時間単位)。
# 未登録の値付き flag があっても、その値が数値ならここで読み飛ばしてコマンド本体に
# 到達できる。実行可能ファイル名が純粋な数値になることは実質無いので、読み飛ばしが
# コマンドを食う心配がない。**flag 表の取り違えに対する構造的な安全網**であり、
# これがあるおかげで `watch` のように一次情報を取れない wrapper の表を空にできる。
_ARG_LIKE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")


# --- 算術評価 `(( ... ))` の検出 (**この 1 箇所が唯一の判定**) ---------------
#
# `((` の意味を複数の経路がそれぞれ推測すると必ず食い違う。実際 v0.9.0 開発中に
# `split_on_operators` は算術として保護しているのに `_strip_leading_syntax` は
# ただの括弧として剥がしており、`(( gh ))` が `gh` に正規化されて**実行されない
# コマンドで誤 deny**していた。算術かどうかの判断は必ずこの関数を通すこと。
def _arithmetic_span(text: str, i: int) -> int | None:
    """`text[i:]` が算術評価 `(( ... ))` で始まるなら、その**直後**の index を返す。

    算術でない (= `((` で始まらない) か、閉じ `))` が現れないまま尽きるなら None。
    閉じない `((` を算術扱いすると残り全部を飲み込んでしまうため、
    **閉じているものだけ**を算術と認める (未終端 heredoc と同じ規律)。
    `$(( ... ))` は `$(` の subshell 追跡側で保護されるのでここでは扱わない。
    """
    n = len(text)
    if i + 1 >= n or text[i] != "(" or text[i + 1] != "(":
        return None
    depth = 2
    j = i + 2
    while j < n:
        ch = text[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return None


# --- ANSI-C quoting `$'...'` の展開 ------------------------------------------
#
# `man 1 bash` QUOTING 節の逐語表 (bash 3.2 / 5.x の両方を確認):
#   \a alert / \b backspace / \e \E escape / \f form feed / \n newline /
#   \r carriage return / \t tab / \v vertical tab / \\ backslash /
#   \' single quote / \" double quote / \? question mark /
#   \nnn 8 進 (1-3 桁) / \xHH 16 進 (1-2 桁) /
#   \uHHHH (1-4 桁) / \UHHHHHHHH (1-8 桁) / \cx control-x
#
# 展開せずに literal のまま delimiter にすると、`<<$'E\x4fF'` の terminator
# (実際は `EOF`) を見つけられず、**後続コマンドを本文として飲み込む** = 検証が消える。
_ANSI_C_SIMPLE = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _expand_ansi_c(text: str) -> str | None:
    """`$'...'` の中身を展開する。解釈できないエスケープがあれば None。

    None を返した場合、呼び出し側は **heredoc と見なさない** (本文行が候補になる
    = 過剰検証側)。誤って別の delimiter に解決して本文を飲み込むより安全。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            return None  # 末尾の孤立した backslash は解釈不能
        nxt = text[i + 1]
        if nxt in _ANSI_C_SIMPLE:
            out.append(_ANSI_C_SIMPLE[nxt])
            i += 2
            continue
        if nxt in "01234567":
            j = i + 1
            while j < n and j < i + 4 and text[j] in "01234567":
                j += 1
            out.append(chr(int(text[i + 1:j], 8) & 0xFF))
            i = j
            continue
        if nxt in "xuU":
            limit = {"x": 2, "u": 4, "U": 8}[nxt]
            j = i + 2
            while j < n and j < i + 2 + limit and text[j] in "0123456789abcdefABCDEF":
                j += 1
            if j == i + 2:
                return None  # 桁が無い (`\x` 単独) は解釈不能
            value = int(text[i + 2:j], 16)
            out.append(chr(value & 0xFF) if nxt == "x" else chr(value))
            i = j
            continue
        if nxt == "c":
            if i + 2 >= n:
                return None
            out.append(chr(ord(text[i + 2].upper()) ^ 0x40))
            i += 3
            continue
        return None  # 未知のエスケープ
    return "".join(out)


# `${` から閉じ `}` を探す範囲の上限 (文字数)。範囲内で閉じないものは「閉じていない」扱い
# (= 1 領域にしない = 中の演算子で切る側。検証が消える側ではない)。
_PARAM_EXPANSION_SCAN_LIMIT = 4096

# `${` と `$(` の入れ子 (相互再帰) の深さの上限。これより深い `${` は「閉じていない」扱い
# (`${x:-$(echo ${x:-$(echo ...` が数百段あっても再帰の上限に当たらない)。深さには `$(` の
# 入れ子も数える (`$(` が 64 段より深い位置の `${` も閉じていない扱い)。`$(` そのものは深さに
# 関係なく閉じ `)` まで辿る。
_DOLLAR_NEST_LIMIT = 64


class _DollarMemo:
    """`$(` / `${` の終端の memo (同じ text の同じ位置を 2 度走査しない)。

    `${` が閉じないと、呼び出し側はその中身を 1 文字ずつ読み直し、中の `$(` をもう一度走査する。
    memo が無いと入れ子 1 段ごとに走査が倍になり、`${x:-$(echo ` の繰り返し 40 段で
    事実上止まる。text が変わったら捨てる。深さの上限で「閉じていない」とした結果も memo に
    残るので、終端は厳密には text と位置だけでは決まらない: 深い位置で上限に当たった `${` を
    浅い位置から引き直すと、閉じていない扱いのまま返る。トップレベルでは分割が増える側だが、
    `$(` の中では、閉じない扱いの `${` の中の `(` / `)` を `$(` が数えるので、`$(` の閉じる位置が
    ずれて検証が減る側に倒れうる (入れ子が 64 段近いときだけ。0.28.0 は `${` を常に 1 文字ずつ
    読んでいたので、それより悪くはならない)。

    `brace` は走査中のパラメータ展開 `${...}` の段数 (その中の `$(` では comment を読まない)。
    """

    __slots__ = ("text", "ends", "depth", "brace")

    def __init__(self) -> None:
        self.text: str | None = None
        self.ends: dict[int, int | None] = {}
        self.depth = 0
        self.brace = 0


_DOLLAR_MEMO = _DollarMemo()
_UNSET = object()


def _ansi_c_quote_end(text: str, i: int, limit: int | None = None) -> int | None:
    r"""`text[i:]` の `$'` から始まる ANSI-C 引用符の閉じ `'` の直後の index。`limit` までに閉じなければ None。

    中の `\` は次の 1 文字と組で飛ばす (`\'` と `\\` は引用符を閉じない)。bash 3.2 / zsh とも
    `echo $'\'' ; cmd` の `cmd` は走る。`'...'` と同じに読むと 2 つ目の `'` で閉じ、3 つ目の `'` から
    残り全部を引用符の中と読んで `cmd` を落とす。
    """
    n = len(text) if limit is None else limit
    j = i + 2
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        j += 1
    return None


# --- シェルで読み方が割れる形の別の読み (`extract_candidate_readings`) -------------
#
# bash 3.2 と zsh で分解が割れ、どちらに合わせても、もう一方のシェルで走る CLI を落とす形がある。
# 既定の読みはこれまでどおりにし、割れる形があるときだけ別の読みでも分解して候補の和集合を取る。
#
# zsh の読み (`_ZSH_READING`):
#   - パラメータ展開 `${...}` の中の素の `{` を数え、対応する `}` では展開を閉じない
#     (`${x:-{} #}; cmd` の `cmd` は zsh でだけ走る。bash は最初の `}` で閉じ、後ろの ` #}; cmd` を
#     comment と読む)
_ZSH_READING = False
# bash の読み (`_BASH_READING`):
#   - 配列代入 `name=(...)` の閉じの直後の `#` は語の続き (`a=(1 2)# ; cmd` の `cmd` は bash でだけ
#     走る)。zsh は comment と読む (`a=(1 2)#'` + 改行 + `cmd` の `cmd` は zsh でだけ走る)。既定の
#     読みは comment (0.29.1 まで同じ)
#   - 配列代入の中の `(` は構文エラーで、行の残りを読み捨てる (既定の読みは読み続ける)
_BASH_READING = False
# ANSI-C の読み (`_ANSI_C_READING`): `$'...'` を ANSI-C 引用符として読み、中の `\'` で閉じない
# (bash 3.2 / zsh とも。`echo $'\'' ; cmd` の `cmd` は走る)。既定の読みは 0.29.1 までと同じく
# `$` + `'...'` と読む。既定の読みを置き換えると、`$'` の中の `\'` で引用符の向きが入れ替わる
# 形のうち、`$(` の中の comment などシェルで割れる読みと重なる形で、0.29.1 が候補にしていた
# CLI を落とす (差分 fuzz で 6 万件中 7 件)。そこで別の読みとして足す (bash / zsh の読みも
# ANSI-C で読む)。
_ANSI_C_READING = False
# `split_with_separators` が配列代入の `(` を読んだか (bash の読みが要るか)。
_SAW_ARRAY = [False]

# トップレベルの `(` の種類。閉じ `)` の直後が語の続きかを決める。
_PAREN_SUBSHELL = "subshell"  # サブシェル・関数定義の `()`。閉じの後ろは語の先頭
_PAREN_PROCESS = "process"  # プロセス置換 `<(` / `>(`。閉じの後ろは語の続き (bash 3.2 / zsh とも)
_PAREN_ARRAY = "array"  # 配列代入 `name=(`。閉じの後ろはシェルで割れる (`_BASH_READING`)


def _param_expansion_end(text: str, i: int) -> int | None:
    """`text[i:]` が閉じたパラメータ展開 `${...}` なら、その直後の index。閉じていなければ None。

    入れ子は `${` (と `$(`) だけを数え、展開の中の素の `{` は数えない。bash (3.2 / 5.2) は
    素の `{` を数えずに最初の `}` で閉じ (`${x:-{}; cmd; echo }` の `cmd` は走る)、zsh は数える。
    閉じる位置が早い側 (bash) に合わせると、zsh で展開の中にある部分はこちらでは外に出て
    分割が増える (検証が増える側)。ただし外に出た部分の `#` は comment になり、zsh で
    走る後ろのコマンドを落とす (`${x:-{} #}; cmd`)。そこで `extract_candidates` は、素の `{` を
    数える zsh の読み方 (`_ZSH_READING`) でも分解して候補の和集合を取る。素の `{` が出たら 1 領域に
    しない方式は、中の `#` をトップレベルの comment と読んで後ろを落とす (`${x:-{ #}; cmd` の
    `cmd` は bash で走る) ので採らない。`$${` は 2 つ目の `$` からの `${` として数える (bash / zsh
    とも)。`$'...'` は ANSI-C 引用符として飛ばす (`$$'` も 2 つ目の `$` からの `$'` として読む。
    `${x:-$$'\''} ; cmd` の `cmd` は bash / zsh とも走る)。
    中の `${` / `$(` は `_skip_opaque` で飛ばし (閉じない・深さの上限なら外側も閉じない)、
    エスケープ・引用符・backtick も飛ばす。
    """
    n = min(len(text), i + _PARAM_EXPANSION_SCAN_LIMIT)
    j = i + 2
    braces = 0  # zsh の読み方で数える素の `{` の段数
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if _ANSI_C_READING and c == "$" and j + 1 < n and text[j + 1] == "'":
            end = _ansi_c_quote_end(text, j, n)
            if end is None:
                return None
            j = end
            continue
        if c == "$" and j + 1 < n and text[j + 1] in "({":
            # 展開の中の入れ子の展開とコマンド置換 (`${x:-${y}}` / `${x:-$(echo })}`)。
            # 中の `}` は外側を閉じない。
            end = _skip_opaque(text, j)
            if end is None or end > n:
                return None
            j = end
            continue
        if c in "'\"`":
            k = j + 1
            while k < n and text[k] != c:
                k += 2 if (c != "'" and text[k] == "\\") else 1
            if k >= n:
                return None
            j = k + 1
            continue
        if c == "{" and _ZSH_READING:
            braces += 1
        elif c == "}":
            if not braces:
                return j + 1
            braces -= 1
        j += 1
    return None


# --- シェル構文として解釈しない領域のスキップ (**この 1 箇所が唯一の判定**) -----
#
# quote / コマンド置換 / バッククォート / 算術評価の内側は「シェル構文もどき」が
# 現れても構文として解釈してはいけない。この判断を経路ごとに書くと必ず食い違う —
# v0.9.0 開発中に `split_on_operators` はクォートを追跡しているのに heredoc の
# 再スキャンが追跡しておらず、`--metadata '{"k":"<<X bar"}' <<EOF` の `X` を
# 先に delimiter として登録してしまい、**本文行が引数として解釈されて誤った
# コンテキストで検証が通る**という質の悪い失敗を起こしていた (R6 P2)。
def _skip_opaque(text: str, i: int) -> int | None:
    """`text[i]` から始まる「構文として解釈しない領域」の終端 index を返す。

    対象は backslash エスケープ / `'...'` / `"..."` / `` `...` `` / `$(...)` /
    閉じている `${...}` / `$$` / 算術評価 `(( ... ))`。該当しなければ None。
    未終端のクォート等は末尾までを 1 領域とみなす (bash も行を跨いで読み続ける)。
    """
    n = len(text)
    if i >= n:
        return None
    ch = text[i]
    if ch == "\\" and i + 1 < n:
        return i + 2
    if ch in "'\"":
        j = i + 1
        while j < n:
            if ch == '"' and text[j] == "\\" and j + 1 < n:
                j += 2
                continue
            if text[j] == ch:
                return j + 1
            j += 1
        return n
    if ch == "`":
        j = text.find("`", i + 1)
        return n if j == -1 else j + 1
    if ch == "$" and i + 1 < n and text[i + 1] == "$":
        # `$$` (PID)。後ろの `{` を `${` の始まりと読まない (`echo $${; cmd; echo }` の
        # `cmd` は bash / zsh とも走る。`${` と読むと最後の `}` まで 1 領域になり `cmd` が消える)。
        # 後ろの `'` も `$'` の始まりと読まない (`echo $$'\'' ; cmd '` の `cmd` は走らない)。
        return i + 2
    if _ANSI_C_READING and ch == "$" and i + 1 < n and text[i + 1] == "'":
        end = _ansi_c_quote_end(text, i)
        return n if end is None else end
    if ch == "$" and i + 1 < n and text[i + 1] in "({":
        memo = _DOLLAR_MEMO
        if memo.text is not text:
            memo.text = text
            memo.ends = {}
        end = memo.ends.get(i, _UNSET)
        if end is not _UNSET:
            return end  # type: ignore[return-value]
        if text[i + 1] == "(":
            return _command_substitution_end(text, i, memo)
        memo.depth += 1
        memo.brace += 1
        try:
            # パラメータ展開 `${...}`。中の `&` / `;` / `|` は演算子ではない
            # (`${x:-a&b}` を単独の `&` で切ると、後ろの option が別セグメントに移って
            # 誤ったコンテキストで照合される)。**閉じているものだけ**を 1 領域とみなす
            # (閉じない `${` は bash でも構文エラー。残りを飲み込むと検証が消える)。
            # 入れ子が深すぎるものも閉じていない扱い (分割が増える側)。
            end = (
                _param_expansion_end(text, i)
                if memo.depth <= _DOLLAR_NEST_LIMIT
                else None
            )
        finally:
            memo.depth -= 1
            memo.brace -= 1
        memo.ends[i] = end
        return end
    return _arithmetic_span(text, i)


def _comment_end(text: str, i: int) -> int:
    """`text[i]` の `#` から始まる comment の終わり (改行の位置。改行が無ければ末尾)。"""
    eol = text.find("\n", i)
    return len(text) if eol == -1 else eol


def _command_substitution_end(text: str, i: int, memo: _DollarMemo) -> int:
    """`text[i:]` の `$(` の閉じ `)` の直後の index。閉じなければ `len(text)` (残りを 1 領域)。

    入れ子の `$(` は再帰せず、開いている `$(` の位置を明示スタックに積んで 1 回の走査で辿る
    (`$(` を 1000 段ほど重ねると、再帰では Python の再帰の上限に当たって hook が内部エラーになり、
    検証をスキップしていた)。各 `$(` の終端は memo に残す (入れ子の `$(` を後から引き直しても
    走査し直さない)。開いている `$(` の段数は `memo.depth` に数え、中の `${` の深さの上限
    (`_DOLLAR_NEST_LIMIT`) の判定に使う。中の `${` / 引用符 / backtick などは `_skip_opaque` で
    飛ばす (`${` だけは再帰するが、その深さは上限で抑えてある)。
    """
    n = len(text)
    base = memo.depth
    starts = [i]
    parens = [1]
    memo.depth = base + 1
    # `#` から改行の手前までを comment として飛ばす (中の `)` / `'` / `$'\'` で `$(` を閉じたり
    # 引用符を始めたりしない。`x=$(true # don't` + 改行 + `)` の後ろのコマンドは bash 3.2 / zsh
    # とも走る)。comment と読むのは bash 3.2 と zsh の**両方**が comment と読む `#` だけ:
    #   - 素の空白・タブ・改行の直後と、トップレベルの `$(` の先頭
    #   - パラメータ展開 `${...}` の中では読まない (bash 3.2 は `${x:-$( #)}` の `#` を comment に
    #     しない。zsh は `$(echo ${ #)}` の `#` を comment にしない)
    #   - 行末が `\` の comment は読まない (bash 3.2 は `\` + 改行で comment を次の行まで続け、
    #     zsh は改行で閉じる)
    # 片方のシェルだけが comment と読む形 (`;` / `&` / `|` / `(` / `)`・算術評価の閉じ・入れ子の
    # `$(` の先頭の直後は zsh だけ、エスケープした空白の直後は bash 3.2 だけ) は、0.29.0 と同じく
    # comment にしない。どちらに倒しても、もう一方のシェルで走るコマンドを見失う形があり、
    # 0.29.0 で候補に出ていたものを落とさない側に揃える。
    comments = memo.brace == 0
    # `$(` の先頭。ここに来る `$(` は、トップレベルのものか `${...}` の中のもの (comments が偽) だけ
    # (`$(` の中の `$(` はこの走査が積んで辿る)。
    word_start = True
    brace_until = -1  # 1 文字ずつ読んでいる `${...}` の終わり (その中では comment を読まない)
    try:
        j = i + 2
        while j < n:
            if text[j] == "#" and word_start and comments and j >= brace_until:
                end = _comment_end(text, j)
                if text[end - 1] != "\\":
                    j = end
                    continue
            if text[j] == "$" and j + 1 < n and text[j + 1] == "(":
                cached = memo.ends.get(j, _UNSET)
                word_start = False  # 入れ子の `$(` の先頭と、置換の閉じの後ろ
                if cached is not _UNSET:
                    j = cached  # type: ignore[assignment]
                    continue
                starts.append(j)
                parens.append(1)
                memo.depth += 1
                j += 2
                continue
            inner = _skip_opaque(text, j)
            if (
                inner is not None
                and text.startswith("${", j)
                and ")" in text[j:inner]
            ):
                # bash 3.2 は `$(` の中の `${...}` にある `)` で `$(` を閉じる
                # (`echo $(echo ${x:-)}` + 改行 + `aws ...` の aws は走る)。`)` を含む
                # `${...}` は 1 領域にせず、1 文字ずつ読んで `)` を数える (閉じる位置が
                # 早い側。zsh で `$(` の中にある部分は分割が増える側に出る)。
                brace_until = max(brace_until, inner)
                inner = None
            if inner is not None and inner > j:
                if not (text[j] == "\\" and text[j + 1] == "\n"):
                    # 行継続 (`\` + 改行) はその前の状態を引き継ぐ。
                    word_start = False
                j = inner
                continue
            word_start = text[j] in " \t\n"
            if text[j] == "(":
                parens[-1] += 1
            elif text[j] == ")":
                parens[-1] -= 1
                if parens[-1] == 0:
                    parens.pop()
                    memo.ends[starts.pop()] = j + 1
                    memo.depth -= 1
                    if not starts:
                        return j + 1
            j += 1
        # 閉じないまま尽きた: 開いている `$(` はどれも残り全部 (再帰版と同じ)。
        for start in starts:
            memo.ends[start] = n
        return n
    finally:
        memo.depth = base


# --- heredoc の開始 (`<<[-]word`) ------------------------------------------
#
# `man 1 bash` Here Documents 節の逐語: 形式は `<<[-]word`、本文は「word のみを
# 含む行」まで。「If any characters in word are quoted, the delimiter is the
# result of quote removal on word」。
#
# つまり word は**シェルの 1 語**であり、`E"OF"` のように隣接する断片の連結で
# 書ける (結果は `EOF`)。断片の先頭だけを読むと delimiter を読み違え、一致する
# 行が現れないまま**後続コマンドを本文として飲み込む** = 検証が消える。
_HEREDOC_WORD_STOP = " \t\n;&|<>()"


def _scan_heredoc_word(text: str, i: int) -> tuple[str | None, int]:
    r"""`text[i:]` の先頭にあるシェル語を読み、(quote 除去後の delimiter, 次の index)。

    連結された断片をすべて連結してから quote を除去する:
      `EOF` / `'EOF'` / `"EOF"` / `\EOF` / `E"OF"` / `E'OF'` / `"EO"F` / `$'EOF'`
    いずれも `EOF` に解決する。クォートが閉じない等で解決できないときは
    (None, i) を返し、**heredoc と見なさない** (本文行が候補になる = 過剰検証側)。
    """
    n = len(text)
    parts: list[str] = []
    while i < n:
        ch = text[i]
        if ch in _HEREDOC_WORD_STOP:
            break
        if ch == "$" and i + 1 < n and text[i + 1] in "'\"":
            quote = text[i + 1]
            if quote == "'":
                # 中の `\'` では閉じない (`<<$'E\'F'` の delimiter は `E'F`)。
                close = _ansi_c_quote_end(text, i)
                end = -1 if close is None else close - 1
            else:
                end = text.find(quote, i + 2)
            if end == -1:
                return None, i
            body = text[i + 2:end]
            if quote == "'":
                # ANSI-C quoting: エスケープを展開してから連結する。literal のまま
                # 使うと `$'E\x4fF'` の terminator (`EOF`) を見つけられず、
                # 後続コマンドを本文として飲み込む (R6 P1)。
                expanded = _expand_ansi_c(body)
                if expanded is None:
                    return None, i  # 解釈不能 → heredoc と見なさない
                parts.append(expanded)
            else:
                # `$"..."` は locale 翻訳。C/POSIX ロケールでは `$` が無視されるだけ。
                parts.append(body)
            i = end + 1
            continue
        if ch in "'\"":
            end = text.find(ch, i + 1)
            if end == -1:
                return None, i
            parts.append(text[i + 1:end])
            i = end + 1
            continue
        if ch == "\\" and i + 1 < n:
            parts.append(text[i + 1])
            i += 2
            continue
        parts.append(ch)
        i += 1
    word = "".join(parts)
    if not word:
        return None, i
    return word, i


def _scan_heredoc_start(text: str, i: int) -> tuple[str, bool, int] | None:
    """`text[i:]` が `<<[-]word` なら (delimiter, tab 除去するか, 次の index)。"""
    n = len(text)
    if i + 1 >= n or text[i] != "<" or text[i + 1] != "<":
        return None
    if i + 2 < n and text[i + 2] == "<":
        return None  # here-string `<<<` は heredoc ではない
    j = i + 2
    dash = False
    if j < n and text[j] == "-":
        dash = True
        j += 1
    while j < n and text[j] in " \t":
        j += 1
    delim, end = _scan_heredoc_word(text, j)
    if delim is None:
        return None
    return delim, dash, end


def _find_heredoc_end(command: str, i: int, delim: str, strip_tabs: bool) -> int | None:
    """delimiter 行の**次**の index を返す。terminator が無ければ None。

    None のときは呼び出し側が heredoc として扱わずに通常の改行分割へ戻す。
    「terminator が見つからなければ末尾まで飲み込む」方式にすると、delimiter の
    誤検出 (算術シフト等) がそのまま「以降すべて検証しない」に化けるため、
    **実際に閉じている heredoc だけを heredoc として扱う**。
    """
    n = len(command)
    while i < n:
        eol = command.find("\n", i)
        end = n if eol == -1 else eol + 1
        line = command[i:end - 1] if eol != -1 else command[i:]
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return end
        i = end
    return None


def split_on_operators(command: str) -> list[str]:
    """`&&`, `||`, `;`, `|`, `|&`, 単独の `&`, `\\n` でトップレベル分割。

    区切りの演算子も要るときは `split_with_separators` を使う (分割の規則は共有)。
    """
    return [seg for seg, _sep in split_with_separators(command)]


# 「リスト」(後続のコマンドとは別に完結する単位) を閉じる区切り。`&&` / `||` / `|` / `|&`
# は同じリストの中の区切り。単独の `&` はリスト全体をバックグラウンド (サブシェル) で
# 走らせるので、その中の `cd` は後続に効かない (`core/workdir.py` の `segment_dirs`)。
LIST_TERMINATORS = frozenset({";", "\n", "&", ""})
BACKGROUND = "&"


def split_with_separators(command: str) -> list[tuple[str, str]]:
    """トップレベルで分割し、各セグメントと**その直後の区切り**の組を返す。

    区切りは `&&` / `||` / `;` / `|` / `|&` / `&` (単独 = バックグラウンド) / `\\n` /
    `""` (末尾)。空のセグメントは落とす。

    単独の `&` は次のものと区別する (どれも区切りではない):

    - `&&` (AND リスト)
    - `>&` / `<&` / `2>&1` (fd の複製。直前が引用符・エスケープの外の `>` / `<`)
    - `&>` / `&>>` (stdout と stderr のリダイレクト)
    - `|&` (`2>&1 |` の略記。`|` と同じパイプの区切りとして扱う)
    - 引用符・`$(...)`・`${...}`・バッククォート・算術・エスケープ (`\\&`) の中の `&`
      (`_skip_opaque` がまとめて飛ばす)

    quote / `$(...)` / `${...}` / バッククォート / 算術評価 `(( ... ))` の内側は
    **シェル構文として解釈しない**。この判断は `_skip_opaque` に一本化してあり、
    heredoc の再スキャン (`_strip_heredoc_bodies`) や括弧の均衡判定
    (`_has_unbalanced_closer`) も同じ関数を通す。経路ごとに書くと必ず食い違う。

    unquoted な `#` (行頭 / 空白 / 演算子の直後に来るもの) 以降改行までは
    bash コメントとして無視する。

    heredoc (`cat > x.sh <<'EOF'` 〜 `EOF`) の本文は**改行で分割せず**、開始行と
    同じセグメントに取り込む。取り込まないと本文中の 1 行 (`gh release create v1`
    等) が独立した候補セグメントになり、スクリプトや PR 本文を heredoc で書く
    だけで検証が走って誤 deny される。here-string (`<<<`) は heredoc ではないので
    対象外。同一行に複数の heredoc (`cmd <<A <<B`) がある形も宣言順に取り込む。
    """
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    pending_heredocs: list[tuple[str, bool]] = []
    # 直前に buf へ入れたのが引用符・エスケープの外の `>` / `<` か (`2>&1` / `<&3` の判定)。
    # `command[i - 1]` で見ると `\>&` (エスケープした `>` の後の背景実行) を取り違える。
    after_redirect = False
    # 次の文字が語の先頭か (そこの `#` だけが comment)。直前の 1 文字ではなく、直前に読んだものの
    # 種類で決める: エスケープ (`\;` / `\)` / `\ `)・`$(...)`・`$((...))`・引用符の直後は語の続き
    # (`echo $(true)# ; cmd` / `echo \)# ; cmd` の `cmd` は bash 3.2 / zsh とも走る)。トップレベルの
    # 算術評価 `((...))` の直後は語の先頭 (`((1))# ; cmd` の `cmd` は両方で走らない)。行継続
    # (`\` + 改行) は前の状態を引き継ぐ (`echo a\` + 改行 + `#; cmd` の `cmd` は走る)。
    word_start = True
    # 開いている `(` の種類 (`_PAREN_*`)。閉じ `)` の後ろが語の続きかを決める。
    # 要素は (種類, 開いたときのセグメント数)。
    parens: list[tuple[str, int]] = []
    prev_plain = ""  # 直前に buf へ 1 文字ずつ入れた文字 (`<(` / `=(` の判定)

    def flush(sep: str) -> None:
        segments.append(("".join(buf), sep))
        buf.clear()

    while i < n:
        # quote / 置換 / 算術は 1 領域としてそのまま buf へ (構文解釈しない)。
        opaque = _skip_opaque(command, i)
        if opaque is not None and opaque > i:
            after_redirect = False
            if command[i] == "\\" and i + 1 < n and command[i + 1] == "\n":
                # **quote 外の行継続 (行末の `\` + 改行) はシェルと同じく取り除く。**
                # 残すと候補文字列に改行が残り、**改行を跨げない判定表エントリだけが
                # 死ぬ**。行末 `\` で折り返した `aws ssm get-parameter --name n
                # --with-decryption` は DISCLOSING (`.*` が改行を跨げない) が外れ、
                # 1 行目だけで一致する QUERY の `get-*` に落ちて検証が deny から
                # 警告に緩む。折り返した `aws configure export-credentials` /
                # `kubectl cluster-info dump` も同型 (`\s+` が `\` を食えない)。
                #
                # 空白 1 個への置換ではなく**削除**する。置換すると語の途中で
                # 折り返した形 (`aws configure exp` + 改行 + `ort-credentials`) が
                # `aws configure exp ort-credentials` に化けて READONLY の
                # `configure` に当たり、実際に走る開示形が素通しする。削除なら
                # シェルの結果 (`aws configure export-credentials`) と一致する。
                # 継続の前後にあった空白・インデントは残るので語の区切りは
                # 失われない。quote 内の継続は畳まない (quote 領域は
                # `_skip_opaque` が 1 領域で返すのでここには来ない)。
                i = opaque
                continue
            # 算術評価 `((...))` の後ろだけが語の先頭。`<((1))` / `>((1))` はプロセス置換
            # (`echo <((1))# ; cmd` の `cmd` は bash 3.2 / zsh とも走る)。
            word_start = command[i] == "(" and prev_plain not in ("<", ">")
            prev_plain = ""
            buf.append(command[i:opaque])
            i = opaque
            continue

        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""

        if ch == "#" and word_start:
            while i < n and command[i] != "\n":
                i += 1
            continue

        if ch == "<" and nxt == "<":
            # here-string `<<<` は heredoc ではない (本文行を持たない)。
            if i + 2 < n and command[i + 2] == "<":
                buf.append("<<<")
                word_start = False
                prev_plain = ""
                i += 3
                continue
            found = _scan_heredoc_start(command, i)
            if found is not None:
                delim, dash, hd_end = found
                pending_heredocs.append((delim, dash))
                buf.append(command[i:hd_end])
                word_start = False
                prev_plain = ""
                i = hd_end
                continue
            # delimiter を解決できない `<<` はそのまま流す = heredoc と見なさない
            # (本文行が候補になる = 過剰検証側)。
            buf.append("<<")
            word_start = False
            prev_plain = ""
            i += 2
            continue

        if ch == "\n" and pending_heredocs:
            # **閉じている heredoc だけ**を本文として取り込む。terminator を
            # 探してから取り込むことで、delimiter の誤検出が「以降すべて検証
            # しない」に化けるのを防ぐ (誤検出時は下の通常分割にそのまま落ちる)。
            body_end = i + 1
            for delim, strip_tabs in pending_heredocs:
                found_end = _find_heredoc_end(command, body_end, delim, strip_tabs)
                if found_end is None:
                    body_end = None
                    break
                body_end = found_end
            pending_heredocs = []
            if body_end is not None:
                buf.append(command[i:body_end])
                i = body_end
                # delimiter 行の改行はトップレベルの区切りなので、ここでセグメントを
                # 閉じる。閉じないと heredoc の**次**のコマンドが吸収される。
                flush("\n")
                word_start = True
                prev_plain = ""
                continue

        # heredoc の宣言から本文の終わりまでは、途中に演算子が来ても**セグメントを
        # 切らない**。切ると本文が後続セグメントに付き、あとで再分割されて
        # 「データとして書いているだけの行」が実行可能な候補になる (R7 B)。
        # 例: `cat <<EOF; echo done\ngh pr create\nEOF` の `gh pr create` は本文。
        # 宣言と本文を 1 つの不透明なセグメントに保ち、本文の除去は
        # `_strip_heredoc_bodies` に任せる (宣言の後ろに続く実コマンドは、本文除去後の
        # 再分割で改めて候補になるので取りこぼさない)。
        if not pending_heredocs:
            if ch == "&" and nxt == "&":
                flush("&&")
                word_start = True
                prev_plain = ""
                i += 2
                continue
            if ch == "|" and nxt in "|&" and nxt:
                flush("|" + nxt)
                word_start = True
                prev_plain = ""
                i += 2
                continue
            if ch == "&" and not after_redirect and nxt != ">":
                # 単独の `&` (バックグラウンド実行)。0.28.0 までは区切りに数えず、
                # `true & kubectl apply ...` が `true` のセグメントに畳まれて後ろの CLI が
                # どれも検証されなかった。`>&` / `<&` (fd の複製) と `&>` (リダイレクト) は
                # 区切りではない。
                flush("&")
                word_start = True
                prev_plain = ""
                i += 1
                continue
            if ch in ";|\n":
                flush(ch)
                word_start = True
                prev_plain = ""
                i += 1
                continue

        if ch == "(":
            if _BASH_READING and parens and parens[-1][0] == _PAREN_ARRAY:
                # bash 3.2 は配列代入の中の `(` を構文エラーにし、その行の残りを読み捨てて
                # 次の行から続ける (`a=(x (y) 'q` + 改行 + `cmd` の `cmd` は bash でだけ走る)。
                while i < n and command[i] != "\n":
                    i += 1
                parens.clear()
                word_start = True
                prev_plain = ""
                continue
            if prev_plain in ("<", ">"):
                kind = _PAREN_PROCESS
            elif prev_plain == "=":
                kind = _PAREN_ARRAY
                _SAW_ARRAY[0] = True
            else:
                kind = _PAREN_SUBSHELL
            parens.append((kind, len(segments)))
            word_start = True
        elif ch == ")":
            kind, opened_at = parens.pop() if parens else (_PAREN_SUBSHELL, -1)
            if opened_at != len(segments):
                # 開いてから区切りをまたいだ (`<(true` + 改行 + `aws ...)#`)。閉じの直後は
                # 0.29.1 までと同じく語の先頭として読む (後ろが中のコマンドの続きに付くのを避ける)。
                word_start = True
            elif kind == _PAREN_ARRAY:
                word_start = not _BASH_READING
            else:
                word_start = kind == _PAREN_SUBSHELL
        else:
            word_start = ch in " \t\n;&|"
        buf.append(ch)
        after_redirect = ch in "<>"
        prev_plain = ch
        i += 1

    flush("")
    return [(seg.strip(), sep) for seg, sep in segments if seg.strip()]


def _scan_value_end(cmd: str, start: int) -> int | None:
    """代入値の終端 index を返す。$() や backtick が途中にあれば None (保守的 stop)。"""
    i = start
    n = len(cmd)
    in_sq = False
    in_dq = False
    while i < n:
        ch = cmd[i]
        if ch == "\\" and not in_sq and i + 1 < n:
            i += 2
            continue
        if _ANSI_C_READING and ch == "$" and not in_sq and not in_dq:
            # `$'...'` は ANSI-C 引用符 (中の `\'` で閉じない。`A=$'\'' aws ...` の aws は走る)。
            # `$$'` は `$$` の後ろの普通の引用符 (`_skip_opaque` と同じ)。
            if cmd.startswith("$$'", i):
                i += 2
                continue
            if cmd.startswith("$'", i):
                end = _ansi_c_quote_end(cmd, i)
                i = n if end is None else end
                continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            i += 1
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            i += 1
            continue
        if in_sq or in_dq:
            i += 1
            continue
        if ch == "$" and i + 1 < n and cmd[i + 1] == "(":
            return None
        if ch == "`":
            return None
        if ch in " \t":
            return i
        i += 1
    return i


def _unquote_env_value(raw: str) -> str | None:
    """env 代入値の shell quote を除去する。

    `prod` → `prod`, `"a b"` → `a b`, `a"b"c` → `abc`, `""` → ``。
    quote 除去後に未展開の変数参照 (`$VAR`) が残る値は静的に解決できないため
    None を返す (= 収集しない)。`_scan_value_end` が `$(` / backtick で既に
    stop しているので、ここで弾くのは `$VAR` 形式のみ。
    """
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    value = "".join(parts) if parts else ""
    if "$" in value:
        return None
    return value


# 代入の頭 (`NAME=` / `NAME+=` / `NAME[...]=`)。group 2 は添字、group 3 は `+`。
_ASSIGNMENT_HEAD = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^\]\s]*\])?(\+)?=")


def _array_value_end(cmd: str, start: int) -> int | None:
    """`cmd[start]` の `(` から始まる配列の値の閉じ `)` の直後。閉じなければ None。

    引用符・置換・エスケープは `_skip_opaque` で飛ばす (中の `)` で閉じない)。入れ子の `(` は
    数える (bash 3.2 は構文エラーにするが、閉じる位置を早めると後ろを値に含めない側になる)。
    """
    depth = 0
    i = start
    n = len(cmd)
    while i < n:
        opaque = _skip_opaque(cmd, i)
        if opaque is not None and opaque > i:
            i = opaque
            continue
        if cmd[i] == "(":
            depth += 1
        elif cmd[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _parse_leading_env(cmd: str) -> tuple[str, dict[str, str]]:
    """先頭の `KEY=VALUE` 群を剥がし (残りコマンド, 収集 env dict) を返す。

    剥がし条件は従来の strip_leading_env と同一:
    - 値に $() / バッククォートが含まれると保守的に停止
    - `FOO=bar` のみで後続コマンドが無いケースはそのまま返す (空コマンド化回避)
    収集 env には静的に解決できた値のみ入る (`$VAR` を含む値はキーごと除外)。
    同一キーが複数回代入された場合は shell semantics に合わせ最右 (最後) を採用する。
    """
    collected: dict[str, str] = {}
    while True:
        m = _ASSIGNMENT_HEAD.match(cmd)
        if not m:
            return cmd, collected
        if cmd.startswith("(", m.end()):
            end = _array_value_end(cmd, m.end())
        else:
            end = _scan_value_end(cmd, m.end())
        if end is None:
            return cmd, collected
        rest = cmd[end:]
        if not rest or rest[0] not in " \t":
            return cmd, collected
        key = m.group(1)
        # 配列の代入 (`a=(1 2)`)・追記 (`a+=x`)・要素の代入 (`a[1]=x`) は剥がすが、値は集めない
        # (静的な文字列の値として検証に渡せない)。0.29.1 までは剥がさず、`a=(1 2) aws ...` /
        # `a+=(x) aws ...` の aws を検証していなかった (bash 3.2 / zsh とも aws は走る)。
        plain = not m.group(2) and not m.group(3) and not cmd.startswith("(", m.end())
        value = _unquote_env_value(cmd[m.end():end]) if plain else None
        if value is not None:
            # 同一キーの重複代入は shell と同じく最右 (最後) の値が勝つ。
            # `AWS_PROFILE=dev AWS_PROFILE=prod aws ...` は prod で実行されるため、
            # setdefault (最初優先) だと実行時と異なる profile で検証が通りうる。
            collected[key] = value
        cmd = rest.lstrip()


def strip_leading_env(cmd: str) -> str:
    """先頭の `KEY=VALUE` 形式の環境変数割当を順次剥がす (収集 env は捨てる)。"""
    return _parse_leading_env(cmd)[0]


def _tokens(cmd: str) -> list[str]:
    return cmd.split()


def _drop_tokens(cmd: str, n: int) -> str:
    remaining = cmd.lstrip()
    for _ in range(n):
        m = re.match(r"^\S+\s*", remaining)
        if not m:
            return ""
        remaining = remaining[m.end():]
    return remaining.lstrip()


# option の**値**を 1 トークンとして取るための matcher。クォートされた値
# (`sudo -p 'my prompt'` / `npx -c 'gh pr create'`) は空白を含むので `\S+` では
# 途中で切れてしまい、残りが候補の先頭に混ざってコマンドを見失う。
_VALUE_TOKEN_RE = re.compile(r"^('[^']*'|\"[^\"]*\"|\S+)")
# シェルの 1 「語」。`--call='gh pr create'` のようにクォート部分に空白を含む語を
# 1 トークンとして取る (`\S+` だと `--call='gh` で切れる)。
_WORD_TOKEN_RE = re.compile(r"""^((?:[^\s'"]|'[^']*'|"[^"]*")+)""")


def _parse_wrapper_flags(rest: str, wrapper: str) -> tuple[list[tuple[str, str | None]], str]:
    """wrapper の flag 領域を 1 回だけ解析し `(flags, 残りの文字列)` を返す。

    `flags` は `[(option 名, 値 or None), ...]`。**この関数が flag 消費の唯一の実装**で、
    「剥がす」「特定 flag があるか」「その値は何か」「終端 option か」の 4 用途が
    すべてここを通る。用途ごとに走査を書くと消費規則が食い違い、実際に
    `timeout -vk 1 5 cmd` の `-k` の値を取り逃してコマンドを見失っていた (R7 A)。

    消費規則:

    - `--` は POSIX の flag 終端。それ以降は解析しない
    - `--key=value` / `-x=value` は 1 トークンに値が埋まっている
    - **短縮 option の連結** (`-vk` = `-v -k`) を分解する。POSIX/GNU の慣行どおり、
      連結の途中で値を取る option に当たったら**それ以降の文字が値**になり
      (`-k1` の `1`)、文字が尽きていれば**次のトークンが値**になる
      (`-vk 1` の `1`)。GNU の「long option の必須引数は短縮形でも必須」に対応
    - optional 引数 (`--key[=value]`) の bare 形は次のトークンを消費しない
    - 未知の option は値を取らない (bool) と見なす
    """
    with_value = _WRAPPER_FLAGS_WITH_VALUE.get(wrapper, set())
    optional = _WRAPPER_FLAGS_OPTIONAL_VALUE.get(wrapper, set())
    flags: list[tuple[str, str | None]] = []
    s = rest.lstrip()
    while s:
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok == "--":
            return flags, s[m.end():].lstrip()
        if tok == "-" or not tok.startswith("-"):
            break
        s = s[m.end():].lstrip()
        name, eq, embedded = tok.partition("=")
        if eq:
            flags.append((name, embedded))
            continue
        if tok.startswith("--"):
            if tok in with_value and tok not in optional:
                m2 = _VALUE_TOKEN_RE.match(s)
                if m2:
                    flags.append((tok, m2.group(1)))
                    s = s[m2.end():].lstrip()
                else:
                    flags.append((tok, None))
            else:
                flags.append((tok, None))
            continue
        # 短縮 option の連結を 1 文字ずつ分解する。
        letters = tok[1:]
        k = 0
        while k < len(letters):
            short = "-" + letters[k]
            tail = letters[k + 1:]
            if short in with_value:
                if tail:
                    flags.append((short, tail))
                else:
                    m2 = _VALUE_TOKEN_RE.match(s)
                    if m2:
                        flags.append((short, m2.group(1)))
                        s = s[m2.end():].lstrip()
                    else:
                        flags.append((short, None))
                break  # 以降の文字は値として消費済み
            if short in optional:
                # optional は引っ付け形でのみ値を取る (bare は次を消費しない)。
                flags.append((short, tail or None))
                break
            flags.append((short, None))
            k += 1
    return flags, s


def _drop_wrapper_flags(cmd: str, wrapper: str, skip_arg_like: bool = True) -> str:
    """wrapper 直後の flag 領域を剥がし、コマンド本体の先頭を返す。

    消費規則は `_parse_wrapper_flags` に一本化。最後に `skip_arg_like` が真なら
    **コマンド名ではありえない引数トークン** (純粋な数値 + 任意の時間単位) を
    読み飛ばす。未登録の値付き flag があっても値が数値ならコマンド本体に到達できる
    安全網で、これがあるので一次情報を取れない wrapper (`watch` 等) の flag 表を
    空にできる。位置引数を持つ `timeout` だけは自前の DURATION 処理と衝突するため
    無効にする。
    """
    _flags, s = _parse_wrapper_flags(cmd, wrapper)
    if skip_arg_like:
        while s:
            m = _WORD_TOKEN_RE.match(s)
            if not m or not _ARG_LIKE_RE.match(m.group(1)):
                break
            s = s[m.end():].lstrip()
    return s


# GNU coreutils `timeout` が受理する DURATION。
#
# 「浮動小数点数 + 任意の接尾辞」だが、**strtod が受理する = timeout が受理する
# ではない**。timeout.c の `parse_duration()` は strtod の上に独自の検証を重ねる:
#
#   1. strtod で解釈できること (ERANGE = 桁溢れは許容)
#   2. `! (0 <= duration)` で弾く → **NaN と負値は拒否**
#      (NaN との比較は常に偽なので `0 <= nan` が偽になり拒否される)
#   3. 数値の後に残ってよい文字は **1 文字だけ**
#   4. その 1 文字は `apply_suffix()` の switch にある `s` `m` `h` `d` のみ
#      (**小文字のみ**。`30S` は拒否される)
#
# 受理しすぎると「実行されないコマンド」で誤 deny (R7 C)、受理し損ねると wrapper を
# 剥がせず検証が消える (R4 A)。**両方向にリスクがある**ので、strtod 文法ではなく
# timeout の受理規則そのものを実装する。
_C_DOUBLE_RE = re.compile(
    r"""^[+-]?(?:
          inf(?:inity)?
        | nan(?:\([0-9A-Za-z_]*\))?
        | 0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)(?:[pP][+-]?[0-9]+)?
        | (?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?
    )$""",
    re.VERBOSE | re.IGNORECASE,
)
_TIMEOUT_SUFFIXES = "smhd"


def _parse_c_double(text: str) -> float | None:
    """C の strtod と同じ文法で解釈して値を返す (解釈できなければ None)。"""
    if not _C_DOUBLE_RE.match(text):
        return None
    try:
        if "x" in text.lower():
            return float.fromhex(text)
        return float(text)
    except (ValueError, OverflowError):
        return None


def _is_timeout_duration(token: str) -> bool:
    """`timeout` が DURATION として受理する形かどうか。"""
    if not token:
        return False
    candidates = [token]
    if len(token) > 1 and token[-1] in _TIMEOUT_SUFFIXES:
        candidates.append(token[:-1])
    for body in candidates:
        value = _parse_c_double(body)
        if value is None:
            continue
        # timeout.c: `! (0 <= duration)` が NaN と負値を弾く。
        return 0 <= value
    return False


def _drop_timeout_duration(rest: str) -> str | None:
    """`timeout` のフラグを剥がした後に残る位置引数 DURATION を消費する。

    `timeout 30 gh pr create` → `gh pr create`。DURATION の形をしていない
    (= 文書化された呼び出し形ではない) ときは None を返して wrapper 自体を
    剥がさない — 誤って本体トークンを食うより、従来どおり不透明なセグメントとして
    検証をスキップする方が安全側 (lenient)。
    """
    m = re.match(r"^(\S+)", rest)
    if not m or not _is_timeout_duration(m.group(1)):
        return None
    return rest[m.end():].lstrip()


def _has_flag(rest: str, wrapper: str, names) -> bool:
    """wrapper の flag 領域に `names` のいずれかがあるか (消費規則は共有)。"""
    flags, _rest = _parse_wrapper_flags(rest, wrapper)
    return any(name in names for name, _value in flags)


def _flag_argument(rest: str, wrapper: str, names) -> str | None:
    """wrapper の flag 領域から `names` のいずれかの**値**を取り出す (消費規則は共有)。"""
    flags, _rest = _parse_wrapper_flags(rest, wrapper)
    for name, value in flags:
        if name in names:
            return value
    return None


def _unquote_shell_command(text: str) -> str:
    """シェルコマンド文字列として渡された引数のクォートを 1 枚剥がす。

    `watch 'gh pr create'` の引数は `sh -c` に渡る**コマンド文字列**なので、
    クォートを剥がして中身を解析対象にする。単一のクォート塊でないとき
    (`watch -n 5 kubectl get pods` のような素の引数列) はそのまま返す。
    """
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "'\"":
        inner = stripped[1:-1]
        if stripped[0] not in inner:
            return inner
    return text


def _shell_command_argument(rest: str, wrapper: str) -> str | None:
    """wrapper がシェル経由で実行する場合の「コマンド文字列」を返す (無ければ None)。"""
    spec = _WRAPPER_SHELL_EXEC.get(wrapper)
    if spec is None:
        return None
    kind, names = spec
    if kind == "shell_trailing":
        # `--exec` が付いていれば直接 exec なのでシェル解釈しない。
        if _has_flag(rest, wrapper, names):
            return None
        return _unquote_shell_command(_drop_wrapper_flags(rest, wrapper))
    if kind == "shell_flag":
        if not _has_flag(rest, wrapper, names):
            return None
        return _unquote_shell_command(_drop_wrapper_flags(rest, wrapper))
    if kind == "shell_value":
        value = _flag_argument(rest, wrapper, names)
        if value is None:
            return None
        shell_cmd = _unquote_shell_command(value)
        # `npx -c '<cmd>' <positional>` の positional は npm では package spec 扱いで
        # 実行されない**はず**だが、その意味論まで裏が取れていない。取りこぼしで
        # 検証が消えるより過剰検証側が安全なので、**両方**を候補として返す
        # (改行はトップレベルの区切りなので extract_candidates が再分割する)。
        remainder = _drop_wrapper_flags(rest, wrapper)
        if remainder:
            return f"{shell_cmd}\n{remainder}"
        return shell_cmd
    return None


def _wrapper_terminates(rest: str, wrapper: str) -> bool:
    """wrapper の flag 領域に「表示して終了する」option があれば True。

    True のとき wrapper を剥がさないので、セグメントは不透明なまま残り検証されない
    (= 実行されないコマンドで deny しない)。効果は wrapper が包む引数列に限定され、
    `watch --help; gh pr create` のように**別セグメント**として続くコマンドは
    セグメント分割が先に走るため従来どおり検証される。消費規則は共有パーサ。
    """
    terminating = _TERMINATING_FLAGS | _WRAPPER_EXTRA_TERMINATING.get(wrapper, frozenset())
    flags, _rest = _parse_wrapper_flags(rest, wrapper)
    return any(name in terminating for name, _value in flags)


def _command_is_query(rest: str) -> bool:
    """`command -v` / `command -V` (存在確認クエリ) なら True。

    `man 1 bash` の逐語: `command [-pVv] command [arg ...]` /「If either the -V or
    -v option is supplied, a description of command is printed」。つまり `-v` /
    `-V` 付きの `command` は後続 CLI を**実行しない**ので、wrapper として剥がして
    しまうと `command -v gh` が `gh` 単体の候補になり、単なる存在確認で
    アカウント検証が走って誤 deny される。連結形 (`-pv`) も共有パーサが分解する。
    """
    flags, _rest = _parse_wrapper_flags(rest, "command")
    return any(name in ("-v", "-V") for name, _value in flags)


def _strip_one_wrapper(cmd: str) -> str | None:
    toks = _tokens(cmd)
    if not toks:
        return None

    if len(toks) >= 3 and (toks[0], toks[1], toks[2]) in _WRAPPERS_THREE:
        return _drop_tokens(cmd, 3)

    if len(toks) >= 2 and (toks[0], toks[1]) in _WRAPPERS_TWO:
        return _drop_tokens(cmd, 2)

    t0 = toks[0]

    if t0 == "env":
        if len(toks) < 2:
            return None
        if toks[1].startswith("-"):
            return None
        return _drop_tokens(cmd, 1)

    if t0 in _WRAPPERS_SINGLE:
        rest = _drop_tokens(cmd, 1)
        if t0 == "command" and _command_is_query(rest):
            return None
        # `--help` / `--version` 付きは後続を実行しないので剥がさない。
        if _wrapper_terminates(rest, t0):
            return None
        # シェル経由で実行する wrapper は、引数をコマンド**文字列**として解釈する。
        shell_cmd = _shell_command_argument(rest, t0)
        if shell_cmd is not None:
            return shell_cmd
        # timeout は位置引数 DURATION を自前で消費するので、汎用の
        # arg-like 読み飛ばしを無効にする (有効だと DURATION まで食われ、
        # _drop_timeout_duration がコマンド名を DURATION と誤判定して剥がしを諦める)。
        rest = _drop_wrapper_flags(rest, t0, skip_arg_like=(t0 != "timeout"))
        if t0 == "timeout":
            return _drop_timeout_duration(rest)
        return rest

    return None


# sudo が `-E` 無しに継承環境を scrub しないことを示す preserve-env フラグ群。
# `_WRAPPER_FLAGS_WITH_VALUE["sudo"]` には含まれない (値を取らない bool 扱いの
# `-E` / `--preserve-env`、および `--preserve-env=LIST` の = 形式)。
_SUDO_PRESERVE_ENV_FLAGS = {"-E", "--preserve-env"}


def _sudo_preserves_env(cmd_after_sudo: str) -> bool:
    """`sudo` 直後のフラグ列に preserve-env 指定があるかを判定する。

    `sudo` は `-E` / `--preserve-env` / `--preserve-env=LIST` が無いと継承環境を
    scrub する。これらが**コマンド本体に到達する前** (= flag 領域内) に現れた場合
    のみ True を返す。`--` 単独トークン (POSIX flag 終端) または非 `-` トークン
    (コマンド本体の開始) が現れた時点で flag 領域は終わる。

    `--preserve-env=LIST` は指定リストのみ保持する形式だが、リスト解析や sudoers の
    env_keep/env_reset まで静的には不可知なので、preserve 指定があれば「pre-sudo
    env を伝播してよい」と保守的に判断する (= scrub による誤 allow を防ぐのが目的で、
    保持しすぎ方向は誤 deny を増やさない安全側)。
    """
    s = cmd_after_sudo.lstrip()
    while s:
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok == "--":
            return False
        if not tok.startswith("-"):
            return False
        if tok in _SUDO_PRESERVE_ENV_FLAGS:
            return True
        # `--preserve-env=AWS_PROFILE` のような = 形式。
        if tok.startswith("--preserve-env="):
            return True
        # 値を取るフラグ (`-u deploy` 等) は次トークンが値なのでまとめて skip。
        # そうしないと値トークン (例: `-u -E`... は通常無いが) を誤って flag と
        # 解釈しうる。`_drop_wrapper_flags` と同じ消費規則に合わせる。
        if "=" in tok:
            s = s[m.end():].lstrip()
            continue
        if tok in _WRAPPER_FLAGS_WITH_VALUE.get("sudo", set()):
            s = s[m.end():].lstrip()
            m2 = re.match(r"^(\S+)", s)
            if m2:
                s = s[m2.end():].lstrip()
            continue
        s = s[m.end():].lstrip()
    return False


# セグメント先頭に現れうる shell 予約語。これらの直後がコマンド本体になる形
# (`if gh pr create; then` / `for f in *; do gh release upload ...; done` を
# `;` で割った `do gh release upload ...`) を候補として拾えるようにする。
# `for` / `case` / `select` は「コマンドではなく変数名/値」が続くので含めない。
_LEADING_RESERVED_WORDS = frozenset(
    {"if", "then", "elif", "else", "do", "while", "until", "!"}
)

# コマンド本体の**前**に置けるリダイレクト (`</dev/null gh ...` / `2>/dev/null gh ...`)。
# `>` 単独のように対象が別トークンの形もあるため group(1) の有無で消費数を変える。
_LEADING_REDIRECT_RE = re.compile(
    r"^(?:[0-9]*(?:>>|>&|>\||<>|<&|>|<)|&>>|&>)(\S*)$"
)

_CLOSER_TO_OPENER = {")": "(", "}": "{"}


def _strip_heredoc_bodies(cmd: str) -> str:
    """セグメントに取り込まれた heredoc の**本文と宣言トークン**を落とす。

    `split_on_operators` は heredoc 本文を分割しないよう同一セグメントへ取り込み、
    宣言から本文終端までは演算子でもセグメントを切らない。ここで残骸を掃除する:

    1. **本文**を落とす — 本文はコマンドの引数ではなく**データ**。残すと後段の
       option 走査 (`cli_options.find_context_options`) が本文の行をコマンドラインの
       一部として読む。実害は両方向にあり、本文の `--profile prod` で別 profile を
       検証して allow したり (false-allow)、YAML の `- --context` を指定と誤読して
       誤 deny したりする
    2. **`<<delim` の宣言トークン**も落とす — 本文を消した後まで宣言が残っていると、
       再分割のときに再び「本文待ち」と解釈され、`cat <<EOF; gh pr create` の
       `;` で切れなくなって**実行される `gh` が候補から消える**
       (R7 B の修正で演算子 flush を抑止した副作用)。宣言はもう消費済みなので
       落として構わない

    quote / 置換 / 算術の内側は構文として解釈しない — `split_on_operators` と
    **同じ `_skip_opaque`** を通す。追跡を怠ると
    `--metadata '{"k":"<<X bar"}' <<EOF` の `X` を先に登録してしまい、本文の除去に
    失敗して本文行が引数として解釈される (誤コンテキスト検証)。
    """
    if "<<" not in cmd:
        return cmd
    # 行ごとではなく全体を 1 回で読む。引用符は改行をまたぐ (`E'F` + 改行 + `\;<<$'E\'F''<<'EOF' & aws ...`
    # の 1 行目の `'` は 2 行目の `$'` までの引用符で、その後ろの `<<'EOF'` が宣言)。行ごとに読むと、
    # 前の行から続く引用符の中を構文として読み、宣言を見失ったり引用符の中を宣言と読んだりする。
    # 本文は宣言のある行の改行の直後から始まる。
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    k = 0
    n = len(cmd)
    line_start = line_end = 0  # 切り出し済みの行 (`line`) の範囲
    line = ""
    while k < n:
        opaque = _skip_opaque(cmd, k)
        if opaque is not None and opaque > k:
            out.append(cmd[k:opaque])
            k = opaque
            continue
        if cmd[k] == "<" and k + 1 < n and cmd[k + 1] == "<":
            # here-string `<<<` は heredoc ではない (本文行を持たない)。
            # `split_on_operators` と同じ guard を置かないと、3 つ目の `<` から
            # 再び `<<` を読み取って後続を delimiter と誤認する。
            if k + 2 < n and cmd[k + 2] == "<":
                out.append("<<<")
                k += 3
                continue
            # delimiter の語は行 (改行まで) の中で読む (0.29.1 までの行ごとの読みと同じ)。
            # 行は 1 度だけ切り出す。
            if not line_start <= k < line_end:
                line_start = cmd.rfind("\n", 0, k) + 1
                eol = cmd.find("\n", k)
                line_end = n if eol == -1 else eol + 1
                line = cmd[line_start:line_end]
            found = _scan_heredoc_start(line, k - line_start)
            if found is not None:
                pending.append((found[0], found[1]))
                k = line_start + found[2]  # 宣言トークンは out に入れない (= 落とす)
            else:
                out.append(cmd[k])
                k += 1
        else:
            out.append(cmd[k])
            k += 1
        if pending and cmd[k - 1] == "\n":
            # 宣言のある行の改行の直後から本文 (改行は宣言の語の `\` が飲んでいることもある)。
            for delim, strip_tabs in pending:
                found_end = _find_heredoc_end(cmd, k, delim, strip_tabs)
                if found_end is None:
                    break
                k = found_end  # 本文 + delimiter 行を出力せず読み飛ばす
            pending = []
    return "".join(out)


def _strip_leading_syntax(cmd: str) -> str:
    """コマンド本体の前に置かれた shell 構文プレフィックスを剥がす。

    対象: `(` / `{` (グルーピング)、`!` (否定)、`if`/`then`/`do` 等の予約語、
    コマンド本体前のリダイレクト。剥がさないと `(gh pr create --fill)` /
    `do gh release upload ...` / `2>/dev/null gh pr create` が行頭 anchored な
    PATTERNS (`^gh(?=\\s|$)`) に一致せず、検証をすり抜ける。
    """
    s = cmd.lstrip()
    while s:
        # 算術コマンド `(( ... ))` は**括弧を剥がしてはいけない**。剥がすと
        # `(( gh ))` が `gh` になり、bash は変数を評価するだけで CLI を実行しない
        # のに検証が走って誤 deny する (R5 P2)。判定は `split_on_operators` と
        # 同じ `_arithmetic_span` を共有する。
        if _arithmetic_span(s, 0) is not None:
            break
        # `(` はコマンド名と連結できる (`(gh pr create`)。`{` は bash では空白必須
        # だが、同じ扱いで剥がしても実害が無い。
        if s[0] in "({":
            s = s[1:].lstrip()
            continue
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok in _LEADING_RESERVED_WORDS:
            s = s[m.end():].lstrip()
            continue
        rm = _LEADING_REDIRECT_RE.match(tok)
        if rm:
            s = s[m.end():].lstrip()
            if not rm.group(1):
                # `> out.txt gh ...` のように対象が別トークンの形。
                m2 = re.match(r"^(\S+)", s)
                if m2:
                    s = s[m2.end():].lstrip()
            continue
        break
    return s


def _has_unbalanced_closer(s: str, closer: str) -> bool:
    """構文として解釈する範囲で数えて、閉じ括弧が開き括弧より多ければ True。

    quote / 置換 / 算術の内側は `_skip_opaque` で飛ばす (共有機構)。飛ばした領域は
    内部で均衡しているので、外側の均衡判定には影響しない。
    """
    opener = _CLOSER_TO_OPENER[closer]
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        opaque = _skip_opaque(s, i)
        if opaque is not None and opaque > i:
            i = opaque
            continue
        if s[i] == opener:
            depth += 1
        elif s[i] == closer:
            depth -= 1
        i += 1
    return depth < 0


def _strip_trailing_syntax(cmd: str) -> str:
    """末尾の `;` / `&` と、対応する開き括弧を持たない `)` / `}` を剥がす。

    `(gh pr create --fill)` は先頭 `(` を剥がした後に `)` だけが余るため、
    ここで落とさないと self-remediation / readonly の `\\s*$` 終端 anchored
    pattern に一致しない。一方 `gh pr view $(echo 1)` の `)` は開き括弧と
    対応しているので剥がさない (引数を壊さない)。
    """
    s = cmd.rstrip()
    while s:
        last = s[-1]
        if last in ";&":
            s = s[:-1].rstrip()
            continue
        if last in _CLOSER_TO_OPENER and _has_unbalanced_closer(s, last):
            s = s[:-1].rstrip()
            continue
        break
    return s


_ASSIGNMENT_HEAD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_command_path(cmd: str) -> str:
    """コマンド名のディレクトリ部と `\\` エスケープを落として basename に揃える。

    `/opt/homebrew/bin/gh pr create` / `./node_modules/.bin/firebase deploy` /
    `\\gh pr create` はいずれも実体として同じ CLI を起動するが、行頭 anchored な
    PATTERNS には一致しない。ここで正規化して照合対象に載せる
    (`\\gh` は alias を迂回する bash の記法)。

    quote を含むトークン (`"/opt/homebrew/bin/gh"`) は分解すると壊れるので触らない。
    """
    m = re.match(r"^(\S+)", cmd)
    if not m:
        return cmd
    tok = m.group(1)
    if "'" in tok or '"' in tok:
        return cmd
    # `NAME=値` の代入だけのセグメント (後ろにコマンド語が無い。後ろにあれば `_parse_leading_env` が
    # 先に剥がしている) は、コマンドの呼び出しではない。値が `/…/firebase-tools` で終わるだけで
    # basename に揃えると、firebase のコマンドとして検出され誤 deny になる。代入の値は
    # コマンド名として読まない。
    # 値に `$(` / バッククォートがある代入は `_parse_leading_env` が剥がさずに止まる。その中の
    # コマンド置換に書かれたパス付き CLI (`X=$(/usr/bin/firebase deploy)`) は従来どおり
    # basename 化して検出する (ここで返すと deny が allow に変わる)。
    am = _ASSIGNMENT_HEAD_RE.match(tok)
    if am and _scan_value_end(tok, am.end()) is not None:
        return cmd
    name = tok
    if name.startswith("\\") and len(name) > 1:
        name = name[1:]
    if "/" in name:
        base = name.rsplit("/", 1)[1]
        if base:
            name = base
    if name == tok:
        return cmd
    return name + cmd[m.end():]


def _normalize_segment(cmd: str, max_iter: int = 8) -> tuple[str, dict[str, str]]:
    """wrapper と先頭 env を剥がし (正規化コマンド, 収集 env) を返す。

    多段 (`FOO=bar sudo time mise exec -- foo`) に対応するため最大 max_iter
    回繰り返し、各段で現れた先頭 env を収集する。同名キーは内側 (後に出現 =
    コマンド本体に近い) を優先する。`AWS_PROFILE=expected env AWS_PROFILE=other
    aws ...` は `env` が内側の `other` を実行環境へ適用するため、検証も `other`
    で行う必要がある (外側優先だと実行時と異なる profile で検証が通ってしまう)。

    **sudo の env scrub 補正 (D16)**: `AWS_PROFILE=prod sudo aws ...` のように
    `sudo` の**前**に置かれたインライン env (pre-sudo env) は、`sudo` が
    `-E` / `--preserve-env` / `--preserve-env=LIST` 無しに継承環境を scrub する
    ため、実行時の `sudo aws ...` には伝播しない。これを検証 subprocess に渡すと
    「検証は prod / 実行は別アカウント」の非対称が生じ、未承認 profile で mutating
    コマンドが通る false-allow になる。そのため preserve-env 指定の無い `sudo` を
    跨いだ時点で、それまでに収集した pre-sudo env を破棄する。env を捨てると検証は
    デフォルト環境で走り deny されうるが、それは安全方向 (false-allow → 安全側
    deny)。`sudo` 直後の command-line env (`sudo FOO=bar cmd` の post-sudo env) は
    sudo 自身が target へ渡すため破棄せず伝播を維持する。env scrub は `sudo` 固有
    の挙動で、`time` / `nohup` / `command` / `exec` / `env` 等の他 wrapper は
    pre-wrapper env を素通しするため従来どおり伝播する。
    """
    collected: dict[str, str] = {}
    # heredoc 本文はデータであってコマンドラインではない。先に落としておかないと
    # 後段の option 走査が本文の行を引数として読む (`_strip_heredoc_bodies` 参照)。
    cmd = _strip_heredoc_bodies(cmd)
    for _ in range(max_iter):
        before = cmd
        # 構文プレフィックス → 先頭 env → コマンド名のパス正規化 の順。
        # env 割当の前にリダイレクトや `(` が来る形 (`(FOO=bar gh ...`) と、
        # env の後ろにパス付きコマンドが来る形 (`FOO=bar /usr/bin/gh ...`) の
        # 両方を 1 ラウンドで畳めるようにしてある。
        cmd = _strip_leading_syntax(cmd)
        cmd, env = _parse_leading_env(cmd)
        collected.update(env)  # 内側 (後段) が外側を上書きする
        cmd = _strip_command_path(cmd)
        # sudo を剥がす直前の判定: preserve-env 指定が無ければ、ここまでに集めた
        # pre-sudo env を sudo が scrub するので破棄する。剥がし自体は
        # _strip_one_wrapper に委ねる (sudo の flag 消費規則と一致させる)。
        toks = _tokens(cmd)
        if toks and toks[0] == "sudo":
            rest_after_sudo = _drop_tokens(cmd, 1)
            if not _sudo_preserves_env(rest_after_sudo):
                collected.clear()
        stripped = _strip_one_wrapper(cmd)
        if stripped is not None:
            cmd = stripped
            continue
        # wrapper は剥がれなかったが構文 / env / パスの正規化が進んだなら、
        # その結果に対してもう一度 wrapper 判定をやり直す。
        if cmd == before:
            break
    return _strip_trailing_syntax(cmd), collected


def strip_transparent_wrappers(cmd: str, max_iter: int = 6) -> str:
    """後続コマンドの挙動を変えない wrapper と先頭の env 割当を剥がす。

    多段 (sudo time mise exec -- foo) に対応するため最大 max_iter 回繰り返す。
    収集した env は捨てる (env も必要なら _normalize_segment を使う)。
    """
    return _normalize_segment(cmd, max_iter)[0]


def extract_candidates(command: str) -> list[tuple[str, dict[str, str]]]:
    """検証対象候補の断片と、その断片のインライン env の dict を返す。

    - `cd /tmp && FOO=bar gh pr create`
        → [(`cd /tmp`, {}), (`gh pr create`, {"FOO": "bar"})]
    - `AWS_PROFILE=prod aws s3 ls`
        → [(`aws s3 ls`, {"AWS_PROFILE": "prod"})]
    - `sudo time mise exec -- firebase deploy` → [(`firebase deploy`, {})]
    - `gh auth status && gh pr list`
        → [(`gh auth status`, {}), (`gh pr list`, {})]
    - `true & kubectl apply -f x.yaml` → [(`true`, {}), (`kubectl apply -f x.yaml`, {})]

    env は検証 subprocess に渡され、コマンド実行時と同条件でアカウント検証する
    (インライン `AWS_PROFILE` 等を剥がすだけで検証に使わない非対称を解消)。
    区切りの演算子も要るときは `extract_candidates_with_separators`。

    bash と zsh で分解が割れる形 (`extract_candidate_readings`) は、どの読みの候補も返す
    (既定の読みの候補の後ろに、別の読みにだけある候補を足す)。
    """
    first, *others = extract_candidate_readings(command)
    out = [(cand, env) for cand, env, _sep in first]
    seen = {(cand, tuple(sorted(env.items()))) for cand, env in out}
    for reading in others:
        for cand, env, _sep in reading:
            key = (cand, tuple(sorted(env.items())))
            if key not in seen:
                seen.add(key)
                out.append((cand, env))
    return out


def extract_candidate_readings(
    command: str,
) -> list[list[tuple[str, dict[str, str], str]]]:
    """コマンドの読み方ごとの `extract_candidates_with_separators` の結果 (先頭が既定の読み)。

    次の形があるときだけ、別の読みを足す (同じ結果になる読みは足さない):

    - zsh の読み: パラメータ展開の中に素の `{` があるとき。`${...}` の中の素の `{` を数える
      (`echo ${x:-{} #}; aws ...` の aws は zsh でだけ走る)
    - bash の読み: 配列代入 `name=(` を読んだとき。閉じの直後の `#` を語の続きと読み
      (`a=(1 2)# ; aws ...` の aws は bash でだけ走る)、中の `(` で行の残りを読み捨てる
    - ANSI-C の読み: `$'` があるとき。`$'...'` の中の `\'` で引用符を閉じない
      (`echo $'\'' ; aws ...` の aws は bash / zsh とも走る)。bash / zsh の読みも ANSI-C で読む

    どれも既定の読みに足す側で、既定の読みの候補は減らない。読みごとに区切りの並びを保つので、
    `cd` の追跡 (`core/workdir.py`) は読みごとに辿る。
    """
    _SAW_ARRAY[0] = False
    readings = [extract_candidates_with_separators(command)]
    alternatives = []
    if "$'" in command:
        alternatives.append((False, False))
    if _SAW_ARRAY[0]:
        alternatives.append((False, True))
    if "${" in command and command.count("{") > command.count("${"):
        alternatives.append((True, False))
    for zsh, bash in alternatives:
        with _reading(zsh, bash):
            other = extract_candidates_with_separators(command)
        if other not in readings:
            readings.append(other)
    return readings


@contextlib.contextmanager
def _reading(zsh: bool, bash: bool):
    """分解の読み方を一時的に切り替える (別の読みは ANSI-C で読む。`$(` / `${` の終端の memo も捨てる)。"""
    global _ZSH_READING, _BASH_READING, _ANSI_C_READING
    saved = (_ZSH_READING, _BASH_READING, _ANSI_C_READING)
    _ZSH_READING, _BASH_READING, _ANSI_C_READING = zsh, bash, True
    _DOLLAR_MEMO.text = None
    try:
        yield
    finally:
        _ZSH_READING, _BASH_READING, _ANSI_C_READING = saved
        _DOLLAR_MEMO.text = None


def extract_candidates_with_separators(
    command: str,
) -> list[tuple[str, dict[str, str], str]]:
    """`extract_candidates` の各候補に、そのセグメントの直後の区切りを添えて返す。

    区切りは `split_with_separators` と同じ (`&` = バックグラウンド、`""` = 末尾)。
    正規化した結果をもう一度分割した候補 (シェル経由 wrapper の中身・heredoc の本文を
    落とした後の宣言行) は、中の区切りをそのまま使い、最後の 1 つにだけ外側の区切りを付ける。
    正規化して空になったセグメントは候補にならず、その区切りも捨てる。
    返すのは現在の読み (既定の読み) の結果だけ。別の読みも要るときは `extract_candidate_readings`。
    """
    out: list[tuple[str, dict[str, str], str]] = []
    for seg, sep in split_with_separators(command):
        normalized, env = _normalize_segment(seg)
        if not normalized:
            continue
        # シェル経由 wrapper のクォートを剥がすと、中身が複合コマンドのことがある
        # (`watch 'gh pr create && aws s3 rm x'`)。剥がした結果をもう一度分割して
        # 各段を候補にする。分割が要らない通常のセグメントはここを素通りする。
        parts = split_with_separators(normalized)
        if len(parts) > 1:
            last = len(parts) - 1
            for k, (part, inner_sep) in enumerate(parts):
                inner, inner_env = _normalize_segment(part)
                if inner:
                    out.append((inner, {**env, **inner_env}, sep if k == last else inner_sep))
            continue
        out.append((normalized, env, sep))
    return out
