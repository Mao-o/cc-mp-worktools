#!/usr/bin/env python3
"""verify-cloud-account: クラウドサービスコマンドの実行前アカウント検証フック。

PreToolUse:Bash に 1 エントリだけ登録し、内部でサービスを振り分ける。
対応サービスは services/ 配下のモジュールとして登録する。
PostToolUse:Bash にも同じコマンドを登録し、アカウント状態を変えうるコマンドの
実行後に成功 cache の epoch を進める (検証・出力はしない。core/cache.py 参照)。
"""
import json
import sys

from core.dispatcher import dispatch, invalidate_after


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    command = input_data.get("tool_input", {}).get("command", "")
    if not command:
        return

    # PostToolUse (切替コマンドの実行後) は cache の epoch を進めるだけ。
    # 検証 (CLI 呼出) も deny 出力もしない。
    if input_data.get("hook_event_name") == "PostToolUse":
        invalidate_after(command)
        return

    cwd = input_data.get("cwd", "")
    result = dispatch(command, cwd)
    if result is not None:
        json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
