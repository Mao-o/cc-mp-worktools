"""プロジェクトごとの CLI (bd / br) の決め方のテスト。

    python3 -m unittest discover -s tests

- ProjectKindTable: registry.project_kind の判定表 (ファイルの印だけ。CLI は打たない)
- Routing: 偽の bd・br (tests/fake_beads.py) を PATH に置き、サーバー・書き込み・summary・hook が
  「br と確定した dir にしか br を打たない (読みだけ。bd は打たない)」「bd の形の dir では br を 1 回も打たない」
  「決められない dir ではどの CLI も打たない」「書き込めない (br・決められない) dir への POST はどの CLI も打たない」
  ことを呼び出しの記録 (FAKE_BEADS_LOG) で確かめる
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
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_server as ts  # noqa: E402  helper だけ借りる (TestCase は名前空間に入れない)

registry = ts.registry
HOOK_PATH = ts.HOOK_PATH

BD_META = {"database": "dolt", "backend": "dolt", "dolt_mode": "embedded"}
SQLITE_META = {"database": "beads.db", "jsonl_export": "issues.jsonl"}


def make_beads(root: str, files: dict | None = None, dirs: tuple = ()) -> str:
    """root/.beads を作る。files は {相対パス: 文字列 | dict (JSON にする)}、dirs は作る空ディレクトリ。"""
    beads = os.path.join(root, ".beads")
    os.makedirs(beads, exist_ok=True)
    for name, body in (files or {}).items():
        path = os.path.join(beads, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    for d in dirs:
        os.makedirs(os.path.join(beads, d), exist_ok=True)
    return root


class ProjectKindTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="humanq-kind-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def kind(self, files=None, dirs=(), cli=None) -> tuple[str, str]:
        self.n += 1
        root = os.path.join(self.tmp, f"p{self.n}")
        make_beads(root, files, dirs)
        try:
            return registry.project_kind(root, cli)
        except Exception as e:  # noqa: BLE001  例外が漏れる変異を ERROR ではなく FAIL に数える
            self.fail(f"project_kind が例外を上げた: {type(e).__name__}: {e}")

    def assertKind(self, want: str, got: tuple[str, str], msg: str = ""):
        self.assertEqual(len(got), 2, got)
        kind, reason = got
        self.assertEqual(kind, want, f"{msg} reason={reason}")
        self.assertIsInstance(reason, str)
        self.assertTrue(reason, "reason が空")

    # ---- br の印 (どれか 1 つで br) ----
    def test_br_mark_gitignore_br_history(self):
        self.assertKind("br", self.kind({".gitignore": "*.log\n.br_history/\n", "metadata.json": SQLITE_META}))

    def test_br_mark_gitignore_fsqlite(self):
        self.assertKind("br", self.kind({".gitignore": "beads.db-fsqlite-ns-*\n"}))

    def test_br_mark_br_history_dir(self):
        self.assertKind("br", self.kind(dirs=(".br_history",)))

    def test_br_mark_fsqlite_ns_file(self):
        self.assertKind("br", self.kind({"beads.db-fsqlite-ns-1234": ""}))

    def test_br_mark_db_lock_file(self):
        self.assertKind("br", self.kind({".br-db-openers-1.lock": ""}))

    def test_br_mark_alone_without_metadata(self):
        # metadata.json が無くても、印だけで決まる
        self.assertKind("br", self.kind({".br-db-x.lock": ""}))

    # ---- bd の印 ----
    def test_bd_mark_metadata_database_dolt(self):
        self.assertKind("bd", self.kind({"metadata.json": {"database": "dolt"}}))
        # 印として数えられていること (印の無い dir も bd になるので、br の印と並べて conflict になるかで見る)
        self.assertKind("conflict", self.kind({"metadata.json": {"database": "dolt"}, ".br-db-x.lock": ""}))

    def test_bd_mark_metadata_backend_dolt(self):
        self.assertKind("bd", self.kind({"metadata.json": {"database": "other", "backend": "dolt"}}))
        self.assertKind("conflict", self.kind({"metadata.json": {"database": "other", "backend": "dolt"}, ".br-db-x.lock": ""}))

    def test_bd_mark_embeddeddolt_dir(self):
        self.assertKind("bd", self.kind({"metadata.json": SQLITE_META}, dirs=("embeddeddolt",)))

    # ---- 両方 / 印なし ----
    def test_both_marks_is_conflict(self):
        self.assertKind("conflict", self.kind({"metadata.json": BD_META, ".br-db-x.lock": ""}))
        self.assertKind("conflict", self.kind({".gitignore": ".br_history/\n"}, dirs=("embeddeddolt",)))

    def test_sqlite_shape_without_marks_is_unknown(self):
        self.assertKind("unknown", self.kind({"metadata.json": SQLITE_META}))
        self.assertKind("unknown", self.kind({"metadata.json": {"database": "old.db"}}))

    def test_nothing_is_bd(self):
        self.assertKind("bd", self.kind())
        self.assertKind("bd", self.kind({"metadata.json": "{not json"}))
        self.assertKind("bd", self.kind({"metadata.json": "[1]"}))
        self.assertKind("bd", self.kind({"metadata.json": {"database": 3}}))

    def test_issues_jsonl_is_not_a_mark(self):
        self.assertKind("bd", self.kind({"issues.jsonl": "", "metadata.json": {"jsonl_export": "issues.jsonl"}}))
        self.assertKind("unknown", self.kind({"issues.jsonl": "", "metadata.json": SQLITE_META}))

    def test_comment_line_in_gitignore_is_not_a_mark(self):
        self.assertKind("bd", self.kind({".gitignore": "# .br_history と fsqlite は br のもの\n*.tmp\n"}))

    def test_unrelated_gitignore_is_not_a_mark(self):
        self.assertKind("bd", self.kind({".gitignore": "*.db\ndolt/\n"}))

    # ---- 明示の設定 ----
    def test_explicit_agrees_with_marks(self):
        self.assertKind("br", self.kind({".br-db-x.lock": ""}, cli="br"))
        self.assertKind("bd", self.kind({"metadata.json": BD_META}, cli="bd"))

    def test_explicit_without_marks_is_as_told(self):
        self.assertKind("br", self.kind(cli="br"))
        self.assertKind("bd", self.kind(cli="bd"))
        self.assertKind("br", self.kind({"metadata.json": SQLITE_META}, cli="br"))
        self.assertKind("bd", self.kind({"metadata.json": SQLITE_META}, cli="bd"))

    def test_explicit_against_marks_is_conflict(self):
        self.assertKind("conflict", self.kind({"metadata.json": BD_META}, cli="br"))
        self.assertKind("conflict", self.kind(dirs=("embeddeddolt",), cli="br"))
        self.assertKind("conflict", self.kind({".br-db-x.lock": ""}, cli="bd"))
        self.assertKind("conflict", self.kind({".gitignore": "fsqlite\n"}, cli="bd"))

    def test_explicit_with_both_marks_is_conflict(self):
        both = {"metadata.json": BD_META, ".br-db-x.lock": ""}
        self.assertKind("conflict", self.kind(both, cli="br"))
        self.assertKind("conflict", self.kind(both, cli="bd"))

    def test_auto_and_invalid_values_read_like_unset(self):
        for raw in (None, "", "auto", " AUTO ", "foo", "bd,br", "0"):
            self.assertKind("br", self.kind({".br-db-x.lock": ""}, cli=raw), repr(raw))
            self.assertKind("unknown", self.kind({"metadata.json": SQLITE_META}, cli=raw), repr(raw))
            self.assertKind("conflict", self.kind({"metadata.json": BD_META, ".br-db-x.lock": ""}, cli=raw), repr(raw))

    def test_explicit_is_trimmed_and_case_insensitive(self):
        self.assertKind("br", self.kind(cli=" BR "))
        self.assertKind("bd", self.kind(cli="Bd"))

    def test_cli_setting_table(self):
        rows = [(None, (None, True)), ("", (None, True)), ("auto", (None, True)), ("AUTO", (None, True)),
                ("bd", ("bd", True)), ("br", ("br", True)), (" br ", ("br", True)), ("BD", ("bd", True)),
                ("foo", (None, False)), ("bd br", (None, False)), (3, (None, False))]
        for raw, want in rows:
            self.assertEqual(registry.cli_setting(raw), want, repr(raw))

    def test_project_kind_never_runs_a_cli(self):
        # 印を読むだけ: PATH に偽の bd・br を置いても、1 回も呼ばれない
        bin_tmp = tempfile.mkdtemp(prefix="humanq-kind-bin-")
        self.addCleanup(shutil.rmtree, bin_tmp, True)
        for prog in ("bd", "br"):
            ts.install_fake(bin_tmp, prog)
        log = os.path.join(self.tmp, "calls.jsonl")
        env = {"PATH": bin_tmp + os.pathsep + os.environ.get("PATH", ""), "FAKE_BEADS_LOG": log}
        with mock.patch.dict(os.environ, env):
            for files in ({"metadata.json": BD_META}, {".br-db-x.lock": ""}, {"metadata.json": SQLITE_META}):
                self.kind(files)
        self.assertFalse(os.path.exists(log), "project_kind が CLI を起動した")


class RegistryEntry(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp(prefix="humanq-reg-")
        self.addCleanup(shutil.rmtree, self.sd, True)

    def entry(self, root: str) -> dict:
        return json.loads(Path(self.sd, "projects.json").read_text(encoding="utf-8"))["projects"][root]

    def test_register_writes_cli_or_null(self):
        registry.register(self.sd, "/x/a")
        self.assertEqual(self.entry("/x/a")["cli"], None)
        self.assertIn("last_seen", self.entry("/x/a"))
        registry.register(self.sd, "/x/a", "br")
        self.assertEqual(self.entry("/x/a")["cli"], "br")
        registry.register(self.sd, "/x/a", "bd")
        self.assertEqual(self.entry("/x/a")["cli"], "bd")
        registry.register(self.sd, "/x/a", "nonsense")
        self.assertIsNone(self.entry("/x/a")["cli"], "不正な値は auto (null) で書く")

    def test_project_cli_reads_entry_and_ignores_bad_ones(self):
        registry.register(self.sd, "/x/a", "br")
        registry.register(self.sd, "/x/b")
        self.assertEqual(registry.project_cli(self.sd, "/x/a"), "br")
        self.assertIsNone(registry.project_cli(self.sd, "/x/b"))
        self.assertIsNone(registry.project_cli(self.sd, "/x/none"))
        Path(self.sd, "projects.json").write_text(json.dumps({"projects": {"/x/c": {"cli": "zzz"}, "/x/d": "str", "/x/e": {"cli": 5}}}))
        for r in ("/x/c", "/x/d", "/x/e"):
            self.assertIsNone(registry.project_cli(self.sd, r), r)
        Path(self.sd, "projects.json").write_text("[]")
        self.assertIsNone(registry.project_cli(self.sd, "/x/c"))
        Path(self.sd, "projects.json").write_text(json.dumps({"projects": ["/x/c"]}))
        self.assertIsNone(registry.project_cli(self.sd, "/x/c"))


class Routing(unittest.TestCase):
    """偽の bd・br を PATH に置いて、サーバー・hook が打つ CLI を呼び出しの記録で確かめる。"""

    @classmethod
    def setUpClass(cls):
        cls.bin_tmp = tempfile.mkdtemp(prefix="humanq-kind-fakebin-")
        cls.bin = os.path.join(cls.bin_tmp, "bin")
        os.makedirs(cls.bin)
        for prog in ("bd", "br"):
            ts.install_fake(cls.bin, prog)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.bin_tmp, ignore_errors=True)

    def setUp(self):
        leaked = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.realpath(d) in ts.FAKE_BIN_DIRS]
        self.assertEqual(leaked, [], "前のテストの偽の CLI が PATH に残っている")
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-route-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.sd = os.path.join(self.tmp, "state")
        os.makedirs(self.sd)
        env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
                                           "FAKE_BEADS_LOG": self.log, "CLAUDE_PLUGIN_DATA": self.sd})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FAKE_BEADS_FAIL", None)
        os.environ.pop(registry.CLI_ENV, None)

    # ---- プロジェクトの作り方 ----
    def bd_project(self, name: str = "bdp") -> str:
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        ts.bdc(root, "init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq")
        make_beads(root, dirs=("embeddeddolt",))      # 本物の bd が作る印 (偽の init は metadata.json だけ作る)
        return root

    def br_project(self, name: str = "brp") -> str:
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        p = subprocess.run(["br", "init"], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        make_beads(root, {".gitignore": ".br_history/\n*fsqlite*\n"})   # br init が書く .gitignore の印
        return root

    def sqlite_project(self, name: str = "sqlp") -> str:
        return make_beads(os.path.join(self.tmp, name), {"metadata.json": SQLITE_META})

    def reset_log(self):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.log)

    def calls(self) -> list[dict]:
        try:
            lines = Path(self.log).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(x) for x in lines if x.strip()]

    def progs(self) -> set[str]:
        return {c["prog"] for c in self.calls()}

    def store(self, *roots_and_cli) -> "ts.humanq.Store":
        for item in roots_and_cli:
            root, cli = item if isinstance(item, tuple) else (item, None)
            registry.register(self.sd, root, cli)
        return ts.humanq.Store(self.sd)

    def project_of(self, store, root: str) -> dict:
        got = [p for p in store.all() if p["path"] == root]
        self.assertEqual(len(got), 1, got)
        self.assertLessEqual({"ok", "cli", "writable", "human", "answered"}, set(got[0]), got[0])
        return got[0]

    def assertNoDoltFiles(self, root: str):
        beads = os.path.join(root, ".beads")
        self.assertEqual(sorted(n for n in os.listdir(beads) if n.startswith("dolt")), [], "br が .beads/ に dolt のファイルを作った")

    # ---- サーバーが起動する CLI ----
    def test_bd_shape_reads_with_bd_only(self):
        root = self.bd_project()
        self.reset_log()
        p = self.project_of(self.store(root), root)
        self.assertLessEqual({"ok", "cli", "writable", "human"}, set(p), p)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (True, "bd", True), p)
        self.assertEqual(self.progs(), {"bd"}, "bd の形の dir で br が呼ばれた")
        self.assertNoDoltFiles(root)

    def assertBrOnlyAt(self, root: str):
        """記録の全部が、root を cwd にした br の呼び出しであること (bd も、ほかの dir の br も無い)。"""
        calls = self.calls()
        self.assertNotEqual(calls, [], "br を一度も打っていない")
        self.assertEqual({c["prog"] for c in calls}, {"br"}, "br の dir で bd が呼ばれた")
        self.assertEqual({c["cwd"] for c in calls}, {root}, "br を別の dir で打った")

    def test_br_shape_reads_with_br_only(self):
        root = self.br_project()
        self.reset_log()
        p = self.project_of(self.store(root), root)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (True, "br", False), p)
        self.assertNotIn("error", p)
        self.assertEqual((p["human"], p["answered"]), ([], []))
        self.assertBrOnlyAt(root)
        self.assertNoDoltFiles(root)

    def test_sqlite_shape_is_unknown_and_starts_no_cli(self):
        root = self.sqlite_project()
        self.reset_log()
        p = self.project_of(self.store(root), root)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (False, None, False), p)
        self.assertIn("bd と br のどちらで読むか決められない (", p["error"])
        self.assertIn("HUMANQ_BEADS_CLI で指定してください", p["error"])
        self.assertIn("beads.db", p["error"], "reason が入っていない")
        self.assertEqual(self.calls(), [])

    def test_conflict_starts_no_cli(self):
        root = self.bd_project()
        make_beads(root, dirs=(".br_history",))
        self.reset_log()
        p = self.project_of(self.store(root), root)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (False, None, False), p)
        self.assertIn("決められない", p["error"])
        self.assertEqual(self.calls(), [], "conflict なのに CLI が起動された")

    def test_br_setting_on_bd_shape_is_conflict_and_makes_no_dolt_files(self):
        root = self.bd_project()
        self.reset_log()
        shutil.rmtree(os.path.join(root, ".beads", "embeddeddolt"))
        make_beads(root, {"metadata.json": BD_META})
        p = self.project_of(self.store((root, "br")), root)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (False, None, False), p)
        self.assertIn("HUMANQ_BEADS_CLI=br", p["error"])
        self.assertEqual(self.calls(), [], "br を bd の形に向けた")
        self.assertNoDoltFiles(root)

    def test_br_setting_on_unmarked_dir_is_br_and_reads_with_br(self):
        root = self.sqlite_project()
        p = self.project_of(self.store((root, "br")), root)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (True, "br", False), p)
        self.assertBrOnlyAt(root)

    def test_bd_setting_on_sqlite_shape_reads_with_bd(self):
        # 明示の bd は、決められない形を bd として読む (bd は SQLite の形なので失敗するが、読みに行く)
        root = self.sqlite_project()
        p = self.project_of(self.store((root, "bd")), root)
        self.assertEqual((p["cli"], p["writable"]), ("bd", True), p)
        self.assertEqual(self.progs(), {"bd"})
        self.assertFalse(p["ok"])
        self.assertIn("historical SQLite", p["error"])

    def test_bd_setting_on_br_shape_is_conflict(self):
        root = self.br_project()
        self.reset_log()
        p = self.project_of(self.store((root, "bd")), root)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (False, None, False), p)
        self.assertEqual(self.calls(), [])

    def test_invalid_setting_in_projects_json_reads_as_auto(self):
        br, bd = self.br_project(), self.bd_project()
        self.store(br, bd)
        data = json.loads(Path(self.sd, "projects.json").read_text())
        for root in (br, bd):
            data["projects"][root]["cli"] = "zzz"
        Path(self.sd, "projects.json").write_text(json.dumps(data))
        self.reset_log()
        st = ts.humanq.Store(self.sd)
        self.assertEqual(self.project_of(st, br)["cli"], "br")
        self.assertEqual(self.project_of(st, bd)["cli"], "bd")
        self.assertEachCliAtItsOwnDir(br=br, bd=bd)

    def assertEachCliAtItsOwnDir(self, br: str, bd: str):
        """br は br の dir を cwd にしてだけ、bd は -C で bd の dir を指してだけ打たれた (どちらも 1 回以上)。"""
        calls = self.calls()
        self.assertEqual({c["prog"] for c in calls}, {"bd", "br"}, calls)
        for c in calls:
            if c["prog"] == "br":
                self.assertEqual(c["cwd"], br, c)
                self.assertNotIn(bd, c["argv"], c)
            else:
                self.assertIn("-C", c["argv"], c)
                self.assertEqual(c["argv"][c["argv"].index("-C") + 1], bd, c)
                self.assertNotIn(br, c["argv"], c)

    def test_mixed_projects_each_use_their_own_cli(self):
        br, bd, sql = self.br_project(), self.bd_project(), self.sqlite_project()
        self.reset_log()
        st = self.store(br, bd, sql)
        by = {p["name"]: p for p in st.all()}
        for p in by.values():
            self.assertLessEqual({"ok", "cli", "writable"}, set(p), p)
        self.assertEqual({k: (v["ok"], v["cli"], v["writable"]) for k, v in by.items()},
                         {"brp": (True, "br", False), "bdp": (True, "bd", True), "sqlp": (False, None, False)})
        self.assertEachCliAtItsOwnDir(br=br, bd=bd)
        self.assertTrue(all(sql not in c["argv"] and c["cwd"] != sql for c in self.calls()), "決められない dir に CLI が起動された")

    # ---- 書き込み (HTTP) ----
    def serve(self) -> tuple[int, str]:
        port = ts.free_port()
        proc = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, proc)
        return port, ts.page_token(port)

    def post(self, port: int, token: str, path: str, body: dict) -> tuple[int, dict]:
        code, _, raw = ts.fetch(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json", "X-Humanq-Token": token})
        return code, json.loads(raw)

    def test_post_to_unwritable_projects_is_400_readonly_without_cli(self):
        br, sql = self.br_project(), self.sqlite_project()
        conflict = self.bd_project("cfp")
        make_beads(conflict, dirs=(".br_history",))
        for r in (br, sql, conflict):
            registry.register(self.sd, r)
        self.reset_log()
        port, token = self.serve()
        for r in (br, sql, conflict):
            for path, op, body in (("/api/answer", "answer", {"id": "x-1", "option": "a"}),
                                   ("/api/undo", "undo", {"id": "x-1"})):
                code, j = self.post(port, token, path, {"project": r, **body})
                self.assertEqual((code, j.get("code"), j.get("op")), (400, "readonly", op), (r, path, j))
        self.assertEqual(self.calls(), [], "writable:false への POST で CLI が起動された")

    def test_post_to_bd_project_is_not_readonly(self):
        root = self.bd_project()
        registry.register(self.sd, root)
        port, token = self.serve()
        code, j = self.post(port, token, "/api/answer", {"project": root, "id": "hq-nothing", "option": "a"})
        self.assertEqual(code, 400, j)
        self.assertEqual(j.get("code"), "gone", j)       # readonly ではなく、チケットが無いという通常の失敗
        self.assertIn("bd", self.progs())

    def test_items_have_cli_and_writable_and_keep_old_keys(self):
        bd, br = self.bd_project(), self.br_project()
        for r in (bd, br):
            registry.register(self.sd, r)
        port, _ = self.serve()
        code, _, raw = ts.fetch(f"http://127.0.0.1:{port}/api/items")
        self.assertEqual(code, 200)
        by = {p["name"]: p for p in json.loads(raw)["projects"]}
        for p in by.values():
            self.assertLessEqual({"path", "name", "ok", "human", "answered", "cli", "writable"}, set(p), p)
        self.assertEqual((by["bdp"]["cli"], by["bdp"]["writable"], by["brp"]["cli"], by["brp"]["writable"]), ("bd", True, "br", False))

    # ---- summary ----
    def summary(self, port: int, root: str) -> dict:
        from urllib.parse import quote
        code, _, raw = ts.fetch(f"http://127.0.0.1:{port}/api/summary?project={quote(root, safe='')}&fresh=1")
        self.assertEqual(code, 200, raw)
        return json.loads(raw)

    def test_summary_has_ok_and_error(self):
        bd, br, sql = self.bd_project(), self.br_project(), self.sqlite_project()
        for r in (bd, br, sql):
            registry.register(self.sd, r)
        port, _ = self.serve()
        for r, cli, writable in ((bd, "bd", True), (br, "br", False)):
            s = self.summary(port, r)
            self.assertLessEqual({"ok", "error", "human", "answered", "cli", "writable"}, set(s), s)
            self.assertEqual((s["ok"], s["human"], s["answered"], s["cli"], s["writable"]), (True, 0, [], cli, writable), s)
            self.assertIsNone(s["error"])
        s = self.summary(port, sql)
        self.assertLessEqual({"ok", "error", "human", "answered", "cli"}, set(s), s)
        self.assertIs(s["ok"], False, s)
        self.assertIn("決められない", s["error"])
        self.assertIsNone(s["cli"])
        self.assertEqual((s["human"], s["answered"]), (0, []), "既存の key を残す")

    # ---- hook ----
    def load_hook(self, port: int):
        spec = importlib.util.spec_from_file_location("humanq_hook_route_test", HOOK_PATH)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        hook.PORT = port
        hook.URL = f"http://127.0.0.1:{port}/"
        return hook

    def run_hook(self, hook, fn: str, cwd: str, session: str = "s1") -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            getattr(hook, fn)({"cwd": cwd, "session_id": session})
        return buf.getvalue()

    def context_of(self, out: str) -> str:
        self.assertTrue(out.strip(), "hook の出力が無い")
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_hook_says_unreadable_not_zero(self):
        sql = self.sqlite_project()
        self.reset_log()
        port = ts.free_port()
        proc = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, proc)
        hook = self.load_hook(port)
        out = self.run_hook(hook, "session_start", sql)
        ctx = self.context_of(out)
        self.assertIn("このプロジェクトの判断待ちを読めない: bd と br のどちらで読むか決められない (", ctx)
        self.assertNotIn("0 件", ctx)
        self.assertNotIn("0 件", out)
        self.assertEqual(self.calls(), [])
        self.assertIsNone(registry.project_cli(self.sd, sql))

    def test_hook_passes_the_setting_to_the_registry(self):
        bd = self.bd_project()
        self.reset_log()
        port = ts.free_port()
        proc = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, proc)
        hook = self.load_hook(port)
        os.environ[registry.CLI_ENV] = "br"
        ctx = self.context_of(self.run_hook(hook, "session_start", bd))
        self.assertEqual(registry.project_cli(self.sd, bd), "br")
        self.assertIn("決められない", ctx)
        self.assertNotIn("0 件", ctx)
        self.assertEqual(self.calls(), [], "br を bd の形に向けた")
        os.environ[registry.CLI_ENV] = "auto"
        ctx = self.context_of(self.run_hook(hook, "session_start", bd))
        self.assertIsNone(registry.project_cli(self.sd, bd))
        self.assertIn("判断待ち 0 件", ctx)         # 読めたときの 0 件は 0 件と言う

    def test_hook_warns_once_on_invalid_setting_and_treats_as_auto(self):
        bd, br = self.bd_project(), self.br_project()
        port = ts.free_port()
        proc = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, proc)
        hook = self.load_hook(port)
        os.environ[registry.CLI_ENV] = "zzz"
        for root in (bd, br):
            ctx = self.context_of(self.run_hook(hook, "session_start", root))
            warns = [ln for ln in ctx.splitlines() if registry.CLI_ENV in ln]
            self.assertEqual(len(warns), 1, ctx)
            self.assertIn("auto", warns[0])
            self.assertIsNone(registry.project_cli(self.sd, root))
        ts.fetch(f"http://127.0.0.1:{port}/api/ping")
        os.environ.pop(registry.CLI_ENV)
        ctx = self.context_of(self.run_hook(hook, "session_start", bd))
        self.assertNotIn(registry.CLI_ENV, ctx, "設定が正しいときは警告を出さない")

    def test_hook_warns_when_server_is_down_too(self):
        bd = self.bd_project()
        hook = self.load_hook(ts.free_port())
        os.environ[registry.CLI_ENV] = "zzz"
        with mock.patch.object(hook, "ensure_server", lambda sd: "port-busy"):
            ctx = self.context_of(self.run_hook(hook, "session_start", bd))
        self.assertIn("サーバー: port-busy", ctx)
        self.assertEqual(len([ln for ln in ctx.splitlines() if registry.CLI_ENV in ln]), 1, ctx)

    def test_prompt_submit_keeps_remembered_answers_when_unreadable(self):
        sql = self.sqlite_project()
        registry.register(self.sd, sql)
        port = ts.free_port()
        proc = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, proc)
        hook = self.load_hook(port)
        s = hook.summary(sql, 5.0, fresh=True)         # prompt_submit が見るキャッシュを埋める (無いと pending で何もせず戻る)
        self.assertIsNotNone(s)
        self.assertIs(s.get("ok"), False, s)
        seen = hook._seen_path(self.sd, "s9")
        Path(seen).write_text(json.dumps(["hq-1"]))
        self.assertEqual(self.run_hook(hook, "prompt_submit", sql, "s9"), "")
        self.assertEqual(json.loads(Path(seen).read_text()), ["hq-1"], "読めなかった応答で、伝えた回答の記憶を消した")


if __name__ == "__main__":
    unittest.main()
