"""bd と話す層 (server/backend.py) の約束と、humanq.py に残した差し替え口のテスト。

    python3 -m unittest discover tests

bd は打たない (bd を 1 回打つ関数を台本の関数に替える)。bd を通す読み書きのシナリオは test_server.py にある。
HUMANQ_SERVER_DIR で別の server/ を試せる。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER_DIR = Path(os.environ.get("HUMANQ_SERVER_DIR") or HERE.parent / "server")
sys.path.insert(0, str(SERVER_DIR))
import backend  # noqa: E402
import humanq  # noqa: E402

Q = {"v": 1, "kind": "decision", "ask": "Q?", "context": "c", "recommended": "a",
     "options": [{"key": "a", "label": "案A", "desc": "d"}, {"key": "b", "label": "案B", "desc": "e"}]}
ANS_A = "[humanq] 回答: 案A (a)\n"
ROW_KEYS = {"id", "title", "priority", "labels", "created_at", "description", "notes", "question_raw"}
ANSWERED_LIST = ("list", "--label", "answered", "--flat", "-n", "0", "--json")      # status では絞らない


def issue(iid: str, **kw) -> dict:
    base = {"id": iid, "title": "t", "priority": 1, "labels": ["human"], "created_at": "2026-11-20T10:00:00Z",
            "description": "d", "notes": "n", "metadata": {"humanq": Q}}
    base.update(kw)
    return base


class Script:
    """bd を 1 回打つ関数の代わり。args の先頭の語で出力を返し、呼び出しを記録する (-f のファイルは <tmp> に置き換える)。"""

    def __init__(self, human="[]", answered="[]", comments: dict | None = None):
        self.human, self.answered, self.comments_out = human, answered, comments or {}
        self.calls: list[tuple] = []

    def __call__(self, repo, *args):
        a = list(args)
        if "-f" in a:
            k = a.index("-f")
            if k + 1 < len(a):
                a[k + 1] = "<tmp>"
        self.calls.append((repo, *a))
        if args[:2] == ("human", "list"):
            return self.human
        if args[:1] == ("list",):
            return self.answered
        if args[:1] == ("comments",) and len(args) > 1 and args[1] != "add":
            return self.comments_out.get(args[1], "[]")
        return ""


def no_subprocess(*a, **kw):
    raise AssertionError(f"台本の外で bd を起動した: {a}")


class HumanqSwapPoint(unittest.TestCase):
    """humanq.bd の差し替えが、読み書きのすべての bd の呼び出しに届くこと (本物の bd のシナリオ WithBd は、これで失敗を作り
    呼び出しを数える。bd の無い CI では WithBd は skip されるので、ここで固定する)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="humanq-backend-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(backend.subprocess, "run", side_effect=no_subprocess)
        p.start()
        self.addCleanup(p.stop)

    def test_every_read_and_write_goes_through_humanq_bd(self):
        rec = Script(human=json.dumps([issue("hq-1")]), answered=json.dumps([issue("hq-2", labels=["answered"])]),
                     comments={"hq-2": json.dumps([{"text": ANS_A}])})
        with mock.patch.object(humanq, "bd", rec):
            try:
                data = humanq.load_project("/r")
                store = humanq.Store(self.tmp)
                store.answer("/r", {"id": "hq-1", "option": "a"})
                store.undo("/r", {"id": "hq-2"})
                listed = humanq.bd_json("/r", "x")
            except AssertionError:
                raise
            except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
                self.fail(f"想定外の例外: {type(e).__name__}: {e} (呼び出し: {rec.calls})")
        self.assertEqual(listed, [])
        self.assertEqual(([i["id"] for i in data["human"]], [i["id"] for i in data["answered"]]), (["hq-1"], ["hq-2"]))
        w = ("/r", "--actor", "user/humanq")
        self.assertEqual(rec.calls, [
            ("/r", "human", "list", "--json"), ("/r", *ANSWERED_LIST), ("/r", "comments", "hq-2", "--json"),
            ("/r", "human", "list", "--json"), ("/r", "comments", "hq-1", "--json"),
            (*w, "comments", "add", "hq-1", "-f", "<tmp>"), (*w, "label", "add", "hq-1", "answered"),
            (*w, "label", "remove", "hq-1", "human"),
            ("/r", *ANSWERED_LIST), ("/r", "comments", "hq-2", "--json"),
            (*w, "comments", "add", "hq-2", "-f", "<tmp>"), (*w, "label", "add", "hq-2", "human"),
            (*w, "label", "remove", "hq-2", "answered"),
            ("/r", "x", "--json"),
        ])

    def test_snooze_and_unsnooze_go_through_humanq_bd(self):
        # 読みでコメントを打つのは保留のラベルの付いた判断待ち (hq-3) だけ。保留は status と担当に触れない (update を打たない)
        rec = Script(human=json.dumps([issue("hq-1"), issue("hq-3", labels=["human", "snoozed"])]),
                     comments={"hq-3": json.dumps([{"text": "[humanq] 保留: 期限なし\n", "created_at": "2026-11-20T10:00:00Z"}])})
        with mock.patch.object(humanq, "bd", rec):
            try:
                data = humanq.load_project("/r")
                store = humanq.Store(self.tmp)
                store.snooze("/r", {"id": "hq-1", "until": None, "note": "x"})
                store.unsnooze("/r", {"id": "hq-3"})
            except AssertionError:
                raise
            except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
                self.fail(f"想定外の例外: {type(e).__name__}: {e} (呼び出し: {rec.calls})")
        self.assertEqual(([i["id"] for i in data["human"]], [i["id"] for i in data["snoozed"]]), (["hq-1"], ["hq-3"]))
        w = ("/r", "--actor", "user/humanq")
        self.assertEqual(rec.calls, [
            ("/r", "human", "list", "--json"), ("/r", *ANSWERED_LIST), ("/r", "comments", "hq-3", "--json"),
            ("/r", "human", "list", "--json"), ("/r", "comments", "hq-1", "--json"),
            (*w, "comments", "add", "hq-1", "-f", "<tmp>"), (*w, "label", "add", "hq-1", "snoozed"),
            ("/r", "human", "list", "--json"), ("/r", "comments", "hq-3", "--json"),
            (*w, "comments", "add", "hq-3", "-f", "<tmp>"), (*w, "label", "remove", "hq-3", "snoozed"),
        ])

    def test_a_snooze_whose_comments_cannot_be_read_fails_as_the_comment_stage(self):
        # bd の pending は thread を持たないので、保留の書き込みは comments で読む。読めなければ何も書かずに comment の段階
        for fn, body in (("snooze", {"id": "hq-1", "until": None}), ("unsnooze", {"id": "hq-1"})):
            with self.subTest(fn):
                rec = Script(human=json.dumps([issue("hq-1", labels=["human", "snoozed"])]), comments={"hq-1": "oops"})
                with mock.patch.object(humanq, "bd", rec):
                    try:
                        getattr(humanq.Store(self.tmp), fn)("/r", body)
                        self.fail("WriteError が上がらなかった")
                    except humanq.WriteError as e:
                        self.assertEqual((e.stage, e.written), ("comment", False))
                    except AssertionError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self.fail(f"想定外の例外: {type(e).__name__}: {e}")
                self.assertEqual(rec.calls, [("/r", "human", "list", "--json"), ("/r", "comments", "hq-1", "--json")],
                                 "読めないコメントのまま書き込んだ")

    def test_errors_keep_their_types(self):
        self.assertIs(humanq.BdError, backend.BdError, "HTTP 502 の応答とテストが humanq.BdError で backend の失敗を捕まえる")
        self.assertTrue(issubclass(humanq.WriteError, backend.BdError))


class BackendRows(unittest.TestCase):
    """一覧の Row: 値は bd が返したまま、問いの生の値は metadata.humanq。"""

    @staticmethod
    def backend_of(issues) -> backend.BdBackend:
        return backend.BdBackend(run=Script(human=json.dumps(issues), answered=json.dumps(issues)))

    def rows(self, issues) -> list[dict]:
        be = self.backend_of(issues)
        try:
            human, answered = be.list_human("/r"), be.list_answered("/r")
        except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
            self.fail(f"Row を作れない: {type(e).__name__}: {e}")
        self.assertEqual(human, answered, "判断待ちと回答済みで Row の作り方が違う")
        return human

    def test_rows_carry_the_question_raw_and_nothing_cli_specific(self):
        cases = {
            "dict": ({"humanq": Q}, Q),
            "string": ({"humanq": json.dumps(Q)}, json.dumps(Q)),
            "no humanq key": ({"other": 1}, None),
            "metadata null": (None, None),
            "metadata not a dict": ("x", None),
            "metadata list": ([{"humanq": Q}], None),
        }
        for what, (meta, raw) in cases.items():
            with self.subTest(what):
                rows = self.rows([{**issue("hq-1"), "metadata": meta, "status": "open", "assignee": "x"}])
                self.assertEqual(len(rows), 1, rows)
                self.assertEqual(rows[0], {"id": "hq-1", "title": "t", "priority": 1, "labels": ["human"],
                                           "created_at": "2026-11-20T10:00:00Z", "description": "d", "notes": "n",
                                           "question_raw": raw})
        rows = self.rows([{"id": "hq-1"}])                       # metadata の key も無い (bd は metadata の無いチケットで key を出さない)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0], dict.fromkeys(ROW_KEYS) | {"id": "hq-1"}, "Row は既定値を補わない (補うのは読む側)")

    def test_a_row_that_is_not_a_dict_is_not_read(self):
        # 呼ぶ側 (Store.project) が、そのプロジェクトだけを読めないことにする
        for odd in ("hq-1", 5, None, [1]):
            be = self.backend_of([odd])
            for name in ("list_human", "list_answered"):
                with self.subTest(odd=odd, name=name), self.assertRaises(AttributeError):
                    getattr(be, name)("/r")

    def shaped(self, i: dict) -> dict:
        try:
            return humanq.shape(backend.BdBackend.row(i))
        except Exception as e:  # noqa: BLE001
            self.fail(f"Row を画面のチケットにできない: {type(e).__name__}: {e}")

    def test_shape_fills_the_defaults_from_a_bare_row(self):
        self.assertEqual(self.shaped({"id": "hq-1"}), {"id": "hq-1", "title": "", "priority": None, "labels": [], "created_at": "",
                                                       "description": "", "notes": "", "q": None, "qsig": humanq.qsig(None)})
        it = self.shaped(issue("hq-2"))
        self.assertIsNotNone(it["q"])
        self.assertEqual((it["q"], it["created_at"], it["labels"]), (humanq.question({"humanq": Q}), "2026-11-20", ["human"]))


class BackendParse(unittest.TestCase):
    """bd の --json の出力の読み。0 件の形 (空・null・案内文 1 行) は []、list でないもの (envelope を含む) と JSON でないものは BdError。"""

    def parse(self, out: str):
        try:
            return backend.parse_bd_list(out)
        except backend.BdError as e:
            return e
        except Exception as e:  # noqa: BLE001
            self.fail(f"BdError でない例外: {type(e).__name__}: {e}")

    def test_empty_forms_are_empty_lists(self):
        for out in ("", " \n", "null\n", "[]\n", "No human-needed beads found.\n", "No issues found."):
            with self.subTest(out=out):
                self.assertEqual(self.parse(out), [])
        self.assertEqual(self.parse('[{"id": "hq-1"}]\n'), [{"id": "hq-1"}])

    def test_other_forms_are_bd_errors(self):
        for out, head in (('{"issues": []}', "bd の出力が list ではない (dict)"), ('"x"', "bd の出力が list ではない (str)"),
                          ("5", "bd の出力が list ではない (int)"), ("Update available: bd 9.9\n[]", "bd の出力が JSON ではない"),
                          ("No\nmore", "bd の出力が JSON ではない"), ("oops", "bd の出力が JSON ではない")):
            with self.subTest(out=out):
                e = self.parse(out)
                self.assertIsInstance(e, backend.BdError)
                self.assertTrue(str(e).startswith(head), e)

    def test_deep_nesting_is_a_bd_error(self):
        # json の parser の限界 (RecursionError) を、サーバーの外へ漏らさずに読めない出力にする
        for what, out in {"閉じない": "[" * 100000, "閉じる": "[" * 50000 + "]" * 50000, "object": '{"a":' * 100000}.items():
            with self.subTest(what):
                e = self.parse(out)
                self.assertIsInstance(e, backend.BdError)
                self.assertTrue(str(e).startswith("bd の出力が JSON ではない (入れ子が深すぎる)"), str(e)[:120])


class BackendLabeled(unittest.TestCase):
    """backend.labeled: ラベルは list の要素としてだけ読む (文字列の部分一致にしない)。Row でないものは付いていない。"""

    def test_only_a_list_of_labels_is_read(self):
        cases = (({"labels": ["human", "snoozed"]}, True), ({"labels": ["human"]}, False), ({"labels": []}, False),
                 ({"labels": "snoozed"}, False), ({"labels": "human,snoozed"}, False), ({"labels": None}, False),
                 ({"labels": {"snoozed": 1}}, False), ({"labels": [["snoozed"]]}, False), ({}, False),
                 ("snoozed", False), (["snoozed"], False), (None, False))
        for row, want in cases:
            with self.subTest(row=row):
                try:
                    got = backend.labeled(row, "snoozed")
                except Exception as e:  # noqa: BLE001
                    self.fail(f"例外が漏れた: {type(e).__name__}: {e}")
                self.assertIs(got, want)


class BackendSnoozedThread(unittest.TestCase):
    """BdBackend.load: 保留のラベルの付いた判断待ちにだけ thread を付ける (コメントの読みを増やすのはそのチケットだけ)。"""

    def test_only_snoozed_rows_get_a_thread(self):
        sn = [{"text": "[humanq] 保留: 期限なし\n", "created_at": "2026-11-20T10:00:00Z"}]
        s = Script(human=json.dumps([issue("hq-1"), issue("hq-2", labels=["human", "snoozed"]),
                                     issue("hq-3", labels="human snoozed"), issue("-x", labels=["human", "snoozed"]),
                                     issue(5, labels=["human", "snoozed"]), issue("hq-4", labels=["snoozed", "human"])]),
                   comments={"hq-2": json.dumps(sn), "hq-4": "oops"})
        try:
            human, answered = backend.BdBackend(run=s).load("/r")
        except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
            self.fail(f"想定外の例外: {type(e).__name__}: {e} (呼び出し: {s.calls})")
        self.assertEqual(answered, [])
        by = {r["id"]: r for r in human}
        self.assertEqual(len(by), 6, human)
        self.assertEqual(by["hq-2"].get("thread"), sn)
        for iid in ("hq-1", "hq-3"):
            with self.subTest(iid=iid):
                self.assertNotIn("thread", by[iid], "保留のラベルの無い判断待ちに thread を付けた")
        for iid in ("-x", 5, "hq-4"):
            with self.subTest(iid=iid):
                self.assertIn("thread", by[iid])
                self.assertIsNone(by[iid]["thread"], "渡せない id・読めないコメントは None (期日を読めない)")
        self.assertEqual([c[1:] for c in s.calls if c[1] == "comments"],
                         [("comments", "hq-2", "--json"), ("comments", "hq-4", "--json")], "渡せない id で bd を打った")


class LoadProjectSnoozeGate(unittest.TestCase):
    """humanq.load_project は、保留のラベルの付いた判断待ちの thread だけを保留として読む (backend が thread を付けすぎても、
    ラベルの無いチケットを保留中に隠さない)。"""

    def test_a_thread_without_the_label_is_not_a_snooze(self):
        sn = [{"text": "[humanq] 保留: 期限なし\n", "created_at": "2026-11-20T10:00:00Z"}]

        class Loose:
            def load(self, repo):
                return ([{**backend.BdBackend.row(issue("hq-1")), "thread": sn},
                         {**backend.BdBackend.row(issue("hq-2", labels=["human", "snoozed"])), "thread": sn}], [])

        try:
            data = humanq.load_project("/r", Loose())
        except Exception as e:  # noqa: BLE001
            self.fail(f"想定外の例外: {type(e).__name__}: {e}")
        self.assertEqual(([i["id"] for i in data["human"]], [i["id"] for i in data["snoozed"]]), (["hq-1"], ["hq-2"]))


class UnreadableProjects(unittest.TestCase):
    """読めないプロジェクトの data も、読めたときと同じ一覧の key (human・answered・snoozed) を持つ (画面と /api/summary が引く)。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-unreadable-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.repo, ".beads"))
        p = mock.patch.object(backend.subprocess, "run", side_effect=no_subprocess)
        p.start()
        self.addCleanup(p.stop)

    def project(self, run) -> dict:
        with mock.patch.object(humanq, "bd", run), mock.patch.object(humanq, "br", run):
            try:
                return humanq.Store(self.tmp).project(self.repo, fresh=True)
            except Exception as e:  # noqa: BLE001
                self.fail(f"想定外の例外: {type(e).__name__}: {e}")

    def test_every_unreadable_path_has_the_three_lists(self):
        def bd_down(repo, *args):
            raise backend.BdError("down")

        def odd(repo, *args):
            raise KeyError("odd")

        want = {"human": [], "answered": [], "snoozed": []}
        for what, run in {"BdError": bd_down, "想定外の例外": odd}.items():
            with self.subTest(what):
                data = self.project(run)
                self.assertEqual(data.get("ok"), False, data)
                self.assertEqual({k: data.get(k) for k in want}, want)
        Path(self.repo, ".beads", ".gitignore").write_text(".br_history/\n", encoding="utf-8")       # br の印と
        os.makedirs(os.path.join(self.repo, ".beads", "embeddeddolt"))                               # bd の印 (conflict)
        data = self.project(no_subprocess)
        self.assertEqual(data.get("writable"), False, data)
        self.assertEqual({k: data.get(k) for k in want}, want)


class SnoozeOf(unittest.TestCase):
    """保留の読み (humanq.snooze_of): 最後の [humanq] コメントの 1 行目が保留の形のときだけ {until, note}。
    形の崩れた・日付として読めない・壊れた入力は保留と読まない (None。判断待ちに出す側)。"""

    SN = {"text": "[humanq] 保留: 期限なし\n", "created_at": "2026-11-20T10:00:00Z"}

    def of(self, thread):
        try:
            return humanq.snooze_of(thread)
        except Exception as e:  # noqa: BLE001  例外が漏れる変異を、走っていない ERROR にしない
            self.fail(f"例外が漏れた: {type(e).__name__}: {e} (thread: {thread!r:.200})")

    def test_the_forms_that_are_snoozes(self):
        cases = {
            "期日": ("[humanq] 保留: 2026-12-01 まで\n", {"until": "2026-12-01", "note": ""}),
            "期日と補足 (複数行)": ("[humanq] 保留: 2026-12-01 まで\n\n仕様の確認待ち\n2 行目\n",
                                {"until": "2026-12-01", "note": "仕様の確認待ち\n2 行目"}),
            "期限なし": ("[humanq] 保留: 期限なし\n", {"until": None, "note": ""}),
            "期限なしと補足": ("[humanq] 保留: 期限なし\n\nあとで見る", {"until": None, "note": "あとで見る"}),
            "1 行目の末尾の空白と CR": ("[humanq] 保留: 2026-12-01 まで \r\n", {"until": "2026-12-01", "note": ""}),
            "閏日": ("[humanq] 保留: 2028-02-29 まで", {"until": "2028-02-29", "note": ""}),
        }
        for what, (text, want) in cases.items():
            with self.subTest(what):
                self.assertEqual(self.of([{"text": "エージェントの問い"}, {"text": text, "created_at": "x"}]), want)

    def test_broken_or_other_texts_are_not_snoozes(self):
        texts = {
            "回答": ANS_A, "取り消し": humanq.UNDO_HEAD + "\n", "保留の解除": humanq.UNSNOOZE_HEAD + "\n",
            "月が 13": "[humanq] 保留: 2026-13-01 まで\n", "2 月 30 日": "[humanq] 保留: 2026-02-30 まで\n",
            "閏年でない 2 月 29 日": "[humanq] 保留: 2027-02-29 まで\n", "年が 0": "[humanq] 保留: 0000-01-01 まで\n",
            "日付の後に文字": "[humanq] 保留: 2026-12-01 まで です\n", "まで が無い": "[humanq] 保留: 2026-12-01\n",
            "短い日付": "[humanq] 保留: 2026-1-1 まで\n", "時刻つき": "[humanq] 保留: 2026-12-01T10:00 まで\n",
            "全角数字": "[humanq] 保留: ２０２６-１２-０１ まで\n", "アラビア・インド数字": "[humanq] 保留: ٢٠٢٦-١٢-٠١ まで\n",
            "NUL (期日)": "[humanq] 保留: 2026-12-01 まで\x00\n", "NUL (期限なし)": "[humanq] 保留: 期限なし\x00",
            "期限なしの後に文字": "[humanq] 保留: 期限なしで\n", "空の期日": "[humanq] 保留:  まで\n",
            "コロンの後の空白が無い": "[humanq] 保留:期限なし\n", "1 行目が空": "[humanq] \n保留: 期限なし",
        }
        for what, text in texts.items():
            with self.subTest(what):
                self.assertIsNone(self.of([self.SN, {"text": text, "created_at": "x"}]), "最後の [humanq] コメントで決めていない")

    def test_threads_that_cannot_be_read_and_what_counts_as_the_last(self):
        for thread in (None, "x", 5, self.SN, [], [{"text": "エージェントの問い"}], [{"text": 5}], [None], [[self.SN["text"]]],
                       [{"text": " " + self.SN["text"]}]):
            with self.subTest(thread=thread):
                self.assertIsNone(self.of(thread))
        # 最後の [humanq] コメントで決める: 後ろのエージェントのコメント・本文が文字列でない要素・dict でない要素は数えない
        self.assertEqual(self.of([self.SN, {"text": "エージェントの追記"}, {"text": None}, {"text": ["[humanq] 回答: x"]}, "odd"]),
                         {"until": None, "note": ""})
        self.assertIsNone(self.of([self.SN, {"text": ANS_A}]))
        self.assertIsNone(self.of([self.SN, {"text": humanq.UNSNOOZE_HEAD + "\n"}]))


class BackendComments(unittest.TestCase):
    def comments(self, out) -> list[dict]:
        try:
            return backend.BdBackend(run=Script(comments={"hq-1": json.dumps(out)})).comments("/r", "hq-1")
        except Exception as e:  # noqa: BLE001
            self.fail(f"コメントを読めない: {type(e).__name__}: {e}")

    def test_comments_come_oldest_first_as_bd_returns_them(self):
        # bd は古い順で返す (偽の bd・bd 1.3.4 の実測)。層はそのまま渡す。同じ時刻のコメントの並びも崩さない
        t = "2026-11-20T10:00:00Z"
        out = [{"id": "c1", "issue_id": "hq-1", "author": "agent", "text": "問い", "created_at": t},
               {"id": "c2", "issue_id": "hq-1", "author": "user/humanq", "text": ANS_A, "created_at": t},
               {"id": "c3", "issue_id": "hq-1", "author": "agent", "text": "後のメモ", "created_at": "2026-11-20T10:00:01Z"}]
        cs = self.comments(out)
        self.assertEqual(cs, [{"text": "問い", "created_at": t}, {"text": ANS_A, "created_at": t},
                              {"text": "後のメモ", "created_at": "2026-11-20T10:00:01Z"}])

    def test_comments_of_an_unexpected_shape_become_comments_without_text(self):
        cs = self.comments([{"text": ANS_A}, "x", 5, None, {}, {"text": 7, "created_at": 1}])
        self.assertEqual(cs, [{"text": ANS_A, "created_at": None}, *[{"text": None, "created_at": None}] * 4,
                              {"text": 7, "created_at": 1}])
        self.assertEqual(humanq.last_of(cs), (ANS_A, humanq.asig(1, ANS_A)))


class BackendRun(unittest.TestCase):
    """bd を 1 回打つ関数 (backend.run_bd) の失敗は、どれも BdError (HTTP 502 の応答が依る)。"""

    def run_with(self, **kw) -> Exception | None:
        with mock.patch.object(backend.subprocess, "run", **kw) as run:
            try:
                out = backend.run_bd("/r", "human", "list")
            except backend.BdError as e:
                return e
            except Exception as e:  # noqa: BLE001
                self.fail(f"BdError でない例外: {type(e).__name__}: {e}")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["bd", "-C", "/r", "human", "list"])
        self.assertNotIn("cwd", run.call_args.kwargs, "cwd は渡さず -C で指す")
        return out

    def test_failures_to_start_are_bd_errors(self):
        for exc in (FileNotFoundError(2, "No such file", "bd"), PermissionError(13, "denied"),
                    subprocess.TimeoutExpired(["bd"], 60)):
            with self.subTest(exc=type(exc).__name__):
                e = self.run_with(side_effect=exc)
                self.assertIsInstance(e, backend.BdError)
                self.assertTrue(str(e).startswith("bd を起動できない: "), e)

    def test_a_non_zero_exit_is_a_bd_error_with_the_message(self):
        done = subprocess.CompletedProcess
        cases = [(done([], 1, "", "Error: no beads project found\n"), "Error: no beads project found"),
                 (done([], 1, '{"error": "x"}\n', ""), '{"error": "x"}'),
                 (done([], 3, "", "  \n"), "bd exit 3"),
                 (done([], 1, "", "e" * 900), "e" * 800)]
        for p, msg in cases:
            with self.subTest(msg=msg[:30]):
                e = self.run_with(return_value=p)
                self.assertIsInstance(e, backend.BdError)
                self.assertEqual(str(e), msg)
        self.assertEqual(self.run_with(return_value=done([], 0, "[]\n", "warn")), "[]\n")


class Question(unittest.TestCase):
    def test_question_still_takes_the_metadata_dict(self):
        for meta in ({"humanq": Q}, {"humanq": json.dumps(Q)}, {"humanq": "{not json"}, {"humanq": {"options": []}}, {}, None, "x"):
            with self.subTest(meta=meta):
                raw = meta.get("humanq") if isinstance(meta, dict) else None
                try:
                    got = humanq.question(meta)
                except Exception as e:  # noqa: BLE001
                    self.fail(f"metadata {meta!r} で例外: {type(e).__name__}: {e}")
                self.assertEqual(got, humanq.question_from_raw(raw))
        self.assertEqual(humanq.question_from_raw(json.dumps(Q)), humanq.question_from_raw(Q))
        q = humanq.question_from_raw(Q)
        self.assertIsInstance(q, dict)
        self.assertEqual((q["ask"], [o["key"] for o in q["options"]]), ("Q?", ["a", "b"]))
        for raw in (None, "{not json", "5", [Q], {"ask": 5}):
            with self.subTest(raw=raw):
                try:
                    got = humanq.question_from_raw(raw)
                except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
                    self.fail(f"{raw!r} で例外: {type(e).__name__}: {e}")
                self.assertIsNone(got)
    def test_options_that_are_not_a_list_of_key_label_objects_make_the_question_unstructured(self):
        """list でない options (数・真偽値・文字列・object)・object でない要素・key か label が文字列でない要素は、
        例外にせず問いが未整理 (None)。options が無い・null・[] は選択肢なしの問い。"""
        ok = {"key": "a", "label": "案A"}
        broken = {"数": 5, "真偽値": True, "偽": False, "空文字": "", "文字列": "案A", "object": ok, "要素が数": [1],
                  "要素が文字列": ["a"], "要素が null": [None], "key の欠け": [{"label": "案A"}], "label の欠け": [{"key": "a"}],
                  "key が数": [{"key": 1, "label": "案A"}], "label が数": [{"key": "a", "label": 2}],
                  "正しい要素の後に壊れた要素": [ok, 7]}
        for name, options in broken.items():
            with self.subTest(options=name):
                try:
                    got = humanq.question_from_raw({"ask": "Q?", "options": options})
                    got_json = humanq.question_from_raw(json.dumps({"ask": "Q?", "options": options}))
                except Exception as e:  # noqa: BLE001  漏れた例外を、走っていない ERROR にしない
                    self.fail(f"options={options!r} で例外: {type(e).__name__}: {e}")
                self.assertIsNone(got)
                self.assertIsNone(got_json)
        for name, q in {"options が無い": {"ask": "Q?"}, "null": {"ask": "Q?", "options": None}, "[]": {"ask": "Q?", "options": []}}.items():
            with self.subTest(options=name):
                got = humanq.question_from_raw(q)
                self.assertIsInstance(got, dict)
                self.assertEqual(got["options"], [])
        got = humanq.question_from_raw({"ask": "Q?", "options": [{"key": "a", "label": "案A", "desc": 3}, {"key": "b", "label": "案B"}]})
        self.assertEqual(got["options"], [{"key": "a", "label": "案A", "desc": "3"}, {"key": "b", "label": "案B", "desc": ""}])

    def test_deep_nesting_in_metadata_is_unstructured(self):
        # metadata の humanq が深い入れ子の JSON 文字列: json の parser は RecursionError を上げる。問いが未整理にする
        # (漏らすと Store.project の catch-all で、そのプロジェクトの全チケットが読めなくなる)
        try:
            got = humanq.question({"humanq": "[" * 100000})
        except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
            self.fail(f"深い入れ子の metadata で例外: {type(e).__name__}")
        self.assertIsNone(got)


def str_constants(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return imports, consts


class Layering(unittest.TestCase):
    """CLI の形 (起動・argv の語・問いの置き場) を知っているのは backend.py だけ。humanq.py は CLI に依らない形を読む。"""

    CLI_WORDS = {"bd", "-C", "--json", "--actor", "--label", "--flat", "-n", "-f", "metadata", "comments", "list",
                 "br", "-l", "--limit", "show", "agent_context"}
    # backend.py は今は使わない (回答済みの読みは status で絞らない) が、humanq.py に持ち込まれないようにする語
    UNUSED_CLI_WORDS = {"--status"}

    def test_humanq_py_does_not_know_the_cli(self):
        imports, consts = str_constants(SERVER_DIR / "humanq.py")
        self.assertNotIn("subprocess", imports)
        self.assertEqual(consts & (self.CLI_WORDS | self.UNUSED_CLI_WORDS), set())
        b_imports, b_consts = str_constants(SERVER_DIR / "backend.py")     # 照合の語が backend 側に実際にある (空の検査にしない)
        self.assertIn("subprocess", b_imports)
        self.assertEqual(self.CLI_WORDS - b_consts, set())


# ---- br (BrBackend) ------------------------------------------------------------------------------------------

# status では絞らない (-s を付けない): br の既定 (closed と tombstone だけを除く) のまま読む
BR_HUMAN = ("list", "-l", "human", "--limit", "0", "--json")
BR_ANSWERED = ("list", "-l", "answered", "--limit", "0", "--json")


def envelope(issues, **kw) -> str:
    return json.dumps({"issues": issues, "total": len(issues), "limit": 0, "offset": 0, "has_more": False, **kw})


def br_issue(iid: str, **kw) -> dict:
    """br 0.7.6 の list の要素の形 (agent_context は list にも出るが、読む側は show の値だけを使う)。"""
    base = {"id": iid, "title": f"t {iid}", "status": "open", "priority": 1, "issue_type": "task",
            "created_at": "2026-11-26T17:39:33.136991Z", "updated_at": "2026-11-26T17:39:33.136991Z", "labels": ["human"],
            "dependency_count": 0, "dependent_count": 0}
    base.update(kw)
    return base


def br_shown(iid: str, ctx=None, comments=None, **kw) -> dict:
    """br 0.7.6 の show の要素の形。ctx は agent_context (None なら key を出さない)、comments も None なら key を出さない。"""
    d = {k: v for k, v in br_issue(iid, **kw).items() if k not in ("dependency_count", "dependent_count")}
    if ctx is not None:
        d["agent_context"] = ctx
    if comments is not None:
        d["comments"] = comments
    return d


def bc(cid, text, at="2026-11-26T17:39:33Z", author="agent") -> dict:
    return {"id": cid, "issue_id": "x", "author": author, "text": text, "created_at": at}


class BrScript:
    """br を 1 回打つ関数の代わり。human / answered の一覧と show の応答を返し、呼び出しを記録する。
    show は shows の先頭から順に使う (要素が例外なら投げる)。尽きたら最後の要素を使い続ける。
    書き込み (--actor で始まる呼び出し) は空の出力で成功する (-f のファイルは <tmp> に置き換えて記録する)。"""

    def __init__(self, human=(), answered=(), shows=None):
        self.human, self.answered = envelope(list(human)), envelope(list(answered))
        self.shows = list(shows or [])
        self.calls: list[tuple] = []

    def __call__(self, repo, *args):
        if args[:1] == ("--actor",):
            a = list(args)
            if "-f" in a and a.index("-f") + 1 < len(a):
                a[a.index("-f") + 1] = "<tmp>"
            self.calls.append((repo, *a))
            return ""
        self.calls.append((repo, *args))
        if args == BR_HUMAN:
            return self.human
        if args == BR_ANSWERED:
            return self.answered
        if args[:1] == ("show",):
            out = self.shows.pop(0) if len(self.shows) > 1 else self.shows[0]
            if isinstance(out, BaseException):
                raise out
            return out if isinstance(out, str) else json.dumps(out)
        raise AssertionError(f"台本に無い br の呼び出し: {args}")


Q_OTHER = json.dumps({"other": {"x": 1}, "humanq": Q, "zzz": [1, 2]})     # agent_context に他の key がある


class BrRun(unittest.TestCase):
    """br を 1 回打つ関数 (backend.run_br): cwd がプロジェクト・DB の場所の env を渡さない・失敗はどれも BdError。"""

    def run_with(self, env: dict | None = None, **kw):
        with mock.patch.dict(os.environ, env or {}), mock.patch.object(backend.subprocess, "run", **kw) as run:
            try:
                out = backend.run_br("/r", "list", "--json")
            except backend.BdError as e:
                out = e
            except Exception as e:  # noqa: BLE001
                self.fail(f"BdError でない例外: {type(e).__name__}: {e}")
        self.assertEqual(run.call_count, 1)
        return out, run.call_args

    def test_runs_br_in_the_project_dir_without_the_db_env(self):
        target = {name: f"/elsewhere/{name}" for name in backend.BR_TARGET_ENV}
        self.assertEqual(set(target), {"BEADS_DB", "BD_DB", "BD_DATABASE", "BEADS_DIR", "BEADS_JSONL"})
        out, call = self.run_with(env={**target, "HUMANQ_KEEP_ME": "1"},
                                  return_value=subprocess.CompletedProcess([], 0, '{"issues": []}', ""))
        self.assertEqual(out, '{"issues": []}')
        self.assertEqual(call.args[0], ["br", "list", "--json"], "br に -C は無い")
        self.assertEqual(call.kwargs.get("cwd"), "/r", "br はプロジェクトの dir を cwd にして打つ")
        self.assertIn("env", call.kwargs)
        env = call.kwargs["env"]
        for name in backend.BR_TARGET_ENV:
            with self.subTest(name=name):
                self.assertNotIn(name, env, "DB の場所の env を渡すと、登録したプロジェクトではない DB を読む")
        self.assertEqual(env.get("HUMANQ_KEEP_ME"), "1", "ほかの env は渡す")

    def test_failures_to_start_are_bd_errors_that_point_at_path(self):
        for exc in (FileNotFoundError(2, "No such file", "br"), PermissionError(13, "denied")):
            with self.subTest(exc=type(exc).__name__):
                e, _ = self.run_with(side_effect=exc)
                self.assertIsInstance(e, backend.BdError)
                self.assertTrue(str(e).startswith("br を起動できない (PATH を確認): "), e)
        e, _ = self.run_with(side_effect=subprocess.TimeoutExpired(["br"], 60))
        self.assertIsInstance(e, backend.BdError)
        self.assertIn("br が 60 秒で終わらない", str(e))

    def test_a_non_zero_exit_keeps_the_code_and_the_message(self):
        done = subprocess.CompletedProcess
        for p, code, msg in ((done([], 3, '{"error": {"code": "ISSUE_NOT_FOUND"}}\n', ""), 3, '{"error": {"code": "ISSUE_NOT_FOUND"}}'),
                             (done([], 2, "", "error: unexpected argument\n"), 2, "error: unexpected argument"),
                             (done([], 1, "", " \n"), 1, "br exit 1")):
            with self.subTest(code=code):
                e, _ = self.run_with(return_value=p)
                self.assertIsInstance(e, backend.CliExit)
                self.assertEqual((e.code, str(e)), (code, msg))


class BrWarnings(unittest.TestCase):
    """br が exit 0 で stderr に出す警告 (JSONL の自動 export の失敗) は、結果を変えずに、サーバーの stderr に 1 行ずつ残す。"""

    EXPORT = "Warning: Mutation succeeded, but automatic JSONL export failed: disk full"

    def run_with(self, p):
        err = io.StringIO()
        with mock.patch.object(backend.subprocess, "run", return_value=p), contextlib.redirect_stderr(err):
            try:
                out = backend.run_br("/r", "label", "add", "hq-1", "answered")
            except Exception as e:  # noqa: BLE001  例外が漏れる変異を、走っていない ERROR にしない
                self.fail(f"例外が漏れた: {type(e).__name__}: {e}")
        return out, err.getvalue()

    def test_a_warning_on_exit_zero_is_logged_once_and_the_result_is_unchanged(self):
        out, logged = self.run_with(subprocess.CompletedProcess([], 0, '[{"status": "added"}]', self.EXPORT + "\n"))
        self.assertEqual(out, '[{"status": "added"}]')
        self.assertEqual(logged.splitlines(), ["humanq: br: " + self.EXPORT])

    def test_only_the_warning_lines_are_logged(self):
        _, logged = self.run_with(subprocess.CompletedProcess([], 0, "", f"progress: 1\n{self.EXPORT}\n\nnote: x\n"))
        self.assertEqual(logged.splitlines(), ["humanq: br: " + self.EXPORT])

    def test_no_warning_logs_nothing(self):
        for err in ("", None, "note: something\n"):
            with self.subTest(stderr=err):
                out, logged = self.run_with(subprocess.CompletedProcess([], 0, "[]", err))
                self.assertEqual((out, logged), ("[]", ""))

    def test_the_log_is_bounded(self):
        _, logged = self.run_with(subprocess.CompletedProcess([], 0, "", "".join(f"Warning: w{n} " + "x" * 1000 + "\n" for n in range(9))))
        lines = logged.splitlines()
        self.assertEqual(len(lines), 5, "警告の行数に上限が無い")
        self.assertTrue(all(len(ln) <= len("humanq: br: ") + 300 for ln in lines), "1 行の長さに上限が無い")

    def test_a_failing_exit_is_still_an_error_with_the_message(self):
        with mock.patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", self.EXPORT + "\n")):
            with self.assertRaises(backend.CliExit) as cm:
                backend.run_br("/r", "list")
        self.assertEqual(str(cm.exception), self.EXPORT)


class BrLoad(unittest.TestCase):
    """BrBackend.load: 3 プロセスの読み (一覧 2 つ + show 1 つ)、show の exit 3 の再試行、出力の形の検査。"""

    def load(self, script: BrScript):
        try:
            return backend.BrBackend(run=script).load("/r")
        except backend.BdError as e:
            return e
        except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
            self.fail(f"BdError でない例外: {type(e).__name__}: {e} (呼び出し: {script.calls})")

    def ok(self, script: BrScript):
        got = self.load(script)
        self.assertNotIsInstance(got, Exception, got)
        self.assertEqual(len(got), 2, got)
        return got

    def test_three_processes_with_the_argv_of_br(self):
        s = BrScript(human=[br_issue("p-1"), br_issue("p-3", labels=["answered", "human"])],
                     answered=[br_issue("p-2", labels=["answered"]), br_issue("p-3", labels=["answered", "human"])],
                     shows=[[br_shown("p-1", Q_OTHER), br_shown("p-2", comments=[]), br_shown("p-3")]])
        human, answered = self.ok(s)
        self.assertEqual(s.calls, [("/r", *BR_HUMAN), ("/r", *BR_ANSWERED), ("/r", "show", "p-1", "p-3", "p-2", "--json")],
                         "id はまとめて 1 回の show に、重ねずに渡す")
        self.assertEqual([r["id"] for r in human], ["p-1", "p-3"])
        self.assertEqual([r["id"] for r in answered], ["p-2", "p-3"])
        for r in human:
            self.assertEqual(set(r), ROW_KEYS, "判断待ちの Row に thread は付けない")
        for r in answered:
            self.assertEqual(set(r), ROW_KEYS | {"thread"})
        self.assertEqual(human[0]["question_raw"], Q, "agent_context の top-level の humanq を問いにする (他の key は無視)")

    def test_snoozed_pending_rows_take_the_thread_from_the_same_show(self):
        sz = ["human", "snoozed"]
        cs = [bc(2, "[humanq] 保留: 期限なし\n", "2026-11-26T17:40:00Z", "user/humanq"), bc(1, "a")]      # 新しい順で返る
        s = BrScript(human=[br_issue("p-1"), br_issue("p-2", labels=sz), br_issue("p-3", labels=sz), br_issue("p-4", labels=sz)],
                     shows=[[br_shown("p-1", comments=[bc(1, "x")]), br_shown("p-2", comments=cs, labels=sz),
                             br_shown("p-3", labels=sz), br_shown("p-4", comments={"text": "x"}, labels=sz)]])
        human, answered = self.ok(s)
        self.assertEqual(s.calls, [("/r", *BR_HUMAN), ("/r", *BR_ANSWERED), ("/r", "show", "p-1", "p-2", "p-3", "p-4", "--json")],
                         "保留があっても show は 1 回")
        self.assertEqual(answered, [])
        by = {r["id"]: r for r in human}
        self.assertEqual(len(by), 4, human)
        self.assertEqual(set(by["p-1"]), ROW_KEYS, "保留のラベルの無い判断待ちに thread を付けた")
        self.assertEqual([c["text"] for c in by["p-2"].get("thread") or []], ["a", "[humanq] 保留: 期限なし\n"], "thread は古い順")
        self.assertEqual((by["p-3"].get("thread", "key が無い"), by["p-4"].get("thread", "key が無い")), ([], None),
                         "comments の key が無い show は 0 件、list でなければ読めない (None)")

    def test_empty_lists_do_not_run_show(self):
        s = BrScript(shows=[AssertionError("show を打った")])
        self.assertEqual(self.ok(s), ([], []))
        self.assertEqual(s.calls, [("/r", *BR_HUMAN), ("/r", *BR_ANSWERED)])

    def test_list_output_that_is_not_a_whole_envelope_is_a_bd_error(self):
        cases = {
            "bd の形 (素の配列)": "[]",
            "null": "null",
            "issues が null": json.dumps({"issues": None, "has_more": False}),
            "issues が無い": json.dumps({"total": 0}),
            "途中で切れた一覧": envelope([br_issue("p-1")], has_more=True),
            "JSON でない": "Update available\n{}",
            "空": "",
            "深い入れ子": "[" * 100000,
        }
        for what, out in cases.items():
            with self.subTest(what):
                s = BrScript(shows=[[]])
                s.human = out
                e = self.load(s)
                self.assertIsInstance(e, backend.BdError)
                self.assertTrue(str(e).startswith("br list の"), e)
                self.assertEqual(len(s.calls), 1, "読めない一覧の後に打ち続けた")
        s = BrScript(shows=[[]])
        s.human = envelope([br_issue("p-1")], has_more=True)
        self.assertIn("has_more", str(self.load(s)))

    def test_ids_that_cannot_be_passed_to_show_are_bd_errors(self):
        for what, it in {"id が無い": {"title": "x"}, "id が数": {"id": 5}, "id が空": {"id": ""},
                         "flag に読まれる id": {"id": "--db=/other.db"}, "dict でない": "p-1"}.items():
            with self.subTest(what):
                s = BrScript(human=[it], shows=[AssertionError("show を打った")])
                e = self.load(s)
                self.assertIsInstance(e, backend.BdError)
                self.assertTrue(str(e).startswith("br list の要素の id を読めない"), e)
                self.assertEqual(len(s.calls), 2)

    def test_show_exit_3_is_retried_once_from_the_lists(self):
        not_found = backend.CliExit('{"error": {"code": "ISSUE_NOT_FOUND"}}', 3)
        s = BrScript(human=[br_issue("p-1")], shows=[not_found, [br_shown("p-1", Q_OTHER)]])
        human, answered = self.ok(s)
        self.assertEqual(([r["id"] for r in human], answered), (["p-1"], []))
        show = ("/r", "show", "p-1", "--json")
        self.assertEqual(s.calls, [("/r", *BR_HUMAN), ("/r", *BR_ANSWERED), show] * 2, "一覧から読み直して show を打ち直す")

    def test_show_exit_3_twice_is_a_bd_error(self):
        not_found = backend.CliExit("Issue not found: p-1", 3)
        s = BrScript(human=[br_issue("p-1")], shows=[not_found])
        e = self.load(s)
        self.assertIsInstance(e, backend.BdError)
        self.assertIn("br show が一覧を読み直しても失敗した: Issue not found: p-1", str(e))
        self.assertEqual(len(s.calls), 6, "再試行は 1 回だけ")

    def test_other_show_failures_are_not_retried(self):
        for code in (1, 2, 7):
            with self.subTest(code=code):
                s = BrScript(human=[br_issue("p-1")], shows=[backend.CliExit("boom", code)])
                e = self.load(s)
                self.assertIsInstance(e, backend.CliExit)
                self.assertEqual(e.code, code)
                self.assertEqual(len(s.calls), 3)

    def test_show_output_of_an_unexpected_shape_is_a_bd_error(self):
        cases = {"list でない": ({"issues": []}, "br show の出力が list ではない"),
                 "JSON でない": ("oops", "br show の出力が JSON ではない"),
                 "要素が dict でない": (["p-1"], "br show の要素を読めない"),
                 "要素の id が無い": ([{"title": "x"}], "br show の要素を読めない"),
                 "id が欠けている": ([br_shown("p-9")], "br show の出力に p-1 が無い")}
        for what, (out, head) in cases.items():
            with self.subTest(what):
                e = self.load(BrScript(human=[br_issue("p-1")], shows=[out]))
                self.assertIsInstance(e, backend.BdError)
                self.assertTrue(str(e).startswith(head), e)

    def test_rows_take_the_question_only_from_show_and_prefer_show_values(self):
        listed = br_issue("p-1", title="list の題", agent_context=json.dumps({"humanq": {"ask": "list の問い"}}))
        cases = {"show に agent_context が無い": (br_shown("p-1", title="show の題"), None),
                 "show の agent_context": (br_shown("p-1", Q_OTHER, title="show の題"), Q)}
        for what, (shown, raw) in cases.items():
            with self.subTest(what):
                human, _ = self.ok(BrScript(human=[listed], shows=[[shown]]))
                self.assertEqual(len(human), 1)
                self.assertEqual((human[0]["title"], human[0]["question_raw"]), ("show の題", raw))
        human, _ = self.ok(BrScript(human=[br_issue("p-1", description="list の本文", notes="list のメモ")], shows=[[br_shown("p-1")]]))
        self.assertEqual(len(human), 1)
        self.assertEqual((human[0]["description"], human[0]["notes"]), ("list の本文", "list のメモ"), "show に無い key は一覧の値")

    def test_inherited_context_is_not_a_question(self):
        # br 0.7.6 (.beads/config.yaml の inherited_context.enabled): 子の show は親の agent_context を、agent_context
        # ではなく別の key inherited_context に出す。子の問いにはしない (親の問いと qsig を子のカードに出さない)
        parent_ctx = json.dumps({"humanq": Q})
        inherited = [{"source_id": "p-1", "source_role": "epic", "source_title": "親", "field_used": "agent_context",
                      "content": parent_ctx}]
        cases = {"子に agent_context が無い": (br_shown("p-1.1", inherited_context=inherited), None),
                 "子にも agent_context がある": (br_shown("p-1.1", json.dumps({"other": 1}), inherited_context=inherited), None)}
        for what, (child, raw) in cases.items():
            with self.subTest(what):
                human, _ = self.ok(BrScript(human=[br_issue("p-1.1")], shows=[[child]]))   # list には出ない (実測)
                self.assertEqual(len(human), 1)
                self.assertEqual(human[0]["question_raw"], raw, "親から継いだ agent_context を子の問いにした")


class BrQuestion(unittest.TestCase):
    """agent_context (JSON の文字列) の top-level key humanq。読めなければ問いが未整理 (q=None)。"""

    def test_question_raw_table(self):
        deep = "[" * 100000
        cases = {
            "他の key がある": (Q_OTHER, Q),
            "humanq だけ": (json.dumps({"humanq": Q}), Q),
            "humanq の値が JSON の文字列": (json.dumps({"humanq": json.dumps(Q)}), json.dumps(Q)),
            "object で返す版": ({"humanq": Q}, Q),
            "JSON でない": ("{not json", None),
            "空": ("", None),
            "空白だけ": ("  \n", None),
            "JSON の配列": (json.dumps([{"humanq": Q}]), None),
            "JSON の文字列": (json.dumps("humanq"), None),
            "humanq key が無い": (json.dumps({"other": Q}), None),
            "None": (None, None),
            "数": (5, None),
            "深い入れ子": (deep, None),
        }
        for what, (ctx, raw) in cases.items():
            with self.subTest(what):
                try:
                    got = backend.BrBackend.question_raw(ctx)
                except Exception as e:  # noqa: BLE001
                    self.fail(f"例外: {type(e).__name__}: {e}")
                self.assertEqual(got, raw)

    def question(self, ctx):
        try:
            return humanq.question_from_raw(backend.BrBackend.question_raw(ctx))
        except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
            self.fail(f"例外: {type(e).__name__}: {e}")

    def test_questions_that_humanq_cannot_read_are_none(self):
        for what, ctx in {"ask が文字列でない": json.dumps({"humanq": {"ask": 5, "options": []}}),
                          "humanq が配列": json.dumps({"humanq": [Q]}), "JSON でない": "{",
                          "humanq の値が深い入れ子の JSON 文字列": json.dumps({"humanq": "[" * 100000})}.items():
            with self.subTest(what):
                self.assertIsNone(self.question(ctx))
        q = self.question(Q_OTHER)
        self.assertIsInstance(q, dict)
        self.assertEqual(q, humanq.question({"humanq": Q}), "bd の metadata.humanq と同じ問いになる (qsig も同じ)")


class BrThread(unittest.TestCase):
    """show の comments は並びが決まっていない。created_at → id で古い順にする。"""

    def thread(self, raw):
        try:
            return backend.br_thread(raw)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {type(e).__name__}: {e}")

    def test_comments_are_sorted_oldest_first_by_time_then_id(self):
        raw = [bc(4, "d", "2026-11-26T17:40:00Z"), bc(2, "b"), bc(3, "c"), bc(1, "a"),
               bc(9, "early", "2026-11-26T17:00:00Z")]       # id は大きいが時刻が古い: 時刻が先
        self.assertEqual([c["text"] for c in self.thread(raw)], ["early", "a", "b", "c", "d"])
        self.assertEqual(self.thread(raw)[0], {"text": "early", "created_at": "2026-11-26T17:00:00Z"})

    def test_times_in_other_forms_compare_as_times(self):
        raw = [bc(1, "naive-late", "2026-11-26T18:00:00"), bc(2, "offset-early", "2026-11-26T20:00:00+09:00"),
               bc(3, "micro", "2026-11-26T17:39:33.500000Z"), bc(4, "sec", "2026-11-26T17:39:33Z")]
        self.assertEqual([c["text"] for c in self.thread(raw)], ["offset-early", "sec", "micro", "naive-late"])

    def test_unreadable_times_ids_and_items_sort_oldest(self):
        raw = [bc(5, "ok"), {"id": 6, "text": "no-time"}, bc(7, "bad-time", "yesterday"), bc(8, "bad-date", "2026-13-40T00:00:00Z"),
               bc(9, "num-time", 5), bc("x", "str-id"), bc(None, "no-id"), "odd", None]
        got = self.thread(raw)
        self.assertEqual(len(got), len(raw))
        self.assertEqual(got[-1]["text"], "ok", "読める時刻のコメントが最新")
        self.assertEqual([c["text"] for c in got[-3:]], ["str-id", "no-id", "ok"], "同じ時刻なら id の無いものが古い側")
        self.assertEqual(got.count({"text": None, "created_at": None}), 2)

    def test_missing_null_and_odd_containers(self):
        self.assertEqual(self.thread(None), [], "コメントの無いチケットで br は key を出さない")
        self.assertEqual(self.thread([]), [])
        for odd in ({"text": "x"}, "x", 5):
            with self.subTest(odd=odd):
                self.assertIsNone(self.thread(odd), "list でなければ読めない")


class BrProject(unittest.TestCase):
    """humanq.load_project を br の backend で: 回答の本文と署名、両ラベルの向きを、崩れた順のコメントで決める。"""

    def project(self, script: BrScript) -> dict:
        with mock.patch.object(humanq, "br", script), mock.patch.object(humanq, "bd", no_subprocess), \
                mock.patch.object(backend.subprocess, "run", side_effect=no_subprocess):
            try:
                return humanq.load_project("/r", humanq._BR)
            except Exception as e:  # noqa: BLE001
                self.fail(f"想定外の例外: {type(e).__name__}: {e} (呼び出し: {script.calls})")

    def test_latest_humanq_comment_is_the_answer_even_when_br_returns_them_newest_first(self):
        ans_b = "[humanq] 回答: 案B (b)\n"
        cs = [bc(4, "後のメモ", "2026-11-26T17:39:34Z"), bc(3, ans_b), bc(2, "[humanq] 取り消し\n"), bc(1, ANS_A)]  # 同じ秒の 3 件は id で
        s = BrScript(answered=[br_issue("p-1", labels=["answered"])], shows=[[br_shown("p-1", Q_OTHER, comments=cs)]])
        d = self.project(s)
        self.assertEqual(len(d["answered"]), 1, d)
        a = d["answered"][0]
        self.assertEqual((a["answer"], a["asig"]), (ans_b, humanq.asig(3, ans_b)))
        self.assertIsNotNone(a["q"])
        self.assertEqual(a["qsig"], humanq.qsig(humanq.question({"humanq": Q})))

    def test_both_labels_take_the_direction_of_the_latest_humanq_comment(self):
        both = ["answered", "human"]
        undone_last = [bc(2, "[humanq] 取り消し\n"), bc(1, ANS_A)]             # 新しい順で返る
        answered_last = [bc(4, ANS_A), bc(3, "[humanq] 取り消し\n")]
        s = BrScript(human=[br_issue("p-u", labels=both), br_issue("p-a", labels=both), br_issue("p-n", labels=both)],
                     answered=[br_issue("p-u", labels=both), br_issue("p-a", labels=both), br_issue("p-n", labels=both)],
                     shows=[[br_shown("p-u", comments=undone_last), br_shown("p-a", comments=answered_last), br_shown("p-n")]])
        d = self.project(s)
        self.assertEqual(sorted(i["id"] for i in d["human"]), ["p-u"], "最後が取り消しなら判断待ち")
        self.assertEqual(sorted(i["id"] for i in d["answered"]), ["p-a", "p-n"], "最後が回答・コメント無しなら回答済み")
        by = {i["id"]: i for i in d["answered"]}
        self.assertEqual((by["p-n"]["answer"], by["p-n"]["asig"]), ("", humanq.asig(0, None)), "コメントが 0 件 (key が無い)")

    def test_unreadable_comments_leave_an_empty_signature(self):
        s = BrScript(answered=[br_issue("p-1", labels=["answered"])], shows=[[br_shown("p-1", comments={"text": ANS_A})]])
        d = self.project(s)
        self.assertEqual(len(d["answered"]), 1, d)
        self.assertEqual((d["answered"][0]["answer"], d["answered"][0]["asig"]), ("", ""))

    def test_agent_context_shapes_become_questions_or_none(self):
        cases = {"他の key がある": Q_OTHER, "JSON でない": "{x", "空": "", "key が無い": None,
                 "humanq の値が深い入れ子の JSON 文字列": json.dumps({"humanq": "[" * 100000})}   # 1 件で全件を読めなくしない
        s = BrScript(human=[br_issue(f"p-{n}") for n in range(len(cases))],
                     shows=[[br_shown(f"p-{n}", ctx) for n, ctx in enumerate(cases.values())]])
        d = self.project(s)
        self.assertEqual(len(d["human"]), len(cases), d)
        got = {i["id"]: i["q"] for i in d["human"]}
        self.assertEqual(got["p-0"], humanq.question({"humanq": Q}))
        self.assertEqual([got[f"p-{n}"] for n in (1, 2, 3, 4)], [None, None, None, None])


class BrWrite(unittest.TestCase):
    """BrBackend の書き込みの側 (pending・list_answered・comments・add_comment・label) の argv と出力の形。"""

    def call(self, script: BrScript, method: str, *args):
        try:
            return getattr(backend.BrBackend(run=script), method)("/r", *args)
        except backend.BdError as e:
            return e
        except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
            self.fail(f"BdError でない例外: {type(e).__name__}: {e} (呼び出し: {script.calls})")

    def test_pending_shows_only_the_ticket_being_answered(self):
        cs = [bc(3, "c", "2026-11-26T17:40:00Z"), bc(2, "b"), bc(1, "a")]            # 新しい順で返る
        s = BrScript(human=[br_issue("p-1"), br_issue("p-2", title="一覧の題")],
                     shows=[[br_shown("p-2", Q_OTHER, comments=cs, title="show の題")]])
        rows = self.call(s, "pending", "p-2")
        self.assertNotIsInstance(rows, Exception, rows)
        self.assertEqual(s.calls, [("/r", *BR_HUMAN), ("/r", "show", "p-2", "--json")], "show は答えるチケットの 1 件だけ")
        self.assertEqual(len(rows), 1, rows)
        r = rows[0]
        self.assertEqual(set(r), ROW_KEYS | {"thread"})
        self.assertEqual((r["id"], r["title"], r["question_raw"]), ("p-2", "show の題", Q), "問いは show の agent_context")
        self.assertEqual([c["text"] for c in r["thread"]], ["a", "b", "c"], "thread は古い順 (送り直しの判定がこれを読む)")

    def test_pending_of_a_ticket_not_in_the_list_runs_no_show(self):
        for what, human in {"一覧に無い": [br_issue("p-1")], "空の一覧": [],
                            "dict でない要素と同じ文字列": ["p-9", br_issue("p-1")]}.items():
            with self.subTest(what):
                s = BrScript(human=human, shows=[AssertionError("show を打った")])
                self.assertEqual(self.call(s, "pending", "p-9"), [])
                self.assertEqual(s.calls, [("/r", *BR_HUMAN)])

    def test_list_answered_reads_only_the_list(self):
        s = BrScript(answered=[br_issue("p-1", labels=["answered"]), "odd", br_issue("p-2", labels=["answered"])],
                     shows=[AssertionError("show を打った")])
        rows = self.call(s, "list_answered")
        self.assertNotIsInstance(rows, Exception, rows)
        self.assertEqual([r["id"] for r in rows], ["p-1", "p-2"], "dict でない要素は数えない")
        self.assertEqual(s.calls, [("/r", *BR_ANSWERED)])
        for r in rows:
            self.assertEqual(set(r), ROW_KEYS)

    def test_comments_come_from_show_oldest_first(self):
        cases = {"新しい順": ([bc(2, "b"), bc(1, "a")], ["a", "b"]), "key が無い": (None, []), "空": ([], [])}
        for what, (raw, texts) in cases.items():
            with self.subTest(what):
                s = BrScript(shows=[[br_shown("p-1", comments=raw)]])
                got = self.call(s, "comments", "p-1")
                self.assertNotIsInstance(got, Exception, got)
                self.assertEqual([c["text"] for c in got], texts)
                self.assertEqual(s.calls, [("/r", "show", "p-1", "--json")])

    def test_comments_that_are_not_a_list_are_a_bd_error(self):
        for odd in ({"text": ANS_A}, "x", 5):
            with self.subTest(odd=odd):
                e = self.call(BrScript(shows=[[br_shown("p-1", comments=odd)]]), "comments", "p-1")
                self.assertIsInstance(e, backend.BdError)
                self.assertIn("comments を読めない", str(e))

    def test_show_of_one_ticket_must_be_exactly_that_ticket(self):
        cases = {"空": [], "2 件": [br_shown("p-1"), br_shown("p-2")], "別の id": [br_shown("p-2")],
                 "dict でない": ["p-1"], "id が無い": [{"title": "x"}]}
        for what, out in cases.items():
            with self.subTest(what):
                for method, args in (("comments", ("p-1",)), ("pending", ("p-1",))):
                    s = BrScript(human=[br_issue("p-1")], shows=[out])
                    e = self.call(s, method, *args)
                    self.assertIsInstance(e, backend.BdError, (method, e))
                    self.assertIn("の 1 件ではない", str(e))
        e = self.call(BrScript(shows=[{"issues": []}]), "comments", "p-1")
        self.assertIsInstance(e, backend.BdError)
        self.assertTrue(str(e).startswith("br show の出力が list ではない"), e)

    def test_ids_that_cannot_be_passed_to_br_are_not_shown(self):
        for iid in ("--db=/other.db", "-x", "", 5, None):
            with self.subTest(iid=iid):
                s = BrScript(human=[{**br_issue("p-1"), "id": iid}], shows=[AssertionError("show を打った")])
                for method in ("comments", "pending"):
                    e = self.call(s, method, iid)
                    self.assertIsInstance(e, backend.BdError, (method, e))
                    self.assertIn("br show に渡せない id", str(e))
                self.assertEqual([c for c in s.calls if "show" in c], [], "渡せない id で show を打った")

    def test_writes_use_the_actor_and_the_same_form_as_bd(self):
        s = BrScript()
        for method, args in (("add_comment", ("p-1", "/tmp/c.md")), ("label", ("add", "p-1", "answered")),
                             ("label", ("remove", "p-1", "human"))):
            self.assertIsNone(self.call(s, method, *args))
        w = ("/r", "--actor", "user/humanq")
        self.assertEqual(s.calls, [(*w, "comments", "add", "p-1", "-f", "<tmp>"), (*w, "label", "add", "p-1", "answered"),
                                   (*w, "label", "remove", "p-1", "human")])


class BrStore(unittest.TestCase):
    """br と判定したプロジェクトの回答・取り消し・保留・保留の解除 (humanq.Store) が、humanq.br の差し替え口だけを通ること。bd は打たない。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-brstore-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.repo, ".beads"))
        Path(self.repo, ".beads", ".gitignore").write_text(".br_history/\n", encoding="utf-8")       # br の印
        for p in (mock.patch.object(backend.subprocess, "run", side_effect=no_subprocess),
                  mock.patch.object(humanq, "bd", no_subprocess)):
            p.start()
            self.addCleanup(p.stop)

    def run_store(self, script: BrScript, fn: str, body: dict):
        with mock.patch.object(humanq, "br", script):
            try:
                return getattr(humanq.Store(self.tmp), fn)(self.repo, body)
            except AssertionError:
                raise
            except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
                self.fail(f"想定外の例外: {type(e).__name__}: {e} (呼び出し: {script.calls})")

    def test_answer_and_undo_go_through_humanq_br(self):
        r, w = self.repo, (self.repo, "--actor", "user/humanq")
        s = BrScript(human=[br_issue("p-1")], shows=[[br_shown("p-1", Q_OTHER, comments=[bc(1, "メモ")])]])
        sig = self.run_store(s, "answer", {"id": "p-1", "option": "a", "qsig": humanq.qsig(humanq.question({"humanq": Q}))})
        self.assertEqual(sig, humanq.asig(1, ANS_A))
        self.assertEqual(s.calls, [(r, *BR_HUMAN), (r, "show", "p-1", "--json"), (*w, "comments", "add", "p-1", "-f", "<tmp>"),
                                   (*w, "label", "add", "p-1", "answered"), (*w, "label", "remove", "p-1", "human")])
        s = BrScript(answered=[br_issue("p-2", labels=["answered"])],
                     shows=[[br_shown("p-2", comments=[bc(1, ANS_A, author="user/humanq")])]])
        self.assertIsNone(self.run_store(s, "undo", {"id": "p-2", "asig": humanq.asig(1, ANS_A)}))
        self.assertEqual(s.calls, [(r, *BR_ANSWERED), (r, "show", "p-2", "--json"), (*w, "comments", "add", "p-2", "-f", "<tmp>"),
                                   (*w, "label", "add", "p-2", "human"), (*w, "label", "remove", "p-2", "answered")])

    def test_snooze_and_unsnooze_go_through_humanq_br(self):
        r, w, sz = self.repo, (self.repo, "--actor", "user/humanq"), ["human", "snoozed"]
        q = humanq.qsig(humanq.question({"humanq": Q}))
        s = BrScript(human=[br_issue("p-1")], shows=[[br_shown("p-1", Q_OTHER, comments=[bc(1, "メモ")])]])
        self.assertIsNone(self.run_store(s, "snooze", {"id": "p-1", "until": None, "note": "x", "qsig": q}))
        self.assertEqual(s.calls, [(r, *BR_HUMAN), (r, "show", "p-1", "--json"), (*w, "comments", "add", "p-1", "-f", "<tmp>"),
                                   (*w, "label", "add", "p-1", "snoozed")], "コメントは pending の show から読む。status は触らない")
        sn = bc(2, "[humanq] 保留: 期限なし\n\nx\n", "2026-11-26T17:40:00Z", "user/humanq")
        s = BrScript(human=[br_issue("p-1", labels=sz)], shows=[[br_shown("p-1", Q_OTHER, comments=[sn, bc(1, "メモ")], labels=sz)]])
        self.assertIsNone(self.run_store(s, "unsnooze", {"id": "p-1"}))
        self.assertEqual(s.calls, [(r, *BR_HUMAN), (r, "show", "p-1", "--json"), (*w, "comments", "add", "p-1", "-f", "<tmp>"),
                                   (*w, "label", "remove", "p-1", "snoozed")])
        s = BrScript(human=[br_issue("p-1")], shows=[[br_shown("p-1", Q_OTHER, comments=[bc(1, "メモ")])]])
        self.assertIsNone(self.run_store(s, "unsnooze", {"id": "p-1"}))
        self.assertEqual(s.calls, [(r, *BR_HUMAN), (r, "show", "p-1", "--json")], "保留でないチケットの解除で書いた")

    def test_a_last_comment_whose_text_is_not_a_string_does_not_stop_the_write(self):
        # 送り直しの判定は、本文が文字列でない最後のコメントを同じ本文とみなさず、書く (例外で書き込みを止めない)
        recent = humanq.datetime.now(humanq.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for odd in (5, None, ["[humanq] 保留: 期限なし"], {"t": 1}):
            for fn, body in (("snooze", {"id": "p-1", "until": None}), ("answer", {"id": "p-1", "option": "a"})):
                with self.subTest(text=odd, fn=fn):
                    cs = [bc(2, odd, recent, "user/humanq"), bc(1, "メモ", recent)]
                    s = BrScript(human=[br_issue("p-1")], shows=[[br_shown("p-1", Q_OTHER, comments=cs)]])
                    self.run_store(s, fn, body)
                    self.assertIn(("--actor", "user/humanq", "comments"), [c[1:4] for c in s.calls], "コメントを書かなかった")

    def test_a_snooze_whose_comments_cannot_be_read_fails_as_the_comment_stage(self):
        s = BrScript(human=[br_issue("p-1")], shows=[[br_shown("p-1", Q_OTHER, comments={"text": "x"})]])
        with mock.patch.object(humanq, "br", s):
            try:
                humanq.Store(self.tmp).snooze(self.repo, {"id": "p-1", "until": None})
                self.fail("WriteError が上がらなかった")
            except humanq.WriteError as e:
                self.assertEqual((e.stage, e.written), ("comment", False))
            except AssertionError:
                raise
            except Exception as e:  # noqa: BLE001
                self.fail(f"想定外の例外: {type(e).__name__}: {e}")
        self.assertEqual([c[1] for c in s.calls], ["list", "show", "show"], "読めないコメントのまま書き込んだ")

    def test_a_resend_reuses_the_comments_of_the_pending_show(self):
        # 最後のコメントが同じ回答 (送り直し): コメントは書かずにラベルだけ直す。読みは一覧と show の 2 回だけ
        recent = humanq.datetime.now(humanq.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cs = [bc(2, ANS_A, recent, "user/humanq"), bc(1, "メモ", recent)]                   # 新しい順。同じ秒は id で
        s = BrScript(human=[br_issue("p-1")], shows=[[br_shown("p-1", Q_OTHER, comments=cs)]])
        sig = self.run_store(s, "answer", {"id": "p-1", "option": "a"})
        self.assertEqual(sig, humanq.asig(1, ANS_A))
        self.assertEqual([c[1:3] for c in s.calls], [BR_HUMAN[:2], ("show", "p-1"), ("--actor", "user/humanq"),
                                                    ("--actor", "user/humanq")])
        self.assertEqual([c[3:4] for c in s.calls[2:]], [("label",), ("label",)], "送り直しでコメントを書いた")

    def test_unreadable_comments_in_the_pending_show_are_read_again_and_fail_as_the_comment_stage(self):
        s = BrScript(human=[br_issue("p-1")], shows=[[br_shown("p-1", Q_OTHER, comments={"text": "x"})]])
        with mock.patch.object(humanq, "br", s):
            try:
                humanq.Store(self.tmp).answer(self.repo, {"id": "p-1", "option": "a"})
                self.fail("WriteError が上がらなかった")
            except humanq.WriteError as e:
                self.assertEqual((e.stage, e.written), ("comment", False))
            except Exception as e:  # noqa: BLE001
                self.fail(f"想定外の例外: {type(e).__name__}: {e}")
        self.assertEqual([c[1] for c in s.calls], ["list", "show", "show"], "読めないコメントのまま書き込んだ")


if __name__ == "__main__":
    unittest.main()
