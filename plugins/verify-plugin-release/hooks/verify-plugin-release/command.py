"""Bash コマンド文字列から `gh pr create` / `gh pr ready` の呼び出しを見つける。

ゲートを走らせるかどうかの判定だけが目的で、shell を完全に解釈するわけではない。
`cd <dir> && gh pr create` の `cd` は追跡する (ゲートを走らせる repo が変わるため)。

shlex で分解できないコマンド (クォートの閉じ忘れ等) でも、文字列に
`gh pr create` / `gh pr ready` が現れるなら検出扱いにする。ゲートは異常時に
止める側に倒す方針なので、「解析できないから素通し」にはしない。
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# shlex の punctuation_chars に改行を含め、改行も区切りとして扱う
# (複数行のコマンドを 1 セグメントに潰さないため)。
_PUNCT = "();<>|&\n"
_SEPARATOR_CHARS = set(";&|\n")
_WRAPPERS = {"env", "command", "exec", "time", "nohup"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FALLBACK = re.compile(r"(?:^|[\s;&|(])gh\s+pr\s+(create|ready)\b")


@dataclass(frozen=True)
class Invocation:
    kind: str  # "create" | "ready"
    base: str | None = None  # --base で明示された base branch
    draft: bool = False  # draft PR として作成する
    target: str | None = None  # `gh pr ready <target>` の対象 (番号 / branch / URL)
    head: str | None = None  # `gh pr create --head` で明示された head branch
    cd: str | None = None  # 直前の `cd <dir>` (最後のもの)
    parsed: bool = True  # False = shlex で分解できず文字列一致で検出した


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= _SEPARATOR_CHARS:
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _strip_prefix(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok == "(" or tok in _WRAPPERS or _ASSIGNMENT.match(tok):
            i += 1
            continue
        break
    return seg[i:]


def _is_gh(tok: str) -> bool:
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in ("gh", "gh.exe")


def _parse_create(args: list[str]) -> Invocation:
    base = None
    head = None
    draft = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-B", "--base") and i + 1 < len(args):
            base = args[i + 1]
            i += 2
            continue
        if a in ("-H", "--head") and i + 1 < len(args):
            head = args[i + 1]
            i += 2
            continue
        if a.startswith("--base="):
            base = a.split("=", 1)[1]
        elif a.startswith("--head="):
            head = a.split("=", 1)[1]
        elif a in ("-d", "--draft"):
            draft = True
        i += 1
    return Invocation(kind="create", base=base or None, head=head or None, draft=draft)


# `gh pr ready` で値を取るオプション (位置引数と取り違えないため)。
_READY_VALUE_OPTS = {"-R", "--repo"}


def _parse_ready(args: list[str]) -> Invocation | None:
    target = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--undo":
            return None  # ready -> draft への巻き戻しは対象外
        if a in _READY_VALUE_OPTS:
            i += 2
            continue
        if not a.startswith("-") and target is None:
            target = a
        i += 1
    return Invocation(kind="ready", target=target)


def _skip_global_flags(args: list[str]) -> list[str]:
    """`gh -R owner/repo pr create` のように subcommand の前に置いた flag を読み飛ばす。"""
    i = 0
    while i < len(args):
        a = args[i]
        if a in _READY_VALUE_OPTS:
            i += 2
        elif a.startswith("--repo="):
            i += 1
        else:
            break
    return args[i:]


def find_invocation(command: str) -> Invocation | None:
    """コマンド中の最初の `gh pr create` / `gh pr ready` を返す。無ければ None。"""
    if "gh" not in command or "pr" not in command:
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        m = _FALLBACK.search(command)
        if not m:
            return None
        return Invocation(
            kind=m.group(1),
            draft=bool(re.search(r"\s(?:--draft|-d)\b", command)),
            parsed=False,
        )

    cd = None
    for seg in _segments(tokens):
        seg = _strip_prefix(seg)
        if not seg:
            continue
        if seg[0] == "cd":
            cd = seg[1] if len(seg) > 1 else None
            continue
        if _is_gh(seg[0]):
            seg = [seg[0], *_skip_global_flags(seg[1:])]
        if len(seg) >= 3 and _is_gh(seg[0]) and seg[1] == "pr":
            if seg[2] == "create":
                inv = _parse_create(seg[3:])
            elif seg[2] == "ready":
                inv = _parse_ready(seg[3:])
                if inv is None:
                    continue
            else:
                continue
            return Invocation(
                kind=inv.kind,
                base=inv.base,
                head=inv.head,
                draft=inv.draft,
                target=inv.target,
                cd=cd,
            )
    return None
