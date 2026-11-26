"""br のプロジェクトの読みを、偽の br・bd (tests/fake_beads.py) で端から端まで確かめる。
回答・取り消しの書き込みのシナリオは test_server.py の WithFakeBr (bd と同じシナリオを偽の br で回す) にある。

    python3 -m unittest discover tests

- サーバーは br のプロジェクトを br の list 2 回と show 1 回で読み、{ok:true, cli:"br", writable:true} で返す
- br はプロジェクトの dir を cwd にして打ち、bd は 1 回も打たない
- 偽の br はコメントを新しい順で返す (本物の並びは決まっていない)。古い順を前提にした読みはここで落ちる
- hook は br のプロジェクトでは件数だけを伝え、bd のコマンドを含む案内を出さない
BrBackend の細かい約束 (出力の形・再試行・コメントの並べ替え) は test_backend.py にある。
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
humanq = ts.humanq

LIST_HUMAN = ["list", "-l", "human", "--limit", "0", "--json"]          # status では絞らない (br の既定のまま)
LIST_ANSWERED = ["list", "-l", "answered", "--limit", "0", "--json"]
Q = ts.QUESTION["humanq"]
CTX = json.dumps({"other": {"keep": True}, "humanq": Q})     # agent_context の top-level に他の key もある
ANS_A = "[humanq] 回答: 案A (a)\n"
ANS_B = "[humanq] 回答: 案B (b)\n"
UNDO = "[humanq] 取り消し\n"
BD_WORDS = ("`bd", "bd comments", "bd label", "metadata")      # br のプロジェクトの hook の出力に出してはいけない語


class BrRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bin_tmp = tempfile.mkdtemp(prefix="humanq-brread-fakebin-")
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
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-brread-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.sd = os.path.join(self.tmp, "state")
        os.makedirs(self.sd)
        env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
                                           "FAKE_BEADS_LOG": self.log, "CLAUDE_PLUGIN_DATA": self.sd})
        env.start()
        self.addCleanup(env.stop)
        for name in ("FAKE_BEADS_FAIL", registry.CLI_ENV):
            os.environ.pop(name, None)
        self.n = 0

    # ---- 偽の br のプロジェクト ----
    def brc(self, root: str, *args: str) -> str:
        p = subprocess.run(["br", *args], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, f"br {args}: {p.stderr or p.stdout}")
        return p.stdout

    def br_project(self, name: str = "brp") -> str:
        root = os.path.join(self.tmp, name)
        os.makedirs(root)
        self.brc(root, "init", "--prefix", "pq")
        Path(root, ".beads", ".gitignore").write_text(".br_history/\n*-fsqlite-ns-gate\n", encoding="utf-8")   # br init が書く印
        return root

    def file(self, text: str) -> str:
        self.n += 1
        path = os.path.join(self.tmp, f"f{self.n}.txt")
        Path(path).write_text(text, encoding="utf-8")
        return path

    def create(self, root: str, title: str, labels: str, ctx: str | None = None, p: int = 1, **kw) -> str:
        created = json.loads(self.brc(root, "create", title, "-p", str(p), "-l", labels, "--json"))
        self.assertIn("id", created)
        iid = created["id"]
        if ctx is not None:
            self.brc(root, "update", iid, "--agent-context", "@" + self.file(ctx))
        for flag, value in kw.items():
            self.brc(root, "update", iid, f"--{flag}", value)
        return iid

    def comment(self, root: str, iid: str, text: str, actor: str = "user/humanq") -> None:
        self.brc(root, "--actor", actor, "comments", "add", iid, "-f", self.file(text))

    def calls(self) -> list[dict]:
        try:
            lines = Path(self.log).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(x) for x in lines if x.strip()]

    def reset_log(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.log)

    def read(self, root: str) -> dict:
        registry.register(self.sd, root)
        try:
            got = [p for p in humanq.Store(self.sd).all() if p["path"] == root]
        except Exception as e:  # noqa: BLE001  実装の変異で漏れた例外を、走っていない ERROR にしない
            self.fail(f"Store.all が例外を上げた: {type(e).__name__}: {e}")
        self.assertEqual(len(got), 1, got)
        self.assertLessEqual({"ok", "cli", "writable", "human", "answered"}, set(got[0]), got[0])
        return got[0]

    def assertBrOnly(self, root: str) -> list[list[str]]:
        calls = self.calls()
        self.assertEqual({c["prog"] for c in calls}, {"br"}, "br のプロジェクトで bd が呼ばれた")
        self.assertEqual({c["cwd"] for c in calls}, {root}, "br を別の dir で打った")
        return [c["argv"] for c in calls]

    # ---- サーバーの読み ----
    def test_questions_and_answers_are_read_with_two_lists_and_one_show(self):
        root = self.br_project()
        q1 = self.create(root, "問いのある判断待ち", "human", CTX, p=1, notes="メモ")
        a2 = self.create(root, "回答済み", "answered", p=2)
        for text, actor in ((ANS_A, "user/humanq"), (UNDO, "user/humanq"), ("エージェントの問い直し\n", "agent"),
                            (ANS_B, "user/humanq")):
            self.comment(root, a2, text, actor)
        b3 = self.create(root, "両方のラベル", "human,answered", p=0)
        self.comment(root, b3, ANS_A)
        self.comment(root, b3, UNDO)                                    # 最後が取り消し: 判断待ちに出す
        c4 = self.create(root, "閉じた回答済み", "answered")
        self.brc(root, "close", c4)
        d5 = self.create(root, "保留の判断待ち", "human", p=3)
        self.brc(root, "update", d5, "--status", "deferred")
        self.reset_log()

        p = self.read(root)
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (True, "br", True), p)
        self.assertEqual([i["id"] for i in p["human"]], [b3, q1, d5], "優先度の順。両方のラベルで最後が取り消しなら判断待ち")
        self.assertEqual([i["id"] for i in p["answered"]], [a2], "closed は読まない")
        h = {i["id"]: i for i in p["human"]}
        self.assertEqual(h[q1]["q"], humanq.question({"humanq": Q}), "agent_context の key humanq が問いになる")
        self.assertEqual(h[q1]["qsig"], humanq.qsig(humanq.question({"humanq": Q})))
        self.assertEqual((h[q1]["notes"], h[q1]["title"]), ("メモ", "問いのある判断待ち"))
        self.assertIsNone(h[d5]["q"], "agent_context の無いチケットは問いが未整理")
        a = p["answered"][0]
        self.assertEqual((a["answer"], a["asig"]), (ANS_B, humanq.asig(3, ANS_B)),
                         "偽の br は新しい順で返す。最後の [humanq] コメント (案B) が回答")
        self.assertEqual(self.assertBrOnly(root), [LIST_HUMAN, LIST_ANSWERED, ["show", q1, b3, d5, a2, "--json"]])
        self.assertEqual(sorted(n for n in os.listdir(os.path.join(root, ".beads")) if n.startswith("dolt")), [])

    def test_tickets_in_every_unfinished_status_are_read(self):
        # br 0.7.6: `br list -l human` (既定) は closed と tombstone だけを除く。組み込みの draft・pinned と、
        # update --status が宣言なしで受け付ける独自の status (review・打ち間違いの in-progress) も出る。
        # 画面 (と、同じ読みから数える hook の件数) にも同じものを出す。回答済みの側も同じ
        unfinished = ("open", "in_progress", "blocked", "deferred", "draft", "pinned", "review", "in-progress")
        root = self.br_project()
        want: dict[str, list[str]] = {"human": [], "answered": []}
        for label in want:
            for st in unfinished:
                iid = self.create(root, f"{label} {st}", label)
                if st != "open":
                    self.brc(root, "update", iid, "--status", st)
                want[label].append(iid)
            self.brc(root, "close", self.create(root, f"{label} closed", label))
            self.brc(root, "delete", self.create(root, f"{label} deleted", label))      # tombstone になる
        self.reset_log()
        p = self.read(root)
        self.assertEqual((p["ok"], p["cli"]), (True, "br"), p)
        for label in want:
            with self.subTest(label):
                self.assertEqual(sorted(i["id"] for i in p[label]), sorted(want[label]),
                                 "終わっていない status のチケットが消えた / closed か tombstone が出た")
        self.assertEqual(self.assertBrOnly(root)[:2], [LIST_HUMAN, LIST_ANSWERED])

    def test_an_empty_project_is_read_without_show(self):
        root = self.br_project()
        self.reset_log()
        p = self.read(root)
        self.assertEqual((p["ok"], p["cli"], p["writable"], p["human"], p["answered"]), (True, "br", True, [], []), p)
        self.assertEqual(self.assertBrOnly(root), [LIST_HUMAN, LIST_ANSWERED])

    def test_a_failing_br_makes_only_that_project_unreadable(self):
        br = self.br_project()
        self.create(br, "q", "human", CTX)
        bd = os.path.join(self.tmp, "bdp")
        os.makedirs(bd)
        ts.bdc(bd, "init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq")
        os.makedirs(os.path.join(bd, ".beads", "embeddeddolt"))
        registry.register(self.sd, bd)
        os.environ["FAKE_BEADS_FAIL"] = "show"
        self.reset_log()
        p = self.read(br)
        self.assertEqual((p["ok"], p["cli"], p["writable"], p["human"]), (False, "br", True, []), p)
        self.assertIn("injected failure (show)", p["error"])
        self.assertEqual(len([c for c in self.calls() if c["prog"] == "br"]), 3, "exit 1 の show は再試行しない")
        other = [x for x in humanq.Store(self.sd).all() if x["path"] == bd]
        self.assertEqual(len(other), 1)
        self.assertEqual((other[0]["ok"], other[0]["cli"]), (True, "bd"))

    # ---- HTTP と hook ----
    def serve(self) -> int:
        port = ts.free_port()
        proc = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, proc)
        return port

    def load_hook(self, port: int):
        spec = importlib.util.spec_from_file_location("humanq_hook_br_test", ts.HOOK_PATH)
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

    def hook_out(self, out: str) -> dict:
        self.assertTrue(out.strip(), "hook の出力が無い")
        try:
            o = json.loads(out)
        except ValueError:
            self.fail(f"hook の出力が JSON でない: {out!r}")
        self.assertIn("hookSpecificOutput", o)
        return o

    def test_items_and_summary_over_http(self):
        root = self.br_project()
        q1 = self.create(root, "問い", "human", CTX)
        a2 = self.create(root, "回答済み", "answered")
        self.comment(root, a2, ANS_A)
        registry.register(self.sd, root)
        port = self.serve()
        code, _, raw = ts.fetch(f"http://127.0.0.1:{port}/api/items")
        self.assertEqual(code, 200, raw)
        ps = json.loads(raw)["projects"]
        self.assertEqual(len(ps), 1, ps)
        p = ps[0]
        self.assertEqual((p["ok"], p["cli"], p["writable"]), (True, "br", True), p)
        self.assertEqual(([i["id"] for i in p["human"]], [i["id"] for i in p["answered"]]), ([q1], [a2]))
        self.assertEqual(p["human"][0]["q"]["ask"], Q["ask"])
        self.assertEqual(p["answered"][0]["answer"], ANS_A)
        from urllib.parse import quote
        code, _, raw = ts.fetch(f"http://127.0.0.1:{port}/api/summary?project={quote(root, safe='')}&fresh=1")
        self.assertEqual(code, 200, raw)
        s = json.loads(raw)
        self.assertEqual({k: s.get(k) for k in ("ok", "cli", "writable", "human", "answered")},
                         {"ok": True, "cli": "br", "writable": True, "human": 1, "answered": [a2]})
        self.assertEqual({c["prog"] for c in self.calls() if c["cwd"] == root or root in c["argv"]}, {"br"})

    def forget(self, hook, session: str, ids: list[str]) -> None:
        """このセッションで前に伝えた回答済みを ids だけにする (サーバーのキャッシュは数秒有効なので、チケットを後から
        足す代わりに、伝えた記憶の側を減らして「増えた」を作る)。"""
        Path(hook._seen_path(self.sd, session)).write_text(json.dumps(ids), encoding="utf-8")

    def test_hook_tells_counts_without_bd_commands(self):
        root = self.br_project()
        self.create(root, "問い", "human", CTX)
        a2 = self.create(root, "回答済み", "answered")
        a3 = self.create(root, "もう 1 件の回答済み", "answered")
        port = self.serve()
        hook = self.load_hook(port)
        o = self.hook_out(self.run_hook(hook, "session_start", root, "s-br"))
        ctx = o["hookSpecificOutput"]["additionalContext"]
        self.assertIn("このプロジェクト (br)", ctx)
        self.assertIn("ユーザーは手が空いたときにそこで答える", ctx, "br のプロジェクトも画面から回答できる (0.15.0)")
        self.assertNotIn("回答できない", ctx)
        self.assertIn(f"判断待ち 1 件・answered ラベルの付いたもの 2 件 ({a2}, {a3})", ctx)
        for word in BD_WORDS:
            self.assertNotIn(word, ctx, f"br のプロジェクトに bd の案内 ({word}) を出した")
        self.assertEqual(o.get("systemMessage"), f"humanq: 判断待ち 1 件 / 回答済み 2 件 → {hook.URL}")

        self.forget(hook, "s-br", [a2])
        o = self.hook_out(self.run_hook(hook, "prompt_submit", root, "s-br"))
        ctx = o["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith(f"humanq: 前に伝えた後で、answered ラベルの付いたチケットが 1 件増えた: {a3}。"), ctx)
        self.assertIn("(created_at, id) の昇順に並べ直し", ctx)
        self.assertEqual(self.run_hook(hook, "prompt_submit", root, "s-br"), "", "増えたものが無いのに知らせた")
        self.assertEqual({c["prog"] for c in self.calls() if c["cwd"] == root or root in c["argv"]}, {"br"})

    def test_hook_with_an_old_read_only_server_says_it_cannot_answer(self):
        # br を読みだけで返す古いサーバー (writable:false) が動いているときは、回答できないと伝える (bd の案内は出さない)
        root = self.br_project()
        hook = self.load_hook(ts.free_port())
        old = {"human": 1, "answered": ["pq-2"], "ok": True, "error": None, "cli": "br", "writable": False}
        with mock.patch.object(hook, "ensure_server", lambda sd: "running"), \
                mock.patch.object(hook, "summary", lambda *a, **kw: dict(old)):
            o = self.hook_out(self.run_hook(hook, "session_start", root, "s-old"))
        ctx = o["hookSpecificOutput"]["additionalContext"]
        self.assertIn("このプロジェクト (br)", ctx)
        self.assertIn("画面からは回答できない", ctx)
        self.assertNotIn("そこで答える", ctx)
        self.assertIn("判断待ち 1 件・answered ラベルの付いたもの 1 件 (pq-2)", ctx)
        for word in BD_WORDS:
            self.assertNotIn(word, ctx, f"br のプロジェクトに bd の案内 ({word}) を出した")

    def test_hook_still_tells_bd_projects_the_bd_commands(self):
        # 対照: bd のプロジェクトの案内は変えていない (br の分岐が bd を巻き込まない)
        bd = os.path.join(self.tmp, "bdp")
        os.makedirs(bd)
        ts.bdc(bd, "init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq")
        os.makedirs(os.path.join(bd, ".beads", "embeddeddolt"))
        ids = []
        for title in ("回答済み", "もう 1 件"):
            created = json.loads(ts.bdc(bd, "create", title, "-l", "answered", "--json"))
            self.assertIn("id", created)
            ids.append(created["id"])
        port = self.serve()
        hook = self.load_hook(port)
        ctx = self.hook_out(self.run_hook(hook, "session_start", bd, "s-bd"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("このプロジェクト (bd)", ctx)
        self.assertIn("metadata の humanq", ctx)
        self.assertIn("`bd label remove <id> answered`", ctx)
        self.forget(hook, "s-bd", ids[:1])
        ctx = self.hook_out(self.run_hook(hook, "prompt_submit", bd, "s-bd"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"1 件に回答した: {ids[1]}。`bd comments <id>`", ctx)
        self.assertEqual(self.run_hook(hook, "prompt_submit", bd, "s-bd"), "", "増えたものが無いのに知らせた")


if __name__ == "__main__":
    unittest.main()
