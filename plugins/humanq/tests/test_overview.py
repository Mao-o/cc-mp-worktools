"""見通し (画面の「見通し」のタブ。GET /api/overview) のテスト。

    python3 -m unittest tests.test_overview

- backend の読み (bd・br の状態別の件数と ready) の argv と、出力の形が崩れたときに BdError になること (ほかの例外で止めない)
- 画面に出す形 (overview_of): 回答済みを ready から除く・判断待ちに印を付ける・優先度 → 作成日の上位 5 件・ラベルを解釈しない
- 偽の bd・偽の br (tests/fake_beads.py) を通した端から端まで: 状態別の件数と ready の上位 5 件・1 プロジェクトあたり 2 プロセス・
  60 秒のキャッシュ・書き込みでキャッシュを捨てる
- 判断待ちを主役に保つ条件: /api/items と /api/summary は見通しのデータを含めず、見通しの CLI も打たない。hook は見通しを読まない
  (画面の側の条件 (ポーリングで読まない・ボタンが無い・リンク) は tests/ui_test.mjs)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER_DIR = Path(os.environ.get("HUMANQ_SERVER_DIR") or HERE.parent / "server")
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(HERE))                   # fake_cli (`-m unittest tests.test_overview` のように親から指したときも読む)
import backend  # noqa: E402
import fake_cli  # noqa: E402
import humanq  # noqa: E402
import registry  # noqa: E402

HOOK_PATH = SERVER_DIR.parent / "hooks" / "humanq_hook.py"
OVERVIEW_KEYS = {"counts", "total", "ready", "ready_total", "blocked"}     # 見通しにだけ出す key (/api/items に出さない)


def install_fake(bin_dir: str, prog: str) -> str:
    """偽物を prog の名前で置く (偽物は argv[0] の basename で振る舞いを決める。置き方は tests/fake_cli.py)。"""
    return fake_cli.install(bin_dir, prog)


def fetch(url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


# ---- backend: 読みの argv と出力の形 ------------------------------------------------------------------

class Recorder:
    """backend に渡す run。呼ばれた argv を残し、out を返す (例外なら上げる)。"""

    def __init__(self, out):
        self.out, self.calls = out, []

    def __call__(self, repo, *args):
        self.calls.append(args)
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


class BackendReads(unittest.TestCase):
    def call(self, fn, *args):
        """読めるはずの読み。例外は FAIL にする (変異で例外が漏れたとき、走っていない ERROR に数えない)。"""
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.fail(f"読めるはずの出力で例外: {type(e).__name__}: {e}")

    def refused(self, fn, *args) -> str:
        try:
            fn(*args)
        except backend.BdError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            self.fail(f"BdError ではない例外で止まった: {type(e).__name__}: {e}")
        self.fail(f"BdError にならなかった: {args}")

    def test_argv_of_the_two_reads(self):
        cases = {
            ("bd", "status_counts"): (backend.BdBackend, '{"total": 0, "groups": [], "schema_version": 1}',
                                      ("count", "--by-status", "--json")),
            ("bd", "ready"): (backend.BdBackend, "[]", ("ready", "--limit", "0", "--json")),
            ("br", "status_counts"): (backend.BrBackend, '{"total": 0, "groups": []}', ("count", "--by", "status", "--json")),
            ("br", "ready"): (backend.BrBackend, "[]", ("ready", "--limit", "0", "--json")),
        }
        for (cli, what), (cls, out, argv) in cases.items():
            with self.subTest(cli=cli, read=what):
                run = Recorder(out)
                got = self.call(getattr(cls(run=run), what), "/r")
                self.assertEqual(got, [])
                self.assertEqual(run.calls, [argv])

    def test_counts_keep_the_order_of_the_groups(self):
        out = json.dumps({"total": 4, "groups": [{"group": "draft", "count": 1}, {"group": "open", "count": 3}]})
        for cls in (backend.BdBackend, backend.BrBackend):
            with self.subTest(cls.__name__):
                self.assertEqual(self.call(cls(run=Recorder(out)).status_counts, "/r"), [("draft", 1), ("open", 3)])
        null = json.dumps({"total": 0, "groups": None, "schema_version": 1})       # bd は空の list を null で返しうる
        self.assertEqual(self.call(backend.BdBackend(run=Recorder(null)).status_counts, "/r"), [])

    def test_counts_that_cannot_be_read_are_a_bd_error(self):
        deep = "[" * 100000 + "]" * 100000
        bad = {
            "JSON でない": "Total: 3",
            "空": "",
            "深い入れ子": deep,
            "list": "[]",
            "数": "3",
            "groups が無い": '{"total": 3}',
            "groups が dict": '{"total": 1, "groups": {"open": 1}}',
            "groups が文字列": '{"total": 1, "groups": "open"}',
            "groups が数": '{"total": 1, "groups": 5}',
            "要素が dict でない": '{"total": 1, "groups": [["open", 1]]}',
            "group が文字列でない": '{"total": 1, "groups": [{"group": 1, "count": 1}]}',
            "group が無い": '{"total": 1, "groups": [{"count": 1}]}',
            "count が真偽値": '{"total": 1, "groups": [{"group": "open", "count": true}]}',
            "count が負": '{"total": 1, "groups": [{"group": "open", "count": -1}]}',
            "count が小数": '{"total": 1, "groups": [{"group": "open", "count": 1.0}]}',
            "count が文字列": '{"total": 1, "groups": [{"group": "open", "count": "1"}]}',
        }
        for cls in (backend.BdBackend, backend.BrBackend):
            for name, out in bad.items():
                with self.subTest(cls=cls.__name__, form=name):
                    self.refused(cls(run=Recorder(out)).status_counts, "/r")

    def test_ready_rows_keep_the_values_and_drop_broken_elements(self):
        rows = [{"id": "a-1", "title": "t", "priority": 1, "status": "open", "labels": ["human", "plugin:x"],
                 "created_at": "2026-11-20T00:00:00Z", "description": "長い本文", "metadata": {"humanq": {}}},
                {"id": "a-2"}, "x", None, {"id": ""}, {"id": 3}, {"title": "id 無し"}]
        want = [{"id": "a-1", "title": "t", "priority": 1, "status": "open", "labels": ["human", "plugin:x"],
                 "created_at": "2026-11-20T00:00:00Z"},
                {"id": "a-2", "title": None, "priority": None, "status": None, "labels": None, "created_at": None}]
        for cls in (backend.BdBackend, backend.BrBackend):
            with self.subTest(cls.__name__):
                self.assertEqual(self.call(cls(run=Recorder(json.dumps(rows))).ready, "/r"), want)

    def test_ready_that_cannot_be_read_is_a_bd_error(self):
        for name, out in {"JSON でない": "oops", "envelope": '{"issues": []}', "数": "3", "深い入れ子": "[" * 100000,
                     "桁の多い数": "[" + "1" * 5000 + "]"}.items():
            for cls in (backend.BdBackend, backend.BrBackend):
                with self.subTest(cls=cls.__name__, form=name):
                    self.refused(cls(run=Recorder(out)).ready, "/r")

    def test_bd_ready_reads_null_and_the_no_result_line_as_empty(self):
        # bd は 0 件の一覧を null で返すことがある (human list)。案内文 1 行だけの版も (parse_bd_list と同じ読み)
        for out in ("null", "", "No ready work found"):
            with self.subTest(out=out):
                self.assertEqual(self.call(backend.BdBackend(run=Recorder(out)).ready, "/r"), [])

    def test_a_failed_cli_is_raised_as_it_was(self):
        for cls in (backend.BdBackend, backend.BrBackend):
            for what in ("status_counts", "ready"):
                with self.subTest(cls=cls.__name__, read=what):
                    self.assertIn("boom", self.refused(getattr(cls(run=Recorder(backend.BdError("boom"))), what), "/r"))


# ---- 画面に出す形 ----------------------------------------------------------------------------------

def rr(iid, prio=2, labels=(), created="2026-11-01T00:00:00Z", status="open", title=None):
    return {"id": iid, "title": title if title is not None else f"t {iid}", "priority": prio, "status": status,
            "labels": list(labels), "created_at": created}


class OverviewShape(unittest.TestCase):
    def shape(self, raw, pending=(), answered=()):
        try:
            return humanq.overview_of(raw, set(pending), set(answered))
        except Exception as e:  # noqa: BLE001
            self.fail(f"overview_of が例外を上げた: {type(e).__name__}: {e}")

    def ok(self, counts=(), ready=()):
        return {"ok": True, "cli": "bd", "counts": list(counts), "ready": list(ready)}

    def ids(self, d):
        self.assertIn("ready", d)
        return [r["id"] for r in d["ready"]]

    def test_answered_are_left_out_by_label_and_by_the_items_cache(self):
        d = self.shape(self.ok(ready=[rr("a", 0, ["answered"]), rr("b", 1), rr("c", 1), rr("d", 2, ["answered", "human"])]),
                       answered={"c"})
        self.assertEqual(self.ids(d), ["b"])
        self.assertEqual(d["ready_total"], 1, "ready_total は除いた後の件数")

    def test_human_is_marked_by_label_and_by_the_items_cache(self):
        d = self.shape(self.ok(ready=[rr("a", 1, ["human"]), rr("b", 1), rr("c", 1, ["human", "snoozed"]), rr("d", 1)]),
                       pending={"b"})
        self.assertEqual([(r["id"], r["human"]) for r in d["ready"]], [("a", True), ("b", True), ("c", True), ("d", False)])

    def test_top_five_by_priority_then_created_then_id(self):
        ready = [rr("p2-old", 2, created="2026-01-01T00:00:00Z"), rr("p3", 3), rr("none", None), rr("bool", True),
                 rr("p0", 0, created="2026-11-20T00:00:00Z"), rr("p1-new", 1, created="2026-11-10T00:00:00Z"),
                 rr("p1-old", 1, created="2026-11-09T00:00:00Z"), rr("p2-new-b", 2, created="2026-11-11T00:00:00Z"),
                 rr("p2-new-a", 2, created="2026-11-11T00:00:00Z"), rr("p4", 4), rr("str", "1")]
        d = self.shape(self.ok(ready=ready))
        self.assertEqual(self.ids(d), ["p0", "p1-old", "p1-new", "p2-old", "p2-new-a"])
        self.assertEqual(d["ready_total"], 11)
        # 数でない優先度 (None・真偽値・文字列) は最後 (9 と同じ)。全部を並べると見える
        with mock.patch.object(humanq, "READY_TOP", 20):
            d = self.shape(self.ok(ready=ready))
        self.assertEqual(self.ids(d)[-4:], ["p4", "bool", "none", "str"])
        self.assertEqual([r["priority"] for r in d["ready"]][-3:], [None, None, None], "数でない優先度は None で渡す")

    def test_labels_are_passed_as_strings_without_being_read(self):
        d = self.shape(self.ok(ready=[rr("a", 1, ["plugin:x", "lane:y", 3, None, "human"]),
                                      {**rr("b", 2), "labels": "plugin:x"}, {**rr("c", 3), "labels": None}]))
        self.assertEqual([r["labels"] for r in d["ready"]], [["plugin:x", "lane:y", "human"], [], []])
        # ラベルは並びに効かない (plugin: や lane: の付いたものを先にしない)
        d = self.shape(self.ok(ready=[rr("z", 1), rr("a", 1, ["lane:1", "plugin:humanq"])]))
        self.assertEqual(self.ids(d), ["a", "z"], "同じ優先度・作成日は id の順")

    def test_row_values_are_cut_to_the_shape_of_the_page(self):
        d = self.shape(self.ok(ready=[{"id": "a", "title": None, "priority": 1, "status": None, "labels": None,
                                       "created_at": "2026-11-20T12:34:56.789Z"}]))
        self.assertEqual(d["ready"], [{"id": "a", "title": "", "priority": 1, "status": "", "labels": [],
                                       "created_at": "2026-11-20", "human": False}])

    def test_counts_are_ordered_by_status_and_summed(self):
        counts = [("review", 1), ("open", 5), ("closed", 9), ("blocked", 2), ("in_progress", 3), ("alpha", 1), ("tombstone", 1),
                  ("deferred", 4)]
        d = self.shape(self.ok(counts=counts))
        self.assertEqual([c["status"] for c in d["counts"]],
                         ["open", "in_progress", "blocked", "deferred", "alpha", "review", "closed", "tombstone"])
        self.assertEqual(d["counts"][0], {"status": "open", "count": 5})
        self.assertEqual(d["total"], 26)

    def test_a_failed_read_has_empty_lists_and_the_error(self):
        d = self.shape({"ok": False, "error": "boom", "cli": "br"})
        self.assertEqual(d, {"ok": False, "error": "boom", "cli": "br", "counts": [], "total": 0, "ready": [], "ready_total": 0})


# ---- 偽の bd・偽の br を通す ------------------------------------------------------------------------

# 状態・優先度・ラベルの違うチケット (名前, 優先度, ラベル, status)
SCENARIO = [("p3-plain", 3, "", ""), ("p2-plugin", 2, "plugin:x,lane:y", ""), ("p0-answered", 0, "answered", ""),
            ("p1-human", 1, "human", ""), ("p2-plain", 2, "", ""), ("p4-plain", 4, "", ""), ("p1-plain", 1, "", ""),
            ("p0-busy", 0, "", "in_progress"), ("p0-blocked", 0, "", "blocked"), ("p0-closed", 0, "", "closed")]


def cli_env(bin_dir: str) -> dict:
    """偽物を打つ env。beads 系の env と偽物の env を外し、PATH の先頭に偽物の bin dir を置く。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_BEADS_", "BEADS_", "BD_", "BR_"))}
    env["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    return env


def run_cli(prog: str, repo: str, *args: str, env: dict | None = None) -> str:
    """偽物を repo で打って stdout を返す (bd は -C <repo>、br は cwd)。0 以外で終わったら AssertionError (FAIL に数える)。"""
    argv = [prog, "-C", repo, *args] if prog == "bd" else [prog, *args]
    p = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=60, env=env)
    if p.returncode != 0:
        raise AssertionError(f"{argv}: {p.stderr or p.stdout}")
    return p.stdout


def new_ticket(prog: str, repo: str, title: str, prio: int = 2, labels: str = "", status: str = "",
               env: dict | None = None) -> str:
    args = ["create", title, "-p", str(prio)] + (["-l", labels] if labels else []) + ["--json"]
    created = json.loads(run_cli(prog, repo, *args, env=env))
    if not isinstance(created, dict):
        raise AssertionError(f"create の戻りが object でない: {created!r}")
    iid = created["id"]
    if status == "closed":
        run_cli(prog, repo, "close", iid, env=env)
    elif status:
        run_cli(prog, repo, "update", iid, "--status", status, env=env)
    return iid


class FakeCliProject:
    """偽の CLI のプロジェクト 1 つを Store と HTTP で読む道具。サブクラスが PROG を決める。
    SCENARIO のチケットを入れたプロジェクトはクラスごとに 1 回だけ偽物で作り、テストごとにその写しを読む (偽物の状態は
    .beads/ の中のファイルだけなので、写しは同じ打ち方で作り直したものと同じ。テストの間で状態を分け合わない)。"""

    PROG = ""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-overview-class-"))
        cls.addClassCleanup(shutil.rmtree, tmp, True)
        cls.bin_dir = os.path.join(tmp, "bin")
        os.makedirs(cls.bin_dir)
        for prog in ("bd", "br"):
            install_fake(cls.bin_dir, prog)
        env = cli_env(cls.bin_dir)
        cls.template = os.path.join(tmp, "proj")
        os.makedirs(cls.template)
        if cls.PROG == "bd":
            run_cli("bd", cls.template, "init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq", env=env)
        else:
            run_cli("br", cls.template, "init", "--prefix", "hq", env=env)
            Path(cls.template, ".beads", ".gitignore").write_text(".br_history/\n", encoding="utf-8")   # br init が書く印
        cls.template_ids = {name: new_ticket(cls.PROG, cls.template, name, prio, labels, status, env=env)
                            for name, prio, labels, status in SCENARIO}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-overview-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        patch = mock.patch.dict(os.environ, {**cli_env(self.bin_dir), "FAKE_BEADS_LOG": self.log}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = os.path.join(self.tmp, "proj")
        shutil.copytree(self.template, self.repo, symlinks=True)
        self.assertEqual(registry.project_kind(self.repo)[0], self.PROG)
        self.sd = os.path.join(self.tmp, "state")
        os.makedirs(self.sd)
        registry.register(self.sd, self.repo)
        self.store = humanq.Store(self.sd)

    def cli(self, *args: str) -> str:
        return run_cli(self.PROG, self.repo, *args)

    def new(self, title: str, prio: int = 2, labels: str = "", status: str = "") -> str:
        return new_ticket(self.PROG, self.repo, title, prio, labels, status)

    def entries(self) -> list[dict]:
        try:
            lines = Path(self.log).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(x) for x in lines if x.strip()]

    def argvs(self, start: int = 0) -> list[list[str]]:
        """start から後の、偽物の呼び出しの argv (bd の -C <repo> は外す)。"""
        out = []
        for e in self.entries()[start:]:
            self.assertEqual(e["prog"], self.PROG, e)
            a = e["argv"]
            out.append(a[2:] if a[:2] == ["-C", self.repo] else a)
        return out

    def overview(self) -> dict:
        try:
            return self.store.overview(self.repo)
        except Exception as e:  # noqa: BLE001
            self.fail(f"Store.overview が例外を上げた: {type(e).__name__}: {e}")

    def serve(self) -> int:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = srv.server_address[1]
        srv.RequestHandlerClass = humanq.make_handler(self.store, port, "tok", "<html></html>", "t")
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()   # shutdown を待たせない
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return port

    def get(self, port: int, path: str) -> tuple[int, dict]:
        """GET して JSON を読む。応答が無い (サーバーの handler が例外で切れた)・JSON でないときは FAIL にする。"""
        try:
            code, raw = fetch(f"http://127.0.0.1:{port}{path}")
            return code, json.loads(raw)
        except (OSError, ValueError) as e:
            self.fail(f"{path} の応答を読めない: {type(e).__name__}: {e}")

    def post(self, port: int, path: str, body: dict) -> tuple[int, dict]:
        code, raw = fetch(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                          {"Content-Type": "application/json", "X-Humanq-Token": "tok"})
        return code, json.loads(raw)

    def scenario(self) -> dict:
        """SCENARIO のチケット (このテストのプロジェクトは、クラスで作ったものの写し)。返り値は名前 -> id。"""
        self.assertEqual(len(self.template_ids), len(SCENARIO))
        return dict(self.template_ids)


class FakeCliOverview(FakeCliProject):
    """見通しのシナリオ (偽の bd と偽の br で同じもの)。"""

    def test_counts_and_the_top_five_ready(self):
        ids = self.scenario()
        d = self.overview()
        self.assertTrue(d.get("ok"), d)
        self.assertEqual(d["cli"], self.PROG)
        # CLI の既定の範囲をそのまま出す: bd は closed も数え (bd 1.3.4 の実測)、br は数えない (br 0.7.6 の実測)
        want = {"open": 7, "in_progress": 1, "blocked": 1}
        order = ["open", "in_progress", "blocked"]
        if self.PROG == "bd":
            want["closed"] = 1
            order.append("closed")
        self.assertEqual({c["status"]: c["count"] for c in d["counts"]}, want, f"{self.PROG} の既定の範囲")
        self.assertEqual([c["status"] for c in d["counts"]], order)
        self.assertEqual(d["total"], sum(want.values()))
        by = {v: k for k, v in ids.items()}
        self.assertEqual([by.get(r["id"]) for r in d["ready"]], ["p1-human", "p1-plain", "p2-plugin", "p2-plain", "p3-plain"],
                         "回答済みを除き、優先度 → 作成日の上位 5 件 (in_progress・blocked・closed は ready に出ない)")
        self.assertEqual(d["ready_total"], 6)
        self.assertEqual([r["human"] for r in d["ready"]], [True, False, False, False, False])
        row = d["ready"][2]
        self.assertEqual((row["labels"], row["status"], row["priority"], row["title"]), (["lane:y", "plugin:x"], "open", 2, "p2-plugin"))
        self.assertRegex(row["created_at"], r"^\d{4}-\d\d-\d\d$")

    def test_two_cli_runs_per_read_and_a_cache_of_sixty_seconds(self):
        self.scenario()
        start = len(self.entries())
        self.overview()
        count_argv = ["count", "--by-status", "--json"] if self.PROG == "bd" else ["count", "--by", "status", "--json"]
        self.assertEqual(self.argvs(start), [count_argv, ["ready", "--limit", "0", "--json"]])
        mid = len(self.entries())
        self.overview()
        self.assertEqual(self.argvs(mid), [], "キャッシュの内は打たない")
        self.assertEqual(humanq.OVERVIEW_CACHE_SEC, 60.0)
        self.assertGreater(humanq.OVERVIEW_CACHE_SEC, humanq.CACHE_SEC, "見通しのキャッシュは判断待ちより長い")
        with mock.patch.object(humanq, "OVERVIEW_CACHE_SEC", 0.0):
            self.overview()
        self.assertEqual(self.argvs(mid), [count_argv, ["ready", "--limit", "0", "--json"]], "キャッシュが切れたら 2 回読む")
        if self.PROG == "br":
            self.assertEqual({e["cwd"] for e in self.entries()}, {self.repo}, "br はプロジェクトの dir で打つ")

    def test_the_read_waits_for_the_read_lock_of_the_project(self):
        # bd の埋め込み Dolt を同時に開かない: 判断待ちの読みと同じロックを待ってから読む
        self.scenario()
        done = threading.Event()
        lock = self.store._lock(self.store._locks, self.repo)
        with lock:
            t = threading.Thread(target=lambda: (self.store.overview(self.repo), done.set()), daemon=True)
            t.start()
            self.assertFalse(done.wait(0.5), "判断待ちの読みのロックを待たずに見通しを読んだ")
        self.assertTrue(done.wait(20), "ロックを放した後も見通しの読みが終わらない")

    def test_items_and_summary_neither_carry_nor_read_the_overview(self):
        self.scenario()
        port = self.serve()
        code, _ = self.get(port, "/api/overview")
        self.assertEqual(code, 200)
        start = len(self.entries())
        code, items = self.get(port, "/api/items")
        self.assertEqual(code, 200, items)
        self.assertEqual(len(items.get("projects") or []), 1, items)
        self.assertEqual(set(items) & OVERVIEW_KEYS, set())
        self.assertEqual(set(items["projects"][0]) & OVERVIEW_KEYS, set(), "/api/items に見通しのデータが入った")
        code, summ = self.get(port, "/api/summary?project=" + urllib.parse.quote(self.repo, safe="") + "&fresh=1")
        self.assertEqual(code, 200, summ)
        self.assertEqual(set(summ) & OVERVIEW_KEYS, set(), "/api/summary に見通しのデータが入った")
        self.assertEqual([a for a in self.argvs(start) if a[:1] in (["ready"], ["count"])], [],
                         "/api/items・/api/summary の読みで見通しの CLI を打った")
        self.assertGreater(len(self.argvs(start)), 0, "/api/items が CLI を読んでいない (比べる対象が無い)")

    def test_the_overview_api_for_all_and_for_one_project(self):
        self.scenario()
        port = self.serve()
        code, all_ = self.get(port, "/api/overview")
        self.assertEqual(code, 200, all_)
        self.assertEqual([(p["path"], p["name"], p["ok"]) for p in all_["projects"]], [(self.repo, "proj", True)])
        self.assertEqual(len(all_["projects"][0]["ready"]), 5)
        code, one = self.get(port, "/api/overview?project=" + urllib.parse.quote(self.repo, safe=""))
        self.assertEqual(code, 200, one)
        self.assertEqual(one, all_, "project を付けても同じ形で、そのプロジェクトだけ")
        code, err = self.get(port, "/api/overview?project=" + urllib.parse.quote(os.path.join(self.tmp, "nope"), safe=""))
        self.assertEqual((code, err), (404, {"error": "未登録のプロジェクト"}))

    def test_a_failed_read_is_only_that_project_and_items_still_read(self):
        self.scenario()
        port = self.serve()
        with mock.patch.dict(os.environ, {"FAKE_BEADS_FAIL": "ready"}):
            code, d = self.get(port, "/api/overview")
            self.assertEqual(code, 200, d)
            p = d["projects"][0]
            self.assertEqual((p["ok"], p["counts"], p["ready"], p["cli"]), (False, [], [], self.PROG), p)
            self.assertEqual(p["error"], "Error: fake-beads: injected failure (ready)", "CLI の失敗の文をそのまま出す")
            code, items = self.get(port, "/api/items")
            self.assertEqual(code, 200, items)
            self.assertTrue(items["projects"][0]["ok"], "見通しの読みの失敗が判断待ちの読みに及んだ")

    def test_an_unexpected_exception_is_only_that_project(self):
        self.scenario()
        port = self.serve()
        be = humanq.BACKENDS[self.PROG]
        with mock.patch.object(be, "ready", side_effect=RuntimeError("odd shape")):
            code, d = self.get(port, "/api/overview")
        self.assertEqual(code, 200, d)
        p = d["projects"][0]
        self.assertEqual((p["ok"], p["error"], p["cli"]), (False, "RuntimeError: odd shape", self.PROG), p)

    def test_a_project_that_cannot_be_routed_is_not_read_and_a_route_change_rereads(self):
        self.scenario()
        d = self.overview()
        self.assertTrue(d.get("ok"), d)
        start = len(self.entries())
        registry.register(self.sd, self.repo, "br" if self.PROG == "bd" else "bd")    # 印に反する指定 (conflict)
        d = self.overview()
        self.assertEqual((d.get("ok"), d.get("cli"), d.get("ready")), (False, None, []), d)
        self.assertIn("決められない", d.get("error") or "", d)
        self.assertEqual(self.argvs(start), [], "どちらで読むか決められないプロジェクトで CLI を打った")
        registry.register(self.sd, self.repo, self.PROG)
        d = self.overview()
        self.assertTrue(d.get("ok"), "設定が戻ったのにキャッシュの内の読めない結果を返した")
        self.assertEqual(len(self.argvs(start)), 2)

    def test_an_answer_drops_the_cache_and_takes_the_ticket_out_of_ready(self):
        ids = self.scenario()
        port = self.serve()
        self.get(port, "/api/items")                          # 判断待ちの読み (画面は先にこれを読んでいる)
        code, d = self.get(port, "/api/overview")
        self.assertEqual(code, 200, d)
        self.assertIn(ids["p1-human"], [r["id"] for r in d["projects"][0]["ready"]])
        code, j = self.post(port, "/api/answer", {"project": self.repo, "id": ids["p1-human"], "text": "進めてよい"})
        self.assertEqual(code, 200, j)
        start = len(self.entries())
        code, d = self.get(port, "/api/overview")
        self.assertEqual(code, 200, d)
        self.assertNotIn(ids["p1-human"], [r["id"] for r in d["projects"][0]["ready"]], "回答したチケットが ready に残った")
        self.assertEqual([a[:1] for a in self.argvs(start)], [["count"], ["ready"]], "書き込みの後は読み直す")

    def test_rows_without_labels_are_marked_and_left_out_by_the_items_cache(self):
        # ready の行にラベルが無い CLI (本物の bd の ready の形は測っていない) でも、判断待ちの読みのキャッシュの id で、
        # 判断待ちに印を付け、回答済みを除く (CLI は打ち足さない)
        ids = self.scenario()
        snoozed = self.new("p1-snoozed", 1, "human")
        port = self.serve()
        code, j = self.post(port, "/api/snooze", {"project": self.repo, "id": snoozed, "until": None})
        self.assertEqual(code, 200, j)
        code, items = self.get(port, "/api/items")
        self.assertEqual(code, 200, items)
        self.assertEqual([i["id"] for i in items["projects"][0]["snoozed"]], [snoozed], "前提: 保留中に出ていない")
        real = humanq.bd if self.PROG == "bd" else humanq.br

        def no_labels(repo, *args):
            out = real(repo, *args)
            if "ready" in args:
                rows = json.loads(out)
                for r in rows:
                    r.pop("labels", None)
                return json.dumps(rows)
            return out

        with mock.patch.object(humanq, self.PROG, no_labels):
            start = len(self.entries())
            code, d = self.get(port, "/api/overview")
        self.assertEqual(code, 200, d)
        ready = {r["id"]: r for r in d["projects"][0]["ready"]}
        self.assertNotIn(ids["p0-answered"], ready, "回答済みの一覧にあるチケットを ready に出した")
        self.assertTrue(ready.get(ids["p1-human"], {}).get("human"), "判断待ちの一覧にあるチケットに印が付かない")
        self.assertTrue(ready.get(snoozed, {}).get("human"), "保留中の一覧にあるチケットに印が付かない")
        self.assertEqual(ready.get(ids["p1-plain"], {}).get("human"), False, "判断待ちでないチケットに印が付いた")
        self.assertEqual(len(self.argvs(start)), 2, "判断待ちの id を知るために CLI を打ち足した")


class OverviewWithFakeBd(FakeCliOverview, unittest.TestCase):
    PROG = "bd"


class OverviewWithFakeBr(FakeCliOverview, unittest.TestCase):
    PROG = "br"


# ---- hook は見通しを読まない ------------------------------------------------------------------------

class HookDoesNotReadTheOverview(FakeCliProject, unittest.TestCase):
    """SessionStart と UserPromptSubmit の hook が、見通しの API も見通しの CLI (count・ready) も読まないこと。
    hook は本物のサーバーの handler (偽の bd のプロジェクト) に向け、サーバーの起動だけを差し替える。"""

    PROG = "bd"

    def test_hooks_read_only_the_summary(self):
        self.scenario()
        port = self.serve()
        seen = []
        for name in ("overview", "overviews"):
            real = getattr(self.store, name)

            def rec(*a, _real=real, _name=name, **kw):
                seen.append(_name)
                return _real(*a, **kw)
            setattr(self.store, name, rec)
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.sd, "HUMANQ_PORT": str(port)}):
            spec = importlib.util.spec_from_file_location("humanq_hook_overview_test", HOOK_PATH)
            hook = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(hook)
            paths = []
            real_get = hook._get

            def get(path, timeout):
                paths.append(path.partition("?")[0])
                return real_get(path, timeout)

            start = len(self.entries())
            out = io.StringIO()
            with mock.patch.object(hook, "ensure_server", lambda sd: "running"), mock.patch.object(hook, "_get", get), \
                    contextlib.redirect_stdout(out):
                hook.session_start({"cwd": self.repo, "session_id": "s1"})
                hook.prompt_submit({"cwd": self.repo, "session_id": "s1"})
        self.assertIn("判断待ち 1 件", out.getvalue(), "hook が summary を読めていない (比べる対象が無い)")
        self.assertEqual(sorted(set(paths)), ["/api/summary"])
        self.assertEqual(seen, [], "hook の読みで見通しを読んだ")
        self.assertEqual([a for a in self.argvs(start) if a[:1] in (["ready"], ["count"])], [], "hook の読みで見通しの CLI を打った")


if __name__ == "__main__":
    unittest.main()
