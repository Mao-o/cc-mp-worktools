"""Bash コマンド文字列を検証対象候補セグメントに分解するユーティリティ。

- split_on_operators: `&&` / `||` / `;` / `|` / `\\n` で分割。
  quote / $() / バッククォート内は保護 (subshell 内の quote も追跡)。
  bash コメント (unquoted `#` 以降行末まで) は無視
- strip_leading_env: 先頭の `FOO=bar` 形式環境変数割当を剥がす
- strip_transparent_wrappers: 透過的 wrapper (`sudo`, `time`, `nohup`,
  `env`, `command`, `builtin`, `exec`, `npx`, `pnpm exec`, `pnpm dlx`,
  `mise exec --`, `bun x`) とその直後のフラグ (`sudo -u USER` 等) を剥がす
- extract_candidates: 上記を合成して「コマンドマッチにかける候補断片」と、
  その断片の先頭に書かれていたインライン環境変数 (`AWS_PROFILE=prod` 等) の
  dict を返す。検証 subprocess に同じ env を渡してコマンド実行時と同条件で
  アカウント検証するため (剥がすだけで使わない非対称を解消)

参考: liberzon/claude-hooks の smart_approve.py のアプローチをベースに独自実装。
"""
from __future__ import annotations

import re
import shlex

_WRAPPERS_SINGLE = {"sudo", "time", "nohup", "command", "builtin", "exec", "npx"}
_WRAPPERS_TWO = {("pnpm", "exec"), ("pnpm", "dlx"), ("bun", "x")}
_WRAPPERS_THREE = {("mise", "exec", "--")}

# wrapper ごとに「値を取るフラグ」(短縮 / 長形式)。ここに無い `-X` は bool として
# 単独トークン消費、`-X=value` / `--key=value` は形式的に 1 トークンで消費。
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
}


def split_on_operators(command: str) -> list[str]:
    """`&&`, `||`, `;`, `|`, `\\n` でトップレベル分割。

    quote ('...' / "..."), $(...), バッククォート内は分割しない。
    subshell `$()` 内でも quote をトラッキングし、`$(printf ")")` のように
    値が `)` を含むケースでも paren_depth を正しく保つ。
    unquoted な `#` (行頭 / 空白 / 演算子の直後に来るもの) 以降改行までは
    bash コメントとして無視する。
    """
    segments: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    in_sq = False
    in_dq = False
    paren_depth = 0
    btick = False

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
        return _drop_wrapper_flags(rest, t0)

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


def _normalize_segment(cmd: str, max_iter: int = 6) -> tuple[str, dict[str, str]]:
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
        cmd, env = _parse_leading_env(cmd)
        collected.update(env)  # 内側 (後段) が外側を上書きする
        # sudo を剥がす直前の判定: preserve-env 指定が無ければ、ここまでに集めた
        # pre-sudo env を sudo が scrub するので破棄する。剥がし自体は
        # _strip_one_wrapper に委ねる (sudo の flag 消費規則と一致させる)。
        toks = _tokens(cmd)
        if toks and toks[0] == "sudo":
            rest_after_sudo = _drop_tokens(cmd, 1)
            if not _sudo_preserves_env(rest_after_sudo):
                collected.clear()
        stripped = _strip_one_wrapper(cmd)
        if stripped is None:
            break
        cmd = stripped
    cmd, env = _parse_leading_env(cmd)
    collected.update(env)
    return cmd, collected


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
