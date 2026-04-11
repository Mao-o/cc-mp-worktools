#!/usr/bin/env python3
"""redact-sensitive-reads エントリポイント。

fail-closed wrapper: どこで例外が起きても ask_or_deny にフォールバックする。
`--tool read|bash|edit` で handler を振り分ける。

Phase 0 実測により permissionDecisionReason 経由でのモデル注入のみを使用。
systemMessage トップレベルは使わない。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# `python3 ~/.claude/hooks/redact-sensitive-reads` (ディレクトリ直呼び) に対応するため、
# パッケージディレクトリ自身と hooks/ (共有 _shared 用) を sys.path に入れる
_PKG_DIR = str(Path(__file__).resolve().parent)
_HOOKS_DIR = str(Path(__file__).resolve().parent.parent)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from core import logging as L  # noqa: E402
from core import output  # noqa: E402


def _emit(response: dict) -> None:
    """hook 出力を stdout に書いて exit 0。"""
    try:
        sys.stdout.write(json.dumps(response, ensure_ascii=False))
    except (BrokenPipeError, OSError):
        pass


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="redact-sensitive-reads")
    parser.add_argument(
        "--tool",
        choices=["read", "bash", "edit", "write", "multiedit"],
        required=True,
        help="どの handler に振り分けるか",
    )
    return parser.parse_args(argv)


def _read_envelope() -> dict | None:
    """stdin から hook envelope を読む。失敗時は None。"""
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _dispatch(tool: str, envelope: dict) -> dict:
    """tool 名から handler を呼ぶ。未実装 handler は allow で通す。"""
    if tool == "read":
        from handlers import read_handler
        return read_handler.handle(envelope)
    if tool == "bash":
        from handlers import bash_handler
        return bash_handler.handle(envelope)
    if tool == "edit":
        from handlers import edit_handler
        return edit_handler.handle(envelope, tool_label="Edit")
    if tool == "write":
        from handlers import edit_handler
        return edit_handler.handle(envelope, tool_label="Write")
    if tool == "multiedit":
        from handlers import edit_handler
        return edit_handler.handle(envelope, tool_label="MultiEdit")
    return output.make_allow()


def _is_unsupported_platform() -> bool:
    """Step 0-c 暫定: SIGALRM 非対応 (Windows 等) は現状非対応として扱う。

    outer timeout 発火時に Claude Code が allow (fail-open) を返す可能性があり、
    その場合 hook が hang したまま機密が漏れる最悪パスがあり得る。Step 0-c の
    実測結果が確定するまで安全側 (deny) で倒す。
    """
    import signal as _signal
    return not hasattr(_signal, "SIGALRM")


def main(argv: list[str] | None = None) -> int:
    if _is_unsupported_platform():
        _emit(output.make_deny(
            "redact-hook: 現状 UNIX (Linux/macOS) のみサポート。"
            "Windows 等では fail-closed で deny します。README の既知制限を参照。"
        ))
        return 0

    try:
        args = _parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit:
        # argparse のエラーは exit 2。envelope を読めないので allow はできない
        # が、fail-open を避けるため deny にする
        _emit(output.make_deny(
            "redact-hook 起動引数エラー。管理者に連絡してください。"
        ))
        return 0

    envelope = _read_envelope()
    if envelope is None:
        L.log_error("stdin_parse_failed")
        # envelope が読めないと bypass 判定もできない → 最厳 deny
        _emit(output.make_deny(
            "hook 入力 JSON の解析に失敗しました。安全側で deny します。"
        ))
        return 0

    try:
        response = _dispatch(args.tool, envelope)
    except Exception as e:
        L.log_error("handler_exception", f"{args.tool}:{type(e).__name__}")
        response = output.ask_or_deny(
            f"{args.tool} handler 内部エラー。安全側で一時停止します。",
            envelope,
        )

    _emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
