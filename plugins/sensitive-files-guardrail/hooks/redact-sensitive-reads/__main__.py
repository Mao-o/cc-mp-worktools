#!/usr/bin/env python3
"""redact-sensitive-reads エントリポイント。

fail-closed wrapper: どこで例外が起きても ask_or_deny にフォールバックする。
`--tool read|bash|edit|write|grep` で handler を振り分ける。

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

from _shared.streams import read_stdin, write_stdout  # noqa: E402
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
        choices=["read", "bash", "edit", "write", "grep"],
        required=True,
        help="どの handler に振り分けるか",
    )
    return parser.parse_args(argv)


def _read_envelope() -> tuple[dict | None, str]:
    """stdin から hook envelope を読む。

    Returns:
        ``(envelope, error_category)``。成功時は ``(dict, "")``、失敗時は
        ``(None, <ログ category>)``。

    失敗の種別 (0.32.0 で ``stdin_empty`` を分離、内部バックログ):

    - ``stdin_parse_failed``: 読込例外 / 非 JSON / JSON だが dict でない
    - ``stdin_empty``: **0 byte の stdin**。0.31.0 まではここだけ ``{}`` を
      返しており、各 handler が必須フィールド欠如で ``make_allow()`` に落ちて
      **stderr もログも出ない無音 allow** になっていた (唯一の fail-open 分岐で、
      L109-111 の「envelope が読めないと bypass 判定もできない → 最厳 deny」
      という自身の方針と矛盾していた)。

    ``stdin_empty`` を ``ask`` ではなく **deny** に倒す理由: envelope が無いと
    ``permission_mode`` が読めず、``ask_or_deny`` は ``bypassPermissions``
    判定ができないまま ``make_ask`` に落ちる。Phase 0 実測のとおり
    bypassPermissions 下では ask はそのままツール実行に通る (``core.output``
    の docstring) ので、**修正しようとしている fail-open がその mode で残る**。
    category を分けてあるので、ハーネスが正常系で 0 byte stdin を送ってくる
    (= 全 deny になる) 事態が起きてもログから即座に切り分けられる。
    """
    try:
        raw = read_stdin()
    except Exception:
        return None, "stdin_parse_failed"
    if not raw:
        return None, "stdin_empty"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "stdin_parse_failed"
    if not isinstance(data, dict):
        return None, "stdin_parse_failed"
    return data, ""


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
    if tool == "grep":
        from handlers import grep_handler
        return grep_handler.handle(envelope)
    return output.make_allow()


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
    # 0.34.0 (内部バックログ): `hasattr(signal, "SIGALRM")` を Windows 判定の
    # proxy に使い、非対応プラットフォームでは **機密と無関係な Read まで含めて
    # 全 tool 呼出を deny** していた冒頭ゲートを撤去した。
    #
    # 根拠だった内部 soft-timeout (SIGALRM 1s) は 0.6.0 で撤去済みで
    # (`redaction/engine.py` 冒頭)、本体は SIGALRM を一切使っていない。
    # outer timeout (`hooks.json` の `timeout`) 発火時に Claude Code が hook を
    # discard して allow で継続する fail-open は **全 OS 共通** で、Unix 側にも
    # それを能動的に防ぐ内部タイムアウト機構は無い — つまり「Windows だけ
    # 冒頭 deny」に対応する実際のリスク差は無かった。
    #
    # 撤去後の Windows は「未検証」であって「保護なし」ではない: 内部失敗は
    # 従来どおり catch-all の `ask_or_deny` / `make_deny` に倒れる (fail-closed)。
    # `core/safepath.py` は `O_NOFOLLOW` / `O_CLOEXEC` が無い環境では
    # `classify` の lstat 判定に依存する fallback を持つ。詳細は README の
    # 「対応 OS」節と docs/DESIGN.md。
    _warn_if_python_degraded()

    try:
        args = _parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit:
        # argparse のエラーは exit 2。envelope を読めないので allow はできない
        # が、fail-open を避けるため deny にする
        _emit(output.make_deny(M.hook_invocation_error()))
        return 0

    envelope, read_error = _read_envelope()
    if envelope is None:
        L.log_error(read_error)
        # envelope が読めないと bypass 判定もできない → 最厳 deny
        # (0 byte stdin も同じ扱い。理由は _read_envelope の docstring)
        reason = (
            M.stdin_empty()
            if read_error == "stdin_empty"
            else M.stdin_parse_failed()
        )
        _emit(output.make_deny(reason))
        return 0

    # 0.32.0 (内部バックログ): 判定が確定するまで INFO をバッファし、最終判定に
    # 応じて出すか決める (``SFG_LOG_LEVEL`` で allow 経路の INFO を抑制可能に
    # するため)。「この log 呼出は allow 経路か」は呼出時点では決まらない —
    # ``ask_or_allow`` の結果は runtime の ``permission_mode`` 依存で、同一
    # コマンド内の後続 segment の deny が先行の ask/allow を上書きする。
    # ここ (全 tool 共通の dispatch 境界) に置くことで bash 以外の handler も
    # 同じ扱いになり、「後続 deny が先行 allow を上書き」も 1 回の flush で
    # 正しく leveling される。
    L.begin_deferred()
    decision: str | None = None
    try:
        try:
            response = _dispatch(args.tool, envelope)
        except Exception as e:
            L.log_error("handler_exception", f"{args.tool}:{type(e).__name__}")
            response = output.ask_or_deny(
                M.handler_internal_error(args.tool, type(e).__name__),
                envelope,
            )
        decision = output.decision_of(response)
    finally:
        # flush を落とすとバッファごとログが消えるので必ず通す。
        L.flush_deferred(decision)

    _emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
