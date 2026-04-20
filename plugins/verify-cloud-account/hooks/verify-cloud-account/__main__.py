#!/usr/bin/env python3
"""verify-cloud-account: クラウドサービスコマンドの実行前アカウント検証フック。

PreToolUse:Bash に 1 エントリだけ登録し、内部でサービスを振り分ける。
対応サービスは services/ 配下のモジュールとして登録する。
"""
import json
import sys

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
    result = dispatch(command, cwd)
    if result is not None:
        json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
