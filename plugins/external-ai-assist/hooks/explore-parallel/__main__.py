#!/usr/bin/env python3
"""explore-parallel: Explore agent と並走する補助アナライザ hook。

--phase pre : PreToolUse(Agent) で呼ばれる。subagent_type が Explore の場合、
              ANALYZERS を順に起動し、バックグラウンドで並走調査させる。
--phase post: PostToolUse(Agent) で呼ばれる。ANALYZERS の結果を待機して回収し、
              複数アナライザの出力を結合して additionalContext で親 Claude に注入。

`EXTERNAL_AI_EXPLORE_PARALLEL=0` で無効化できる (0.6.0)。**止めるのは pre だけ**で、
post は常に回す — 直前のターンで起動済みのアナライザが居ると、post を止めた瞬間に
バックグラウンドの cursor と pid / 結果ファイルが孤児になる。何も起動していなければ
post は元から no-op (アナライザ未インストール時と同じ経路)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# hooks/_common を解決するため、hook 内モジュールより先に hooks/ を sys.path に載せる
# (plugin root 内の相対配置なので ${CLAUDE_PLUGIN_ROOT} が cache コピーでも壊れない)。
_HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _common import hooklog, settings  # noqa: E402

import cursor  # noqa: E402

# 新しいアナライザを追加するときは import と ANALYZERS に追記する
ANALYZERS = [cursor]

ENV_ENABLED = "EXTERNAL_AI_EXPLORE_PARALLEL"

log = hooklog.make_logger("explore-parallel")


def enabled() -> bool:
    """`EXTERNAL_AI_EXPLORE_PARALLEL=0` で並走を止める (既定は有効)。

    0.5.0 まではスイッチが無く、`cursor` を PATH から外す以外に止める手段が無かった。
    exitplan-review / post-implementation-review とは独立に切れる
    (`EXTERNAL_AI_REVIEW_MAX=0` は exitplan-review だけを止める)。
    """
    return settings.flag(ENV_ENABLED, default=True)


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
        if not enabled():
            log(f"{ENV_ENABLED}=0 により並走を skip")
            return
        prompt = tool_input.get("prompt", "")
        if not prompt:
            return
        for analyzer in ANALYZERS:
            if not analyzer.is_available():
                continue
            try:
                analyzer.pre(tool_use_id, prompt)
            except Exception as e:
                log(f"{analyzer.NAME}: pre failed: {e}")

    elif args.phase == "post":
        sections = []
        for analyzer in ANALYZERS:
            # is_available() は pre (起動するかどうか) だけのゲート。post では見ない —
            # pre 成功後に CLI が PATH から消えても、起動済みプロセスの pid / 結果ファイルは
            # 実体として残っており、reap は起動可否と無関係に必要 (マージ前レビューの指摘)。
            # 何も起動していなければ analyzer.post() 自体が no-op で返る (pid/結果ファイル無し)。
            try:
                result = analyzer.post(tool_use_id)
            except Exception as e:
                log(f"{analyzer.NAME}: post failed: {e}")
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
        log(f"fatal: {e}")
