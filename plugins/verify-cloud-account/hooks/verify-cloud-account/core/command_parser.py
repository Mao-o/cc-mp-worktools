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

# wrapper ごとに「値を取るフラグ」(短縮 / 長形式)。ここに無い `-X` は bool として
# 単独トークン消費、`-X=value` / `--key=value` は形式的に 1 トークンで消費。
# 短縮の連結形 (`stdbuf -oL` / `xargs -I{}` / `timeout -k10`) は「値を含む 1
# トークン」としてそのまま消費されるため、ここに登録するのは**分離形**
# (`stdbuf -o L`) が正当な flag だけでよい。
_WRAPPER_FLAGS_WITH_VALUE = {
    "sudo": {
        "-u", "-g", "-U", "-p", "-C", "-D", "-h", "-r", "-t", "-T", "-R", "-a",
        "--user", "--group", "--other-user", "--prompt", "--close-from",
        "--chdir", "--host", "--role", "--type", "--command-timeout",
        "--chroot", "--auth-type",
    },
    "time": {"-o", "-f", "--output", "--format"},
    "npx": {
        "-p", "--package", "-c", "--call",
        "--node-options", "--node-arg",
    },
    "exec": {"-a"},
    # GNU coreutils timeout(1): `timeout [OPTION] DURATION COMMAND [ARG]...`。
    # 値を取るのは `-s/--signal` と `-k/--kill-after` のみ (`--preserve-status` /
    # `--foreground` / `-v/--verbose` は bool)。DURATION は flag ではなく
    # 位置引数なので `_drop_timeout_duration` が別に消費する。
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    # nice(1) (macOS man page 実測): `nice [-n increment] utility [argument ...]`。
    # GNU 版の長形式 `--adjustment=N` も同義。`nice -5 cmd` の連結形は 1 トークン消費。
    "nice": {"-n", "--adjustment"},
    # stdbuf(1) (macOS man page 実測): `stdbuf [-e bufdef] [-i bufdef] [-o bufdef]
    # [command [...]]`。GNU 版の長形式も同義。
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    # caffeinate(8) (macOS man page 実測):
    # `caffeinate [-disu] [-t timeout] [-w pid] [utility arguments...]`。
    "caffeinate": {"-t", "-w"},
    # procps-ng watch(1): 値を取るのは `-n/--interval <secs>` のみ
    # (`-d/--differences` は任意引数だが連結形のみ、他は bool)。
    "watch": {"-n", "--interval"},
    # xargs(1) (macOS man page 実測): `xargs [-0oprt] [-E eofstr]
    # [-I replstr [-R replacements] [-S replsize]] [-J replstr] [-L number]
    # [-n number [-x]] [-P maxprocs] [-s size] [utility [argument ...]]`。
    # GNU 版が追加する `-a file` / `-d delim` と長形式も併記する。
    "xargs": {
        "-E", "-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "-a", "-d",
        "--eof", "--replace", "--max-lines", "--max-args", "--max-procs",
        "--max-chars", "--arg-file", "--delimiter", "--process-slot-var",
    },
    # setsid(1) (util-linux) は `-c/--ctty` / `-f/--fork` / `-w/--wait` のみで
    # 値を取る flag が無い (= 既定の bool 扱いで足りる)。
}


# heredoc の開始 (`<<word` / `<<- word` / `<<'word'` / `<<"word"` / `<<\word`)。
# `man 1 bash` の Here Documents 節: 形式は `<<[-]word`、本文は「word のみを含む行」
# (末尾空白なし) までで、`<<-` は入力行および delimiter 行の先頭タブを除去する。
#
# **bare / backslash 形の word は識別子形 (`[A-Za-z_][A-Za-z0-9_]*`) に限定する**。
# `(( x = 1 << 2 ))` のような算術左シフトは既存スキャナでは保護されておらず、
# 数字を delimiter と誤認すると「その数字だけの行」まで飲み込んで後続セグメント
# (= 検証対象) を消してしまう。識別子形に絞ることでこの誤検出を防ぐ
# (`y << z` 形の算術シフトだけは依然として誤検出しうるが、複数行コマンド中の
# 変数シフトという極めて稀な形に限られる)。
_HEREDOC_START_RE = re.compile(
    r"""<<(?P<dash>-?)[ \t]*(?:
          '(?P<sq>[^']*)'
        | "(?P<dq>[^"]*)"
        | \\(?P<bs>[A-Za-z_][A-Za-z0-9_]*)
        | (?P<bare>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)


def _heredoc_delimiter(match: re.Match) -> str:
    for name in ("sq", "dq", "bs", "bare"):
        value = match.group(name)
        if value is not None:
            return value
    return ""


def _consume_heredoc_body(
    command: str, i: int, delim: str, strip_tabs: bool, buf: list[str]
) -> int:
    """delimiter 行まで (delimiter 行を含む) を buf に取り込み、次の index を返す。

    delimiter が現れないまま入力が尽きたら末尾まで取り込む (bash も未終端 heredoc
    では後続を実行しないため、まとめて 1 セグメントに閉じ込めるのが実行時と整合する)。
    """
    n = len(command)
    while i < n:
        eol = command.find("\n", i)
        end = n if eol == -1 else eol + 1
        line = command[i:end - 1] if eol != -1 else command[i:]
        buf.append(command[i:end])
        i = end
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            break
    return i


def split_on_operators(command: str) -> list[str]:
    """`&&`, `||`, `;`, `|`, `\\n` でトップレベル分割。

    quote ('...' / "..."), $(...), バッククォート内は分割しない。
    subshell `$()` 内でも quote をトラッキングし、`$(printf ")")` のように
    値が `)` を含むケースでも paren_depth を正しく保つ。
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
    in_sq = False
    in_dq = False
    paren_depth = 0
    btick = False
    pending_heredocs: list[tuple[str, bool]] = []

    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""

        if ch == "\\" and not in_sq and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue

        if ch == "'" and not in_dq and not btick:
            in_sq = not in_sq
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_sq and not btick:
            in_dq = not in_dq
            buf.append(ch)
            i += 1
            continue
        if in_sq or in_dq:
            buf.append(ch)
            i += 1
            continue

        if ch == "$" and nxt == "(":
            paren_depth += 1
            buf.append("$")
            buf.append("(")
            i += 2
            continue
        if paren_depth > 0:
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
            buf.append(ch)
            i += 1
            continue

        if ch == "`":
            btick = not btick
            buf.append(ch)
            i += 1
            continue
        if btick:
            buf.append(ch)
            i += 1
            continue

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
            m = _HEREDOC_START_RE.match(command, i)
            if m:
                pending_heredocs.append(
                    (_heredoc_delimiter(m), m.group("dash") == "-")
                )
                buf.append(command[i:m.end()])
                i = m.end()
                continue
            # delimiter として解釈できない `<<` (算術左シフト等) はそのまま流す。
            buf.append("<<")
            i += 2
            continue

        if ch == "\n" and pending_heredocs:
            buf.append(ch)
            i += 1
            for delim, strip_tabs in pending_heredocs:
                i = _consume_heredoc_body(command, i, delim, strip_tabs, buf)
            pending_heredocs = []
            # delimiter 行の改行はトップレベルの区切りなので、ここでセグメントを
            # 閉じる。閉じないと heredoc の**次**のコマンド
            # (`EOF` の後ろに続く `gh pr create` 等) が heredoc セグメントに
            # 吸収され、検証されないまま素通りする。
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


def _drop_wrapper_flags(cmd: str, wrapper: str) -> str:
    """wrapper 直後のフラグ (値あり / 値なし) を剥がす。

    - `--` 単独トークンは POSIX の flag 終端として消費し、それ以降は一切剥がさない
    - `-X=value` / `--key=value` は 1 トークンで消費 (bool / 値あり問わず)
    - `-X` / `--key` が `_WRAPPER_FLAGS_WITH_VALUE[wrapper]` に含まれていれば
      次トークンを値として消費、そうでなければ bool と見なし単独消費
    - 非 `-` トークンが現れた時点で終了 (= コマンド本体の始まり)
    """
    flags_with_value = _WRAPPER_FLAGS_WITH_VALUE.get(wrapper, set())
    s = cmd.lstrip()
    while s:
        m = re.match(r"^(\S+)", s)
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
        if tok in flags_with_value:
            s = s[m.end():].lstrip()
            m2 = re.match(r"^(\S+)", s)
            if m2:
                s = s[m2.end():].lstrip()
        else:
            s = s[m.end():].lstrip()
    return s


# GNU coreutils timeout(1) の DURATION: 「浮動小数点数 + 任意の接尾辞 s/m/h/d」。
_TIMEOUT_DURATION_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[smhd]?$")


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
        m = re.match(r"^(\S+)", s)
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
        rest = _drop_wrapper_flags(rest, t0)
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
        m = re.match(r"^(\S+)", s)
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


def _strip_leading_syntax(cmd: str) -> str:
    """コマンド本体の前に置かれた shell 構文プレフィックスを剥がす。

    対象: `(` / `{` (グルーピング)、`!` (否定)、`if`/`then`/`do` 等の予約語、
    コマンド本体前のリダイレクト。剥がさないと `(gh pr create --fill)` /
    `do gh release upload ...` / `2>/dev/null gh pr create` が行頭 anchored な
    PATTERNS (`^gh(?=\\s|$)`) に一致せず、検証をすり抜ける。
    """
    s = cmd.lstrip()
    while s:
        # `(` はコマンド名と連結できる (`(gh pr create`)。`{` は bash では空白必須
        # だが、同じ扱いで剥がしても実害が無い。
        if s[0] in "({":
            s = s[1:].lstrip()
            continue
        m = re.match(r"^(\S+)", s)
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
    """quote を除いて数えたとき、閉じ括弧が開き括弧より多ければ True。"""
    opener = _CLOSER_TO_OPENER[closer]
    depth = 0
    in_sq = False
    in_dq = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and not in_sq and i + 1 < n:
            i += 2
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif not in_sq and not in_dq:
            if ch == opener:
                depth += 1
            elif ch == closer:
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
        if normalized:
            out.append((normalized, env))
    return out
