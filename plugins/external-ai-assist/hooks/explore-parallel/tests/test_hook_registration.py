"""hooks.json の登録形 (post は async / pre は同期) を契約として固定する。

Agent ツールは **subagent が背景に移った時点で戻る** ため、PostToolUse(Agent) は
Explore の完了時ではなく起動直後に発火する。公式 docs (`PreToolUse input` の Agent 表) 逐語:

    `status` ... "completed" for foreground subagents, "async_launched" for background
    subagents. As of v2.1.198, subagents run in the background by default, so an omitted
    `run_in_background` also produces "async_launched"

0.9.1 までの post は同期 hook のまま最大 `TIMEOUT_SEC` 秒ポーリングしていたので、
「並走して待ち時間を隠す」はずが Explore の起動直後に親を止めていた (内部バックログ)。
post を `"async": true` にして解消する (docs `Run hooks in the background`:
"set `\"async\": true` to run the hook in the background while Claude continues working"、
"After the background process exits, Claude Code delivers the `additionalContext` and
`systemMessage` fields ... on the next conversation turn")。

実発火 (次ターンに本当に届くか) はハーネスでしか確認できないため、ここでは登録形だけを
固定する。
"""
import json
import unittest

import _testutil  # noqa: F401  (sys.path 整備)
from _testutil import _PKG_DIR

_HOOKS_JSON = _PKG_DIR.parent / "hooks.json"


def _agent_entry(event: str) -> dict:
    hooks = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    for entry in hooks["hooks"][event]:
        if entry.get("matcher") == "Agent":
            return entry["hooks"][0]
    raise AssertionError(f"hooks.json に {event}(Agent) の登録が無い")


class TestExploreParallelRegistration(unittest.TestCase):
    def test_post_hook_is_async(self):
        self.assertIs(
            _agent_entry("PostToolUse").get("async"),
            True,
            "post が同期 hook に戻っている (Explore 起動直後に親をブロックする)",
        )

    def test_pre_hook_stays_synchronous(self):
        """pre は同期のまま。Agent ツールが走り出す前に analyzer を起動する必要がある。

        `"async": true` にすると起動が Agent ツールの実行と競争になり、「並走」の起点が
        ずれる。pre は Claude に返す出力を持たないので async にする利点も無い。
        """
        self.assertNotIn(
            "async",
            _agent_entry("PreToolUse"),
            "pre が async 化されている (analyzer の起動タイミングが保証されない)",
        )

    def test_both_phases_are_registered_on_the_agent_matcher(self):
        """発火条件 (イベントと matcher) は 0.9.1 から変えていない。"""
        self.assertIn("--phase pre", _agent_entry("PreToolUse")["command"])
        self.assertIn("--phase post", _agent_entry("PostToolUse")["command"])


if __name__ == "__main__":
    unittest.main()
