"""humanq のサーバー側 (server/humanq.py) のテスト。

    python3 -m unittest discover -s tests

bd が PATH に無ければ skip する。bd が要るテストは、一時ディレクトリに git init + bd init した使い捨てのプロジェクトで行う
(本物の bd の DB には書かない)。HTTP の確認は 8770〜8779 の空いているポートで行う。
HUMANQ_SERVER_DIR で別の server/ を試せる。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = Path(os.environ.get("HUMANQ_SERVER_DIR") or HERE.parent / "server")
sys.path.insert(0, str(SERVER_DIR))
import humanq  # noqa: E402
import registry  # noqa: E402

HAVE_BD = shutil.which("bd") is not None
QUESTION = {"humanq": {"v": 1, "kind": "decision", "ask": "Q?", "context": "c", "recommended": "a",
                       "options": [{"key": "a", "label": "案A", "desc": "d"}, {"key": "b", "label": "案B", "desc": "e"}]}}


def free_port() -> int:
    for p in range(8770, 8780):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("8770〜8779 に空きが無い")


def fetch(url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), e.read()
        finally:
            e.close()


def wait_ping(port: int) -> None:
    for _ in range(100):
        try:
            fetch(f"http://127.0.0.1:{port}/api/ping")
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("サーバーが起動しない")


def start_server(state_dir: str, port: int) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, str(SERVER_DIR / "humanq.py"), "--state-dir", state_dir, "--port", str(port)],
                         stdin=subprocess.DEVNULL)
    wait_ping(port)
    return p


def stop(p: subprocess.Popen) -> None:
    p.terminate()
    p.wait(timeout=10)


def page_token(port: int) -> str:
    _, _, body = fetch(f"http://127.0.0.1:{port}/")
    m = re.search(r'const TOKEN = "([^"]*)"', body.decode())
    assert m, "ページにトークンが無い"
    return m.group(1)


def bdc(repo: str, *args: str) -> str:
    p = subprocess.run(["bd", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"bd {args}: {p.stderr or p.stdout}"
    return p.stdout


class ServerTokenAndHeaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="humanq-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_token_survives_restart(self):
        port = free_port()
        p = start_server(self.tmp, port)
        try:
            first = page_token(port)
        finally:
            stop(p)
        p = start_server(self.tmp, port)
        try:
            second = page_token(port)
        finally:
            stop(p)
        self.assertEqual(first, second, "再起動でトークンが変わると、開いたままの画面の回答が 403 になる")
        mode = stat.S_IMODE(os.stat(os.path.join(self.tmp, "token")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_token_file_is_not_clobbered_by_a_loser(self):
        # ポートを取れなかったプロセスは、先に動いているサーバーのトークンのファイルに触らない。
        # 後から起動したものが走る前にファイルを壊しておく (有効な値のままだと、読むだけで書き換えない実装でも通ってしまう)
        port = free_port()
        p = start_server(self.tmp, port)
        try:
            token = page_token(port)
            tok_path = Path(self.tmp, "token")
            tok_path.write_text("broken!!")
            loser = subprocess.run([sys.executable, str(SERVER_DIR / "humanq.py"), "--state-dir", self.tmp, "--port", str(port)],
                                   stdin=subprocess.DEVNULL, timeout=20)
            self.assertEqual(loser.returncode, 0)
            self.assertEqual(tok_path.read_text(), "broken!!", "ポートを取れなかったものがトークンのファイルを書き換えた")
            self.assertEqual(page_token(port), token)
            code, _, _ = fetch(f"http://127.0.0.1:{port}/api/answer", json.dumps({"project": "/nowhere", "id": "x"}).encode(),
                               {"Content-Type": "application/json", "X-Humanq-Token": token, "Host": f"127.0.0.1:{port}"})
            self.assertEqual(code, 400, "先に動いているサーバーが元のトークンの POST を受け付けない (403 は token の拒否)")
        finally:
            stop(p)

    def test_load_token_recreates_a_file_that_is_not_utf8(self):
        Path(self.tmp, "token").write_bytes(b"\xff\xfe\x00")
        tok = humanq.load_token(self.tmp)
        self.assertRegex(tok, r"[A-Za-z0-9_-]{20,}")
        self.assertEqual(Path(self.tmp, "token").read_text().strip(), tok)
        self.assertEqual(humanq.load_token(self.tmp), tok, "作り直した値が次の起動でも使われる")

    def test_load_token_keeps_going_when_it_cannot_save(self):
        os.mkdir(os.path.join(self.tmp, "token"))            # token のパスがディレクトリ: 読めず、書き換えもできない
        tok = humanq.load_token(self.tmp)
        self.assertRegex(tok, r"[A-Za-z0-9_-]{20,}")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".tmp-token-")], [], "一時ファイルが残った")

    def test_server_starts_even_if_the_token_cannot_be_saved(self):
        os.mkdir(os.path.join(self.tmp, "token"))
        port = free_port()
        p = start_server(self.tmp, port)
        try:
            self.assertRegex(page_token(port), r"[A-Za-z0-9_-]{20,}")
        finally:
            stop(p)

    def test_frame_headers(self):
        port = free_port()
        p = start_server(self.tmp, port)
        try:
            for path in ("/", "/api/ping"):
                _, headers, _ = fetch(f"http://127.0.0.1:{port}{path}")
                low = {k.lower(): v for k, v in headers.items()}
                self.assertIn("frame-ancestors 'none'", low.get("content-security-policy", ""), path)
                self.assertEqual(low.get("x-frame-options"), "DENY", path)
        finally:
            stop(p)


HOOK_PATH = SERVER_DIR.parent / "hooks" / "humanq_hook.py"


class HookEnsureServer(unittest.TestCase):
    """hook の ensure_server が、動いているサーバーの版を見て入れ替えるか決めること。
    版を変えたサーバーは、server/ と plugin.json の写し (版だけ書き換える) から起動する。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="humanq-hook-")
        self.sd = os.path.join(self.tmp, "state")
        os.makedirs(self.sd)
        self.port = free_port()
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.kill_server)
        spec = importlib.util.spec_from_file_location("humanq_hook_under_test", HOOK_PATH)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.hook.PORT = self.port
        self.hook.URL = f"http://127.0.0.1:{self.port}/"

    def ping(self):
        try:
            return json.loads(fetch(f"http://127.0.0.1:{self.port}/api/ping")[2])
        except (OSError, ValueError):
            return None

    def kill_server(self):
        info = self.ping()
        if info and isinstance(info.get("pid"), int) and info["pid"] != os.getpid():
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except OSError:
                pass
            for _ in range(50):
                if self.ping() is None:
                    break
                time.sleep(0.1)
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def start_version(self, version: str) -> int:
        root = os.path.join(self.tmp, "plugin-" + re.sub(r"\W", "_", version))
        shutil.copytree(SERVER_DIR, os.path.join(root, "server"), ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(root, ".claude-plugin"))
        Path(root, ".claude-plugin", "plugin.json").write_text(json.dumps({"name": "humanq", "version": version}))
        self.procs.append(subprocess.Popen([sys.executable, os.path.join(root, "server", "humanq.py"), "--state-dir", self.sd,
                                            "--port", str(self.port)], stdin=subprocess.DEVNULL))
        wait_ping(self.port)
        info = self.ping()
        self.assertEqual(info["version"], version)
        return info["pid"]

    def test_a_newer_server_is_left_alone(self):
        # 旧い版の hook (上げる前から動いているセッション) が、新しい版のサーバーを入れ替え戻さない
        pid = self.start_version("99.0.0")
        self.assertEqual(self.hook.ensure_server(self.sd), "running")
        self.assertEqual(self.ping()["pid"], pid, "新しい版のサーバーが入れ替えられた")

    def test_an_older_server_is_replaced(self):
        pid = self.start_version("0.0.1")
        self.assertIn(self.hook.ensure_server(self.sd), ("started", "starting"))
        info = self.ping()
        self.assertNotEqual(info["pid"], pid)
        self.assertEqual(info["version"], registry.plugin_version())

    def test_a_version_that_cannot_be_read_is_replaced(self):
        pid = self.start_version("not-a-version")
        self.assertIn(self.hook.ensure_server(self.sd), ("started", "starting"))
        self.assertNotEqual(self.ping()["pid"], pid)

    def test_the_same_version_keeps_running(self):
        pid = self.start_version(registry.plugin_version())
        self.assertEqual(self.hook.ensure_server(self.sd), "running")
        self.assertEqual(self.ping()["pid"], pid)


@unittest.skipUnless(HAVE_BD, "bd が無い")
class WithBd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="humanq-bd-")
        cls.repo = os.path.join(cls.tmp, "proj")
        os.makedirs(cls.repo)
        subprocess.run(["git", "init", "-q", cls.repo], check=True)
        subprocess.run(["bd", "init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq"],
                       cwd=cls.repo, check=True, capture_output=True, timeout=120)
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def new_question(self, labels=("human",)) -> str:
        type(self).n += 1
        out = bdc(self.repo, "create", f"question {self.n}", "-p", "1", "--json")
        iid = json.loads(out)["id"]
        meta = os.path.join(self.tmp, f"meta{self.n}.json")
        Path(meta).write_text(json.dumps(QUESTION))
        bdc(self.repo, "update", iid, "--metadata", "@" + meta)
        for lab in labels:
            bdc(self.repo, "label", "add", iid, lab)
        return iid

    def comments(self, iid: str) -> list[str]:
        return [c["text"] for c in json.loads(bdc(self.repo, "comments", iid, "--json") or "[]")]

    def labels(self, iid: str) -> set[str]:
        return set(json.loads(bdc(self.repo, "show", iid, "--json"))[0].get("labels") or [])

    def store(self) -> humanq.Store:
        sd = os.path.join(self.tmp, "state")
        os.makedirs(sd, exist_ok=True)
        registry.register(sd, self.repo)
        return humanq.Store(sd)

    def test_human_and_answered_both_listed_only_as_answered(self):
        iid = self.new_question(labels=("human", "answered"))
        data = humanq.load_project(self.repo)
        self.assertNotIn(iid, [i["id"] for i in data["human"]])
        self.assertIn(iid, [i["id"] for i in data["answered"]])

    def test_resend_after_partial_write_keeps_one_comment(self):
        iid = self.new_question()
        store = self.store()
        real = humanq.bd
        fail = {"on": True}

        def flaky(repo, *args):
            if fail["on"] and "remove" in args and "human" in args:
                raise humanq.BdError("label remove boom")
            return real(repo, *args)

        humanq.bd = flaky
        try:
            with self.assertRaises(humanq.BdError) as cm:
                store.answer(self.repo, {"id": iid, "option": "a", "text": "補足"})
            self.assertEqual(getattr(cm.exception, "stage", None), "label")
            self.assertIs(getattr(cm.exception, "written", None), True)
            self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1)
            self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human", "answered"})
            fail["on"] = False
            store.answer(self.repo, {"id": iid, "option": "a", "text": "補足"})      # 送り直し
        finally:
            humanq.bd = real
        self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1, "送り直しでコメントが二重になった")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    def test_resend_with_a_different_answer_adds_a_comment(self):
        iid = self.new_question()
        store = self.store()
        real = humanq.bd

        def flaky(repo, *args):
            if "remove" in args and "human" in args:
                raise humanq.BdError("label remove boom")
            return real(repo, *args)

        humanq.bd = flaky
        try:
            with self.assertRaises(humanq.BdError):
                store.answer(self.repo, {"id": iid, "option": "a", "text": ""})
        finally:
            humanq.bd = real
        store.answer(self.repo, {"id": iid, "option": "b", "text": ""})
        marks = [c for c in self.comments(iid) if c.startswith("[humanq]")]
        self.assertEqual(len(marks), 2)
        self.assertIn("案B", marks[-1])

    def test_label_step_is_retried_once(self):
        iid = self.new_question()
        store = self.store()
        real = humanq.bd
        calls = {"n": 0}

        def once(repo, *args):
            if "remove" in args and "human" in args:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise humanq.BdError("transient")
            return real(repo, *args)

        humanq.bd = once
        try:
            store.answer(self.repo, {"id": iid, "option": "a", "text": ""})
        finally:
            humanq.bd = real
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    def serve(self, store: humanq.Store):
        port = free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), humanq.make_handler(store, port, "tok", "<html></html>", "t"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return port

    def post(self, port: int, path: str, body: dict):
        return fetch(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                     {"Content-Type": "application/json", "X-Humanq-Token": "tok", "Host": f"127.0.0.1:{port}"})

    def test_http_reports_failed_stage_and_gone(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        real = humanq.bd

        def flaky(repo, *args):
            if "add" in args and "answered" in args:
                raise humanq.BdError("label add boom")
            return real(repo, *args)

        humanq.bd = flaky
        try:
            code, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a"})
        finally:
            humanq.bd = real
        j = json.loads(body)
        self.assertEqual((code, j.get("stage"), j.get("written")), (502, "label", True), j)
        gone_code, _, gone_body = self.post(port, "/api/answer", {"project": self.repo, "id": "hq-nothing", "option": "a"})
        self.assertEqual(gone_code, 400)
        self.assertEqual(json.loads(gone_body).get("code"), "gone")

    def test_post_catches_unexpected_exceptions(self):
        store = self.store()
        port = self.serve(store)

        def boom(repo, body):
            raise RuntimeError("unexpected")

        store.answer = boom
        code, headers, body = self.post(port, "/api/answer", {"project": self.repo, "id": "x", "option": "a"})
        self.assertEqual(code, 500)
        self.assertIn("error", json.loads(body))

    def test_one_odd_project_does_not_break_the_list(self):
        store = self.store()
        real = humanq.load_project

        def odd(repo):
            raise AttributeError("'str' object has no attribute 'get'")

        humanq.load_project = odd
        try:
            d = store.project(self.repo)
            self.assertFalse(d["ok"])
            self.assertIn("error", d)
            self.assertEqual(store.all()[0]["ok"], False)
        finally:
            humanq.load_project = real

    def test_bd_json_rejects_non_list(self):
        real = humanq.bd
        try:
            humanq.bd = lambda repo, *a: '{"issues": []}'
            with self.assertRaises(humanq.BdError):
                humanq.bd_json(self.repo, "list")
            humanq.bd = lambda repo, *a: "Update available: bd 9.9\n[]"
            with self.assertRaises(humanq.BdError):
                humanq.bd_json(self.repo, "list")
            humanq.bd = lambda repo, *a: "No human-needed beads found.\n"
            self.assertEqual(humanq.bd_json(self.repo, "human", "list"), [])
            humanq.bd = lambda repo, *a: "null\n"
            self.assertEqual(humanq.bd_json(self.repo, "human", "list"), [])
        finally:
            humanq.bd = real

    # ---- 0.1.5 ----

    def set_question(self, iid: str, **changes) -> None:
        q = json.loads(json.dumps(QUESTION))
        q["humanq"].update(changes)
        meta = os.path.join(self.tmp, f"meta-{iid}.json")
        Path(meta).write_text(json.dumps(q))
        bdc(self.repo, "update", iid, "--metadata", "@" + meta)

    def add_comment(self, iid: str, text: str) -> None:
        f = os.path.join(self.tmp, "comment.md")
        Path(f).write_text(text)
        bdc(self.repo, "comments", "add", iid, "-f", f)

    def failing(self, *needles: str):
        """needles を全部含む引数の bd 呼び出しを失敗させる。返り値を呼ぶと元に戻す。"""
        real = humanq.bd

        def flaky(repo, *args):
            if all(n in args for n in needles):
                raise humanq.BdError("boom")
            return real(repo, *args)

        humanq.bd = flaky
        return lambda: setattr(humanq, "bd", real)

    def item(self, iid: str, key: str = "human") -> dict:
        return next(i for i in humanq.load_project(self.repo)[key] if i["id"] == iid)

    # 1. 問いの署名
    def test_qsig_follows_what_the_question_shows(self):
        base = humanq.question(QUESTION)
        sig = humanq.qsig(base)
        self.assertEqual(sig, humanq.qsig(humanq.question(json.loads(json.dumps(QUESTION)))))
        for changes in ({"ask": "別の問い"}, {"context": "別の背景"}, {"recommended": "b"},
                        {"options": [{"key": "a", "label": "案A+", "desc": "d"}, {"key": "b", "label": "案B", "desc": "e"}]},
                        {"options": [{"key": "a", "label": "案A", "desc": "d"}, {"key": "c", "label": "案B", "desc": "e"}]},
                        {"options": [{"key": "a", "label": "案A", "desc": "d"}]}):
            q = json.loads(json.dumps(QUESTION))
            q["humanq"].update(changes)
            self.assertNotEqual(humanq.qsig(humanq.question(q)), sig, changes)
        q = json.loads(json.dumps(QUESTION))                  # 選択肢の説明 (desc) だけの変更は、答えの意味を変えないので署名に入れない
        q["humanq"]["options"][0]["desc"] = "言い回しだけ直した"
        self.assertEqual(humanq.qsig(humanq.question(q)), sig)
        self.assertNotEqual(humanq.qsig(None), sig)          # 問いが整理されていないチケットにも署名がある
        self.assertEqual(humanq.qsig(None), humanq.qsig(humanq.question({})))

    def test_items_carry_qsig(self):
        iid = self.new_question()
        it = self.item(iid)
        self.assertEqual(it["qsig"], humanq.qsig(it["q"]))

    def test_answer_for_a_changed_question_is_not_recorded(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        seen = self.item(iid)["qsig"]
        self.set_question(iid, ask="書き直した問い", options=[{"key": "a", "label": "まったく別の案", "desc": ""},
                                                             {"key": "b", "label": "案B", "desc": "e"}])
        code, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a", "qsig": seen})
        self.assertEqual((code, json.loads(body).get("code")), (409, "changed"), body)
        self.assertEqual([c for c in self.comments(iid) if c.startswith("[humanq]")], [], "問いが変わったのに回答が書かれた")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})
        # 今の問いの署名なら通る
        code, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a",
                                                         "qsig": self.item(iid)["qsig"]})
        self.assertEqual(code, 200, body)
        self.assertIn("まったく別の案", self.comments(iid)[-1])

    def test_answer_without_qsig_is_still_accepted(self):
        # qsig を送らない古い画面
        iid = self.new_question()
        port = self.serve(self.store())
        code, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a"})
        self.assertEqual(code, 200, body)

    def test_failures_say_which_op_failed(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        restore = self.failing("add", "answered")
        try:
            _, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a"})
        finally:
            restore()
        self.assertEqual(json.loads(body).get("op"), "answer")
        self.assertEqual(json.loads(body).get("stage"), "label")
        iid2 = self.new_question()
        store.answer(self.repo, {"id": iid2, "option": "a"})
        restore = self.failing("add", "human")
        try:
            code, _, body = self.post(port, "/api/undo", {"project": self.repo, "id": iid2})
        finally:
            restore()
        self.assertEqual((code, json.loads(body).get("op"), json.loads(body).get("stage")), (502, "undo", "label"), body)

    # 6, 11. 両方のラベルが付いたチケットは、最後の [humanq] コメントで向きを決める
    def test_both_labels_after_a_partial_undo_is_a_question_again(self):
        iid = self.new_question()
        store = self.store()
        store.answer(self.repo, {"id": iid, "option": "a"})
        restore = self.failing("remove", "answered")
        try:
            with self.assertRaises(humanq.WriteError) as cm:
                store.undo(self.repo, {"id": iid})
        finally:
            restore()
        self.assertEqual((cm.exception.stage, cm.exception.written), ("label", True))
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human", "answered"})
        data = humanq.load_project(self.repo)
        self.assertIn(iid, [i["id"] for i in data["human"]], "取り消したチケットが判断待ちに出ない")
        self.assertNotIn(iid, [i["id"] for i in data["answered"]])

    def test_both_labels_after_a_partial_answer_stay_answered(self):
        iid = self.new_question()
        store = self.store()
        restore = self.failing("remove", "human")
        try:
            with self.assertRaises(humanq.WriteError):
                store.answer(self.repo, {"id": iid, "option": "a"})
        finally:
            restore()
        data = humanq.load_project(self.repo)
        self.assertNotIn(iid, [i["id"] for i in data["human"]])
        self.assertIn(iid, [i["id"] for i in data["answered"]])

    # 9. 送り直しとみなすのは、最後のコメント全体が同じ回答のときだけ
    def test_answer_after_the_question_was_asked_again_is_recorded(self):
        iid = self.new_question()
        store = self.store()
        store.answer(self.repo, {"id": iid, "option": "a"})
        self.add_comment(iid, "問いを書き直した: 案を 1 つ足した")             # README の手順 4
        bdc(self.repo, "label", "add", iid, "human")
        bdc(self.repo, "label", "remove", iid, "answered")
        store.answer(self.repo, {"id": iid, "option": "a"})                  # 10 分以内に同じ選択肢
        texts = self.comments(iid)
        self.assertEqual(len(texts), 3, texts)
        self.assertTrue(texts[-1].startswith("[humanq] 回答: 案A (a)"), "問い直しの後の回答が記録されていない")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    # 10. 担当中などの open 以外のチケット
    def test_answered_includes_tickets_that_are_not_open(self):
        for status in ("in_progress", "blocked"):
            with self.subTest(status=status):
                iid = self.new_question()
                bdc(self.repo, "update", iid, "--status", status)
                store = self.store()
                self.assertIn(iid, [i["id"] for i in humanq.load_project(self.repo)["human"]])
                store.answer(self.repo, {"id": iid, "option": "a"})
                data = humanq.load_project(self.repo)
                self.assertIn(iid, [i["id"] for i in data["answered"]], "回答したチケットがどの一覧にも出ない")
                self.assertNotIn(iid, [i["id"] for i in data["human"]])
                store.undo(self.repo, {"id": iid})                           # 取り消しも Gone にならない
                self.assertIn(iid, [i["id"] for i in humanq.load_project(self.repo)["human"]])

    def test_human_and_answered_both_on_an_in_progress_ticket_show_once_as_answered(self):
        iid = self.new_question(labels=("human", "answered"))
        bdc(self.repo, "update", iid, "--status", "in_progress")
        data = humanq.load_project(self.repo)
        self.assertNotIn(iid, [i["id"] for i in data["human"]])
        self.assertIn(iid, [i["id"] for i in data["answered"]])

    def test_closed_tickets_are_not_listed_as_answered(self):
        iid = self.new_question(labels=("answered",))
        bdc(self.repo, "close", iid)
        self.assertNotIn(iid, [i["id"] for i in humanq.load_project(self.repo)["answered"]])


if __name__ == "__main__":
    unittest.main()
