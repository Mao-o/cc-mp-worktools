"""bd と話す層 (server/backend.py) の約束と、humanq.py に残した差し替え口のテスト。

    python3 -m unittest discover tests

bd は打たない (bd を 1 回打つ関数を台本の関数に替える)。bd を通す読み書きのシナリオは test_server.py にある。
HUMANQ_SERVER_DIR で別の server/ を試せる。
"""
from __future__ import annotations

import ast
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
ANSWERED_LIST = ("list", "--status", "open,in_progress,blocked,deferred", "--label", "answered", "--flat", "-n", "0", "--json")


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
                self.assertIsNone(humanq.question_from_raw(raw))


def str_constants(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return imports, consts


class Layering(unittest.TestCase):
    """bd の形 (起動・argv の語・metadata の置き場) を知っているのは backend.py だけ。humanq.py は CLI に依らない形を読む。"""

    CLI_WORDS = {"bd", "-C", "--json", "--actor", "--status", "--label", "--flat", "-n", "-f", "metadata", "comments", "list"}

    def test_humanq_py_does_not_know_the_cli(self):
        imports, consts = str_constants(SERVER_DIR / "humanq.py")
        self.assertNotIn("subprocess", imports)
        self.assertEqual(consts & self.CLI_WORDS, set())
        b_imports, b_consts = str_constants(SERVER_DIR / "backend.py")     # 照合の語が backend 側に実際にある (空の検査にしない)
        self.assertIn("subprocess", b_imports)
        self.assertEqual(self.CLI_WORDS - b_consts, set())


if __name__ == "__main__":
    unittest.main()
