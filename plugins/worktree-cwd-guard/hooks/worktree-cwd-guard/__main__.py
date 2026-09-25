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
- WORKTREE_CWD_GUARD_ALLOW: 書き込みを許す checkout root (os.pathsep 区切り)
"""
from __future__ import annotations

import json
import os
import sys

import command
from family import Family, detect, norm, owner

MODE_ENV = "WORKTREE_CWD_GUARD_MODE"
ALLOW_ENV = "WORKTREE_CWD_GUARD_ALLOW"
_TAG = "[worktree-cwd-guard]"
_PATH_KEYS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def _mode() -> str:
    v = os.environ.get(MODE_ENV, "enforce").strip().lower()
    return v if v in ("enforce", "warn", "off") else "enforce"


def _allowed_roots() -> set[str]:
    raw = os.environ.get(ALLOW_ENV, "")
    return {norm(p) for p in raw.split(os.pathsep) if p.strip()}


def _out(decision: str | None, text: str) -> dict:
    body: dict = {"hookEventName": "PreToolUse"}
    if decision == "deny":
        body["permissionDecision"] = "deny"
        body["permissionDecisionReason"] = text
    else:
        body["additionalContext"] = text
    return {"hookSpecificOutput": body}


def _file_findings(path: str, cwd: str, fam: Family) -> list[command.Finding]:
    target = norm(path if os.path.isabs(path) else os.path.join(cwd, path))
    o = owner(target, fam)
    if o is not None and o != fam.home:
        return [command.Finding(True, f"別の checkout ({o}) 配下のファイル ({target}) に書き込もうとしている", o)]
    return []


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

    fam = detect(cwd)
    if fam is None:
        return None

    if tool == "Bash":
        findings = command.analyze(str(tool_input.get("command") or ""), cwd, fam)
    else:
        path = tool_input.get(_PATH_KEYS[tool])
        findings = _file_findings(path, cwd, fam) if isinstance(path, str) and path else []

    allowed = _allowed_roots()
    findings = [f for f in findings if not (f.blocked and f.root in allowed)]
    if not findings:
        return None

    blocked = [f.message for f in findings if f.blocked]
    notes = [f.message for f in findings if not f.blocked]
    header = f"この session の worktree: {fam.home}"
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
