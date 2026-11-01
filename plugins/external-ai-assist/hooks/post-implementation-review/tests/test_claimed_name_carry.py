"""Stop が送らずに pending へ戻したファイルを、次の commit レビューが送れること。

Write / Edit の `file_path` が symlink を通る名前 (repo 内の `link -> real`、または repo の
祖先が symlink: 別ボリュームに置いた作業ディレクトリ、macOS の `/tmp -> /private/tmp` など)
のとき、Stop が繰り越しや送信失敗で pending に戻すパスが実体名になっていた。指紋のキーは
編集時の名前のままなので、`state.complete_claim` が指紋を消し (pending に同じ名前が無い)、
消えなかった経路 (`restore_claim`) でも `_verify_fingerprints` が実体名で引けない。結果、
そのファイルを書き換えずに commit すると commit レビューはファイル名だけを通知し、次の
Stop は「差分が空」と通知して、内容がどこにも送られなかった。

経路 (pending に戻す出口) × 配置の形 (実名の対照 / repo 内の symlink / 祖先の symlink /
hard link / 大文字小文字だけ違う名前) の直積を回す。外部 AI CLI は起動しない
(`cursor.review` を fake に差し替える)。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _testutil
from test_commit_flow import OURS, SESSION_A, CommitFlowTestCase


def _case_insensitive_fs(directory: str) -> bool:
    with tempfile.NamedTemporaryFile(prefix="caseProbe", dir=directory) as f:
        return os.path.exists(os.path.join(directory, os.path.basename(f.name).upper()))


class _CarryCase(CommitFlowTestCase):
    """a.py (先に送る) と b.py (送らずに pending へ戻す) を編集し、b.py だけを commit する。"""

    def edit_as(self, file_path: str, content: str, cwd: str) -> None:
        """Write ツール相当。PostToolUse には編集時の名前 (別名のまま) を渡す。"""
        with open(file_path, "w") as f:
            f.write(content)
        self.run_hook("post-tool", {
            "session_id": SESSION_A, "cwd": cwd, "tool_name": "Write",
            "tool_use_id": f"tu_{os.path.basename(file_path)}_{len(content)}",
            "tool_input": {"file_path": file_path, "content": content},
        })

    def stop_with(self, results: list[str | None]) -> str:
        """Stop を 1 回。`cursor.review` は呼ばれた順に `results` を返す (足りなければ最後の値)。"""
        calls: list[str] = []

        def fake_review(diff_text: str, *, cwd: str | None = None):
            calls.append(diff_text)
            return results[min(len(calls), len(results)) - 1]

        with mock.patch.object(self.cursor, "review", side_effect=fake_review):
            output = self.run_hook(
                "stop", {"session_id": SESSION_A, "cwd": self.repo, "stop_hook_active": False}
            )
        self.review_calls = calls
        return output

    # -- 配置の形 ------------------------------------------------------------
    # 返り値: (編集に使う base dir, a の名前, b の名前, commit する b の rel)

    def layout_real(self):
        os.mkdir(os.path.join(self.repo, "real"))
        base = os.path.join(self.repo, "real")
        return base, os.path.join(base, "a.py"), os.path.join(base, "b.py"), "real/b.py"

    def layout_in_repo_symlink(self):
        os.mkdir(os.path.join(self.repo, "real"))
        os.symlink("real", os.path.join(self.repo, "link"))
        base = os.path.join(self.repo, "link")
        return base, os.path.join(base, "a.py"), os.path.join(base, "b.py"), "real/b.py"

    def layout_symlinked_ancestor(self):
        parent = os.path.dirname(self.repo)
        alias = parent + "-alias"
        os.symlink(parent, alias)
        self.addCleanup(os.unlink, alias)
        base = os.path.join(alias, os.path.basename(self.repo))
        return base, os.path.join(base, "a.py"), os.path.join(base, "b.py"), "b.py"

    def layout_hard_link(self):
        # b.py と h.py は同じ inode。編集は h.py の名前で行い、h.py を commit する
        _testutil.write(self.repo, "b.py", "old\n")
        os.link(os.path.join(self.repo, "b.py"), os.path.join(self.repo, "h.py"))
        self.commit("seed hard link")
        base = self.repo
        return base, os.path.join(base, "a.py"), os.path.join(base, "h.py"), "h.py"

    def layout_case_alias(self):
        if not _case_insensitive_fs(self.repo):
            self.skipTest("大文字小文字を区別する FS では同じ実体を指す別名にならない")
        _testutil.write(self.repo, "b.py", "old\n")
        self.commit("seed b.py")
        base = self.repo
        return base, os.path.join(base, "a.py"), os.path.join(base, "B.py"), "b.py"

    # -- 経路 (pending に戻す出口) -------------------------------------------
    # 返り値: Stop の review の戻り値の列、最初の Stop で b.py を backend に渡しているか

    def exit_unsent_chunk(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_TIMEOUT"] = "600"  # 1 Stop 1 塊
        return ["REVIEW_CLEAN"], False

    def exit_failed_chunk(self):
        return ["REVIEW_CLEAN", None], True  # a の塊は返り、b の塊は結果を返さない

    def exit_all_failed(self):
        return [None], True

    def exit_overflow(self):
        self.enterContext(mock.patch.object(self.entry, "MAX_REVIEW_PATHS", 1))
        return ["REVIEW_CLEAN"], False

    def exit_deferred_time(self):
        self.enterContext(mock.patch.object(self.entry, "COLLECT_BUDGET_SEC", -1))
        return ["REVIEW_CLEAN"], False

    def exit_min_lines(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MIN_LINES"] = "1000000"
        return ["REVIEW_CLEAN"], False

    # -- シナリオ -------------------------------------------------------------

    def run_case(self, layout: str, exit_: str) -> None:
        base, a_name, b_name, b_rel = getattr(self, f"layout_{layout}")()
        results, b_sent_first = getattr(self, f"exit_{exit_}")()
        self.edit_as(a_name, _testutil.content_kib(30, "a"), base)
        self.edit_as(b_name, OURS + "\n" + _testutil.content_kib(30, "b"), base)
        stop_out = self.stop_with(results)
        # しきい値は commit レビューにも効くので、最初の Stop を見送らせた後は外す
        os.environ.pop("EXTERNAL_AI_POST_REVIEW_MIN_LINES", None)
        self.assertEqual(
            any(OURS in body for body in self.review_calls), b_sent_first,
            f"前提が崩れている (最初の Stop の通知: {self.notice(stop_out)})",
        )
        self.assertIn(b_name, self.pending(SESSION_A), "編集時の名前で pending に戻っていない")
        self.assertIn(b_name, self.state.fingerprints(SESSION_A), "繰り越したパスの指紋が消えている")

        output = self.window("tu_commit", lambda: self.commit("c", b_rel))
        self.assertTrue(
            any(OURS in body for body in self.review_calls),
            f"commit レビューが繰り越した内容を送っていない (通知: {self.notice(output)})",
        )


LAYOUTS = ("real", "in_repo_symlink", "symlinked_ancestor", "hard_link", "case_alias")
EXITS = ("unsent_chunk", "failed_chunk", "all_failed", "overflow", "deferred_time", "min_lines")


class TestCarriedPathKeepsClaimedName(_CarryCase):
    pass


class TestCaseOnlyAlias(_CarryCase):
    """大文字小文字だけ違う名前 (`B.py` で編集、実体は `b.py`) の内容を Stop も commit レビューも送る。

    0.18.1 までは realpath が大文字小文字を正規化せず、`B.py` のまま literal pathspec で git に
    渡って index の `b.py` に当たらなかった。Stop は「差分が空で取得できませんでした」と誤った理由を
    通知し、commit レビューはファイル名だけを通知して、内容はどこにも送られなかった。
    """

    def test_stop_sends_case_only_alias(self):
        base, _a, b_name, _b_rel = self.layout_case_alias()
        self.edit_as(b_name, OURS + "\nx\n", base)
        stop_out = self.stop_with(["REVIEW_CLEAN"])
        self.assertTrue(
            any(OURS in body for body in self.review_calls),
            f"Stop が内容を送っていない (通知: {self.notice(stop_out)})",
        )
        self.assertNotIn("差分が空で取得できませんでした", self.notice(stop_out))
        # 送った本文は index の綴り (b.py) の差分
        self.assertTrue(any("b/b.py" in body for body in self.review_calls))
        self.assertEqual(self.pending(SESSION_A), [])

    def test_commit_review_sends_case_only_alias(self):
        base, _a, b_name, b_rel = self.layout_case_alias()
        self.edit_as(b_name, OURS + "\ny\n", base)
        output = self.window("tu_commit", lambda: self.commit("c", b_rel))
        self.assertTrue(
            any(OURS in body for body in self.review_calls),
            f"commit レビューが内容を送っていない (通知: {self.notice(output)})",
        )
        self.assertNotIn("ファイル名のみ", self.notice(output))


def _make(layout: str, exit_: str):
    def test(self):
        self.run_case(layout, exit_)
    test.__doc__ = f"{exit_} の出口 × {layout} の配置"
    return test


for _layout in LAYOUTS:
    for _exit in EXITS:
        setattr(TestCarriedPathKeepsClaimedName, f"test_{_exit}__{_layout}", _make(_layout, _exit))


class TestAsClaimed(_CarryCase):
    """`_as_claimed` の単体: 実体名を claim 時の名前に写し戻す。"""

    def test_maps_real_name_back_to_every_claimed_alias(self):
        os.mkdir(os.path.join(self.repo, "real"))
        os.symlink("real", os.path.join(self.repo, "link"))
        _testutil.write(self.repo, "real/x.py", "x\n")
        claimed = [os.path.join(self.repo, "link", "x.py"), os.path.join(self.repo, "real", "x.py")]
        by_rel = self.entry._recorded_by_rel(self.repo, claimed)
        real = os.path.join(self.repo, "real/x.py")
        self.assertEqual(self.entry._as_claimed(self.repo, [real, real], by_rel), claimed)

    def test_unknown_path_is_kept_as_is(self):
        outside = os.path.join(os.path.dirname(self.repo), "elsewhere.py")
        inside = os.path.join(self.repo, "never-claimed.py")
        self.assertEqual(self.entry._as_claimed(self.repo, [outside, inside], {}), [outside, inside])


if __name__ == "__main__":
    unittest.main()
