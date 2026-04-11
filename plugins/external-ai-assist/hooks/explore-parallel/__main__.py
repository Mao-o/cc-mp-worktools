#!/usr/bin/env python3
"""explore-parallel: Explore agent と並走する補助アナライザ hook。

--phase pre : PreToolUse(Agent) で呼ばれる。subagent_type が Explore の場合、
              ANALYZERS を順に起動し、バックグラウンドで並走調査させる。
--phase post: PostToolUse(Agent) で呼ばれる。ANALYZERS の結果を待機して回収し、
              複数アナライザの出力を結合して additionalContext で親 Claude に注入。
"""
from __future__ import annotations

import argparse
import json
import sys

import cursor

# 新しいアナライザを追加するときは import と ANALYZERS に追記する
ANALYZERS = [cursor]


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["pre", "post"], required=True)
    args = parser.parse_args()

    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    tool_input = input_data.get("tool_input", {})
    if tool_input.get("subagent_type") != "Explore":
        return

    tool_use_id = input_data.get("tool_use_id", "")
    if not tool_use_id:
        return

    if args.phase == "pre":
        prompt = tool_input.get("prompt", "")
        if not prompt:
            return
        for analyzer in ANALYZERS:
            if not analyzer.is_available():
                continue
            try:
                analyzer.pre(tool_use_id, prompt)
            except Exception as e:
                print(f"[{analyzer.NAME}] pre failed: {e}", file=sys.stderr)

    elif args.phase == "post":
        sections = []
        for analyzer in ANALYZERS:
            if not analyzer.is_available():
                continue
            try:
                result = analyzer.post(tool_use_id)
            except Exception as e:
                print(f"[{analyzer.NAME}] post failed: {e}", file=sys.stderr)
                continue
            if result:
                sections.append(result)

        if sections:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n\n".join(sections),
                }
            }
            json.dump(output, sys.stdout)


if __name__ == "__main__":
    try:
        _main()
    except SystemExit:
        pass
    except Exception as e:
        # hook は絶対に失敗させない
        print(f"[explore-parallel] fatal: {e}", file=sys.stderr)
