#!/usr/bin/env python3
"""verify-cloud-account: クラウドサービスコマンドの実行前アカウント検証フック。

PreToolUse:Bash に 1 エントリだけ登録し、内部でサービスを振り分ける。
対応サービスは services/ 配下のモジュールとして登録する。
"""
import json
import sys

from core import output
from core.dispatcher import dispatch


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    command = input_data.get("tool_input", {}).get("command", "")
    if not command:
        return

    cwd = input_data.get("cwd", "")
    try:
        result = dispatch(command, cwd)
    except Exception as e:  # noqa: BLE001
        # dispatch() 内の未捕捉例外を最終防波堤で拾う。ここで拾わないと
        # 非ゼロ終了 + JSON 無しになり、公式仕様上は non-blocking error として
        # action がそのまま進行する = 無音 fail-open になる (内部バックログ)。
        # 代わりに additionalContext で fail-open したことを明示し、stderr にも
        # 同じ理由を出す (`claude --verbose` 以外の確認手段が無かったため)。
        msg = (
            "[verify-cloud-account] 内部エラーのため検証をスキップしました: "
            f"{type(e).__name__}: {e}"
        )
        result = output.warn(msg)
        try:
            print(msg, file=sys.stderr)
        except OSError:
            # stderr が閉じている/書き込み不能 (BrokenPipeError 等) でも、この
            # 回復経路自体が例外を吐いて stdout への判定 JSON 出力を妨げては
            # ならない (マージ前レビューの指摘)。診断出力の失敗は判定に影響しない。
            pass
    if result is not None:
        json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
