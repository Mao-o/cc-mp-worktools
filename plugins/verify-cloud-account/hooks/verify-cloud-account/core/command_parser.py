"""Bash コマンド文字列を検証対象候補セグメントに分解するユーティリティ。

- split_on_operators: `&&` / `||` / `;` / `|` / `\\n` で分割。
  quote / $() / バッククォート内は保護 (subshell 内の quote も追跡)。
  bash コメント (unquoted `#` 以降行末まで) は無視
- strip_leading_env: 先頭の `FOO=bar` 形式環境変数割当を剥がす
- strip_transparent_wrappers: 透過的 wrapper (`sudo`, `time`, `nohup`,
  `env`, `command`, `builtin`, `exec`, `npx`, `pnpm exec`, `pnpm dlx`,
  `mise exec --`, `bun x`) とその直後のフラグ (`sudo -u USER` 等) を剥がす
- extract_candidates: 上記を合成して「コマンドマッチにかける候補断片」を返す

参考: liberzon/claude-hooks の smart_approve.py のアプローチをベースに独自実装。
"""
from __future__ import annotations

import re

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


def strip_leading_env(cmd: str) -> str:
    """先頭の `KEY=VALUE` 形式の環境変数割当を順次剥がす。

    値に $() / バッククォートが含まれると剥がすと意味が変わり得るので保守的に停止。
    `FOO=bar` のみで後続コマンドが無いケースはそのまま返す (空コマンド化を避ける)。
    """
    while True:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", cmd)
        if not m:
            return cmd
        end = _scan_value_end(cmd, m.end())
        if end is None:
            return cmd
        rest = cmd[end:]
        if not rest or rest[0] not in " \t":
            return cmd
        cmd = rest.lstrip()


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


def strip_transparent_wrappers(cmd: str, max_iter: int = 6) -> str:
    """後続コマンドの挙動を変えない wrapper と先頭の env 割当を剥がす。

    多段 (sudo time mise exec -- foo) に対応するため最大 max_iter 回繰り返す。
    """
    for _ in range(max_iter):
        cmd = strip_leading_env(cmd)
        stripped = _strip_one_wrapper(cmd)
        if stripped is None:
            break
        cmd = stripped
    return strip_leading_env(cmd)


def extract_candidates(command: str) -> list[str]:
    """検証対象候補の断片リストを返す。

    - `cd /tmp && FOO=bar gh pr create` → [`cd /tmp`, `gh pr create`]
    - `sudo time mise exec -- firebase deploy` → [`firebase deploy`]
    - `gh auth status && gh pr list` → [`gh auth status`, `gh pr list`]
    """
    out: list[str] = []
    for seg in split_on_operators(command):
        normalized = strip_transparent_wrappers(seg)
        if normalized:
            out.append(normalized)
    return out
