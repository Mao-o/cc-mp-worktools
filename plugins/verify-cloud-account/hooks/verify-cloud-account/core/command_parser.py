"""Bash コマンド文字列を検証対象候補セグメントに分解するユーティリティ。

- split_on_operators: `&&` / `||` / `;` / `|` / `\\n` で分割。
  quote / $() / バッククォート内は保護 (subshell 内の quote も追跡)。
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
# 追加時のチェックリストは CLAUDE.local.md の D16 ノート参照。
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
    算術評価 `(( ... ))`。該当しなければ None。
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
    if ch == "$" and i + 1 < n and text[i + 1] == "(":
        depth = 1
        j = i + 2
        while j < n:
            inner = _skip_opaque(text, j)
            if inner is not None and inner > j:
                j = inner
                continue
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    return _arithmetic_span(text, i)


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
    """`&&`, `||`, `;`, `|`, `\\n` でトップレベル分割。

    quote / `$(...)` / バッククォート / 算術評価 `(( ... ))` の内側は
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
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    pending_heredocs: list[tuple[str, bool]] = []

    while i < n:
        # quote / 置換 / 算術は 1 領域としてそのまま buf へ (構文解釈しない)。
        opaque = _skip_opaque(command, i)
        if opaque is not None and opaque > i:
            buf.append(command[i:opaque])
            i = opaque
            continue

        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""

        if ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            while i < n and command[i] != "\n":
                i += 1
            continue

        if ch == "<" and nxt == "<":
            # here-string `<<<` は heredoc ではない (本文行を持たない)。
            if i + 2 < n and command[i + 2] == "<":
                buf.append("<<<")
                i += 3
                continue
            found = _scan_heredoc_start(command, i)
            if found is not None:
                delim, dash, hd_end = found
                pending_heredocs.append((delim, dash))
                buf.append(command[i:hd_end])
                i = hd_end
                continue
            # delimiter を解決できない `<<` はそのまま流す = heredoc と見なさない
            # (本文行が候補になる = 過剰検証側)。
            buf.append("<<")
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
                segments.append("".join(buf))
                buf = []
                continue

        if ch == "&" and nxt == "&":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|" and nxt == "|":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == ";":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch == "|":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch == "\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


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
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", cmd)
        if not m:
            return cmd, collected
        end = _scan_value_end(cmd, m.end())
        if end is None:
            return cmd, collected
        rest = cmd[end:]
        if not rest or rest[0] not in " \t":
            return cmd, collected
        key = m.group(1)
        value = _unquote_env_value(cmd[m.end():end])
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


def _drop_wrapper_flags(cmd: str, wrapper: str, skip_arg_like: bool = True) -> str:
    """wrapper 直後のフラグ (値あり / 値なし / optional) を剥がす。

    - `--` 単独トークンは POSIX の flag 終端として消費し、それ以降は一切剥がさない
    - `-X=value` / `--key=value` は 1 トークンで消費 (bool / 値あり問わず)
    - `--key` が `_WRAPPER_FLAGS_OPTIONAL_VALUE[wrapper]` にあれば **bare 形は値を
      取らない** (GNU の optional argument 規約。値は `=` 形でのみ渡る)
    - `-X` / `--key` が `_WRAPPER_FLAGS_WITH_VALUE[wrapper]` に含まれていれば
      次トークンを値として消費、そうでなければ bool と見なし単独消費
    - 非 `-` トークンが現れた時点で flag 領域は終了 (= コマンド本体の始まり)

    最後に `skip_arg_like` が真なら、**コマンド名ではありえない引数トークン**
    (純粋な数値 + 任意の時間単位) を読み飛ばす。未登録の値付き flag があっても
    値が数値ならコマンド本体に到達できる安全網で、これがあるので一次情報を
    取れない wrapper (`watch` 等) の flag 表を空にできる。位置引数を持つ
    `timeout` だけは自前の DURATION 処理と衝突するため無効にする。
    """
    flags_with_value = _WRAPPER_FLAGS_WITH_VALUE.get(wrapper, set())
    flags_optional = _WRAPPER_FLAGS_OPTIONAL_VALUE.get(wrapper, set())
    s = cmd.lstrip()
    while s:
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok == "--":
            s = s[m.end():].lstrip()
            break
        if not tok.startswith("-"):
            break
        if "=" in tok:
            s = s[m.end():].lstrip()
            continue
        if tok in flags_optional:
            # bare 形は値を取らない (`xargs --replace` の既定は `{}`)。
            s = s[m.end():].lstrip()
            continue
        if tok in flags_with_value:
            s = s[m.end():].lstrip()
            m2 = _VALUE_TOKEN_RE.match(s)
            if m2:
                s = s[m2.end():].lstrip()
        else:
            s = s[m.end():].lstrip()
    if skip_arg_like:
        while s:
            m = _WORD_TOKEN_RE.match(s)
            if not m or not _ARG_LIKE_RE.match(m.group(1)):
                break
            s = s[m.end():].lstrip()
    return s


# GNU coreutils timeout(1) の DURATION。公式記述は「浮動小数点数 + 任意の接尾辞
# (s/m/h/d)」で、実装は strtod ベースなので **10 進の整数・小数だけではない**:
# 科学記数法 (`1e3` / `1E3` / `1e-3`)、小数点始まり (`.5`) / 終わり (`5.`)、
# `inf` / `infinity` / `nan`、符号付き、16 進浮動小数 (`0x1p3`) まで受理される。
# ここで**受理し損ねると wrapper を剥がせず検証が消える**ため、迷う形は受理側に倒す
# (受理しすぎても、消費するのは DURATION の位置にある 1 トークンだけで、
#  コマンド名が数値・inf・nan になることは実質無い)。
_TIMEOUT_DURATION_RE = re.compile(
    r"""^[+-]?(?:
          inf(?:inity)?
        | nan
        | 0[xX][0-9a-fA-F]*(?:\.[0-9a-fA-F]*)?(?:[pP][+-]?[0-9]+)?
        | (?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?
    )[smhd]?$""",
    re.VERBOSE | re.IGNORECASE,
)


def _drop_timeout_duration(rest: str) -> str | None:
    """`timeout` のフラグを剥がした後に残る位置引数 DURATION を消費する。

    `timeout 30 gh pr create` → `gh pr create`。DURATION の形をしていない
    (= 文書化された呼び出し形ではない) ときは None を返して wrapper 自体を
    剥がさない — 誤って本体トークンを食うより、従来どおり不透明なセグメントとして
    検証をスキップする方が安全側 (lenient)。
    """
    m = re.match(r"^(\S+)", rest)
    if not m or not _TIMEOUT_DURATION_RE.match(m.group(1)):
        return None
    return rest[m.end():].lstrip()


def _has_flag(rest: str, wrapper: str, names) -> bool:
    """wrapper の flag 領域に `names` のいずれかがあるか (値の消費規則を尊重)。"""
    flags_with_value = _WRAPPER_FLAGS_WITH_VALUE.get(wrapper, set())
    s = rest.lstrip()
    while s:
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok == "--" or tok == "-" or not tok.startswith("-"):
            break
        if tok.split("=", 1)[0] in names:
            return True
        s = s[m.end():].lstrip()
        if "=" not in tok and tok in flags_with_value:
            m2 = _VALUE_TOKEN_RE.match(s)
            if m2:
                s = s[m2.end():].lstrip()
    return False


def _flag_argument(rest: str, wrapper: str, names) -> str | None:
    """wrapper の flag 領域から `names` のいずれかの**値**を取り出す。"""
    flags_with_value = _WRAPPER_FLAGS_WITH_VALUE.get(wrapper, set())
    s = rest.lstrip()
    while s:
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok == "--" or tok == "-" or not tok.startswith("-"):
            break
        name, eq, embedded = tok.partition("=")
        s = s[m.end():].lstrip()
        if name in names:
            if eq:
                return embedded
            m2 = _VALUE_TOKEN_RE.match(s)
            return m2.group(1) if m2 else None
        if not eq and tok in flags_with_value:
            m2 = _VALUE_TOKEN_RE.match(s)
            if m2:
                s = s[m2.end():].lstrip()
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
    (= 実行されないコマンドで deny しない)。`--` 以降と非 flag トークン以降は
    後続コマンドの引数なので見ない。`watch --help; gh pr create` のように**別
    セグメント**として続くコマンドは、セグメント分割が先に走るためここの影響を
    受けず従来どおり検証される。
    """
    terminating = _TERMINATING_FLAGS | _WRAPPER_EXTRA_TERMINATING.get(wrapper, frozenset())
    flags_with_value = _WRAPPER_FLAGS_WITH_VALUE.get(wrapper, set())
    s = rest.lstrip()
    while s:
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok == "--" or tok == "-" or not tok.startswith("-"):
            break
        name = tok.split("=", 1)[0]
        if name in terminating:
            return True
        s = s[m.end():].lstrip()
        if "=" not in tok and tok in flags_with_value:
            m2 = _VALUE_TOKEN_RE.match(s)
            if m2:
                s = s[m2.end():].lstrip()
    return False


def _command_is_query(rest: str) -> bool:
    """`command -v` / `command -V` (存在確認クエリ) なら True。

    `man 1 bash` の逐語: `command [-pVv] command [arg ...]` /「If either the -V or
    -v option is supplied, a description of command is printed」。つまり `-v` /
    `-V` 付きの `command` は後続 CLI を**実行しない**ので、wrapper として剥がして
    しまうと `command -v gh` が `gh` 単体の候補になり、単なる存在確認で
    アカウント検証が走って誤 deny される。`-pv` のような連結形も同じ扱い。
    `-p` のみ / `--` / フラグ無しは実行するので従来どおり剥がす。
    """
    s = rest.lstrip()
    while s:
        m = _WORD_TOKEN_RE.match(s)
        if not m:
            break
        tok = m.group(1)
        if tok == "--" or tok == "-" or not tok.startswith("-"):
            break
        # `command` に長形式オプションは無い (bash / POSIX とも `-p` `-v` `-V`)。
        if not tok.startswith("--") and ("v" in tok[1:] or "V" in tok[1:]):
            return True
        s = s[m.end():].lstrip()
    return False


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
    """セグメントに取り込まれた heredoc 本文を落とし、コマンド行だけを残す。

    `split_on_operators` は heredoc 本文を分割しないよう同一セグメントへ取り込む。
    しかし本文は**コマンドの引数ではなくデータ**なので、そのまま候補文字列に残すと
    後段の option 走査 (`cli_options.find_context_options`) が本文中の行を
    コマンドラインの一部として読んでしまう。実害は両方向にある:

    - `aws cloudformation deploy --template-file - <<EOF` の本文に `--profile prod`
      があると、実行は既定 profile なのに **prod で検証して allow** する (false-allow)
    - `kubectl apply -f - <<EOF` の YAML 本文に `- --context` / `- prod` があると
      `--context` 指定と誤読して **誤 deny** する

    本文を削っても heredoc の**開始行**は残るので、service の match / readonly 判定は
    従来どおり動く。deny 文面の表示も本文が混ざらず読みやすくなる。
    """
    if "<<" not in cmd:
        return cmd
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        eol = cmd.find("\n", i)
        end = n if eol == -1 else eol + 1
        line = cmd[i:end]
        out.append(line)
        i = end
        # この行で開かれた heredoc の delimiter を宣言順に集める。
        # quote / 置換 / 算術の内側は構文として解釈しない — `split_on_operators`
        # と**同じ `_skip_opaque`** を通す。追跡を怠ると
        # `--metadata '{"k":"<<X bar"}' <<EOF` の `X` を先に登録してしまい、
        # 本文の除去に失敗して本文行が引数として解釈される (誤コンテキスト検証)。
        pending: list[tuple[str, bool]] = []
        k = 0
        while k < len(line):
            opaque = _skip_opaque(line, k)
            if opaque is not None and opaque > k:
                k = opaque
                continue
            if line[k] == "<" and k + 1 < len(line) and line[k + 1] == "<":
                found = _scan_heredoc_start(line, k)
                if found is not None:
                    pending.append((found[0], found[1]))
                    k = found[2]
                    continue
            k += 1
        for delim, strip_tabs in pending:
            found = _find_heredoc_end(cmd, i, delim, strip_tabs)
            if found is None:
                break
            i = found  # 本文 + delimiter 行を出力せず読み飛ばす
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

    env は検証 subprocess に渡され、コマンド実行時と同条件でアカウント検証する
    (インライン `AWS_PROFILE` 等を剥がすだけで検証に使わない非対称を解消)。
    """
    out: list[tuple[str, dict[str, str]]] = []
    for seg in split_on_operators(command):
        normalized, env = _normalize_segment(seg)
        if not normalized:
            continue
        # シェル経由 wrapper のクォートを剥がすと、中身が複合コマンドのことがある
        # (`watch 'gh pr create && aws s3 rm x'`)。剥がした結果をもう一度分割して
        # 各段を候補にする。分割が要らない通常のセグメントはここを素通りする。
        parts = split_on_operators(normalized)
        if len(parts) > 1:
            for part in parts:
                inner, inner_env = _normalize_segment(part)
                if inner:
                    out.append((inner, {**env, **inner_env}))
            continue
        out.append((normalized, env))
    return out
