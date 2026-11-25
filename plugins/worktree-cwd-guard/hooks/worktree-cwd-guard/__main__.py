#!/usr/bin/env python3
"""worktree-cwd-guard: linked worktree で動く session が、同じ repo の別 checkout
(main 側や他の worktree) を書き換える操作を止める PreToolUse hook。

対象:
- Bash: 別 checkout を対象にした git の書き込み操作 (checkout / commit / reset など)
- Write / Edit / NotebookEdit: 別 checkout 配下のファイルへの書き込み

作業ディレクトリが main checkout や repo の外なら何もしない。行き先を静的に解決できない
操作は止めずに注意だけ出す。

環境変数:
- WORKTREE_CWD_GUARD_MODE: enforce (既定) / warn (止めずに伝える) / off
- WORKTREE_CWD_GUARD_ALLOW: 書き込みを許す checkout root (os.pathsep 区切り。Windows では Git Bash の `/c/x` も読む)
"""
from __future__ import annotations

import json
import os
import sys

import command
from family import Family, detect_readings, drive_spellings, norm, owner

MODE_ENV = "WORKTREE_CWD_GUARD_MODE"
ALLOW_ENV = "WORKTREE_CWD_GUARD_ALLOW"
_TAG = "[worktree-cwd-guard]"
_PATH_KEYS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def _mode() -> str:
    v = os.environ.get(MODE_ENV, "enforce").strip().lower()
    return v if v in ("enforce", "warn", "off") else "enforce"


def _allowed_roots() -> set[str]:
    # Windows では `/c/x` を Git Bash の読み (`C:\x`) でも読む。Bash の行き先を読むのと同じ和 (family.drive_spellings) で、
    # deny の行き先と同じ綴りで書いた root が一致しないことを防ぐ
    raw = os.environ.get(ALLOW_ENV, "")
    return {norm(s) for p in raw.split(os.pathsep) if p.strip() for s in drive_spellings(p)}


def _out(decision: str | None, text: str) -> dict:
    body: dict = {"hookEventName": "PreToolUse"}
    if decision == "deny":
        body["permissionDecision"] = "deny"
        body["permissionDecisionReason"] = text
    else:
        body["additionalContext"] = text
    return {"hookSpecificOutput": body}


def _file_findings(path: str, cwd: str, fam: Family) -> list[command.Finding]:
    # Claude Code は Write / Edit のパスを hook の前に絶対パスへ展開して渡す (Windows では `\` 区切りの `C:\...`)。
    # `/c/x` の綴りのまま届いたときは、Bash の行き先と同じく Windows の読みと Git Bash の読みの和で判定する
    # 綴りの違う 2 つの読みが同じ行き先になることがある (`/c/../c/proj` など)。同じ理由を 2 回並べないよう、行き先でまとめる
    targets = (norm(s if os.path.isabs(s) else os.path.join(cwd, s)) for s in drive_spellings(path))
    findings = []
    for target in dict.fromkeys(targets):
        o = owner(target, fam)
        if o is not None and o != fam.home:
            findings.append(command.Finding(True, f"別の checkout ({o}) 配下のファイル ({target}) に書き込もうとしている", o))
    return findings


def evaluate(data: dict) -> dict | None:
    mode = _mode()
    if mode == "off":
        return None
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    if tool != "Bash" and tool not in _PATH_KEYS:
        return None
    if tool == "Bash" and "git" not in str(tool_input.get("command") or ""):
        return None  # git を含まない Bash は対象外 (git を起動せずに返す)

    # cwd が Git Bash の綴り (`/c/...`) で届いたときは、Windows の読みと Git Bash の読みの両方で linked worktree かを見る。
    # 読みごとに、その読みの cwd と Family で判定し、どちらかで別の checkout なら止める (Bash の行き先と同じ和)
    readings = detect_readings(cwd)
    if not readings:
        return None

    if tool == "Bash":
        findings = command.analyze_readings(str(tool_input.get("command") or ""), readings)
    else:
        path = tool_input.get(_PATH_KEYS[tool])
        found = (f for here, fam in readings for f in _file_findings(path, here, fam)) if isinstance(path, str) and path else ()
        findings = list(dict.fromkeys(found))  # 2 つの読みが同じ行き先を指したら、同じ理由を 2 回並べない

    allowed = _allowed_roots()
    findings = [f for f in findings if not (f.blocked and f.root in allowed)]
    if not findings:
        return None

    blocked = [f.message for f in findings if f.blocked]
    notes = [f.message for f in findings if not f.blocked]
    header = f"この session の worktree: {' / '.join(dict.fromkeys(fam.home for _, fam in readings))}"
    if blocked and mode == "enforce":
        lines = [f"{_TAG} 別の checkout を書き換える操作を止めた。", header, *(f"- {m}" for m in blocked)]
        lines.append(
            "自分の worktree の中で作業する。意図した操作なら、対象の checkout を "
            f"{ALLOW_ENV} に加えるか {MODE_ENV}=warn で実行する。"
        )
        return _out("deny", "\n".join(lines))
    msgs = blocked + notes
    prefix = f"{_TAG} 別の checkout を書き換える可能性がある ({MODE_ENV}=warn のため止めていない)" if blocked else f"{_TAG} 対象を確認できなかった操作がある (止めていない)"
    return _out(None, "\n".join([prefix, header, *(f"- {m}" for m in msgs)]))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, OSError):
        return
    if not isinstance(data, dict):
        return
    try:
        result = evaluate(data)
    except Exception as e:  # noqa: BLE001 - 判定の失敗で作業を止めない (うっかり予防の plugin)
        result = _out(None, f"{_TAG} 内部エラーのため判定をスキップした: {type(e).__name__}: {e}")
    if result is not None:
        json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
