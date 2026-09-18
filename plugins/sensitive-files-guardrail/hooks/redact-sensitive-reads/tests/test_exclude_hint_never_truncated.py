"""除外案内は **どの deny 経路でも** 全文残ること (0.23.0)。

レシピ (``!.env``) を見せながら影響範囲の警告を切るのは informed consent の逆。
しかも「詳細な報告が出るケース = ユーザーが実際に行動しやすい場面」に限って
起きるので特に筋が悪い。

このファイルは **経路を列挙して横断的に** 検査する。0.23.0 の実装中、
同じ不具合を Bash 経路で直したあと Edit/Write 経路で作り直した
(可変長セクションが片方の予算計算にしか入っていなかった) ため、
「1 経路だけ直して満足する」パターンをテスト側で塞ぐ。
"""
from __future__ import annotations

import unittest

from _testutil import FIXTURES  # noqa: F401

from core.messages import bash_deny, edit_deny
from core.output import MAX_REASON_BYTES

# 案内の末尾。ここまで残っていれば途中で切れていない。
_CLOSING = "承認なしに自分で追加しないこと"
# 影響範囲の開示の要。レシピだけ見えてこれが無い状態を防ぐ。
_SCOPE = "保護そのもの"


def _long_keys(n: int, length: int) -> list[str]:
    return [("K%03d_" % i) + "X" * (length - 5) for i in range(n)]


class TestExcludeHintSurvivesEveryDenyPath(unittest.TestCase):
    def _assert_intact(self, reason: str, label: str) -> None:
        self.assertLessEqual(
            len(reason.encode("utf-8")), MAX_REASON_BYTES, f"{label}: 予算超過"
        )
        self.assertIn("`!", reason, f"{label}: レシピが無い")
        self.assertIn(_SCOPE, reason, f"{label}: 影響範囲の開示が切れている")
        self.assertIn(_CLOSING, reason, f"{label}: 案内が途中で切れている")

    def test_edit_deny_with_many_suggested_keys(self):
        """書き込む content のキーが多い場合 (Codex R10 の再現形)。"""
        for n, klen in ((5, 20), (30, 57), (50, 57), (80, 40), (200, 30)):
            with self.subTest(keys=n, keylen=klen):
                reason = edit_deny(
                    tool_label="Write",
                    basename=".env",
                    new_keys=_long_keys(n, klen),
                    kind="new",
                    is_dotenv=True,
                )
                self._assert_intact(reason, f"edit new keys={n}x{klen}")

    def test_edit_deny_overwrite_with_big_existing_render(self):
        """既存ファイルの minimal info が大きい場合。"""
        big_render = "\n".join(
            f"  {i}. SOME_KEY_{i:03}  <type=str>  <set>  length=40"
            for i in range(200)
        )
        reason = edit_deny(
            tool_label="Write",
            basename=".env",
            new_keys=_long_keys(30, 57),
            kind="overwrite",
            is_dotenv=True,
            existing_render=big_render,
        )
        self._assert_intact(reason, "edit overwrite")

    def test_bash_deny_with_big_minimal_info(self):
        """Bash deny 側 (Codex R8 で直した経路) の回帰。"""
        big_render = "\n".join(
            f"  {i}. SOME_KEY_{i:03}  <type=str>  <set>  length=40"
            for i in range(200)
        )
        reason = bash_deny(
            first_token="cat",
            operand=".env",
            file_render=big_render,
        )
        self._assert_intact(reason, "bash deny")


# ---- dotenv_info を直接展開する 2 経路 (0.32.0、内部バックログ) -----------

_DATA_OPEN = '<DATA untrusted="true" source="redact-hook" guard="guardrail-v1">'
_DATA_CLOSE = "</DATA>"
_BLIND_CUT = "...[truncated]"


def _big_dotenv(n: int) -> tuple[str, dict]:
    """``render_for_bash`` が返す形の ``(file_render, dotenv_info)`` を作る。

    production では ``dotenv_info`` が非 None なら ``file_render`` も必ず
    非空 (両方を同じ呼出で返す) なので、テストも必ず対にして渡す。
    """
    render = "\n".join(
        [
            _DATA_OPEN,
            "NOTE: sanitized data from a sensitive file."
            " Real values are NOT in context.",
            "file: .env",
            "format: dotenv",
            f"entries: {n}",
            "keys (in order):",
            *(
                f"  {i}. KEY_{i:03}  <type=str>  <set>  length=40"
                for i in range(1, n + 1)
            ),
            "note: real values are not in context. only key names, type, prefix,"
            " length, status tags, and placeholder hints are returned.",
            _DATA_CLOSE,
        ]
    )
    info = {
        "format": "dotenv",
        "entries": n,
        "keys": [
            {
                "name": f"KEY_{i:03}",
                "type": "str",
                "status": ["<set>"],
                "length": 40,
            }
            for i in range(1, n + 1)
        ],
    }
    return render, info


class TestDotenvInfoPathsFoldInsteadOfBlindCut(unittest.TestCase):
    """``read_partial`` / ``search`` は 0.26.0 の折り畳みを通らず、予算超過時に
    ``</DATA>`` 閉じタグ・末尾 note・除外案内を鍵行の途中でちぎっていた
    (実測: 300 鍵の ``head -n 250`` で 3,072 byte + ``...[truncated]``)。"""

    def _assert_folded(self, reason: str, label: str) -> None:
        self.assertLessEqual(
            len(reason.encode("utf-8")), MAX_REASON_BYTES, f"{label}: 予算超過"
        )
        self.assertNotIn(_BLIND_CUT, reason, f"{label}: 盲目 cut に落ちている")
        self.assertIn(_DATA_CLOSE, reason, f"{label}: 閉じタグが消えた")
        # header の免責 (``NOTE: ... Real values are NOT in context.``) は
        # 折り畳みでも必ず残る (header 3 行は常に採用される)
        self.assertIn("Real values are NOT in context", reason, label)
        self.assertIn(_CLOSING, reason, f"{label}: 除外案内が切れている")
        self.assertIn(_SCOPE, reason, f"{label}: 影響範囲の開示が切れている")

    def test_read_partial_folds_at_various_sizes(self):
        for n, cmd_n in ((300, 250), (500, 500), (90, 90), (5, 3)):
            with self.subTest(keys=n, head_n=cmd_n):
                render, info = _big_dotenv(n)
                reason = bash_deny(
                    first_token="head",
                    operand=".env",
                    command=f"head -n {cmd_n} .env",
                    file_render=render,
                    dotenv_info=info,
                )
                self._assert_folded(reason, f"read_partial {n}/{cmd_n}")
                # 末尾 note (per-format の免責) も固定 tail として残る
                self.assertIn("only key names, type, prefix", reason)

    def test_read_partial_keeps_the_true_total_in_the_heading(self):
        """見出しの総数はブロック外の固定行なので切り出し件数に化けない。"""
        render, info = _big_dotenv(300)
        reason = bash_deny(
            first_token="head", operand=".env", command="head -n 250 .env",
            file_render=render, dotenv_info=info,
        )
        self.assertIn("keys (先頭 250, 全 300 件):", reason)

    def test_search_folds_with_many_matched_keys(self):
        for n in (300, 500, 25):
            with self.subTest(matched=n):
                render, info = _big_dotenv(n)
                reason = bash_deny(
                    first_token="grep",
                    operand=".env",
                    command="grep -E KEY_ .env",
                    file_render=render,
                    dotenv_info=info,
                    grep_keys=[f"KEY_{i:03}" for i in range(1, n + 1)],
                )
                self._assert_folded(reason, f"search matched={n}")

    def test_pattern_key_echo_is_capped_with_a_truthful_count(self):
        """鍵名エコーの 1 行が可変長のまま予算を食い潰すと、折り畳み予算が
        負になって結局盲目 cut に落ちる。畳んだら件数を必ず出す。"""
        render, info = _big_dotenv(300)
        reason = bash_deny(
            first_token="grep", operand=".env", command="grep -E KEY_ .env",
            file_render=render, dotenv_info=info,
            grep_keys=[f"KEY_{i:03}" for i in range(1, 301)],
        )
        echo = next(
            ln for ln in reason.split("\n")
            if ln.startswith("matched_pattern_keys:")
        )
        self.assertIn("... (280 more)", echo)
        self.assertIn("KEY_001", echo)
        self.assertNotIn("KEY_100", echo)

    def test_search_pattern_keys_echo_without_dotenv_info_is_capped(self):
        """dotenv parse 無し (``pattern_keys:`` のエコーのみ) の経路も畳む。"""
        reason = bash_deny(
            first_token="grep", operand=".env", command="grep -E KEY_ .env",
            grep_keys=[f"VERY_LONG_KEY_NAME_{i:03}" for i in range(1, 201)],
        )
        self._assert_intact_hint(reason)
        echo = next(
            ln for ln in reason.split("\n") if ln.startswith("pattern_keys:")
        )
        self.assertIn("... (180 more)", echo)

    def _assert_intact_hint(self, reason: str) -> None:
        self.assertLessEqual(len(reason.encode("utf-8")), MAX_REASON_BYTES)
        self.assertIn(_CLOSING, reason)
        self.assertIn(_SCOPE, reason)


if __name__ == "__main__":
    unittest.main()
