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
_WRAPPERS = {"env", "command", "exec", "time", "nohup"}
# 制御構文の予約語。`if x; then gh pr create; fi` の `then gh ...` を読めるようにする
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
# `gh pr new` は `gh pr create` の別名
_CREATE = {"create", "new"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 実行位置に置かれた gh pr create / new / ready (コマンド置換 `$(` や backtick の中を含む)。
# 文中の言及 ("... run gh pr create later") は拾わないよう、直前が区切りか行頭のものに限る
_EXEC_POS = re.compile(
    r"(?:^|[;&|(\n`{]|\$\()\s*(?:(?:if|then|else|elif|do|while|until|!|env|command|exec|time|nohup)\s+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:\S*/)?gh(?:\.exe)?\s+"
    r"(?:(?:-R|--repo)(?:\s+|=)\S+\s+)*pr\s+(?:(?:-R|--repo)(?:\s+|=)\S+\s+)*(create|new|ready)\b([^;&|\n)`]*)"
)
# `bash -c '...'` / `sh -lc "..."` / `eval "..."` の中身。中の PR 操作は構文解析の対象外になる
_NESTED_SHELL = re.compile(
    r"(?:\b(?:ba|z|k|da|a)?sh(?:\.exe)?\s+(?:-\w+\s+)*-\w*c\w*|\beval)\s+(['\"])(.*?)\1",
    re.S,
)
_DIR_CHANGERS = re.compile(r"(?:^|[;&|(\n`{]|\$\()\s*(?:pushd|popd)\b")
_FALLBACK = re.compile(r"(?:^|[\s;&|(])gh\s+pr\s+(create|new|ready)\b")


@dataclass(frozen=True)
class Invocation:
    kind: str  # "create" | "ready"
    base: str | None = None  # --base で明示された base branch
    draft: bool = False  # draft PR として作成する
    target: str | None = None  # `gh pr ready <target>` の対象 (番号 / branch / URL)
    head: str | None = None  # `gh pr create --head` で明示された head branch
    repo: str | None = None  # `-R / --repo` で明示された repo (OWNER/REPO か HOST/OWNER/REPO)
    cd: str | None = None  # 直前の `cd <dir>` (最後のもの)
    parsed: bool = True  # False = shlex で分解できず文字列一致で検出した


def _is_separator(tok: str) -> bool:
    """punctuation だけの token が区切りを含むか。

    shlex は `);` のように隣り合う記号を 1 token にまとめるため、token 全体ではなく
    中に区切り文字があるかで判定する。`>&` / `&>` などのリダイレクトは区切りではない。
    """
    if not set(tok) <= set(_PUNCT):
        return False
    if any(c in tok for c in ";|\n"):
        return True
    return "&" in tok and not tok.startswith((">", "<")) and not tok.endswith(">")


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok and _is_separator(tok):
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _strip_prefix(seg: list[str]) -> tuple[list[str], dict[str, str]]:
    """先頭の `(` / 予約語 / wrapper / `VAR=value` を外し、(残り, 代入) を返す。"""
    assigns: dict[str, str] = {}
    i = 0
    while i < len(seg):
        tok = seg[i]
        if _ASSIGNMENT.match(tok):
            name, _, value = tok.partition("=")
            assigns[name] = value
            i += 1
            continue
        if tok == "(" or tok in _WRAPPERS or tok in _KEYWORDS:
            i += 1
            continue
        break
    return seg[i:], assigns


def _is_gh(tok: str) -> bool:
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name in ("gh", "gh.exe")


def _parse_create(args: list[str]) -> Invocation:
    base = None
    head = None
    repo = None
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
        if a in _READY_VALUE_OPTS and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("--base="):
            base = a.split("=", 1)[1]
        elif a.startswith("--head="):
            head = a.split("=", 1)[1]
        elif a in ("-d", "--draft"):
            draft = True
        i += 1
    return Invocation(kind="create", base=base or None, head=head or None, repo=repo or None, draft=draft)


# `gh pr ready` で値を取るオプション (位置引数と取り違えないため)。
_READY_VALUE_OPTS = {"-R", "--repo"}


def _parse_ready(args: list[str]) -> Invocation | None:
    target = None
    repo = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--undo":
            return None  # ready -> draft への巻き戻しは対象外
        if a in _READY_VALUE_OPTS:
            repo = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
            i += 1
            continue
        if not a.startswith("-") and target is None:
            target = a
        i += 1
    return Invocation(kind="ready", target=target, repo=repo)


def _split_global_flags(args: list[str]) -> tuple[str | None, list[str]]:
    """`gh -R owner/repo pr create` のように subcommand の前に置いた --repo を取り出す。"""
    repo = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in _READY_VALUE_OPTS:
            repo = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif a.startswith("--repo="):
            repo = a.split("=", 1)[1]
            i += 1
        else:
            break
    return repo, args[i:]


def find_invocations(command: str) -> list[Invocation]:
    """コマンド中の `gh pr create` / `gh pr ready` をすべて順に返す。

    `gh pr create --draft && gh pr ready` のように 1 つのコマンドで draft 作成と
    ready 化を続ける形があるため、最初の 1 つだけを見て判定してはいけない。
    """
    if "gh" not in command or "pr" not in command:
        return []
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return [
            Invocation(
                kind="ready" if m.group(1) == "ready" else "create",
                draft=bool(re.search(r"\s(?:--draft|-d)\b", command)) and m.group(1) != "ready",
                parsed=False,
            )
            for m in _FALLBACK.finditer(command)
        ]

    found: list[Invocation] = []
    cd = None
    for seg in _segments(tokens):
        seg, assigns = _strip_prefix(seg)
        if not seg:
            continue
        if seg[0] == "cd":
            cd = seg[1] if len(seg) > 1 else None
            continue
        if not _is_gh(seg[0]):
            continue
        global_repo, rest = _split_global_flags(seg[1:])
        if not rest or rest[0] != "pr":
            continue
        # `gh pr -R owner/repo create` のように pr と action の間にも --repo を置ける
        pr_repo, action = _split_global_flags(rest[1:])
        rest = ["pr", *action]
        global_repo = pr_repo or global_repo
        if len(rest) < 2:
            continue
        if rest[1] in _CREATE:
            inv = _parse_create(rest[2:])
        elif rest[1] == "ready":
            inv = _parse_ready(rest[2:])
            if inv is None:
                continue
        else:
            continue
        found.append(
            Invocation(
                kind=inv.kind,
                base=inv.base,
                head=inv.head,
                repo=inv.repo or global_repo or assigns.get("GH_REPO") or None,
                draft=inv.draft,
                target=inv.target,
                cd=cd,
            )
        )
    return found


def unresolved_reason(command: str, found: list[Invocation]) -> str | None:
    """解析しきれていない PR 操作がありそうなら、その理由を返す。

    shell の書き方は無数にあり (コマンド置換・pushd・関数定義…)、個別に追うと取りこぼす。
    実行位置に現れる PR 操作の数と、構文解析で認識できた数が合わない場合は
    「検査対象を特定できない」として扱い、呼び出し側で止める。
    """
    expected = 0
    for m in _EXEC_POS.finditer(command):
        if m.group(1) == "ready" and "--undo" in m.group(2):
            continue
        expected += 1
    for m in _NESTED_SHELL.finditer(command):
        if _EXEC_POS.search(m.group(2)):
            return "PR 操作が別の shell (bash -c / eval 等) の中にある"
    if expected > len(found) or any(not inv.parsed for inv in found):
        return "PR 操作の位置を解析できない (コマンド置換・サブシェル等の中にある)"
    if found and _DIR_CHANGERS.search(command):
        return "pushd / popd による移動先を追跡できない"
    return None


def find_invocation(command: str) -> Invocation | None:
    """最初の `gh pr create` / `gh pr ready` を返す。無ければ None。"""
    found = find_invocations(command)
    return found[0] if found else None
