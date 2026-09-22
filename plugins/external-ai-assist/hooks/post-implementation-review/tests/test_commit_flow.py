"""commit 単位レビュー (PostToolUse(Bash), 0.12.0) の床テスト。

**この suite が守っているのは「何を外部へ送らないか」**。過去 2 回、commit 済みの
変更を復元しようとした設計 (HEAD SHA 基点 / 内容指紋) は、どちらも「このセッションが
書いた」ことを示せないまま他人の内容を送ってしまい撤去された (経緯は
`CLAUDE.md` の「HEAD 基準が空になったパスは復元せず通知する」節)。

3 回目 (reflog の行 allow-list だけで絞る案) もマージ前レビューで 3 経路が実演されて
撤去された。確定した設計は **I1' (等価性)**:

> commit レビューが外部へ送る差分は、**その Bash の直前に Stop が来ていたら Stop
> 経路が送っていた差分 (`git diff HEAD -- <path>`) と同一**でなければならない。

条件は窓側 W1〜W4 とパス側 P1〜P5 (逐語は `__main__.py` の「commit 単位レビュー」節)。

テストは 2 種類の判定しか使わない:

1. **送信本文に他者の内容を表す目印文字列が現れないこと** (呼び出し回数を見ない)
2. **送信本文の hunk が、窓を開く直前に Stop 経路が集めた diff の hunk と一致すること**
   (`TestWindowEquivalence`)
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

SESSION_A = "sess-commit-a"

#: このセッションが書いた内容の目印
OURS = "OUR_SESSION_CHANGE"
#: **外部へ出てはいけない**他者の内容の目印
FOREIGN = "CONTENT_FROM_ANOTHER_WRITER"


class CommitFlowTestCase(HookTestCase):
    """PreToolUse(Bash) → 任意の git 操作 → PostToolUse(Bash) を 1 窓として回す基底。"""

    def bash_payload(self, tool_use_id: str, cwd: str | None = None) -> dict:
        return {
            "session_id": SESSION_A,
            "cwd": cwd or self.repo,
            "tool_name": "Bash",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": "run git"},
        }

    def window(
        self,
        tool_use_id: str,
        mutate,
        review_result: str | None = "REVIEW_CLEAN",
        cwd: str | None = None,
    ) -> str:
        """1 つの Bash 窓を回し、PostToolUse が stdout に出した JSON 文字列を返す。

        `mutate` は Bash が実際にやることの代わり (git 操作など)。外部 AI CLI は
        起動せず `cursor.review` を差し替えた fake で受け取り、**渡された本文を
        そのまま保存する** (送信内容を assert するため)。
        """
        calls: list[str] = []
        cwds: list[str | None] = []

        def fake_review(diff_text: str, *, cwd: str | None = None):
            calls.append(diff_text)
            cwds.append(cwd)
            return review_result

        payload = self.bash_payload(tool_use_id, cwd)
        self.run_hook("pre-tool", payload)
        mutate()
        with mock.patch.object(self.cursor, "review", side_effect=fake_review):
            output = self.run_hook("post-tool", payload)
        self.review_calls = calls
        self.review_cwds = cwds
        return output

    # -- git ヘルパー ------------------------------------------------------

    def git(self, *args: str, repo: str | None = None):
        return _testutil.git(repo or self.repo, *args)

    def commit(self, message: str, *paths: str, repo: str | None = None) -> None:
        self.git("add", *(paths or ("-A",)), repo=repo)
        self.git("commit", "-qm", message, repo=repo)

    def reflog_path(self, repo: str | None = None) -> str:
        return os.path.join(repo or self.repo, ".git", "logs", "HEAD")

    def reflog_size(self, repo: str | None = None) -> int:
        try:
            return os.path.getsize(self.reflog_path(repo))
        except OSError:
            return 0

    def record_and_commit(self, rel: str, content: str) -> str:
        """「このセッションがレビュー済み、かつ作業ツリーは clean」なパスを作る。

        Stop を 1 回通すので `reviewed` に載る一方、pending からは外れ、commit 済みなので
        `git status` にも出ない。他者の変更が後からこのパスに入ったとき
        「過去に編集した実績があっても送られない」ことを試せる形。
        """
        full = self.edit(SESSION_A, rel, content)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.commit(f"add {rel}")
        return full

    def stop_diff(self, rel: str) -> str:
        """**いま Stop が来ていたら**このパスについて集める diff (`_collect_diffs` と同じ)。

        等価性テストの期待値。tracked なら `git diff HEAD -- rel`、untracked なら
        `/dev/null` との `--no-index` diff で、`__main__._collect_diffs` が呼ぶのと
        同じ `gitscan.path_diff` をそのまま使う (期待値を手で組み直さない)。
        """
        untracked = rel in self.gitscan.untracked_among(self.repo, [rel])
        return self.gitscan.path_diff(
            self.repo, rel, untracked, self.gitscan.head_exists(self.repo)
        )

    # -- assertion ヘルパー ------------------------------------------------

    def sent_body(self) -> str:
        return "\n".join(self.review_calls)

    def assertNothingSent(self) -> None:
        self.assertEqual(self.review_calls, [], "外部 backend に送信してしまった")

    def assertSentOnce(self) -> str:
        self.assertEqual(len(self.review_calls), 1, "レビューが 1 回送られていない")
        return self.review_calls[0]

    def assertForeignNotSent(self) -> None:
        self.assertNotIn(FOREIGN, self.sent_body(), "他者の内容が送信本文に現れている")

    def assertSameHunks(self, expected: str, actual: str, what: str) -> None:
        """2 つの diff の hunk (最初の `@@` 以降) が一致することを確かめる。

        ヘッダ (`diff --git` / `index <hash>..<hash>` / `---` / `+++`) は比較しない:
        object id は基点が違えば当然変わり、untracked の場合は Stop 側が
        `--no-index /dev/null` を使うため左辺の名前も変わる。**内容 (どの行を足して
        どの行を消したか) が 1 バイトも違わないこと**が I1' の主張なので、そこだけ見る。
        """
        self.assertEqual(hunks(expected), hunks(actual), what)

    def notice(self, output: str) -> str:
        return json.loads(output).get("systemMessage", "") if output else ""


def hunks(text: str) -> list[str]:
    """diff テキストから hash 依存のヘッダを落とし、最初の `@@` 以降の行だけを返す。"""
    out: list[str] = []
    started = False
    for line in text.splitlines():
        if not started:
            if not line.startswith("@@"):
                continue
            started = True
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# 床 1: 編集 → 同じ Bash で commit
# ---------------------------------------------------------------------------


class TestCommitInWindowIsReviewed(CommitFlowTestCase):
    def test_edit_then_commit_in_the_same_bash_is_reviewed(self):
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        output = self.window("tu1", lambda: self.commit("ours"))
        diff = self.assertSentOnce()
        self.assertIn(OURS, diff)
        self.assertIn("a.py", diff)
        self.assertEqual(self.review_cwds, [self.repo], "作業ツリー root を cwd に渡す")
        self.assertIn("commit レビュー完了", self.notice(output))

    def test_stop_does_not_report_unretrievable_afterwards(self):
        """B3: commit で片付いたパスは pending から外し、Stop に誤通知させない。"""
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        self.window("tu1", lambda: self.commit("ours"))
        self.assertEqual(self.pending(SESSION_A), [], "commit 済みのパスが pending に残っている")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertNotIn("取得できませんでした", self.notice(output))

    def test_partially_committed_path_stays_pending(self):
        """commit 後も未 commit の変更が残るパスは pending に残す (Stop が続きを見る)。"""
        full = self.edit(SESSION_A, "a.py", "print(1)\n")

        def mutate():
            self.commit("ours")
            _testutil.write(self.repo, "a.py", "print(1)\nprint(2)\n")

        self.window("tu1", mutate)
        self.assertIn(full, self.pending(SESSION_A))

    def test_findings_are_delivered_next_to_the_tool_result(self):
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        output = self.window("tu1", lambda: self.commit("ours"), "1. **直接影響** — 壊れる")
        self.assertTrue(output, "指摘ありのレビュー結果が stdout に出ていない")
        data = json.loads(output)
        self.assertNotIn("decision", data)
        specific = data["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PostToolUse")
        self.assertIn(
            "## 実装直後レビュー結果 (Cursor, commit レビュー)",
            specific["additionalContext"],
        )

    def test_block_mode_returns_reason_next_to_the_tool_result(self):
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_MODE": "block"}):
            self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
            output = self.window("tu1", lambda: self.commit("ours"), "1. 壊れる")
        self.assertTrue(output, "指摘ありのレビュー結果が stdout に出ていない")
        data = json.loads(output)
        self.assertEqual(data["decision"], "block")
        self.assertIn("commit レビュー", data["reason"])

    def test_bash_without_commit_sends_nothing(self):
        """窓の中で commit が無ければ何も送らない (発火条件の下限)。"""
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        before = self.reflog_size()
        self.window("tu1", lambda: None)
        self.assertNothingSent()
        self.assertEqual(self.reflog_size(), before)

    def test_switch_off_disables_commit_review_only(self):
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_COMMIT": "0"}):
            self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
            self.window("tu1", lambda: self.commit("ours"))
        self.assertNothingSent()
        # Stop 側の経路 (未 commit 差分) は従来どおり生きている
        self.edit(SESSION_A, "b.py", "print(2)\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("b.py")


# ---------------------------------------------------------------------------
# 床 2/3: commit 以外の reflog 行 (pull / merge / cherry-pick / revert)
# ---------------------------------------------------------------------------


class TestNonCommitReflogLines(CommitFlowTestCase):
    """他人の内容を運ぶ操作の行を窓から落とすこと。

    どのケースも `shared.py` を**このセッションの記録に載せた上で** 他者の内容を
    そこに入れる。落とせていなければ `FOREIGN` が送信本文に現れる。
    """

    def _add_origin(self) -> tuple[str, str]:
        bare = os.path.join(self._tmp.name, "origin.git")
        _testutil.git(self._tmp.name, "init", "--bare", "-q", "origin.git")
        self.git("remote", "add", "origin", bare)
        branch = self.git("branch", "--show-current").stdout.strip()
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        _testutil.git(bare, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        return bare, branch

    def _push_foreign(self, bare: str, branch: str) -> None:
        clone = os.path.join(self._tmp.name, "clone")
        _testutil.git(self._tmp.name, "clone", "-q", bare, "clone")
        _testutil.git(clone, "config", "user.email", "other@example.com")
        _testutil.git(clone, "config", "user.name", "other")
        _testutil.write(clone, "shared.py", f"base\n{FOREIGN}\n")
        _testutil.git(clone, "add", "-A")
        _testutil.git(clone, "commit", "-qm", "foreign change")
        _testutil.git(clone, "push", "-q", "origin", branch)

    def test_fast_forward_pull_sends_nothing(self):
        bare, branch = self._add_origin()
        self.record_and_commit("shared.py", "base\n")
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        self._push_foreign(bare, branch)

        before = self.reflog_size()
        self.window("tu_pull", lambda: self.git("pull", "-q", "--ff-only", "origin", branch))
        self.assertGreater(self.reflog_size(), before, "reflog が伸びていない (テストが空振り)")
        self.assertNothingSent()
        self.assertForeignNotSent()

    def test_merge_commit_sends_nothing(self):
        bare, branch = self._add_origin()
        self.record_and_commit("shared.py", "base\n")
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        self._push_foreign(bare, branch)
        _testutil.write(self.repo, "local.txt", "diverge\n")
        self.commit("local divergence")

        def mutate():
            self.git("fetch", "-q", "origin")
            self.git("merge", "-q", "--no-edit", f"origin/{branch}")

        before = self.reflog_size()
        self.window("tu_merge", mutate)
        self.assertGreater(self.reflog_size(), before, "reflog が伸びていない (テストが空振り)")
        self.assertNothingSent()
        self.assertForeignNotSent()

    def test_cherry_pick_sends_nothing(self):
        self.record_and_commit("shared.py", "base\n")
        main = self.git("branch", "--show-current").stdout.strip()
        self.git("checkout", "-q", "-b", "side")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.commit("foreign change on a side branch")
        self.git("checkout", "-q", main)

        before = self.reflog_size()
        self.window("tu_cp", lambda: self.git("cherry-pick", "side"))
        self.assertGreater(self.reflog_size(), before, "reflog が伸びていない (テストが空振り)")
        self.assertNothingSent()
        self.assertForeignNotSent()

    def test_revert_sends_nothing(self):
        self.record_and_commit("shared.py", "base\n")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.commit("another writer's change")

        before = self.reflog_size()
        self.window("tu_rev", lambda: self.git("revert", "--no-edit", "HEAD"))
        self.assertGreater(self.reflog_size(), before, "reflog が伸びていない (テストが空振り)")
        self.assertNothingSent()
        self.assertForeignNotSent()

    def test_mixed_window_sends_nothing(self):
        """**W2**: 同じ窓に merge と自分の commit が混ざったら、窓ごと何も送らない。

        0.12.0 の最初の実装は「commit 行はそれ自身の `old..new` だけ使うので安全」と
        して混在窓でも自分の commit を送っていたが、これは偽だった (マージ前レビューで
        `reset --soft` / `merge --squash` / `cherry-pick -n` の 3 経路が実演された)。
        commit 以外で ref が動いた窓では、窓全体が「窓を開いた時点の `git diff HEAD`」
        と等価だと示せない。**厳格化であって緩和ではない。**
        """
        bare, branch = self._add_origin()
        self.record_and_commit("shared.py", "base\n")
        self.git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        self._push_foreign(bare, branch)
        _testutil.write(self.repo, "local.txt", "diverge\n")
        self.commit("local divergence")
        self.edit(SESSION_A, "ours.py", f"print('{OURS}')\n")

        def mutate():
            self.git("fetch", "-q", "origin")
            self.git("merge", "-q", "--no-edit", f"origin/{branch}")
            self.commit("ours")

        output = self.window("tu_mixed", mutate)
        self.assertNothingSent()
        self.assertForeignNotSent()
        self.assertIn("commit 以外の ref 操作", self.notice(output))


# ---------------------------------------------------------------------------
# 床 4/5/6/7: 送信範囲 (I1)
# ---------------------------------------------------------------------------


class TestSendScope(CommitFlowTestCase):
    def test_other_local_writer_commit_outside_the_window_is_not_sent(self):
        """窓の外で別のローカルの書き手が同じパスに commit しても、その内容は入らない。"""
        self.record_and_commit("shared.py", "base\n")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.commit("another local writer's commit")
        self.edit(SESSION_A, "ours.py", f"print('{OURS}')\n")

        self.window("tu_ours", lambda: self.commit("ours"))
        diff = self.assertSentOnce()
        self.assertIn(OURS, diff)
        self.assertForeignNotSent()

    def test_commit_dash_a_reports_unrecorded_paths_by_name_only(self):
        """`git commit -a` が巻き込んだ「記録の無いパス」は名前だけ通知し内容を送らない。"""
        _testutil.write(self.repo, "stranger.py", "base\n")
        self.commit("add stranger.py")
        self.edit(SESSION_A, "ours.py", f"print('{OURS}')\n")
        # この hook を経由しない書き手が作業ツリーに残した変更
        _testutil.write(self.repo, "stranger.py", f"base\n{FOREIGN}\n")

        output = self.window("tu_all", lambda: self.commit("sweep everything"))
        diff = self.assertSentOnce()
        self.assertIn(OURS, diff)
        self.assertForeignNotSent()
        message = self.notice(output)
        self.assertIn("stranger.py", message)
        self.assertIn("未レビューの変更として記録が無い", message)
        self.assertNotIn(FOREIGN, message, "通知に内容を出さない")

    def test_noop_edit_does_not_send_an_unrelated_historical_diff(self):
        """床: 内容を変えなかった編集 + 窓内の無関係な commit で履歴を送らない。

        `TestSameTurnCommitNotification::test_noop_edit_...` と同じ攻撃を commit
        レビュー経路に当てたもの。`a.py` の最後の commit はこのセッションの成果では
        なく、その差分には**このセッションが一度も見ていない内容**が含まれる。
        """
        _testutil.write(self.repo, "a.py", "keep = 1\n")
        self.commit("history before the session")
        _testutil.write(self.repo, "a.py", f"keep = 1\n{FOREIGN} = 2\n")
        self.commit("a change this session did not make")
        # 内容が 1 バイトも変わらない編集 (pending には載る)
        self.edit(SESSION_A, "a.py", f"keep = 1\n{FOREIGN} = 2\n")

        def mutate():
            _testutil.write(self.repo, "unrelated.txt", "something else\n")
            self.commit("unrelated commit in the window")

        output = self.window("tu_noop", mutate)
        self.assertNothingSent()
        self.assertForeignNotSent()
        self.assertNotIn(FOREIGN, self.notice(output))

    def test_excluded_path_in_a_commit_is_not_sent(self):
        self.edit(SESSION_A, ".env", f"SECRET={FOREIGN}\n")
        output = self.window("tu_env", lambda: self.commit("add env"))
        self.assertNothingSent()
        self.assertForeignNotSent()
        message = self.notice(output)
        self.assertIn(".env", message)
        self.assertIn("除外", message)


class TestAmendScope(CommitFlowTestCase):
    def test_amend_of_someone_elses_commit_sends_only_our_increment(self):
        filler = "".join(f"filler{i}\n" for i in range(20))
        _testutil.write(self.repo, "shared.py", f"{FOREIGN} = 1\n{filler}")
        self.commit("another writer's commit")
        self.edit(SESSION_A, "shared.py", f"{FOREIGN} = 1\n{filler}{OURS} = 2\n")

        self.window("tu_amend", lambda: (self.git("add", "-A"), self.git("commit", "-q", "--amend", "--no-edit")))
        diff = self.assertSentOnce()
        self.assertIn(OURS, diff)
        self.assertForeignNotSent()


# ---------------------------------------------------------------------------
# 床 9: fail-closed
# ---------------------------------------------------------------------------


class TestFailClosed(CommitFlowTestCase):
    """窓が信用できないときは**何も送らない**。5 条件それぞれを個別に作る。"""

    def _prepare(self) -> None:
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")

    def _tamper_window(self, tool_use_id: str, tamper) -> str:
        """commit した後に reflog へ細工をしてから PostToolUse を回す。"""

        def mutate():
            self.commit("ours")
            tamper(self.pre_size)

        payload = self.bash_payload(tool_use_id)
        self.run_hook("pre-tool", payload)
        self.pre_size = self.reflog_size()
        calls: list[str] = []
        with mock.patch.object(
            self.cursor, "review", side_effect=lambda d, *, cwd=None: calls.append(d)
        ):
            mutate()
            output = self.run_hook("post-tool", payload)
        self.review_calls = calls
        return output

    def test_missing_pre_snapshot_sends_nothing(self):
        """pre-tool を通っていない Bash (`BASH_TRACKING=0` 等) では何も送らない。"""
        self._prepare()
        payload = self.bash_payload("tu_no_pre")
        calls: list[str] = []
        self.commit("ours")
        with mock.patch.object(
            self.cursor, "review", side_effect=lambda d, *, cwd=None: calls.append(d)
        ):
            self.run_hook("post-tool", payload)  # pre-tool を走らせていない
        self.assertEqual(calls, [])

    def test_bash_tracking_off_sends_nothing(self):
        self._prepare()
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_BASH_TRACKING": "0"}):
            self.window("tu_off", lambda: self.commit("ours"))
        self.assertNothingSent()

    def test_unreadable_reflog_sends_nothing(self):
        """reflog ファイルが消えた窓では何も送らない (かつ hook が例外で死なない)。

        **この条件は mutation で偽造できない**: 読むデータ自体が無いので、検査を
        外しても送信は起きない。ここで固定しているのは「fail-open で例外を投げない」
        ことと「別経路 (`git reflog` を叩き直す等) で窓を作り直さない」こと。
        """
        self._prepare()

        def tamper(_pre_size):
            os.remove(self.reflog_path())

        self._tamper_window("tu_gone", tamper)
        self.assertNothingSent()

    def test_shortened_reflog_sends_nothing(self):
        """`git reflog expire` のように**書き換えで短くなった**窓は信用しない。

        単に末尾を切るだけだと追記そのものが消えてテストが空振りになる (mutation で
        検証済み)。「最後の 1 行 = このセッションの commit だけを残して短くする」形に
        すると、`<old>` は pre の HEAD と一致したままなので、**バイト長の検査だけが
        唯一の防波堤**になる。
        """
        for i in range(6):
            self.git("commit", "-q", "--allow-empty", "-m", f"padding {i}")
        self._prepare()

        def tamper(pre_size):
            with open(self.reflog_path(), "rb") as f:
                lines = f.read().splitlines(keepends=True)
            self.assertLess(len(lines[-1]), pre_size, "書き換え後が短くなっていない")
            with open(self.reflog_path(), "wb") as f:
                f.write(lines[-1])

        self._tamper_window("tu_short", tamper)
        self.assertNothingSent()

    def test_disconnected_first_line_sends_nothing(self):
        """追記の最初の行の `<old>` が pre の HEAD と繋がっていなければ送らない。

        偽装する `<old>` は**実在する古い commit** にする。存在しない SHA に
        すると `git diff` が失敗して勝手に何も送らなくなり、検査を外しても
        テストが落ちない (空振り) ため。基点が古い方へずれると、そのセッションが
        書いていない範囲まで diff に入る。
        """
        _testutil.write(self.repo, "stranger.py", f"{FOREIGN} = 1\n")
        self.commit("another writer's commit")
        root_sha = self.git("rev-parse", "HEAD~1").stdout.strip()
        self._prepare()

        def tamper(pre_size):
            with open(self.reflog_path(), "rb") as f:
                head, tail = f.read(pre_size), f.read()
            with open(self.reflog_path(), "wb") as f:
                f.write(head + root_sha.encode() + tail[len(root_sha) :])

        self._tamper_window("tu_forged", tamper)
        self.assertNothingSent()
        self.assertForeignNotSent()

    def test_unparsable_line_sends_nothing(self):
        self._prepare()

        def tamper(pre_size):
            with open(self.reflog_path(), "rb") as f:
                head, tail = f.read(pre_size), f.read()
            with open(self.reflog_path(), "wb") as f:
                f.write(head + b"not a reflog line at all\n" + tail)

        self._tamper_window("tu_garbage", tamper)
        self.assertNothingSent()

    def test_too_many_commits_sends_nothing(self):
        import reflog

        self._prepare()

        def mutate():
            for i in range(reflog.MAX_COMMITS + 1):
                _testutil.write(self.repo, f"bulk{i}.txt", f"{i}\n")
                self.commit(f"bulk {i}")

        self.window("tu_bulk", mutate)
        self.assertNothingSent()


# ---------------------------------------------------------------------------
# 床 10: linked worktree
# ---------------------------------------------------------------------------


class TestLinkedWorktree(CommitFlowTestCase):
    def test_commit_inside_a_linked_worktree_is_detected(self):
        """`git worktree add` した作業ツリーの commit は、その worktree の reflog に出る。

        main worktree の `logs/HEAD` には現れないので、`--git-path logs/HEAD` を
        窓の起点にしていないと検出できない (git 2.50.1 実測)。
        """
        linked = os.path.join(self._tmp.name, "linked")
        self.git("worktree", "add", "-q", linked, "-b", "feat")
        linked = os.path.realpath(linked)

        full = _testutil.write(linked, "a.py", f"print('{OURS}')\n")
        self.run_hook(
            "post-tool",
            {
                "session_id": SESSION_A,
                "cwd": linked,
                "tool_name": "Write",
                "tool_use_id": "tu_w",
                "tool_input": {"file_path": full},
            },
        )
        before_main = self.reflog_size()
        self.window("tu_linked", lambda: self.commit("ours", repo=linked), cwd=linked)
        diff = self.assertSentOnce()
        self.assertIn(OURS, diff)
        self.assertEqual(self.review_cwds, [linked])
        self.assertEqual(
            self.reflog_size(), before_main, "main worktree の reflog は伸びない (前提の確認)"
        )


# ---------------------------------------------------------------------------
# 床 11: 失敗時に state を触らない / 別 repo への commit
# ---------------------------------------------------------------------------


class TestStateOnFailure(CommitFlowTestCase):
    def test_all_backends_failing_leaves_pending_untouched(self):
        full = self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        self.assertEqual(self.pending(SESSION_A), [full])
        self.window("tu_fail", lambda: self.commit("ours"), review_result=None)
        self.assertEqual(len(self.review_calls), 1, "送信そのものは行われる")
        self.assertEqual(
            self.pending(SESSION_A), [full], "失敗時は pending を整理しない (B3)"
        )

    def test_failure_does_not_record_last_backend(self):
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        self.window("tu_fail", lambda: self.commit("ours"), review_result=None)
        self.assertEqual(self.state.last_backend(SESSION_A), "")


class TestCommitInAnotherRepo(CommitFlowTestCase):
    def test_commit_into_a_different_repo_sends_nothing(self):
        """hook 入力の cwd の repo ではない場所に commit しても、窓は伸びない。

        `--git-path logs/HEAD` は pre-tool 時点の cwd の作業ツリーのものなので、
        別 repo の commit はそのファイルに現れない = 検出も送信も起きない。
        """
        other = _testutil.init_repo(os.path.join(self._tmp.name, "other"))
        _testutil.write(other, "secret.py", f"{FOREIGN} = 1\n")
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")

        before = self.reflog_size()
        self.window("tu_other", lambda: self.commit("in another repo", repo=other))
        self.assertEqual(self.reflog_size(), before, "対象 repo の reflog は伸びない")
        self.assertNothingSent()
        self.assertForeignNotSent()


# ---------------------------------------------------------------------------
# 床 12: P1 — `reviewed` は送信許可ではない
# ---------------------------------------------------------------------------


class TestReviewedIsNotASendPermission(CommitFlowTestCase):
    """マージ前レビューの再現 (P1-1)。

    `reviewed` は「このセッションが過去に一度でも編集した」LRU のセッション全履歴で、
    Stop 経路では再送抑止にしか使われていない。commit レビューの積集合に入れると
    「一度編集したパスは以後ずっと誰が書いた内容でも送ってよい」に昇格し、**0.11.0 の
    Stop が送らない他者の内容を送る**。対照 (0.11.0) では同じ手順で Stop が
    「差分が空で取得できませんでした」と通知するだけで送らないことを実測済み。
    """

    def test_reviewed_only_path_with_a_plain_commit_all(self):
        """git 操作は素の `git commit -a` のみ。`reviewed` 由来の許可だけが効く形。"""
        self.record_and_commit("shared.py", f"print('{OURS}')\n")
        self.assertEqual(self.pending(SESSION_A), [], "前提: pending は空")
        # 別の書き手が shared.py を書き換える (このセッションの hook は発火しない)
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\nprint('{FOREIGN}')\n")
        self.edit(SESSION_A, "unrelated.py", "print('u')\n")

        output = self.window("tu_rev1", lambda: self.commit("chore"))
        self.assertForeignNotSent()
        self.assertIn("shared.py", self.notice(output))
        self.assertNotIn(FOREIGN, self.notice(output), "通知に内容を出さない")

    def test_reviewed_only_path_without_any_other_edit(self):
        """このセッションがそのターンに何も編集していなくても同じ (送信ゼロ)。"""
        self.record_and_commit("shared.py", f"print('{OURS}')\n")
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\nprint('{FOREIGN}')\n")

        self.window("tu_unrelated", lambda: self.commit("chore"))
        self.assertNothingSent()
        self.assertForeignNotSent()


# ---------------------------------------------------------------------------
# 床 13: P2 — 窓の中で初めて内容が入ったパスは送らない
# ---------------------------------------------------------------------------


class TestContentThatArrivedInsideTheWindow(CommitFlowTestCase):
    """`logs/HEAD` に 1 行も書かずに index / 作業ツリーへ他者の内容を入れる操作。

    2026-09-20 実測: `git merge --squash` / `git cherry-pick -n` はどちらも
    `logs/HEAD` のバイト長を 1 バイトも伸ばさない。続く `git commit` は素の
    `commit:` 行にしか見えないので、**行の allow-list では原理的に検出できない**。
    止めているのは P2 (窓を開いた時点で dirty だったパスにしか許可を出さない)。
    """

    def _foreign_side_branch(self) -> str:
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\n")
        self.commit("base")
        main = self.git("branch", "--show-current").stdout.strip()
        self.git("checkout", "-q", "-b", "side")
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\nprint('{FOREIGN}')\n")
        self.commit("foreign change on a side branch")
        side = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", main)
        return side

    def test_merge_squash_then_commit(self):
        self._foreign_side_branch()
        # 最悪ケース: このパスは `reviewed` にも pending にも載せておく
        self.edit(SESSION_A, "shared.py", f"print('{OURS}')\nprint('mine')\n")
        self.git("checkout", "-q", "--", "shared.py")  # 窓を開く時点では clean

        def mutate():
            self.git("merge", "--squash", "side")
            self.git("commit", "-qm", "squash merge")

        self.window("tu_msq", mutate)
        self.assertForeignNotSent()

    def test_cherry_pick_no_commit_then_commit(self):
        side = self._foreign_side_branch()
        self.edit(SESSION_A, "shared.py", f"print('{OURS}')\nprint('mine')\n")
        self.git("checkout", "-q", "--", "shared.py")

        def mutate():
            self.git("cherry-pick", "-n", side)
            self.git("commit", "-qm", "picked")

        self.window("tu_cpn", mutate)
        self.assertForeignNotSent()

    def test_stash_pop_then_commit_in_the_same_window(self):
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\n")
        self.commit("base")
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\nprint('{FOREIGN}')\n")
        self.git("stash", "push", "-q", "-m", "foreign")
        self.edit(SESSION_A, "shared.py", f"print('{OURS}')\nprint('mine')\n")
        self.git("checkout", "-q", "--", "shared.py")

        def mutate():
            self.git("stash", "pop")
            self.commit("committed the popped content")

        self.window("tu_pop", mutate)
        self.assertForeignNotSent()

    def test_restore_source_then_commit_in_the_same_window(self):
        self._foreign_side_branch()
        self.edit(SESSION_A, "shared.py", f"print('{OURS}')\nprint('mine')\n")
        self.git("checkout", "-q", "--", "shared.py")

        def mutate():
            self.git("restore", "--source=side", "--", "shared.py")
            self.commit("committed the restored content")

        self.window("tu_res", mutate)
        self.assertForeignNotSent()


# ---------------------------------------------------------------------------
# 床 14: P3 — 窓の間に作業ツリーが書き換わったパスは送らない
# ---------------------------------------------------------------------------


class TestWorktreeRewrittenInsideTheWindow(CommitFlowTestCase):
    """窓を開いた時点で dirty (P2 を満たす) でも、窓の中で内容がすり替われば送らない。"""

    def _side_with_foreign(self) -> None:
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\n")
        self.commit("base")
        main = self.git("branch", "--show-current").stdout.strip()
        self.git("checkout", "-q", "-b", "side")
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\nprint('{FOREIGN}')\n")
        self.commit("foreign change on a side branch")
        self.git("checkout", "-q", main)

    def test_checkout_ref_into_a_pending_path_then_commit(self):
        self._side_with_foreign()
        self.edit(SESSION_A, "shared.py", f"print('{OURS}')\nprint('mine')\n")

        def mutate():
            self.git("checkout", "side", "--", "shared.py")
            self.commit("committed someone else's version")

        output = self.window("tu_co", mutate)
        self.assertForeignNotSent()
        self.assertIn("一致しない", self.notice(output))

    def test_stash_pop_over_a_pending_path_then_commit(self):
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\n")
        self.commit("base")
        _testutil.write(self.repo, "shared.py", f"print('{OURS}')\nprint('{FOREIGN}')\n")
        self.git("stash", "push", "-q", "-m", "foreign")
        self.edit(SESSION_A, "shared.py", f"print('{OURS}')\nprint('mine')\n")

        def mutate():
            self.git("checkout", "-q", "--", "shared.py")
            self.git("stash", "pop")
            self.commit("committed the popped content")

        self.window("tu_pop2", mutate)
        self.assertForeignNotSent()

    def test_a_hook_rewriting_the_file_after_commit(self):
        """pre-commit フックや formatter が窓の中でファイルを書き換える形。"""
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")

        def mutate():
            self.commit("ours")
            _testutil.write(self.repo, "a.py", f"print('{OURS}')\nprint('reformatted')\n")

        self.window("tu_fmt", mutate)
        self.assertNothingSent()


# ---------------------------------------------------------------------------
# 床 15: P4 — 部分 commit は送らない
# ---------------------------------------------------------------------------


class TestPartialCommit(CommitFlowTestCase):
    def test_staged_foreign_version_committed_while_the_worktree_holds_ours(self):
        """index に他者版、作業ツリーに自分版。素の `git commit` は index を確定する。

        窓を開いた時点で dirty (P2 ✓)、窓の間に作業ツリーは動かない (P3 ✓) が、
        commit 後も `git diff HEAD -- shared.py` が残る = **作業ツリーの内容が
        そのまま commit されたわけではない**。ここで送ると
        「Stop が直前に来ていたら送っていた差分」と違うものを送ることになる。
        """
        _testutil.write(self.repo, "shared.py", "base\n")
        self.commit("base")
        # 他者が index にだけ自分の版を置く
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.git("add", "shared.py")
        # このセッションは作業ツリーを自分の内容で上書きする (index は他者版のまま)
        self.edit(SESSION_A, "shared.py", f"base\n{OURS}\n")

        output = self.window("tu_partial", lambda: self.git("commit", "-qm", "index only"))
        self.assertForeignNotSent()
        self.assertIn("部分 commit", self.notice(output))


# ---------------------------------------------------------------------------
# 床 16: W3 — 行の連鎖
# ---------------------------------------------------------------------------


class TestChainedWindow(CommitFlowTestCase):
    def test_forged_second_line_sends_nothing(self):
        """先頭行は正しく繋がっているが、2 行目が別の系列を指す窓は使わない。

        先頭行だけを検査していた頃は、この形で `pre の HEAD`..`最後の new` が
        **このセッションが作っていない commit** まで含む範囲になった。偽装する
        `<new>` は**実在する commit** にする (存在しない SHA だと `git diff` が
        勝手に失敗して検査を外してもテストが落ちない = 空振りになるため)。
        """
        _testutil.write(self.repo, "shared.py", "base\n")
        self.commit("base")
        main = self.git("branch", "--show-current").stdout.strip()
        self.git("checkout", "-q", "-b", "side")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.commit("foreign side commit")
        foreign = self.git("rev-parse", "HEAD").stdout.strip()
        foreign_parent = self.git("rev-parse", "HEAD~1").stdout.strip()
        self.git("checkout", "-q", main)
        self.edit(SESSION_A, "shared.py", f"base\n{OURS}\n")

        def mutate():
            self.commit("ours")
            with open(self.reflog_path(), "a") as f:
                f.write(
                    f"{foreign_parent} {foreign} forger <f@example.com> 1 +0000"
                    "\tcommit: forged\n"
                )

        self.window("tu_chain", mutate)
        self.assertNothingSent()
        self.assertForeignNotSent()


# ---------------------------------------------------------------------------
# 床 17: W4 — pre の status スナップショットが無い窓
# ---------------------------------------------------------------------------


class TestMissingPreStatus(CommitFlowTestCase):
    def test_window_without_a_pre_status_snapshot_sends_nothing(self):
        """巨大な作業ツリー等で `git status` を諦めた窓では commit レビューをしない。

        P2 / P3 の判定材料が無いため。**送信の有無だけでは mutation を判別できない**
        (W4 を外しても、空の status として扱われた全パスが P2 で落ちる) ので、
        ここは「利用者に理由が 1 行出ること」まで固定する。
        """
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        payload = self.bash_payload("tu_nostatus")
        with mock.patch.object(self.gitscan, "status_snapshot", return_value=None):
            self.run_hook("pre-tool", payload)
        self.commit("ours")
        calls: list[str] = []
        with mock.patch.object(
            self.cursor, "review", side_effect=lambda d, *, cwd=None: calls.append(d)
        ):
            output = self.run_hook("post-tool", payload)
        self.review_calls = calls
        self.assertNothingSent()
        self.assertIn("status スナップショット", self.notice(output))


# ---------------------------------------------------------------------------
# 床 18: 等価性 (I1' そのもの)
# ---------------------------------------------------------------------------


class TestWindowEquivalence(CommitFlowTestCase):
    """**送信本文の hunk = 窓を開く直前に Stop 経路が集めた diff の hunk**。

    これが I1' の主張そのもの。`FOREIGN` は使わない — 等価であることを示すのが目的で、
    他者の内容の有無は別のクラスが見る (diff の文脈行に目印が紛れ込むと、等価なのに
    「漏れた」ように見えて誤判定するため)。
    """

    def assertWindowIsEquivalent(self, rel: str, tool_use_id: str, mutate) -> None:
        expected = self.stop_diff(rel)
        self.assertTrue(hunks(expected), f"前提: 窓を開く時点で {rel} に差分がある")
        self.window(tool_use_id, mutate)
        self.assertSameHunks(
            expected,
            self.sent_body(),
            f"{rel}: 送信本文が「窓を開く直前の Stop 経路の diff」と一致しない",
        )

    def test_plain_commit(self):
        _testutil.write(self.repo, "a.py", "keep = 1\nkeep = 2\nkeep = 3\n")
        self.commit("history")
        self.edit(SESSION_A, "a.py", f"keep = 1\n{OURS} = 2\nkeep = 3\n")
        self.assertWindowIsEquivalent("a.py", "tu_eq1", lambda: self.commit("ours"))

    def test_amend(self):
        filler = "".join(f"filler{i}\n" for i in range(20))
        _testutil.write(self.repo, "a.py", f"first = 1\n{filler}")
        self.commit("the commit that will be amended")
        self.edit(SESSION_A, "a.py", f"first = 1\n{filler}{OURS} = 2\n")
        self.assertWindowIsEquivalent(
            "a.py",
            "tu_eq2",
            lambda: (self.git("add", "-A"), self.git("commit", "-q", "--amend", "--no-edit")),
        )

    def test_two_commits_in_one_window(self):
        _testutil.write(self.repo, "a.py", "keep = 1\n")
        _testutil.write(self.repo, "b.py", "keep = 1\n")
        self.commit("history")
        self.edit(SESSION_A, "a.py", f"keep = 1\n{OURS} = 2\n")
        self.edit(SESSION_A, "b.py", f"keep = 1\n{OURS} = 3\n")
        expected_a = self.stop_diff("a.py")
        expected_b = self.stop_diff("b.py")

        def mutate():
            self.commit("first", "a.py")
            self.commit("second", "b.py")

        self.window("tu_eq3", mutate)
        # 期待値も hook と同じ組み方 (`"\n".join(sections)`) で繋ぐ
        self.assertSameHunks(
            "\n".join([expected_a, expected_b]),
            self.sent_body(),
            "2 commit の窓が 1 本の範囲として等価でない",
        )

    def test_new_untracked_file(self):
        """untracked は Stop 側が `--no-index /dev/null` を使うのでヘッダだけ形が違う。

        `assertSameHunks` が hunk だけを比べるのはこのため (「直した」つもりで
        ヘッダまで比較に含めないこと)。
        """
        self.edit(SESSION_A, "brand_new.py", f"{OURS} = 1\n{OURS} = 2\n")
        self.assertWindowIsEquivalent(
            "brand_new.py", "tu_eq4", lambda: self.commit("add a new file")
        )

    def test_deletion_is_reported_not_sent(self):
        """削除の commit は **blob が無い** ので P6 を通らない = 送らずに通知する。

        内容指紋 (P6) を足す前は「Stop と同一の hunk を送る」で固定していたが、
        **厳格化**した: 削除は編集ツールでは起きない (Bash 経由 =
        指紋なし) ので、内容は送らない側に倒れる。Stop 経路の削除通知は従来どおり。
        """
        _testutil.write(self.repo, "doomed.py", "gone = 1\ngone = 2\n")
        self.commit("history")
        os.remove(os.path.join(self.repo, "doomed.py"))
        self.state.record_pending(SESSION_A, [os.path.join(self.repo, "doomed.py")])
        output = self.window("tu_eq5", lambda: self.commit("delete it"))
        self.assertNothingSent()
        self.assertIn("doomed.py", self.notice(output))

    def test_foreign_content_that_arrived_in_an_earlier_window(self):
        """前の窓で他者の内容が pending のパスに入り、次の窓で commit する形。

        内容指紋 (P6) を足す前は P2〜P4 を通って**送られて**いた (0.11.0 の Stop も
        同じ内容を送るので I1' の範囲内ではあった)。P6 はこれも止める: commit された
        バイト列がこのセッションのツールが最後に書いた内容と違うため。
        **0.11.0 の Stop より狭い**方向の変化なので受け入れる。
        """
        _testutil.write(self.repo, "shared.py", "base\n")
        self.commit("base")
        self.git("checkout", "-q", "-b", "side")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.commit("side")
        self.git("checkout", "-q", "-")
        self.edit(SESSION_A, "shared.py", "base\nmine\n")
        self.git("checkout", "-q", "--", "shared.py")

        # 窓 1: 他者の内容が pending のパスに入る (commit はしない)
        self.window("tu_eq6a", lambda: self.git("restore", "--source=side", "--", "shared.py"))
        self.assertNothingSent()
        # 窓 2: それを commit しても送らない
        output = self.window("tu_eq6b", lambda: self.commit("commit what was restored"))
        self.assertNothingSent()
        self.assertForeignNotSent()
        self.assertIn("shared.py", self.notice(output))


# ---------------------------------------------------------------------------
# 床 19: 通知と state の整合 (次の窓の送信許可にしない)
# ---------------------------------------------------------------------------


class TestUnrecordedPathsDoNotBecomePermission(CommitFlowTestCase):
    def test_commit_all_does_not_push_foreign_paths_into_pending(self):
        """`git commit -a` が巻き込んだ他者パスを pending に積まない (P2-2)。

        積むと「この窓では編集記録が無いので送らない」と通知したパスが、**次の窓では
        正規の送信許可**になる。通知と state が矛盾していた形。
        """
        _testutil.write(self.repo, "foreign.txt", "base\n")
        self.commit("foreign base")
        _testutil.write(self.repo, "foreign.txt", "base\nmodified-by-other\n")
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")

        output = self.window("tu_carry1", lambda: self.commit("w1"))
        self.assertIn("foreign.txt", self.notice(output))
        self.assertNotIn(
            os.path.join(self.repo, "foreign.txt"),
            self.pending(SESSION_A),
            "送らないと通知したパスを pending に積んでいる",
        )

        # 他者がさらに編集 → 次の窓で commit しても内容は送らない
        _testutil.write(self.repo, "foreign.txt", f"base\nmodified-by-other\n{FOREIGN}\n")
        self.edit(SESSION_A, "b.py", "print('b')\n")
        self.window("tu_carry2", lambda: self.commit("w2"))
        self.assertForeignNotSent()

    def test_paths_still_dirty_after_the_window_stay_pending(self):
        """作業ツリーに変更が現に残っているパスは従来どおり pending に積む。

        guard が止めるのは「status から消えただけ」のパスに限る (`sed -i` で書き換えて
        commit しなかった変更まで落とすと、Stop が見るべきものが消える)。
        """
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")

        def mutate():
            self.commit("ours")
            _testutil.write(self.repo, "stranger.py", "written by the bash command\n")

        self.window("tu_carry3", mutate)
        self.assertIn(os.path.join(self.repo, "stranger.py"), self.pending(SESSION_A))


# ---------------------------------------------------------------------------
# 床 20: conflict 解決後の `commit:` 行
# ---------------------------------------------------------------------------


class TestRevertContinue(CommitFlowTestCase):
    """conflict した `git revert` を `--continue` で確定すると、message は `revert:`
    ではなく **`commit: Revert "..."`** になる (実測)。W2 では止まらない。"""

    def _setup(self) -> str:
        _testutil.write(self.repo, "shared.py", "base\n")
        self.commit("base")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.commit("foreign commit")
        foreign = self.git("rev-parse", "HEAD").stdout.strip()
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}-edited\n")
        self.commit("foreign commit 2")
        return foreign

    def _revert(self, *args: str):
        return _testutil.subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_EDITOR": "true"},
        )

    def test_resolution_and_continue_in_the_same_window(self):
        """同じ窓で解決まで済ませると、窓の間にファイルが書き換わるので P3 で落ちる。"""
        foreign = self._setup()
        self.edit(SESSION_A, "shared.py", f"base\n{FOREIGN}-edited\nmine\n")
        self.git("checkout", "-q", "--", "shared.py")

        def mutate():
            if self._revert("revert", "--no-edit", foreign).returncode != 0:
                _testutil.write(self.repo, "shared.py", "base\nresolved\n")
                self.git("add", "-A")
                self._revert("revert", "--continue", "--no-edit")

        self.window("tu_rc1", mutate)
        self.assertForeignNotSent()

    def test_resolution_and_continue_split_across_windows(self):
        """解決を窓 1、`--continue` を窓 2 に分けても、内容指紋 (P6) が止める。

        P6 を足す前は P2〜P4 を通って**送られて**いた (窓 2 の直前に Stop が来ていれば
        0.11.0 も同じ hunk を送るので I1' の範囲内ではあった)。conflict の解決内容は
        編集ツールではなく Bash 経由で作業ツリーに入る = 指紋が無いため、現在は
        内容を送らずファイル名だけ通知する (**厳格化**)。
        """
        foreign = self._setup()

        def window1():
            self._revert("revert", "--no-edit", foreign)
            _testutil.write(self.repo, "shared.py", "base\nresolved\n")
            self.git("add", "-A")

        self.window("tu_rc2a", window1)
        self.assertTrue(hunks(self.stop_diff("shared.py")), "前提: 窓 2 を開く時点で差分がある")
        output = self.window("tu_rc2b", lambda: self._revert("revert", "--continue", "--no-edit"))
        self.assertNothingSent()
        self.assertForeignNotSent()
        self.assertIn("shared.py", self.notice(output))


# ---------------------------------------------------------------------------
# 床 21: 予算・しきい値・取得失敗 (commit 経路側)
# ---------------------------------------------------------------------------


class TestCommitBudgets(CommitFlowTestCase):
    """Stop 経路にしか床が無かった分岐 (マージ前レビューの指摘 P2-6)。"""

    def test_oversized_paths_are_not_sent_and_stay_pending(self):
        """合計予算に収まらないパスは送らず pending に残す (0.4.1 の再発防止)。"""
        big = _testutil.content_kib(3)
        with mock.patch.object(self.entry, "MAX_DIFF_BYTES", 2000), mock.patch.object(
            self.entry, "MAX_FILE_DIFF_BYTES", 1500
        ):
            self.edit(SESSION_A, "big1.py", big)
            second = self.edit(SESSION_A, "big2.py", big)
            output = self.window("tu_bud", lambda: self.commit("big"))
        self.assertIn("big2.py", self.notice(output))
        self.assertIn(second, self.pending(SESSION_A), "予算外のパスが pending から消えた")

    def test_file_over_the_per_file_limit_is_truncated(self):
        big = _testutil.content_kib(3)
        with mock.patch.object(self.entry, "MAX_FILE_DIFF_BYTES", 1500):
            self.edit(SESSION_A, "big1.py", big)
            output = self.window("tu_trunc", lambda: self.commit("big"))
        self.assertIn("truncated for review", self.sent_body())
        self.assertLessEqual(len(self.sent_body().encode()), 1500)
        self.assertIn("先頭のみ送信", self.notice(output))

    def test_min_lines_skips_the_commit_review(self):
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_MIN_LINES": "50"}):
            self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
            output = self.window("tu_min", lambda: self.commit("ours"))
        self.assertNothingSent()
        self.assertIn("MIN_LINES=50", self.notice(output))

    def test_cooldown_skips_the_commit_review_with_a_notice(self):
        self.edit(SESSION_A, "seed.txt", "alpha\nbeta\ngamma\ndelta\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")  # last_review_at を立てる
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC": "600"}):
            self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
            output = self.window("tu_cool", lambda: self.commit("ours"))
        self.assertNothingSent()
        self.assertIn("COOLDOWN_SEC", self.notice(output))

    def test_range_paths_failure_sends_nothing(self):
        """窓の変更パスを取れなければ窓ごと諦める (部分的に進めない)。"""
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        with mock.patch.object(self.gitscan, "range_paths", return_value=None):
            self.window("tu_names", lambda: self.commit("ours"))
        self.assertNothingSent()

    def test_changed_vs_head_failure_sends_nothing(self):
        """P4 を判定できなければ送らない (「部分 commit でない」を示せないため)。"""
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        with mock.patch.object(self.gitscan, "changed_vs_head", return_value=None):
            self.window("tu_p4", lambda: self.commit("ours"))
        self.assertNothingSent()

    def test_oversized_reflog_append_sends_nothing(self):
        """追記が `MAX_APPEND_BYTES` を超える窓は、**部分的に読まず**窓ごと諦める。

        上限を「窓の 1 行目ちょうど」に絞ると、上限検査を外した実装は追記の 1 行目
        だけを読んで「1 commit の窓」として成立してしまう (2 つ目の commit を
        見落としたまま送る)。空振りにしないための作り。
        """
        import reflog

        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        self.edit(SESSION_A, "b.py", "print('b')\n")
        payload = self.bash_payload("tu_append")
        self.run_hook("pre-tool", payload)
        pre_size = self.reflog_size()
        self.commit("first", "a.py")
        with open(self.reflog_path(), "rb") as f:
            f.seek(pre_size)
            first_line = len(f.readline())
        self.commit("second", "b.py")
        calls: list[str] = []
        with mock.patch.object(reflog, "MAX_APPEND_BYTES", first_line), mock.patch.object(
            self.cursor, "review", side_effect=lambda d, *, cwd=None: calls.append(d)
        ):
            self.run_hook("post-tool", payload)
        self.review_calls = calls
        self.assertNothingSent()


# ---------------------------------------------------------------------------
# 床 22: 受け取った指摘を state 整理の失敗で捨てない
# ---------------------------------------------------------------------------


class TestDeliveryAfterReview(CommitFlowTestCase):
    def test_findings_survive_an_exception_while_settling_state(self):
        """backend 呼び出しの**後**の例外で指摘を黙って捨てない (P2-1)。

        以前は `try` が state 整理まで覆っており、`changed_vs_head` が投げると
        「外部へ送ってレビュー結果も受け取ったのに何も返さない」形になっていた
        (cooldown だけ消費される)。送信**前**の例外は従来どおり「送らない」。
        """
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        real = self.gitscan.changed_vs_head
        calls = {"n": 0}

        def flaky(root, rels):
            calls["n"] += 1
            if calls["n"] == 1:  # 送信前の P4 判定は通す
                return real(root, rels)
            raise RuntimeError("boom")

        with mock.patch.object(self.gitscan, "changed_vs_head", side_effect=flaky):
            output = self.window("tu_settle", lambda: self.commit("ours"), "1. **直接影響** — 壊れる")
        self.assertTrue(output, "指摘が配信されていない")
        data = json.loads(output)
        reason = data.get("hookSpecificOutput", {}).get("additionalContext") or data.get(
            "reason", ""
        )
        self.assertIn("直接影響", reason)

    def test_exception_before_sending_does_not_send(self):
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        with mock.patch.object(
            self.gitscan, "range_diff", side_effect=RuntimeError("boom")
        ):
            output = self.window("tu_presend", lambda: self.commit("ours"))
        self.assertNothingSent()
        self.assertFalse(output, "送信前に失敗した窓で Claude へ出力している")


# ---------------------------------------------------------------------------
# 床 24: 代理判定を復元・迂回する攻撃 (マージ前レビュー 2 巡目の P1-1 / P1-2)
# ---------------------------------------------------------------------------


class ProxyCheckAttacks(CommitFlowTestCase):
    """代理判定 (`(size, mtime_ns)` の突合 / `git diff HEAD` が空) を破る 3 手順の土台。"""

    def restored_mtime_window(self, tool_use_id: str) -> str:
        """同一バイト数に書き換えて `os.utime` で mtime を戻す (`touch -r` / `cp -p` 相当)。

        `(size, mtime_ns)` だけの突合では復元できてしまう (P1-1)。
        """
        _testutil.write(self.repo, "shared.py", "base\n")
        self.commit("base")
        full = self.edit(SESSION_A, "shared.py", "base\n" + "A" * len(FOREIGN) + "\n")
        before = os.stat(full)

        def mutate():
            _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
            os.utime(full, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(
                os.stat(full).st_size, before.st_size, "前提: 同一バイト数の書き換え"
            )
            self.assertEqual(
                os.stat(full).st_mtime_ns, before.st_mtime_ns, "前提: mtime が復元できている"
            )
            self.commit("sneak the foreign content in")

        return self.window(tool_use_id, mutate)

    def index_flag_window(self, flag: str, tool_use_id: str) -> str:
        """index に他者版、作業ツリーにこのセッションの版を置いて `flag` を立てて commit。

        `git diff HEAD` は `assume-unchanged` / `skip-worktree` のエントリで作業
        ツリーを見ないため、commit されるのは index の他者版なのに P4 は空になる。
        """
        _testutil.write(self.repo, "shared.py", "base\n")
        self.commit("base")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.git("add", "shared.py")
        self.edit(SESSION_A, "shared.py", f"base\n{OURS}\n")

        def mutate():
            self.git("update-index", flag, "shared.py")
            self.git("commit", "-qm", "commit whatever the index holds")
            self.assertIn(
                FOREIGN,
                self.git("cat-file", "blob", "HEAD:shared.py").stdout,
                "前提: index の他者版が commit されている",
            )

        return self.window(tool_use_id, mutate)


class TestRestoredStatAndIndexFlags(ProxyCheckAttacks):
    """**送信範囲だけ**を見る床 (落とした理由は `TestProxyCheckSkipReasons`)。

    0.11.0 の Stop が同じ時点で送るのはこのセッションの内容なので、`FOREIGN` が
    送信本文に出たら送信範囲の拡大。このクラスは **P6 単独でも通る**ように
    分けてある — P3' / P4' を両方外した mutation コピーで回すと、P6 が本体で
    あることの証明になる。
    """

    def test_same_size_rewrite_with_restored_mtime_is_not_sent(self):
        self.restored_mtime_window("tu_mtime")
        self.assertForeignNotSent()
        self.assertNothingSent()

    def test_assume_unchanged_index_version_is_not_sent(self):
        self.index_flag_window("--assume-unchanged", "tu_assume")
        self.assertForeignNotSent()
        self.assertNothingSent()

    def test_skip_worktree_index_version_is_not_sent(self):
        self.index_flag_window("--skip-worktree", "tu_skipwt")
        self.assertForeignNotSent()
        self.assertNothingSent()


class TestProxyCheckSkipReasons(ProxyCheckAttacks):
    """P3' / P4' **単体**の床 (送信範囲では測れない)。

    P6 を足した後、この 3 攻撃は P6 だけでも送信ゼロになる。つまり P3' / P4' を
    外しても送信本文は変わらず、送信範囲では mutation を検出できない。多層防御を
    黙って失わないよう、**利用者に返す「落とした理由」**で固定する:

    - P3' が効いていれば「窓を開いた時点の未 commit の変更と一致しない」
    - 外れると P4 が素通りして P6 の区分 (「commit された内容が…一致しない」) になる
    """

    def assertWindowMismatch(self, output: str) -> None:
        self.assertIn(
            self.entry._SKIP_NOTICES[self.entry.SKIP_MISMATCH].format(n=1) + "shared.py",
            self.notice(output),
            "「窓を開いた時点の変更と一致しない」で落ちていない (代理判定を通っていない)",
        )

    def test_restored_mtime_is_reported_as_a_window_mismatch(self):
        self.assertWindowMismatch(self.restored_mtime_window("tu_mtime_reason"))

    def test_assume_unchanged_is_reported_as_a_window_mismatch(self):
        self.assertWindowMismatch(
            self.index_flag_window("--assume-unchanged", "tu_assume_reason")
        )

    def test_skip_worktree_is_reported_as_a_window_mismatch(self):
        self.assertWindowMismatch(
            self.index_flag_window("--skip-worktree", "tu_skipwt_reason")
        )


# ---------------------------------------------------------------------------
# 床 25: 内容指紋 (P6) の更新規律
# ---------------------------------------------------------------------------


class TestFingerprintDiscipline(CommitFlowTestCase):
    """「最後に書いた内容」だけが送信許可になること。

    P6 は **commit された blob の生バイト**を読んで指紋と突き合わせるので、
    P3' / P4' / P4 が代理判定として破れても独立に効く。
    """

    def test_the_last_written_content_is_sent(self):
        """2 回編集して最後の内容で commit → 送る (主要フローを壊していないこと)。"""
        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        self.edit(SESSION_A, "a.py", f"{OURS} = 2\n")
        self.window("tu_fp1", lambda: self.commit("the second content"))
        diff = self.assertSentOnce()
        self.assertIn(f"{OURS} = 2", diff)

    def test_content_replaced_outside_any_window_is_not_sent(self):
        """窓の外で他者が差し替えた内容を commit しても送らない。

        **P6 だけが止める形**: 差し替えは窓の外なので指紋は失効せず (`_expire_
        fingerprints` は Bash の窓でしか走らない)、窓を開いた時点の stat と現在の
        stat は一致し (P3')、index のタグも `H` (P4')、commit 後の `git diff HEAD`
        も空 (P4)。P6 以外の条件はすべて通る。
        """
        self.edit(SESSION_A, "shared.py", f"{OURS} = 1\n")
        _testutil.write(self.repo, "shared.py", f"{FOREIGN} = 1\n")
        output = self.window("tu_fp2", lambda: self.commit("commit someone else's content"))
        self.assertNothingSent()
        self.assertForeignNotSent()
        self.assertIn("shared.py", self.notice(output))

    def test_bash_rewrite_expires_the_fingerprint(self):
        """Write の後に Bash がそのファイルを書き換えたら、次の窓の commit も送らない。"""
        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        self.window("tu_fp3a", lambda: _testutil.write(self.repo, "a.py", f"{FOREIGN} = 1\n"))
        self.assertNothingSent()
        self.window("tu_fp3b", lambda: self.commit("commit what the bash wrote"))
        self.assertNothingSent()
        self.assertForeignNotSent()

    def test_git_add_between_windows_keeps_the_fingerprint(self):
        """`git add` は作業ツリーを変えないので指紋を失効させない (過剰な失効の床)。

        `gitscan.changed_between` は status の code が動いただけでも「変化」と
        返すので、`changed` を丸ごと失効対象にすると
        「Write → 窓 1 で `git add` → 窓 2 で commit」が常に落ちる。
        """
        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        self.window("tu_fp4a", lambda: self.git("add", "a.py"))
        self.window("tu_fp4b", lambda: self.git("commit", "-qm", "stage first, commit later"))
        diff = self.assertSentOnce()
        self.assertIn(OURS, diff)

    def test_file_over_the_size_cap_has_no_fingerprint(self):
        """`FINGERPRINT_MAX_BYTES` 超のファイルは指紋なし = 送らずに通知する。"""
        with mock.patch.object(self.entry, "FINGERPRINT_MAX_BYTES", 8):
            self.edit(SESSION_A, "big.py", f"{OURS} = 1\n")
            output = self.window("tu_fp5", lambda: self.commit("a file we could not fingerprint"))
        self.assertNothingSent()
        self.assertIn("big.py", self.notice(output))

    def test_state_written_before_fingerprints_existed_sends_nothing(self):
        """指紋のキーを持たない旧 state から移行しても送らない側に落ちる。"""
        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        path = os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json"
        )
        with open(path) as f:
            raw = json.load(f)
        raw.pop("fingerprints", None)
        with open(path, "w") as f:
            json.dump(raw, f)
        output = self.window("tu_fp6", lambda: self.commit("c"))
        self.assertNothingSent()
        self.assertIn("a.py", self.notice(output))

    def test_pre_status_from_an_older_format_sends_nothing(self):
        """`ctime_ns` を持たない 3 要素のスナップショットは P2 / P3' で判定不能。"""
        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        real = self.gitscan.status_snapshot

        def legacy(root):
            snapshot = real(root)
            if snapshot is None:
                return None
            return {rel: list(meta[:3]) for rel, meta in snapshot.items()}

        with mock.patch.object(self.gitscan, "status_snapshot", side_effect=legacy):
            output = self.window("tu_fp7", lambda: self.commit("c"))
        self.assertNothingSent()
        self.assertIn("a.py", self.notice(output))


# ---------------------------------------------------------------------------
# 床 26: Stop がレビュー済みの内容を commit 経路が再送しない
# ---------------------------------------------------------------------------


class TestReviewedContentIsNotResent(CommitFlowTestCase):
    """I1' の文言「Stop 経路が送っていた差分と同一」には回数も含まれる。

    内容を変えない再編集 (同じ内容の Write / revert して戻した編集) は pending に
    戻すが、Stop は hash が同じなので送らない。commit 経路も同じ判定を通す。
    """

    def test_noop_reedit_after_stop_is_not_sent_again(self):
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")  # 1 バイトも変えない再編集
        self.window("tu_dedup1", lambda: self.commit("commit the reviewed content"))
        self.assertNothingSent()

    def test_content_changed_after_the_review_is_sent(self):
        """逆に、レビュー後に本当に変わった内容は送る (空ガードにしない)。"""
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.edit(SESSION_A, "a.py", f"print('{OURS}')\nprint('{OURS} again')\n")
        self.window("tu_dedup2", lambda: self.commit("commit the newer content"))
        diff = self.assertSentOnce()
        self.assertIn(f"{OURS} again", diff)


# ---------------------------------------------------------------------------
# 床 27: 不変条件だと明言していたのに床が無かった 3 件 (2 巡目 P3-2)
# ---------------------------------------------------------------------------


class TestConflictedMergeCommit(CommitFlowTestCase):
    """`reflog.COMMIT_PREFIXES` に `commit (merge): ` を足してはいけないことの床。

    conflict した `git merge` は **`logs/HEAD` を 1 バイトも伸ばさず** (2026-09-22
    実測)、解決後の commit だけが `commit (merge): <subject>` を書く。つまり W2 の
    接頭辞リストだけがこの窓を落としている — 衝突しない merge
    (`test_merge_commit_sends_nothing`) は `merge <ref>:` 行で落ちる別経路。
    """

    def test_window_with_a_conflicted_merge_commit_sends_nothing(self):
        _testutil.write(self.repo, "shared.py", "base\n")
        self.commit("base")
        self.git("checkout", "-q", "-b", "side")
        _testutil.write(self.repo, "shared.py", f"base\n{FOREIGN}\n")
        self.commit("side")
        self.git("checkout", "-q", "-")
        _testutil.write(self.repo, "shared.py", "base\nother-side\n")
        self.commit("main side")
        # このセッションの正規の編集も窓に入れる (窓ごと落ちることを送信ゼロで見るため)
        self.edit(SESSION_A, "ours.py", f"{OURS} = 1\n")

        def mutate():
            merged = _testutil.subprocess.run(
                ["git", "merge", "--no-edit", "side"],
                cwd=self.repo,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(merged.returncode, 0, "前提: merge が conflict する")
            _testutil.write(self.repo, "shared.py", "base\nresolved\n")
            self.commit("resolved merge")

        before = self.reflog_size()
        self.window("tu_cmerge", mutate)
        self.assertGreater(self.reflog_size(), before, "reflog が伸びていない (テストが空振り)")
        self.assertNothingSent()
        self.assertForeignNotSent()


class TestRecordedSetIsReadFirst(CommitFlowTestCase):
    """P1 の集合は `_record_bash_changes` が pending を書き換える**前**に読む。

    `git commit -a` を走らせた Bash では、他の書き手が残していた変更も
    `changed_between` が「変化」として返す。読む順を逆にすると、その窓の P1 に
    他者のパスが入る。

    **これは送信本文では測れない**: 積む側の guard (通知したパスを pending に
    積まない) と P6 (内容指紋) が同じ漏れ方を二重に塞いでいるため、読み順だけを
    反転しても送信は増えない。将来 guard を狭めたときに読み順が再び効き始めるので、
    ここでは呼び出し順そのものを固定する (2 巡目レビューが空振りと指摘した mutation)。
    """

    def test_p1_set_is_read_before_pending_is_updated(self):
        order: list[str] = []
        real_recorded = self.state.recorded_paths
        real_record = self.state.record_pending

        def spy_recorded(session_id):
            order.append("read")
            return real_recorded(session_id)

        def spy_record(session_id, paths):
            order.append("write")
            return real_record(session_id, paths)

        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        with mock.patch.object(
            self.state, "recorded_paths", side_effect=spy_recorded
        ), mock.patch.object(self.state, "record_pending", side_effect=spy_record):
            self.window("tu_order", lambda: self.commit("commit everything"))
        self.assertIn("read", order, "P1 の集合を読んでいない")
        self.assertIn("write", order, "pending を書いていない (テストが空振り)")
        # 「最初が read」だけでは足りない: guard 用に前で読み、P1 用にもう一度
        # 後ろで読む形を許してしまう。**書いた後の read を 1 回も許さない**
        self.assertNotIn(
            "read",
            order[order.index("write") :],
            f"pending を更新した後に P1 の集合を読み直している: {order}",
        )


class TestCommitReviewSerialization(CommitFlowTestCase):
    """commit 経路も backend lock で直列化する (Stop 側は `TestCursorSerialization`)。

    窓は 1 回きりで次に回らないので、見送りは**無言にしない**。
    """

    def test_window_is_skipped_while_another_review_holds_the_lock(self):
        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        with self.state.cursor_lock(self.repo) as acquired:
            self.assertTrue(acquired, "前提: テスト側がロックを握れている")
            output = self.window("tu_lock", lambda: self.commit("c"))
        self.assertNothingSent()
        self.assertIn("別のレビューが実行中", self.notice(output))


# ---------------------------------------------------------------------------
# 床 28: 通知と state の整合 (symlink 別名 / 配信の整形失敗)
# ---------------------------------------------------------------------------


class TestSymlinkAliasIsSettled(CommitFlowTestCase):
    def test_pending_entry_claimed_by_its_link_name_is_dropped(self):
        """symlink 別名で pending に入ったパスも commit レビュー後に外す。

        外し損ねると、**送信した直後**の Stop が「差分が空で取得できませんでした
        (内容は送信していません)」という嘘の通知を出す。
        """
        os.mkdir(os.path.join(self.repo, "real"))
        os.symlink("real", os.path.join(self.repo, "link"))
        self.edit(SESSION_A, "link/a.py", f"{OURS} = 1\n")
        self.assertEqual(
            self.pending(SESSION_A),
            [os.path.join(self.repo, "link", "a.py")],
            "前提: pending のキーが link 名になっている",
        )

        self.window("tu_alias", lambda: self.commit("c"))
        self.assertSentOnce()
        self.assertEqual(self.pending(SESSION_A), [], "別名の pending が外れていない")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotIn("取得できませんでした", self.notice(output))


class TestDeliveryFormattingFailure(CommitFlowTestCase):
    def test_formatting_failure_is_reported_instead_of_silence(self):
        """指摘を受け取った後の整形で落ちても、無言で終わらない。

        `_quiet` が覆うのは state 整理だけで、`build_reason` / `_resolve_mode` /
        `notify.compose` はその外側にある。例外がそのまま抜けると `__main__` の
        fail-open が握って stdout が空 = 外部へ送った後に指摘ごと捨てる形になる。
        """
        self.edit(SESSION_A, "a.py", f"{OURS} = 1\n")
        with mock.patch.object(
            self.entry, "build_reason", side_effect=RuntimeError("formatting is broken")
        ):
            output = self.window("tu_deliverfail", lambda: self.commit("c"), "1. 指摘あり")
        self.assertSentOnce()
        self.assertIn("整形に失敗", self.notice(output))


if __name__ == "__main__":
    unittest.main()
