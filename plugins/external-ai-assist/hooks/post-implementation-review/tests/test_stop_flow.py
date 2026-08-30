"""Stop hook のターンスコープ化に対する受け入れ基準テスト (end-to-end)。

実際の git repo と隔離 TMPDIR を使い、cursor.review() だけをモックする。
各テストは README / 改修依頼の受け入れ基準 1 項目に対応する。
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

SESSION_A = "sess-aaaa"
SESSION_B = "sess-bbbb"


class TestNoEditsNoReview(HookTestCase):
    """編集 0 件のターンで Stop が走っても cursor が起動しない。"""

    def test_clean_turn_skips_review(self):
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_dirty_worktree_but_no_session_edit_skips_review(self):
        # 作業ツリーは汚れているが、このセッションは一行も編集していない
        _testutil.write(self.repo, "seed.txt", "alpha\nCHANGED\ngamma\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()


class TestSessionIsolation(HookTestCase):
    """セッション A の編集がセッション B のレビュー対象に入らない。"""

    def test_only_own_edits_are_reviewed(self):
        self.edit(SESSION_A, "a.txt", "from A\n")
        self.edit(SESSION_B, "b.txt", "from B\n")

        self.stop(SESSION_B, "REVIEW_CLEAN")
        self.assertReviewed("b.txt")
        self.assertNotIn("a.txt", self.review_calls[0])

    def test_session_with_no_edits_skips_while_other_has_edits(self):
        self.edit(SESSION_A, "a.txt", "from A\n")
        self.stop(SESSION_B, "REVIEW_CLEAN")
        self.assertNotReviewed()


class TestReviewedPathsAreNotRepeated(HookTestCase):
    """REVIEW_CLEAN の後、同じファイルが次ターンで再レビューされない。"""

    def test_clean_then_idle_turn(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.txt")

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_touched_again_without_content_change(self):
        """同じ内容で書き直しただけなら (diff hash 不変) 再レビューしない。"""
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.txt")

        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_real_change_is_reviewed_again(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.edit(SESSION_A, "a.txt", "v2\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.txt")

    def test_block_result_also_marks_reviewed(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        output = self.stop(SESSION_A, "1. **直接影響** — 何か壊れる")
        self.assertBlocked(output)
        self.assertReviewed("a.txt")

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()


class TestFencedCleanSentinel(HookTestCase):
    """コードフェンス付き REVIEW_CLEAN (+ 前置き 1 文) を指摘扱いして Stop を block しない。"""

    REAL_WORLD_CLEAN = "critical 指摘はない\n\n```\nREVIEW_CLEAN\n```\n"

    def test_fenced_clean_does_not_block_and_marks_reviewed(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        output = self.stop(SESSION_A, self.REAL_WORLD_CLEAN)
        self.assertIn("指摘なし", self.assertNotBlocked(output), "clean なのに block している")
        self.assertReviewed("a.txt")

        # レビュー済みとして確定し、次ターンで同じ差分を再レビューしない
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertEqual(self.pending(SESSION_A), [])

    def test_sentinel_followed_by_findings_still_blocks(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        output = self.stop(SESSION_A, "```\nREVIEW_CLEAN\n```\n\n1. **直接影響** — 実は壊れる")
        self.assertBlocked(output)


class TestCursorFailureRestoresPaths(HookTestCase):
    """cursor 失敗の後、同じファイルが次ターンで再レビューされる。"""

    def test_failure_then_retry(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, None)  # cursor.review() が None = 失敗
        self.assertReviewed("a.txt")

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.txt")

    def test_failure_keeps_path_pending(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, None)
        self.assertIn(
            os.path.join(self.repo, "a.txt"),
            self.pending(SESSION_A),
            "cursor 失敗時はパスが pending に戻っていること",
        )


class TestInFlightRecovery(HookTestCase):
    """Stop が in-flight 遷移後に kill されても TTL 経過後に再レビューされる。"""

    def _state_file(self, session_id: str) -> str:
        return os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{session_id}.json"
        )

    def test_expired_in_flight_is_reclaimed(self):
        self.edit(SESSION_A, "a.txt", "v1\n")

        # Stop が claim した直後に kill された状況を作る
        claim = self.state.claim_pending(SESSION_A)
        self.assertIsNotNone(claim)
        self.assertEqual(self.pending(SESSION_A), [])

        # TTL 未満なら回収されない (走行中のレビューを横取りしない)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

        # claim 時刻を TTL 超過まで巻き戻す
        path = self._state_file(SESSION_A)
        with open(path) as f:
            data = json.load(f)
        for entry in data["in_flight"].values():
            entry["at"] = time.time() - self.state.IN_FLIGHT_TTL_SEC - 60
        with open(path, "w") as f:
            json.dump(data, f)

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.txt")

    def test_ttl_exceeds_cursor_timeout(self):
        """TTL <= cursor の timeout だと走行中の in-flight を横取りする (正しさの制約)。"""
        self.assertGreater(self.state.IN_FLIGHT_TTL_SEC, self.cursor.TIMEOUT_SEC)


class TestBashAttribution(HookTestCase):
    """sed -i など Bash 経由でファイルを変更したターンでもレビューが走る。"""

    def test_sed_on_already_dirty_file(self):
        """**すでに HEAD から変更済み**のファイルを同一サイズで書き換えるケース。

        この条件では `git status --porcelain` の行が前後とも ` M seed.txt` のまま
        変わらないため、行集合の比較では検出できない。size も不変なので、
        mtime_ns まで見て初めて拾える。
        """
        seed = os.path.join(self.repo, "seed.txt")
        with open(seed, "w") as f:
            f.write("alpha\nbeta\nGAMMA\n")  # hook 外で dirty にしておく

        def mutate():
            # 実 hook のプロセス起動間隔 (~50ms) を模す。粗い時刻粒度の FS でも
            # mtime が確実に進むようにするための待ち。
            time.sleep(0.02)
            with open(seed, "w") as f:
                f.write("alpha\nBETA\nGAMMA\n")  # 同一バイト数

        self.bash(SESSION_A, "tu_sed", mutate)

        self.assertIn(seed, self.pending(SESSION_A))
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("seed.txt")

    def test_bash_created_file(self):
        def mutate():
            _testutil.write(self.repo, "generated.txt", "made by bash\n")

        self.bash(SESSION_A, "tu_gen", mutate)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("generated.txt")

    def test_bash_without_changes_does_not_trigger_review(self):
        self.bash(SESSION_A, "tu_noop", lambda: None)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_bash_tracking_can_be_disabled(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_BASH_TRACKING"] = "0"

        def mutate():
            _testutil.write(self.repo, "generated.txt", "made by bash\n")

        self.bash(SESSION_A, "tu_gen", mutate)
        self.assertEqual(self.pending(SESSION_A), [])


class TestBashSnapshotFailure(HookTestCase):
    """git status 失敗時に空/不完全な snapshot を {} として保存し、
    作業ツリー全体を誤ってこのセッションの変更と帰属させないこと。"""

    def _bash_payload(self, session_id: str, tool_use_id: str) -> dict:
        return {
            "session_id": session_id,
            "cwd": self.repo,
            "tool_name": "Bash",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": "mutate"},
        }

    def test_pre_snapshot_failure_does_not_attribute_unrelated_dirty_files(self):
        # このセッションの Bash が起こしたのではない「他の変更」を用意しておく
        _testutil.write(self.repo, "unrelated.txt", "someone else's change\n")
        payload = self._bash_payload(SESSION_A, "tu_fail_pre")

        with mock.patch.object(self.gitscan, "status_snapshot", return_value=None):
            self.run_hook("pre-tool", payload)  # git status 失敗 (timeout 等) を模す

        self.run_hook("post-tool", payload)  # post 側は通常どおり動く想定

        self.assertEqual(
            self.pending(SESSION_A),
            [],
            "pre スナップショット失敗時に作業ツリー全体を pending に積んではいけない",
        )

    def test_post_snapshot_failure_does_not_attribute_unrelated_dirty_files(self):
        _testutil.write(self.repo, "unrelated.txt", "someone else's change\n")
        payload = self._bash_payload(SESSION_A, "tu_fail_post")

        self.run_hook("pre-tool", payload)  # pre 側は通常どおり成功させる

        with mock.patch.object(self.gitscan, "status_snapshot", return_value=None):
            self.run_hook("post-tool", payload)  # git status 失敗 (timeout 等) を模す

        self.assertEqual(
            self.pending(SESSION_A),
            [],
            "post スナップショット失敗時に pre 全件を pending に積んではいけない",
        )

    def test_pre_snapshot_failure_writes_no_snapshot_file(self):
        """失敗時は null を書くのではなく、そもそもファイルを作らない (残骸を増やさない)。"""
        payload = self._bash_payload(SESSION_A, "tu_fail_pre2")
        with mock.patch.object(self.gitscan, "status_snapshot", return_value=None):
            self.run_hook("pre-tool", payload)
        snapshot_dir = os.path.join(self.tmpdir, "post-implementation-review", "bashsnap")
        self.assertFalse(
            os.path.exists(snapshot_dir), "snapshot 失敗時にファイルを作ってはいけない"
        )


class TestOverflowCarryOver(HookTestCase):
    """1 回のレビュー上限を超えたパスを黙って捨てず、次ターンでレビューする。"""

    def test_leftover_paths_are_reviewed_next_turn(self):
        self.entry.MAX_REVIEW_PATHS = 2
        for i in range(5):
            self.edit(SESSION_A, f"f{i}.txt", f"content {i}\n")

        self.stop(SESSION_A, "REVIEW_CLEAN")
        first = self.review_calls[0]
        reviewed_first = {f"f{i}.txt" for i in range(5) if f"f{i}.txt" in first}
        self.assertEqual(len(reviewed_first), 2)

        seen = set(reviewed_first)
        for _ in range(3):
            self.stop(SESSION_A, "REVIEW_CLEAN")
            if not self.review_calls:
                break
            seen |= {f"f{i}.txt" for i in range(5) if f"f{i}.txt" in self.review_calls[0]}

        self.assertEqual(
            seen,
            {f"f{i}.txt" for i in range(5)},
            "上限超過分が捨てられず、後続ターンで全てレビューされること",
        )


class TestCursorSerialization(HookTestCase):
    """2 セッションが同時に編集したターンで cursor agent が同時に 2 つ起動しない。"""

    def test_second_session_skips_while_lock_held(self):
        self.edit(SESSION_A, "a.txt", "from A\n")
        self.edit(SESSION_B, "b.txt", "from B\n")

        with self.state.cursor_lock(self.repo) as acquired:
            self.assertTrue(acquired)
            self.stop(SESSION_B, "REVIEW_CLEAN")
            self.assertNotReviewed()

        # ロックを保持している間に pending が消費されていないこと
        self.assertIn(os.path.join(self.repo, "b.txt"), self.pending(SESSION_B))

        self.stop(SESSION_B, "REVIEW_CLEAN")
        self.assertReviewed("b.txt")

    def test_lock_is_per_worktree(self):
        other = _testutil.init_repo(os.path.join(self._tmp.name, "other"))
        with self.state.cursor_lock(self.repo) as first:
            self.assertTrue(first)
            with self.state.cursor_lock(other) as second:
                self.assertTrue(second, "別作業ツリーは互いにブロックしない")


class TestUntrackedOnly(HookTestCase):
    """未追跡ファイルのみを新規作成したケースでもレビューが走る。"""

    def test_new_file_only(self):
        self.edit(SESSION_A, "brand_new.txt", "hello\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("brand_new.txt", "hello")


class TestOutsideWorktree(HookTestCase):
    """作業ツリー外の絶対パスは対象から除外される。"""

    def test_outside_path_alone_skips_review(self):
        outside = os.path.join(self._tmp.name, "outside.txt")
        with open(outside, "w") as f:
            f.write("not in repo\n")
        self.run_hook(
            "post-tool",
            {
                "session_id": SESSION_A,
                "cwd": self.repo,
                "tool_name": "Write",
                "tool_use_id": "tu_outside",
                "tool_input": {"file_path": outside},
            },
        )
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_outside_path_is_dropped_not_restored(self):
        outside = os.path.join(self._tmp.name, "outside.txt")
        with open(outside, "w") as f:
            f.write("not in repo\n")
        self.run_hook(
            "post-tool",
            {
                "session_id": SESSION_A,
                "cwd": self.repo,
                "tool_name": "Write",
                "tool_use_id": "tu_outside",
                "tool_input": {"file_path": outside},
            },
        )
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("a.txt")
        self.assertNotIn("outside.txt", self.review_calls[0])
        self.assertEqual(self.pending(SESSION_A), [], "ツリー外パスが残り続けないこと")


def _parse_output(output: str) -> dict:
    return json.loads(output) if output else {}


class TestExclusion(HookTestCase):
    """機密・非コードファイルの差分を外部 AI に送らない。除外は恒久で通知を出す。"""

    SECRET = "API_KEY=sk-live-DO-NOT-SEND\n"

    def test_secret_file_alone_is_not_reviewed_and_not_kept(self):
        self.edit(SESSION_A, ".env", self.SECRET)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertNotReviewed()
        self.assertEqual(self.pending(SESSION_A), [], "除外は恒久 (pending に戻さない)")
        parsed = _parse_output(output)
        self.assertNotIn("decision", parsed)
        self.assertNotIn("hookSpecificOutput", parsed)
        self.assertIn("1 ファイルを外部 AI レビューから除外", parsed["systemMessage"])
        self.assertIn(".env (既定除外: .env)", parsed["systemMessage"])
        self.assertNotIn("sk-live", parsed["systemMessage"], "内容は通知にも出さない")
        self.assertIn("除外", self.last_stderr)

    def test_secret_is_dropped_while_code_is_reviewed(self):
        self.edit(SESSION_A, "certs/server.pem", "-----BEGIN PRIVATE KEY-----\nXYZ\n")
        self.edit(SESSION_A, "a.py", "print('hi')\n")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("a.py")
        diff = self.review_calls[0]
        self.assertNotIn("server.pem", diff)
        self.assertNotIn("PRIVATE KEY", diff)
        self.assertIn("certs/server.pem (既定除外: *.pem)", _parse_output(output)["systemMessage"])

        # 次ターンでも再掲されない (reviewed にも pending にも無い)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertEqual(self.pending(SESSION_A), [])

    def test_block_output_carries_both_decision_and_notice(self):
        self.edit(SESSION_A, ".env", self.SECRET)
        self.edit(SESSION_A, "a.py", "v1\n")
        parsed = self.assertBlocked(self.stop(SESSION_A, "1. **直接影響** — 何か壊れる"))
        self.assertIn("直接影響", parsed["reason"])
        self.assertIn(".env", parsed["systemMessage"])

    def test_no_notice_when_nothing_is_excluded(self):
        self.edit(SESSION_A, "a.py", "v1\n")
        message = self.assertNotBlocked(self.stop(SESSION_A, "REVIEW_CLEAN"))
        self.assertNotIn("除外", message, "何も除外していないのに除外通知が出ている")

    def test_env_glob_excludes_documents(self):
        os.environ[self.entry.exclusion.ENV_EXCLUDE] = "docs/, *.csv"
        self.edit(SESSION_A, "docs/meeting-notes.txt", "customer said ...\n")
        self.edit(SESSION_A, "data/rows.csv", "a,b\n")
        self.edit(SESSION_A, "a.py", "v1\n")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("a.py")
        self.assertNotIn("customer said", self.review_calls[0])
        self.assertNotIn("rows.csv", self.review_calls[0])
        message = _parse_output(output)["systemMessage"]
        self.assertIn("2 ファイルを外部 AI レビューから除外", message)
        self.assertIn("docs/meeting-notes.txt (EXTERNAL_AI_POST_REVIEW_EXCLUDE: docs/*)", message)

    def test_code_only_drops_markdown(self):
        os.environ[self.entry.exclusion.ENV_CODE_ONLY] = "1"
        self.edit(SESSION_A, "notes.md", "# private notes\n")
        self.edit(SESSION_A, "a.py", "v1\n")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("a.py")
        self.assertNotIn("private notes", self.review_calls[0])
        self.assertIn("notes.md (CODE_ONLY: .md)", _parse_output(output)["systemMessage"])

    def test_bash_created_secret_is_excluded_too(self):
        def mutate():
            _testutil.write(self.repo, ".env", self.SECRET)
            _testutil.write(self.repo, "generated.py", "x = 1\n")

        self.bash(SESSION_A, "tu_gen", mutate)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("generated.py")
        self.assertNotIn("sk-live", self.review_calls[0])

    def test_symlink_named_like_a_secret_is_excluded(self):
        os.makedirs(os.path.join(self.repo, "vault"))
        _testutil.write(self.repo, "vault/c.json", '{"token": "abc"}\n')
        link = os.path.join(self.repo, "credentials.json")
        os.symlink(os.path.join(self.repo, "vault", "c.json"), link)
        self.run_hook(
            "post-tool",
            {
                "session_id": SESSION_A,
                "cwd": self.repo,
                "tool_name": "Write",
                "tool_use_id": "tu_link",
                "tool_input": {"file_path": link},
            },
        )
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertEqual(self.pending(SESSION_A), [])

    def test_edit_through_symlinked_credentials_directory_is_excluded(self):
        """Codex P1: `credentials/` → `ordinary/` の symlink 経由で編集しても外部に送らない。"""
        os.makedirs(os.path.join(self.repo, "ordinary"))
        _testutil.write(self.repo, "ordinary/data.json", '{"k": 1}\n')
        _testutil.git(self.repo, "add", "-A")
        _testutil.git(self.repo, "commit", "-qm", "data")
        os.symlink(os.path.join(self.repo, "ordinary"), os.path.join(self.repo, "credentials"))

        self.edit(SESSION_A, "credentials/data.json", '{"k": "sk-live-DO-NOT-SEND"}\n')
        self.edit(SESSION_A, "a.py", "v1\n")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("a.py")
        self.assertNotIn("sk-live", self.review_calls[0])
        self.assertNotIn("data.json", self.review_calls[0])
        message = _parse_output(output)["systemMessage"]
        self.assertIn('credentials/data.json (既定除外: 語 "credentials")', message)
        self.assertEqual(self.pending(SESSION_A), [])

    def _tracked_ordinary_with_credentials_link(self, track_link: bool) -> str:
        os.makedirs(os.path.join(self.repo, "ordinary"))
        target = _testutil.write(self.repo, "ordinary/data.json", '{"k": 1}\n')
        _testutil.git(self.repo, "add", "-A")
        _testutil.git(self.repo, "commit", "-qm", "data")
        os.symlink(os.path.join(self.repo, "ordinary"), os.path.join(self.repo, "credentials"))
        if track_link:
            _testutil.git(self.repo, "add", "-A")
            _testutil.git(self.repo, "commit", "-qm", "link")
        return target

    def _assert_bash_edit_via_real_path_is_excluded(self, track_link: bool) -> None:
        """Codex R2 P1: `sed -i credentials/data.json` の変更は `git status` では実体名
        `ordinary/data.json` でしか現れない。別名 `credentials/data.json` を生成して除外する。"""
        target = self._tracked_ordinary_with_credentials_link(track_link)

        def mutate():
            with open(target, "w") as f:
                f.write('{"k": "sk-live-DO-NOT-SEND"}\n')

        self.bash(SESSION_A, "tu_sed", mutate)
        self.assertEqual(self.pending(SESSION_A), [target], "claim には実体名しか入らない前提")
        self.edit(SESSION_A, "a.py", "v1\n")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("a.py")
        self.assertNotIn("sk-live", self.review_calls[0])
        self.assertNotIn("data.json", self.review_calls[0])
        self.assertIn(
            'credentials/data.json (既定除外: 語 "credentials")',
            _parse_output(output)["systemMessage"],
        )
        self.assertEqual(self.pending(SESSION_A), [])

    def test_bash_edit_via_real_path_is_excluded_by_tracked_symlink_alias(self):
        self._assert_bash_edit_via_real_path_is_excluded(track_link=True)

    def test_bash_edit_via_real_path_is_excluded_by_untracked_symlink_alias(self):
        self._assert_bash_edit_via_real_path_is_excluded(track_link=False)

    def test_bash_edit_without_any_symlink_is_reviewed_as_before(self):
        """symlink が無い repo では別名生成が何も変えない (挙動不変)。"""
        os.makedirs(os.path.join(self.repo, "ordinary"))
        target = _testutil.write(self.repo, "ordinary/data.json", '{"k": 1}\n')
        _testutil.git(self.repo, "add", "-A")
        _testutil.git(self.repo, "commit", "-qm", "data")

        def mutate():
            with open(target, "w") as f:
                f.write('{"k": 2}\n')

        self.bash(SESSION_A, "tu_sed", mutate)
        message = self.assertNotBlocked(self.stop(SESSION_A, "REVIEW_CLEAN"))
        self.assertNotIn("除外", message, "symlink が無いのに除外通知が出ている")
        self.assertReviewed("ordinary/data.json", '"k": 2')

    def test_stale_state_from_older_version_is_drained(self):
        """0.4.1 以前が pending に積んだ機密パスも、次の Stop で落ちて残り続けない。"""
        self.state.record_pending(SESSION_A, [os.path.join(self.repo, ".env")])
        _testutil.write(self.repo, ".env", self.SECRET)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertEqual(self.pending(SESSION_A), [])

    def test_excluded_paths_do_not_consume_review_slots(self):
        self.entry.MAX_REVIEW_PATHS = 2
        self.edit(SESSION_A, ".env", self.SECRET)
        self.edit(SESSION_A, "id_rsa", "key\n")
        self.edit(SESSION_A, "a.py", "v1\n")
        self.edit(SESSION_A, "b.py", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.py", "b.py")
        self.assertEqual(self.pending(SESSION_A), [], "除外分が枠を食って overflow していない")

    def test_negation_sends_code_under_a_credentials_directory(self):
        os.environ[self.entry.exclusion.ENV_EXCLUDE] = "!credentials-service/*"
        self.edit(SESSION_A, "credentials-service/main.go", "package main\n")
        self.edit(SESSION_A, "credentials.json", '{"k": "sk-live-DO-NOT-SEND"}\n')
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("credentials-service/main.go")
        self.assertNotIn("sk-live", self.review_calls[0])


class TestLiteralPathspecFlow(HookTestCase):
    """L2 P1: glob に見えるファイル名で別セッションの編集が混入しない / 旧 state の glob 風
    エントリで機密が漏れない。"""

    def test_glob_named_file_does_not_pull_in_other_sessions_edit(self):
        _testutil.write(self.repo, "app/[id]/page.tsx", "dynamic\n")
        _testutil.write(self.repo, "app/i/page.tsx", "static\n")
        _testutil.git(self.repo, "add", "-A")
        _testutil.git(self.repo, "commit", "-qm", "routes")

        self.edit(SESSION_A, "app/[id]/page.tsx", "dynamic v2\n")
        self.edit(SESSION_B, "app/i/page.tsx", "OTHER_SESSION_EDIT\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("app/[id]/page.tsx", "dynamic v2")
        self.assertNotIn("OTHER_SESSION_EDIT", self.review_calls[0])
        self.assertNotIn("app/i/page.tsx", self.review_calls[0])

    def test_glob_looking_stale_entry_does_not_leak_tracked_secret(self):
        _testutil.write(self.repo, ".env", "A=1\n")
        _testutil.git(self.repo, "add", "-A")
        _testutil.git(self.repo, "commit", "-qm", "env")
        _testutil.write(self.repo, ".env", "A=sk-live-LEAK\n")  # このセッションの編集ではない
        self.state.record_pending(SESSION_A, [os.path.join(self.repo, "[.]env")])

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertEqual(self.pending(SESSION_A), [])


class TestByteBudgetFlow(HookTestCase):
    """予算に収まらないファイルは pending に戻り hash 未記録、巨大単一ファイルは truncated。"""

    def _content(self, kib: int, seed: str = "x") -> str:
        return _testutil.content_kib(kib, seed)

    def test_second_30kb_file_is_reviewed_next_turn(self):
        self.edit(SESSION_A, "a.py", self._content(30))
        self.edit(SESSION_A, "b.py", self._content(30, "y"))
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("a.py")
        self.assertNotIn("b.py", self.review_calls[0])
        self.assertLessEqual(len(self.review_calls[0].encode()), self.entry.MAX_DIFF_BYTES)
        self.assertEqual(self.pending(SESSION_A), [os.path.join(self.repo, "b.py")])
        message = _parse_output(output)["systemMessage"]
        self.assertIn("予算に収まらないため次ターンに繰り越し", message)
        self.assertIn("b.py", message)

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("b.py")
        self.assertNotIn("a.py", self.review_calls[0], "a.py は hash 記録済みで再掲されない")
        self.assertEqual(self.pending(SESSION_A), [])

    def test_deferred_file_is_not_starved_by_new_edits(self):
        """繰り越したファイルは次ターンの先頭に来る (新しい編集に追い越され続けない)。"""
        self.edit(SESSION_A, "a.py", self._content(30))
        self.edit(SESSION_A, "z.py", self._content(30, "y"))
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.py")

        # 次ターンでアルファベット順で先に来る大きな編集を足しても z.py が先
        self.edit(SESSION_A, "b.py", self._content(30, "w"))
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("z.py")
        self.assertNotIn("b.py", self.review_calls[0])
        self.assertEqual(self.pending(SESSION_A), [os.path.join(self.repo, "b.py")])

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("b.py")
        self.assertEqual(self.pending(SESSION_A), [])

    def test_single_50kb_file_is_truncated_and_marked_reviewed(self):
        self.edit(SESSION_A, "huge.py", self._content(50))
        output = self.stop(SESSION_A, "REVIEW_CLEAN")

        self.assertReviewed("huge.py")
        diff = self.review_calls[0]
        self.assertLessEqual(len(diff.encode()), self.entry.MAX_FILE_DIFF_BYTES)
        self.assertIn("(truncated for review", diff)
        message = _parse_output(output)["systemMessage"]
        self.assertIn("先頭のみ送信 (truncated)", message)
        self.assertIn("huge.py", message)
        self.assertEqual(self.pending(SESSION_A), [])

        # 切り詰め時は hash を記録する: 変わらなければ再掲しない
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

        # 末尾だけ変えても (hash は全文で取る) 再掲される
        self.edit(SESSION_A, "huge.py", self._content(50) + "tail\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("huge.py")

    def test_carry_over_keeps_claim_order_across_overflow_and_budget(self):
        """予算超過 (rels の途中) が上限超過 (rels の外) より先に pending へ戻る。"""
        self.entry.MAX_REVIEW_PATHS = 2
        self.edit(SESSION_A, "a.py", self._content(30))
        self.edit(SESSION_A, "b.py", self._content(30, "y"))
        self.edit(SESSION_A, "c.py", self._content(30, "w"))  # 上限超過で overflow
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.py")
        self.assertEqual(
            [os.path.basename(p) for p in self.pending(SESSION_A)], ["b.py", "c.py"]
        )

        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("b.py")
        self.assertNotIn("c.py", self.review_calls[0])
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("c.py")
        self.assertEqual(self.pending(SESSION_A), [])

    def test_cursor_failure_keeps_deferred_and_submitted_pending(self):
        self.edit(SESSION_A, "a.py", self._content(30))
        self.edit(SESSION_A, "b.py", self._content(30, "y"))
        self.stop(SESSION_A, None)  # cursor 失敗
        self.assertEqual(
            sorted(os.path.basename(p) for p in self.pending(SESSION_A)),
            ["a.py", "b.py"],
            "送ったものも繰り越したものも pending に残る",
        )
        state_path = os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json"
        )
        with open(state_path) as f:
            self.assertEqual(json.load(f)["reviewed"], {}, "失敗時は hash を記録しない")


class TestDisabledOrUnavailableSkipsGitAndState(HookTestCase):
    """無効化 / cursor 不在なら pre-tool・post-tool の先頭で
    即 return し、git status も state 書込も一切行わない。"""

    def _state_dir(self) -> str:
        return os.path.join(self.tmpdir, "post-implementation-review", "state")

    def _bashsnap_dir(self) -> str:
        return os.path.join(self.tmpdir, "post-implementation-review", "bashsnap")

    def test_disabled_pre_tool_writes_no_bash_snapshot(self):
        os.environ["EXTERNAL_AI_POST_REVIEW"] = "0"
        self.run_hook(
            "pre-tool",
            {
                "session_id": SESSION_A,
                "cwd": self.repo,
                "tool_name": "Bash",
                "tool_use_id": "tu_x",
                "tool_input": {"command": "echo hi"},
            },
        )
        self.assertFalse(os.path.exists(self._bashsnap_dir()))

    def test_disabled_post_tool_records_no_pending(self):
        os.environ["EXTERNAL_AI_POST_REVIEW"] = "0"
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.assertFalse(os.path.exists(self._state_dir()))

    def test_cursor_unavailable_skips_pre_and_post_tool(self):
        with mock.patch.object(self.cursor, "is_available", return_value=False):
            self.run_hook(
                "pre-tool",
                {
                    "session_id": SESSION_A,
                    "cwd": self.repo,
                    "tool_name": "Bash",
                    "tool_use_id": "tu_x",
                    "tool_input": {"command": "echo hi"},
                },
            )
            self.edit(SESSION_A, "a.txt", "v1\n")
        self.assertFalse(os.path.exists(self._state_dir()))
        self.assertFalse(os.path.exists(self._bashsnap_dir()))

    def test_enabled_and_available_still_writes_state(self):
        """回帰: 有効時は従来どおり state を書く (早期 return が常時 skip になっていない)。"""
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.assertIn(os.path.join(self.repo, "a.txt"), self.pending(SESSION_A))


class TestNoOpStopCreatesNoStateFile(HookTestCase):
    """編集 0 件のターンの Stop で state ファイルが新規生成
    されない (claim_pending が state ファイル皆無のセッションを開かない)。"""

    def test_no_op_stop_creates_no_state_file(self):
        state_path = os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json"
        )
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertFalse(os.path.exists(state_path))

    def test_state_file_is_created_once_something_is_pending(self):
        """回帰: 実際に何かが起きたセッションでは今までどおり state を書く。"""
        state_path = os.path.join(
            self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json"
        )
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.assertTrue(os.path.exists(state_path))


class TestGuards(HookTestCase):
    """再帰防止・無効化スイッチ。"""

    def test_stop_hook_active_skips(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        with mock.patch.object(self.cursor, "review") as review:
            self.run_hook(
                "stop",
                {"session_id": SESSION_A, "cwd": self.repo, "stop_hook_active": True},
            )
            review.assert_not_called()
        self.assertIn(
            os.path.join(self.repo, "a.txt"),
            self.pending(SESSION_A),
            "再帰防止でスキップしても pending は温存されること",
        )

    def test_disable_switch(self):
        os.environ["EXTERNAL_AI_POST_REVIEW"] = "0"
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_legacy_max_zero_still_disables(self):
        """v0.2.0 で `MAX=0` を無効化スイッチとして使っていた環境の互換。"""
        del os.environ["EXTERNAL_AI_POST_REVIEW"]
        os.environ["EXTERNAL_AI_POST_REVIEW_MAX"] = "0"
        self.edit(SESSION_A, "a.txt", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_legacy_max_two_no_longer_caps_reviews(self):
        """回数予算は撤廃済み — 3 ターン目以降も黙って止まらない。"""
        del os.environ["EXTERNAL_AI_POST_REVIEW"]
        os.environ["EXTERNAL_AI_POST_REVIEW_MAX"] = "2"
        for i in range(4):
            self.edit(SESSION_A, "a.txt", f"v{i}\n")
            self.stop(SESSION_A, "REVIEW_CLEAN")
            self.assertReviewed("a.txt")


class TestReviewCopyPermissions(HookTestCase):
    """内部バックログ: レビュー結果の参照コピー (コード抜粋を含む) が共有 $TMPDIR で
    他ユーザーから読めないこと。"""

    def test_review_copy_file_is_created_with_0600(self):
        self.edit(SESSION_A, "a.txt", "v1\n")
        output = self.stop(SESSION_A, "1. **直接影響** — 何か壊れる")
        self.assertBlocked(output)
        path = self.state.review_copy_path(SESSION_A)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class TestCursorLaunchCwd(HookTestCase):
    """cursor はリポジトリ root (payload の cwd ではなく worktree_root) で起動すること。

    差分のパス (gitscan が返す worktree root 相対) と cursor のワークスペースが
    食い違うと、サブディレクトリで Claude Code を起動したセッションで cursor 側の
    参照・探索が外れる (内部バックログ)。
    """

    def test_review_is_invoked_with_worktree_root_as_cwd(self):
        self.edit(SESSION_A, "a.py", "v1\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.py")
        self.assertEqual(self.review_cwds, [self.repo])


class TestSameTurnCommitNotification(HookTestCase):
    """HEAD 基準の diff が空になったパス (同一ターン内 commit 等) は、復元を
    試みず `systemMessage` で「取得できなかった」と通知する (黙って消費しない)。

    以前 (0.9.0 導入時) は、前回 Stop が記録した HEAD を基点に
    `git rev-list --not --remotes` で「手元由来」(どのリモートにも存在しない)
    と証明できれば、基点まで遡った diff を復元して送る経路があった。この証明は
    「リモート由来でない」ことしか示しておらず「このセッションが書いた」ことは
    示していない。同一 worktree を共有する別のローカルの書き手 (別セッション・
    人間の手動 commit) が push せずに同じパスへ commit すると、その内容が
    丸ごと外部へ送信されてしまうことがマージ前レビューで実演された
    (`test_other_local_writer_commit_is_not_leaked` が回帰テスト)。pull/merge
    経由の混入は元から遮断できていたが (`test_pull_bringing_foreign_commit_is_reported`)、
    これは別ベクトルだった。

    安全な復元には編集時点の内容退避が要り、「PostToolUse を軽く保つ」という
    既存の設計と衝突するため、復元機構そのものを撤去し、常に「取得できない」と
    報告するだけにする (送信範囲が広がる方向には倒さない)。
    """

    def _add_bare_origin(self) -> tuple[str, str]:
        bare = os.path.join(self._tmp.name, "origin.git")
        _testutil.git(self._tmp.name, "init", "--bare", "-q", "origin.git")
        _testutil.git(self.repo, "remote", "add", "origin", bare)
        branch = _testutil.git(self.repo, "branch", "--show-current").stdout.strip()
        _testutil.git(self.repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        _testutil.git(bare, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        return bare, branch

    def _push_foreign_commit(self, bare: str, branch: str) -> None:
        clone = os.path.join(self._tmp.name, "clone")
        _testutil.git(self._tmp.name, "clone", "-q", bare, "clone")
        _testutil.git(clone, "config", "user.email", "other@example.com")
        _testutil.git(clone, "config", "user.name", "other")
        _testutil.write(clone, "foreign.txt", "from someone else\n")
        _testutil.git(clone, "add", "-A")
        _testutil.git(clone, "commit", "-qm", "foreign change")
        _testutil.git(clone, "push", "-q", "origin", branch)

    def _establish_history(self) -> None:
        """レビュー履歴が既にあるセッションでも結果が変わらないことを示すための
        前段 (何かを 1 回レビューさせておくだけ)。"""
        self.edit(SESSION_A, "warmup.txt", "hello\n")
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("warmup.txt")

    def test_same_turn_commit_is_reported_not_reviewed(self):
        """同一ターン内で commit した変更は、レビューされる代わりに
        「取得できませんでした」と報告される (復元しない)。"""
        self._establish_history()

        self.edit(SESSION_A, "a.py", "print(1)\n")
        _testutil.git(self.repo, "add", "a.py")
        _testutil.git(self.repo, "commit", "-qm", "self commit")

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        message = json.loads(output)["systemMessage"] if output else ""
        self.assertIn("取得できませんでした", message)
        self.assertIn("a.py", message)
        self.assertNotIn("print(1)", message, "通知には内容を出さない")
        self.assertEqual(
            self.pending(SESSION_A), [], "取得できなかったパスが pending に残り続けないこと"
        )

    def test_other_local_writer_commit_is_not_leaked(self):
        """regression: マージ前レビューで実演された脆弱性。別のローカルの書き手
        (このセッションの hook を経由しない、別セッション・人間の手動 commit を
        模す) が push せずに同じパスへ commit した内容を、外部 AI CLI へ送らない
        こと。

        `_establish_history()` で先にレビューを 1 回走らせておくのは、旧
        (基点フォールバック) コードが `base_sha` を確立していない最初の Stop
        では復元を試みる余地が無く、この回帰の再現にならないため。
        """
        self._establish_history()

        self.edit(SESSION_A, "a.py", "print('session A')\n")
        # このセッションの hook を経由しない、別の書き手による直接 commit。
        # push していないので、旧コードの `is_local_only_range` では
        # 「手元由来」と判定され、基点まで遡った diff の復元経路に入って
        # `diff_since` がこの内容を拾って送ってしまっていた。
        _testutil.write(self.repo, "a.py", "print('someone else entirely')\n")
        _testutil.git(self.repo, "add", "a.py")
        _testutil.git(self.repo, "commit", "-qm", "another local writer's commit")

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        message = json.loads(output)["systemMessage"] if output else ""
        self.assertIn("取得できませんでした", message)
        self.assertNotIn("someone else entirely", message)
        self.assertEqual(self.pending(SESSION_A), [])

    def test_pull_bringing_foreign_commit_is_reported(self):
        """pull/merge で他人の commit が混ざった場合も同じ経路で報告のみ
        (この既知のベクトルは旧コードでも安全に扱えていた)。"""
        bare, branch = self._add_bare_origin()
        self._establish_history()

        self.edit(SESSION_A, "a.py", "print(1)\n")
        _testutil.git(self.repo, "add", "a.py")
        _testutil.git(self.repo, "commit", "-qm", "self commit")
        self._push_foreign_commit(bare, branch)
        _testutil.git(self.repo, "fetch", "-q", "origin")
        _testutil.git(self.repo, "merge", "-q", "--no-edit", f"origin/{branch}")

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        message = json.loads(output)["systemMessage"] if output else ""
        self.assertIn("取得できませんでした", message)
        self.assertIn("a.py", message)
        self.assertNotIn("print(1)", message, "通知には内容を出さない")
        self.assertEqual(
            self.pending(SESSION_A), [], "報告済みのパスが pending に残り続けないこと"
        )

    def test_first_ever_stop_with_committed_change_is_reported(self):
        """セッション初回の Stop でも、commit 済みで空になったパスを
        黙って消費しない。"""
        self.edit(SESSION_A, "a.py", "print(1)\n")
        _testutil.git(self.repo, "add", "a.py")
        _testutil.git(self.repo, "commit", "-qm", "self commit")

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        message = json.loads(output)["systemMessage"] if output else ""
        self.assertIn("a.py", message)
        self.assertEqual(self.pending(SESSION_A), [])

    def test_edit_without_commit_is_unaffected(self):
        """回帰: commit を挟まない通常編集は影響を受けず、従来どおりレビューされる。"""
        self.edit(SESSION_A, "a.py", "print(1)\n")  # commit しない
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertReviewed("a.py", "print(1)")
        message = json.loads(output)["systemMessage"] if output else ""
        self.assertNotIn("取得できませんでした", message)


if __name__ == "__main__":
    unittest.main()
