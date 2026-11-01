"""塊ごとの送信の途中で kill されたレビューの結果を捨てない (0.17.0)。

Stop は 40 KB を超える差分を塊に分けて順に送る。途中で利用者が次の発言をすると hook ごと
止まる。返ってきた塊の結果 (hash と指摘) は塊が返るたびに in-flight の entry に退避し、
後続のレビューが自分の結果を出す直前に配る (hash はそこで初めて記録する)。claim した hook が
もう居なければ、後続の claim は TTL を待たずに残りの塊のパスを pending に戻す。

kill は `BaseException` の送出で再現する (`_review_claim` の `except Exception` を素通りし、
claim を復元しないまま hook が抜ける = 実際の kill と同じく state がそのまま残る)。claim した
プロセスが居ないことは、終了済みの子プロセスの pid を `state._owner_pid` に返させて作る。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

SESSION_A = "sess-aaaa"
FINDING = "1. **critical** — off-by-one in loop bound"


class Killed(BaseException):
    """hook のプロセスが kill されたことの代わり。"""


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], env=_testutil.hermetic_env())
    proc.wait()
    return proc.pid


class InterruptedTestCase(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dead_pid = _dead_pid()

    def content(self, kib: int, seed: str) -> str:
        return _testutil.content_kib(kib, seed)

    def two_chunks(self) -> None:
        """30 KiB のファイルを 2 つ書く (1 回の送信の上限 40 KB を超え、塊 2 つになる)。"""
        self.edit(SESSION_A, "a.py", self.content(30, "a"))
        self.edit(SESSION_A, "b.py", self.content(30, "b"))

    def killed_stop(self, answers: list, kill_at: int, owner_pid: int | None = None) -> list[str]:
        """Stop を起動し、`kill_at` 回目の送信の最中に kill する。返り値は送った diff。

        `owner_pid` は claim に記録させる pid (既定は終了済みのプロセス)。
        """
        calls: list[str] = []

        def fake_review(diff_text: str, *, cwd: str | None = None):
            calls.append(diff_text)
            if len(calls) == kill_at:
                raise Killed()
            return answers[len(calls) - 1]

        pid = self.dead_pid if owner_pid is None else owner_pid
        with mock.patch.object(self.cursor, "review", side_effect=fake_review), \
                mock.patch.object(self.state, "_owner_pid", return_value=pid), \
                self.assertRaises(Killed):
            self.run_hook(
                "stop", {"session_id": SESSION_A, "cwd": self.repo, "stop_hook_active": False}
            )
        return calls

    def raw_state(self) -> dict:
        path = os.path.join(self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json")
        with open(path) as f:
            return json.load(f)

    def reviewed(self) -> list[str]:
        return sorted(os.path.basename(p) for p in self.raw_state()["reviewed"])

    def reason(self, output: str) -> str:
        return self.assertBlocked(output)["reason"]

    def parsed(self, output: str) -> dict:
        """出力の JSON (空なら assertion の失敗にする。変異で ERROR にしないため)。"""
        self.assertTrue(output, "Stop の出力が空")
        return json.loads(output)


class TestReturnedChunkSurvivesAKill(InterruptedTestCase):
    def test_findings_arrive_at_the_next_stop_and_the_chunk_is_not_resent(self):
        """DoD: 2 塊目の送信中に kill → 1 塊目の指摘が次の Stop で届き、1 塊目は再送しない。"""
        self.two_chunks()
        calls = self.killed_stop([FINDING], kill_at=2)
        self.assertIn("a.py", calls[0])
        self.assertIn("b.py", calls[1])
        self.assertEqual(self.reviewed(), [], "kill の時点ではまだ hash を記録しない")

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        reason = self.reason(output)
        self.assertIn(FINDING, reason)
        self.assertIn(self.entry.RECOVERED_PREFACE, reason)
        self.assertEqual(len(self.review_calls), 1, "TTL を待たずに 2 塊目だけを送り直す")
        self.assertIn("b.py", self.review_calls[0])
        self.assertNotIn("a.py", self.review_calls[0])
        self.assertEqual(self.raw_state()["in_flight"], {})
        self.assertEqual(self.pending(SESSION_A), [])
        self.assertEqual(self.reviewed(), ["a.py", "b.py"])
        message = self.parsed(output)["systemMessage"]
        self.assertIn("中断された前回のレビューで返っていた指摘 (1 回分)", message)
        copy = self.state.review_copy_path(SESSION_A)
        self.assertTrue(os.path.exists(copy), "参照コピーを書いていない")
        with open(copy) as f:
            self.assertIn(FINDING, f.read(), "参照コピーにも残す")
        self.assertFalse(
            [p for p in self.raw_state()["fingerprints"] if p.endswith("a.py")],
            "配り終えたパスの指紋は残さない",
        )

        # 一度配った指摘は二度と配らない。a.py / b.py も送り直さない
        again = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertNotIn(FINDING, again)

    def test_findings_are_added_to_the_current_findings(self):
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        output = self.stop(SESSION_A, "- 今回の指摘")
        reason = self.reason(output)
        self.assertIn("- 今回の指摘", reason)
        self.assertIn(FINDING, reason)
        self.assertLess(reason.index("- 今回の指摘"), reason.index(FINDING))

    def test_block_mode_delivers_them_as_a_reason(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "block"
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        data = self.parsed(output)
        self.assertEqual(data.get("decision"), "block")
        self.assertIn(FINDING, data["reason"])

    def test_block_mode_appends_them_to_the_current_reason(self):
        os.environ["EXTERNAL_AI_POST_REVIEW_MODE"] = "block"
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        data = self.parsed(self.stop(SESSION_A, "- 今回の指摘"))
        self.assertEqual(data.get("decision"), "block")
        self.assertIn("- 今回の指摘", data["reason"])
        self.assertIn(FINDING, data["reason"])

    def test_findings_arrive_when_every_chunk_had_returned(self):
        """最後の塊を退避した直後に kill: 送り直すものは無く (claim も無い)、指摘だけが届く。"""
        self.two_chunks()
        real = self.state.record_chunk_result
        count = [0]

        def stash(*args, **kwargs):
            real(*args, **kwargs)
            count[0] += 1
            if count[0] == 2:
                raise Killed()

        with mock.patch.object(self.state, "record_chunk_result", side_effect=stash):
            self.killed_stop([FINDING, "REVIEW_CLEAN"], kill_at=99)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertIn(FINDING, self.reason(output))
        self.assertEqual(self.reviewed(), ["a.py", "b.py"])
        self.assertEqual(self.raw_state()["in_flight"], {})

    def test_findings_arrive_even_when_the_current_review_raises(self):
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        output = self.stop_raising(SESSION_A, ValueError("boom"))
        self.assertIn(FINDING, self.reason(output))
        self.assertIn("レビューを完了できませんでした (ValueError)", self.parsed(output)["systemMessage"])
        self.assertEqual(self.pending(SESSION_A), [os.path.join(self.repo, "b.py")])

    def test_a_clean_returned_chunk_is_not_resent(self):
        self.two_chunks()
        self.killed_stop(["REVIEW_CLEAN"], kill_at=2)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotBlocked(output)
        self.assertEqual(len(self.review_calls), 1)
        self.assertNotIn("a.py", self.review_calls[0])
        self.assertEqual(self.reviewed(), ["a.py", "b.py"])

    def test_findings_arrive_even_when_nothing_is_pending(self):
        """2 塊目が除外になる等で今回送るものが無くても、退避した指摘は届く。"""
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        # b.py を HEAD と同じ内容 (= 空 diff) に戻す: 今回のレビューは何も送らない
        os.remove(os.path.join(self.repo, "b.py"))
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertIn(FINDING, self.reason(output))
        self.assertEqual(self.reviewed(), ["a.py"])


class TestTransitions(InterruptedTestCase):
    """退避・回収・配送の各遷移の直前直後に kill / Stop を挟む。"""

    def test_kill_right_after_the_result_is_stashed(self):
        self.two_chunks()
        real = self.state.record_chunk_result

        def stash_then_die(*args, **kwargs):
            real(*args, **kwargs)
            raise Killed()

        with mock.patch.object(self.state, "record_chunk_result", side_effect=stash_then_die):
            calls = self.killed_stop([FINDING, "REVIEW_CLEAN"], kill_at=99)
        self.assertEqual(len(calls), 1, "1 塊目を退避した直後に kill")
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        self.assertEqual(len(self.review_calls), 1)
        self.assertNotIn("a.py", self.review_calls[0])

    def test_kill_right_before_the_result_is_stashed_resends_the_chunk(self):
        """退避の前に kill されたら、その塊は送り直す (指摘は送り直した側が返す)。"""
        self.two_chunks()
        with mock.patch.object(self.state, "record_chunk_result", side_effect=Killed()):
            self.killed_stop([FINDING, "REVIEW_CLEAN"], kill_at=99)
        self.assertEqual(self.reviewed(), [])
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertEqual(len(self.review_calls), 2, "a.py も b.py も送り直す")
        self.assertIn("a.py", self.review_calls[0])

    def test_kill_before_any_chunk_returned_resends_everything_without_waiting(self):
        self.two_chunks()
        self.killed_stop([], kill_at=1)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotBlocked(output)
        self.assertEqual(len(self.review_calls), 2)
        self.assertEqual(self.raw_state()["in_flight"], {})
        self.assertEqual(self.reviewed(), ["a.py", "b.py"])

    def test_kill_during_the_review_that_was_to_deliver_them(self):
        """回収した後、配る前に後続のレビューも kill されたら、その次が 1 回だけ配る。"""
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        self.edit(SESSION_A, "c.py", "small change\n")
        self.killed_stop([], kill_at=1)  # 回収 → claim (b.py + c.py) → 送信中に kill
        self.assertEqual(self.reviewed(), [], "配る前に hash を記録しない")

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        sent = "\n".join(self.review_calls)
        self.assertIn("b.py", sent)
        self.assertIn("c.py", sent)
        self.assertNotIn("a.py", sent)
        self.assertEqual(self.reviewed(), ["a.py", "b.py", "c.py"])

        again = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotIn(FINDING, again)

    def test_kill_after_the_release_and_before_the_claim(self):
        """回収 (claim_pending の中) の直後に死んでも、次の Stop が配る。"""
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        real = self.state.claim_pending

        def claim_then_die(sid):
            real(sid)
            raise Killed()

        with mock.patch.object(self.state, "claim_pending", side_effect=claim_then_die):
            self.killed_stop([], kill_at=99)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        self.assertNotIn("a.py", "\n".join(self.review_calls))

    def test_a_live_owner_is_not_taken_over(self):
        """claim した hook がまだ居るなら (TTL 内)、その claim も退避した結果も触らない。"""
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        # claim の pid を生きている別のプロセスに差し替える (自分自身の pid は判定から外している)
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=_testutil.hermetic_env())
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        data = self.raw_state()
        for entry in data["in_flight"].values():
            entry["pid"] = sleeper.pid
        with open(os.path.join(self.tmpdir, "post-implementation-review", "state",
                               f"{SESSION_A}.json"), "w") as f:
            json.dump(data, f)

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertNotIn(FINDING, output)
        self.assertEqual(len(self.raw_state()["in_flight"]), 1)
        self.assertNotIn("orphan", next(iter(self.raw_state()["in_flight"].values())))

    def test_an_entry_without_a_pid_waits_for_the_ttl(self):
        """0.16.x までの形 (pid なし) は TTL だけで回収する。"""
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        path = os.path.join(self.tmpdir, "post-implementation-review", "state", f"{SESSION_A}.json")
        data = self.raw_state()
        for entry in data["in_flight"].values():
            self.assertIsNotNone(entry.pop("pid", None), "claim に pid が無い")
        with open(path, "w") as f:
            json.dump(data, f)
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

        data = self.raw_state()
        for entry in data["in_flight"].values():
            entry["at"] = time.time() - self.state.IN_FLIGHT_TTL_SEC - 60
        with open(path, "w") as f:
            json.dump(data, f)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        self.assertNotIn("a.py", "\n".join(self.review_calls))

    def test_a_reedited_path_is_resent_and_keeps_its_new_hash(self):
        """kill の後に 1 塊目のファイルを書き換えたら、新しい内容は送り、古い hash で上書きしない。"""
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        self.edit(SESSION_A, "a.py", self.content(30, "A"))
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        self.assertIn("a.py", "\n".join(self.review_calls), "新しい内容は送る")
        # 同じ内容をもう一度書く (pending に戻る): 新しい内容の hash が残っていれば送らない
        self.edit(SESSION_A, "a.py", self.content(30, "A"))
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_kill_between_the_review_and_the_delivery(self):
        """回収 → 再編集した 1 塊目をレビューして新しい hash を記録 → 配る前に kill。

        次の Stop は回収を繰り返さない (回収した時点の hash を取り直すと、新しい hash を
        古い hash で上書きして、新しい内容をもう一度送ってしまう)。
        """
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        self.edit(SESSION_A, "a.py", self.content(30, "A"))
        with mock.patch.object(self.state, "take_orphaned_results", side_effect=Killed()):
            self.killed_stop(["REVIEW_CLEAN", "REVIEW_CLEAN"], kill_at=99)
        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()
        self.assertIn(FINDING, self.reason(output))
        self.edit(SESSION_A, "a.py", self.content(30, "A"))  # 同じ内容を書き直す
        self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertNotReviewed()

    def test_normal_completion_leaves_nothing_behind(self):
        self.two_chunks()
        output = self.stop(SESSION_A, FINDING)
        self.assertIn(FINDING, self.reason(output))
        self.assertNotIn(self.entry.RECOVERED_PREFACE, self.reason(output))
        self.assertEqual(self.raw_state()["in_flight"], {})

    def test_failed_review_leaves_nothing_behind(self):
        self.two_chunks()
        self.stop(SESSION_A, None)
        self.assertEqual(self.raw_state()["in_flight"], {})
        self.assertEqual(len(self.pending(SESSION_A)), 2)

    def test_recovered_findings_count_against_the_findings_cap(self):
        """一緒に返す退避分の指摘も、2 回目以降の送信の上限の判定に入る。"""
        long_a = "- " + "a" * (self.entry.selection.MAX_OUTPUT_CHARS - 2)
        self.two_chunks()
        self.killed_stop([long_a], kill_at=2)
        for rel in ("c.py", "d.py"):
            self.edit(SESSION_A, rel, self.content(30, rel))
        long_b = "- " + "b" * (self.entry.selection.MAX_OUTPUT_CHARS - 2)
        self.stop(SESSION_A, long_b)
        # 退避分 16000 + 1 回目 16000 で上限 32000。2 回目 (+16000) は送らない
        self.assertEqual(len(self.review_calls), 1)


class TestMidTurnDeliversThemToo(InterruptedTestCase):
    def test_mid_turn_review_delivers_recovered_findings(self):
        self.two_chunks()
        self.killed_stop([FINDING], kill_at=2)
        self.edit(SESSION_A, "c.py", "next edit\n")  # 閾値 (1 バイト) を超える pending
        stdin = io.StringIO(json.dumps({"session_id": SESSION_A, "cwd": self.repo}))
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"EXTERNAL_AI_POST_REVIEW_MIDTURN": "1",
                                          "EXTERNAL_AI_POST_REVIEW_MIDTURN_BYTES": "1"}), \
                mock.patch.object(self.cursor, "review", return_value="REVIEW_CLEAN"), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = self.entry.main(["--phase", "mid-turn"])
        self.assertEqual(code, self.entry.REWAKE_EXIT_CODE)
        self.assertIn(FINDING, err.getvalue())
        self.assertEqual(self.reviewed(), ["a.py", "b.py", "c.py"])


class TestSymlinkedPath(InterruptedTestCase):
    """claim したパスが symlink 経由の名前で、返った塊の hash のキー (実体名) と違うとき。"""

    def edit_via(self, base: str, rel: str, content: str) -> None:
        _testutil.write(self.repo, rel, content)
        self.run_hook("post-tool", {
            "session_id": SESSION_A, "cwd": base, "tool_name": "Write",
            "tool_input": {"file_path": os.path.join(base, rel), "content": content},
        })

    def test_returned_chunk_not_resent_when_claimed_by_symlinked_path(self):
        link = os.path.join(self.tmpdir, "linkrepo")
        os.symlink(self.repo, link)
        self.edit_via(link, "a.py", self.content(30, "a"))
        self.edit_via(link, "b.py", self.content(30, "b"))
        link_a = os.path.join(link, "a.py")
        self.assertIn(link_a, self.raw_state()["fingerprints"], "前提: 指紋は生の名前で記録される")
        self.killed_stop([FINDING], kill_at=2)

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        self.assertEqual(len(self.review_calls), 1, "返った塊 (a.py) を送り直さない")
        self.assertIn("b.py", self.review_calls[0])
        self.assertNotIn("a.py", self.review_calls[0])
        self.assertEqual(self.raw_state()["in_flight"], {})
        self.assertNotIn(
            link_a, self.raw_state()["fingerprints"], "symlink 経由で回収した塊の指紋も消す"
        )

    def test_returned_chunk_not_resent_when_claimed_by_case_only_alias(self):
        """実体 `a.py` を `A.py` の名前で claim した (大文字小文字を区別しない FS だけ)。"""
        _testutil.write(self.repo, "a.py", "old\n")
        if not os.path.exists(os.path.join(self.repo, "A.py")):
            self.skipTest("大文字小文字を区別する FS では同じ実体を指す別名にならない")
        _testutil.git(self.repo, "add", "a.py")
        _testutil.git(self.repo, "commit", "-q", "-m", "seed a.py")
        self.edit_via(self.repo, "A.py", self.content(30, "a"))
        self.edit_via(self.repo, "b.py", self.content(30, "b"))
        self.killed_stop([FINDING], kill_at=2)

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        self.assertEqual(len(self.review_calls), 1, "返った塊 (a.py) を送り直さない")
        self.assertIn("b.py", self.review_calls[0])
        self.assertNotIn("a.py", self.review_calls[0])
        self.assertEqual(self.raw_state()["in_flight"], {})

    def test_returned_chunk_not_resent_when_claimed_by_index_spelling(self):
        """git を通さずに `a.py` → `A.py` と rename した後、index の綴り `a.py` で claim した。

        返った塊のキーは index の綴り (`a.py`)、ディスク上の綴りは `A.py`。ディスク上の綴りだけで
        比べると返った塊を送り直す。
        """
        _testutil.write(self.repo, "a.py", "old\n")
        if not os.path.exists(os.path.join(self.repo, "A.py")):
            self.skipTest("大文字小文字を区別する FS では同じ実体を指す別名にならない")
        _testutil.git(self.repo, "add", "a.py")
        _testutil.git(self.repo, "commit", "-q", "-m", "seed a.py")
        os.rename(os.path.join(self.repo, "a.py"), os.path.join(self.repo, "tmp"))
        os.rename(os.path.join(self.repo, "tmp"), os.path.join(self.repo, "A.py"))
        self.edit_via(self.repo, "a.py", self.content(30, "a"))
        self.edit_via(self.repo, "b.py", self.content(30, "b"))
        self.killed_stop([FINDING], kill_at=2)

        output = self.stop(SESSION_A, "REVIEW_CLEAN")
        self.assertIn(FINDING, self.reason(output))
        self.assertEqual(len(self.review_calls), 1, "返った塊 (a.py) を送り直さない")
        self.assertIn("b.py", self.review_calls[0])
        self.assertNotIn("a.py", self.review_calls[0])
        self.assertEqual(self.raw_state()["in_flight"], {})


class TestOwnerGone(unittest.TestCase):
    """`state._owner_gone`: 居ないと言い切れるときだけ True。"""

    def setUp(self) -> None:
        _testutil.load_entry()
        self.state = sys.modules["state"]

    def test_cases(self):
        alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=_testutil.hermetic_env())
        self.addCleanup(alive.wait)
        self.addCleanup(alive.kill)
        cases = [
            ("終了したプロセス", _dead_pid(), True),
            ("生きているプロセス", alive.pid, False),
            ("自分自身", os.getpid(), False),
            ("0 (process group)", 0, False),
            ("負の pid", -1, False),
            ("bool", True, False),
            ("文字列", "123", False),
            ("無い", None, False),
        ]
        for label, pid, expected in cases:
            with self.subTest(label):
                self.assertIs(self.state._owner_gone(pid), expected)

    def test_permission_error_is_treated_as_alive(self):
        with mock.patch.object(self.state.os, "kill", side_effect=PermissionError()):
            self.assertFalse(self.state._owner_gone(999999))


if __name__ == "__main__":
    unittest.main()
