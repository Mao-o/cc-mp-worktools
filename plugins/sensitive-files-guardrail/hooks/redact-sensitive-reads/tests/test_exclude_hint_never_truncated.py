"""除外案内は **どの deny 経路でも** 全文残ること (0.21.0)。

レシピ (``!.env``) を見せながら影響範囲の警告を切るのは informed consent の逆。
しかも「詳細な報告が出るケース = ユーザーが実際に行動しやすい場面」に限って
起きるので特に筋が悪い。

このファイルは **経路を列挙して横断的に** 検査する。0.21.0 の実装中、
同じ不具合を Bash 経路で直したあと Edit/Write 経路で作り直した
(可変長セクションが片方の予算計算にしか入っていなかった) ため、
「1 経路だけ直して満足する」パターンをテスト側で塞ぐ。
"""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
