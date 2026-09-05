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

## post を `async` hook にした理由 (0.10.0)

公式 docs (`PreToolUse input` の Agent 表) 逐語:

> `status` ... `"completed"` for foreground subagents, `"async_launched"` for
> background subagents. As of v2.1.198, subagents run in the background by default,
> so an omitted `run_in_background` also produces `"async_launched"`
> For background subagents, the tool returns when the task moves to the background

つまり Agent ツールは **起動した時点で戻る**ので、PostToolUse(Agent) は Explore の完了時
ではなく起動直後に発火する。0.9.1 までの post は同期 hook のまま最大 `TIMEOUT_SEC` 秒
ポーリングしていたため、「並走して待ち時間を隠す」設計と裏腹に、Explore が走り出した
直後に親を止めていた。

hooks.json 側で post を `"async": true` にして解消する (docs `Run hooks in the background`):

> set `"async": true` to run the hook in the background while Claude continues working.
> After the background process exits, Claude Code delivers the `additionalContext` and
> `systemMessage` fields from the hook's JSON response to Claude on the next
> conversation turn.

発火条件 (どのイベント・どの matcher) は変えていない。変わるのは「親をブロックするか」と
「結果が届くのが次ターンになるか」だけ。`tool_response.status` を読んで待機を出し分ける案は
採らなかった — async 化すると foreground / background のどちらでも親は止まらないので、
分岐しても挙動が変わらない死んだコードになる。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# hooks/_common を解決するため、hook 内モジュールより先に hooks/ を sys.path に載せる
# (plugin root 内の相対配置なので ${CLAUDE_PLUGIN_ROOT} が cache コピーでも壊れない)。
_HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _common import hooklog, settings  # noqa: E402

import cursor  # noqa: E402
import state  # noqa: E402

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


def gc_orphans(current_tool_use_id: str = "") -> int:
    """TTL 超過の残骸 (走り続けている analyzer + pid / 結果ファイル) を掃除し、件数を返す。

    **pre / post の両方で回す**。post が来ない経路 — Agent ツールの失敗、ユーザー中断、
    セッション終了、`async` hook が `claude -p` の teardown で kill される — では
    `analyzer.post()` の後始末に到達しないため、次に hook が動いたときに拾うしかない。

    現在の tool_use_id は除外する。停止は analyzer 側 (`reap_orphan`) に委ね、ここは
    「どれが残骸か」と「予算内で打ち切る」だけを見る。
    """
    by_name = {a.NAME: a for a in ANALYZERS}
    deadline = time.monotonic() + state.GC_BUDGET_SEC
    removed = 0
    try:
        entries = state.stale_entries(exclude_tool_use_id=current_tool_use_id)
    except Exception as e:  # GC の失敗で hook 本体を止めない
        log(f"GC 走査に失敗: {e}")
        return 0

    for name, result_file, pid_file in entries:
        if time.monotonic() >= deadline:
            log(f"GC を予算 ({state.GC_BUDGET_SEC}s) で打ち切り — 残りは次回")
            break
        analyzer = by_name.get(name)
        if analyzer is not None and pid_file.is_file():
            try:
                analyzer.reap_orphan(pid_file)
            except Exception as e:
                log(f"{name}: 孤児の停止に失敗: {e}")
        state.cleanup(result_file, pid_file)
        removed += 1
    return removed


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

    gc_orphans(tool_use_id)

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
