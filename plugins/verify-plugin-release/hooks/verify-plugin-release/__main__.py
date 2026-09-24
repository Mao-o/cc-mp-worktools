#!/usr/bin/env python3
"""verify-plugin-release: `gh pr create` / `gh pr ready` の前に完了条件を検査する hook。

PreToolUse:Bash に登録する。対象コマンドでなければ何も出力せずに終わる。

判定:
- FAIL がある → PR 作成を止める (deny)。理由には FAIL / WARN の行だけを載せる
- ゲートを完了できない (時間切れ・内部エラー・設定の破損) → 同じく止める。
  hook 自体が時間切れになると Claude Code はコマンドをそのまま実行するため、
  ゲート内部で先に打ち切って止める判断を返す
- draft PR の作成 / VERIFY_PLUGIN_RELEASE_MODE=warn → 止めずに結果を伝える
- VERIFY_PLUGIN_RELEASE_MODE=off → 何もしない

手動実行: `python3 <plugin>/hooks/verify-plugin-release check [--base BRANCH] [PATH]`
(終了コード 0 = PASS / 1 = FAIL / 2 = ゲートを完了できなかった)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import config
import gate
import layout
from command import find_invocation
from runner import Deadline, GateTimeout, git, run

MODE_ENV = "VERIFY_PLUGIN_RELEASE_MODE"
_REASON_LIMIT = 1800
_TAG = "[verify-plugin-release]"


def _mode() -> str:
    v = os.environ.get(MODE_ENV, "enforce").strip().lower()
    return v if v in ("enforce", "warn", "off") else "enforce"


def format_report(rep: gate.Report, verbose: bool) -> str:
    lines = [f"base: {rep.base or '?'} / branch: {rep.branch or '?'}"]
    lines += [f"NOTE: {n}" for n in rep.notes]
    for r in rep.results:
        if verbose or r.status in (gate.FAIL, gate.WARN):
            lines.append(f"{r.status:<4}  {r.check}: {r.detail}")
    return "\n".join(lines)


def _clip(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 20] + "\n... (省略)"


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _clip(reason),
        }
    }


def _context(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": _clip(text)}}


def _repo_root(cwd: Path, dl: Deadline) -> Path | None:
    try:
        r = git(["rev-parse", "--show-toplevel"], cwd, dl, cap=5)
    except (OSError, GateTimeout):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def _pr_refs(root: Path, target: str | None, dl: Deadline) -> tuple[str, str] | None:
    """`gh pr view` で (head, base) を得る。取れなければ None。"""
    args = ["gh", "pr", "view", "--json", "headRefName,baseRefName"]
    if target:
        args.insert(3, target)
    try:
        r = run(args, root, dl, cap=10)
        data = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("headRefName") and data.get("baseRefName"):
        return data["headRefName"], data["baseRefName"]
    return None


def evaluate(command: str, cwd: str) -> dict | None:
    mode = _mode()
    if mode == "off":
        return None
    inv = find_invocation(command)
    if inv is None:
        return None

    workdir = Path(cwd or os.getcwd())
    if inv.cd:
        workdir = (workdir / os.path.expanduser(inv.cd)).resolve()

    probe = Deadline(10)
    root = _repo_root(workdir, probe)
    if root is None:
        return None  # git repo の外。gh 自身のエラーに任せる
    if not layout.is_plugin_repo(root):
        return None

    soft = mode == "warn" or inv.draft
    try:
        cfg = config.load(root)
        dl = Deadline(cfg.timeout_seconds)
        base_hint = inv.base
        if inv.kind == "ready":
            refs = _pr_refs(root, inv.target, dl)
            if refs is None and inv.target:
                raise RuntimeError(f"`gh pr view {inv.target}` で PR の branch を取得できない")
            if refs is not None:
                head_now = git(["rev-parse", "--abbrev-ref", "HEAD"], root, dl).stdout.strip()
                if refs[0] != head_now:
                    return _context(
                        f"{_TAG} 対象 PR の branch ({refs[0]}) が現在の checkout "
                        f"({head_now}) と異なるため検査していない"
                    )
                base_hint = refs[1]
        rep = gate.run_gate(root, cfg, dl, base_hint=base_hint)
    except (config.ConfigError, GateTimeout, OSError, RuntimeError) as e:
        return _error(e, soft)
    except Exception as e:  # noqa: BLE001 - 想定外の例外も「止める」側に倒す
        return _error(e, soft)

    body = format_report(rep, verbose=False)
    if rep.failed:
        if soft:
            why = "draft PR" if inv.draft else f"{MODE_ENV}=warn"
            return _context(f"{_TAG} 完了条件を満たしていない ({why} のため止めていない)\n{body}")
        return _deny(
            f"{_TAG} PR 前の完了条件を満たしていない。FAIL を解消してから再実行する。\n"
            f"{body}\n"
            f"(一時的に止めずに通すには {MODE_ENV}=warn)"
        )
    if any(r.status == gate.WARN for r in rep.results) or rep.notes:
        return _context(f"{_TAG} PASS (WARN あり)\n{body}")
    return None


def _error(e: BaseException, soft: bool) -> dict:
    msg = f"{_TAG} ゲートを完了できなかった: {type(e).__name__}: {e}"
    if soft:
        return _context(msg)
    return _deny(
        f"{msg}\n異常時は PR 作成を止める設定になっている。原因を解消するか、"
        f"一時的に {MODE_ENV}=warn で実行する。"
    )


def _manual(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="verify-plugin-release check")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--base", default=None)
    args = ap.parse_args(argv)
    root = _repo_root(Path(args.path).resolve(), Deadline(10))
    if root is None:
        print("git repo ではない", file=sys.stderr)
        return 2
    try:
        cfg = config.load(root)
        rep = gate.run_gate(root, cfg, Deadline(cfg.timeout_seconds), base_hint=args.base)
    except Exception as e:  # noqa: BLE001
        print(f"ゲートを完了できなかった: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(format_report(rep, verbose=True))
    print("結果:", "FAIL" if rep.failed else "PASS")
    return 1 if rep.failed else 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        sys.exit(_manual(sys.argv[2:]))
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    if not isinstance(command, str) or not command:
        return
    result = evaluate(command, data.get("cwd") or "")
    if result is not None:
        json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
