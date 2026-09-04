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

# ディレクトリ直呼び (`python3 <hook-dir>`) に対応するため、
# パッケージディレクトリ自身と hooks/ (共有 _shared 用) を sys.path に入れる
_PKG_DIR = str(Path(__file__).resolve().parent)
_HOOKS_DIR = str(Path(__file__).resolve().parent.parent)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _shared.streams import write_stdout  # noqa: E402
from core import logging as L  # noqa: E402
from core import messages as M  # noqa: E402
from core import output  # noqa: E402


def _emit(response: dict) -> None:
    """hook 出力を stdout に書いて exit 0。

    ``print`` / ``sys.stdout.write`` ではなく ``write_stdout`` (UTF-8 bytes を
    バイナリ層へ書く) を通す。deny reason の大半は日本語なので、
    ``PYTHONIOENCODING=ascii`` 等の非 UTF-8 stdout では書込みが
    ``UnicodeEncodeError`` になり、hook が exit 1 で落ちて判定が届かず
    **tool 呼出が素通りする** (fail-open。外部レビュー R2 P2-A)。
    """
    try:
        write_stdout(json.dumps(response, ensure_ascii=False))
    except (BrokenPipeError, OSError):
        pass


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="redact-sensitive-reads")
    parser.add_argument(
        "--tool",
        choices=["read", "bash", "edit", "write"],
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
    return output.make_allow()


def _is_unsupported_platform() -> bool:
    """SIGALRM 非対応 (Windows 等) は現状非対応として扱う。

    outer timeout (`hooks.json` の `timeout`) 発火時、Claude Code はこの
    hook を discard し allow で継続する (fail-open。公式ドキュメントで確定
    済み、Step 0-c — docs/DESIGN.md 参照)。この fail-open は Windows 固有
    ではなく全 OS 共通だが、本 plugin は Windows でだけ `signal.SIGALRM` に
    よる内部 soft timeout (処理が長引く前に能動的に deny/ask を返す仕組み)
    が使えないため、hang したまま機密が漏れる最悪パスを避けるべく hook
    冒頭から deny で倒す。
    """
    import signal as _signal
    return not hasattr(_signal, "SIGALRM")


def _warn_if_python_degraded() -> None:
    """Python 3.11 未満では TOML の構造付き minimal info が opaque に劣化する
    (`redaction/tomllike.py`)。fail-open にはしない (hook 自体は継続) が、
    サイレント劣化にしないためログにだけ残す (内部バックログ)。
    """
    if sys.version_info < (3, 11):
        L.log_info(
            "python_version_degraded",
            f"{sys.version_info[0]}.{sys.version_info[1]}",
        )


def main(argv: list[str] | None = None) -> int:
    if _is_unsupported_platform():
        _emit(output.make_deny(M.unsupported_platform()))
        return 0

    _warn_if_python_degraded()

    try:
        args = _parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit:
        # argparse のエラーは exit 2。envelope を読めないので allow はできない
        # が、fail-open を避けるため deny にする
        _emit(output.make_deny(M.hook_invocation_error()))
        return 0

    envelope = _read_envelope()
    if envelope is None:
        L.log_error("stdin_parse_failed")
        # envelope が読めないと bypass 判定もできない → 最厳 deny
        _emit(output.make_deny(M.stdin_parse_failed()))
        return 0

    try:
        response = _dispatch(args.tool, envelope)
    except Exception as e:
        L.log_error("handler_exception", f"{args.tool}:{type(e).__name__}")
        response = output.ask_or_deny(
            M.handler_internal_error(args.tool, type(e).__name__),
            envelope,
        )

    _emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
