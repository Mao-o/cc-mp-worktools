"""humanq のサーバー側 (server/humanq.py) のテスト。

    python3 -m unittest discover -s tests

bd を通す読み書きのシナリオ (BdScenarios) は 2 通りで回す:
- WithFakeBd: 偽の bd (tests/fake_beads.py) で回す。本物の bd が無い環境 (CI) でも skip しない。
  サーバーが打つ bd の argv の列も、ここで固定する (test_golden_argv)
- WithBd: 本物の bd で回す。bd が PATH に無ければ skip する。一時ディレクトリに git init + bd init した
  使い捨てのプロジェクトで行う (本物の bd の DB には書かない)
モジュールごと読み込んだとき (discover など) は、この 2 つのシナリオを HUMANQ_TEST_JOBS 個 (既定 3) のプロセスで
並列に回す (下の「並列のプロセスで回す」。1 で直列)。
サーバーのプロセスを起動する確認も、シナリオの中の HTTP も、OS が選んだポートで行う (同じマシンで suite を同時に回しても取り合わない)。
HUMANQ_SERVER_DIR で別の server/ を試せる。
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
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
import traceback
import types
import unittest
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER_DIR = Path(os.environ.get("HUMANQ_SERVER_DIR") or HERE.parent / "server")
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(HERE))                   # bd_parallel (`-m unittest tests.test_server` のように親から指したときも読む)
import bd_parallel  # noqa: E402
import humanq  # noqa: E402
import registry  # noqa: E402

HAVE_BD = shutil.which("bd") is not None
QUESTION = {"humanq": {"v": 1, "kind": "decision", "ask": "Q?", "context": "c", "recommended": "a",
                       "options": [{"key": "a", "label": "案A", "desc": "d"}, {"key": "b", "label": "案B", "desc": "e"}]}}


def free_port() -> int:
    # OS に空きを選ばせる (port 0)。固定の範囲を順に調べると、同じマシンで suite を同時に回したとき、
    # 調べてから使うまでの間にもう一方が同じ番号を選んで、お互いのサーバーを掴む
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


FAKE_BEADS = HERE / "fake_beads.py"
FAKE_BIN_DIRS: set[str] = set()        # 偽の bd を置いた dir (realpath)。WithBd がそこの bd を拾っていないことを確かめる


def install_fake(bin_dir: str, prog: str = "bd") -> str:
    """tests/fake_beads.py を bin_dir に prog の名前で置く。偽物は argv[0] の basename で振る舞いを決めるので、
    名前を変えた写しにし、shebang を今の python に替えて実行権限を付ける (元のファイルの mode に頼らない)。"""
    _, _, body = FAKE_BEADS.read_text(encoding="utf-8").partition("\n")
    shebang = "#!/usr/bin/env python3" if re.search(r"\s", sys.executable) else "#!" + sys.executable
    dst = os.path.join(bin_dir, prog)
    Path(dst).write_text(shebang + "\n" + body, encoding="utf-8")
    os.chmod(dst, 0o755)
    FAKE_BIN_DIRS.add(os.path.realpath(bin_dir))
    return dst


def argv_has(words: list[str], args) -> bool:
    """words が args にこの順で現れるか (間に別の語を挟んでよい)。偽の bd の FAKE_BEADS_FAIL と同じ規則。"""
    it = iter(args)
    return all(any(a == w for a in it) for w in words)


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


class FreePort(unittest.TestCase):
    def hold(self, port: int) -> socket.socket:
        """番号を bind だけして握る (listen はしない)。握れないなら、返された番号がすでに誰かのものだったということ"""
        s = socket.socket()
        self.addCleanup(s.close)
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            self.fail(f"free_port が返した {port} をすでに誰かが使っている: {e}")
        return s

    def test_free_port_skips_a_port_that_is_bound_but_not_listening(self):
        # 範囲を connect で調べる実装は、bind しただけで listen していない番号を「空き」と読み、同じ番号を返す
        first = free_port()
        self.hold(first)
        second = free_port()
        self.assertNotEqual(first, second, "bind 済みの番号をもう一度返した (同時に回した suite と取り合う)")

    def test_free_port_gives_distinct_numbers_while_each_is_held(self):
        held, ports = [], []
        try:
            for _ in range(5):
                p = free_port()
                held.append(self.hold(p))
                ports.append(p)
        finally:
            for s in held:
                s.close()
        self.assertEqual(len(ports), 5)
        self.assertEqual(len(set(ports)), 5, ports)


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


class BdScenarios:
    """bd を通す読み書きのシナリオ。本物の bd (WithBd) と偽の bd (WithFakeBd) の両方で回す。

    サブクラスは self.repo と self.tmp (bd init 済みの使い捨てのプロジェクトと、その置き場) と、次の 2 つを用意する:
      failing(rule, times=None): bd の呼び出しのうち、argv に rule の語がこの順で現れるものを失敗させる
                                  (times を付けると最初の times 回だけ)。返り値を呼ぶと元に戻す
      calls(rule): setUp から後に、argv に rule の語がこの順で現れた bd の呼び出しの数"""

    repo: str
    tmp: str
    n = 0

    def new_question(self, labels=("human",)) -> str:
        # metadata とラベルは create で一緒に付ける (本物の bd は 1 回の呼び出しに 0.2 秒以上かかるので、3 回を 1 回にする)
        type(self).n += 1
        meta = os.path.join(self.tmp, f"meta{self.n}.json")
        Path(meta).write_text(json.dumps(QUESTION))
        out = bdc(self.repo, "create", f"question {self.n}", "-p", "1", "-l", ",".join(labels), "--metadata", "@" + meta, "--json")
        created = json.loads(out)
        assert isinstance(created, dict) and "id" in created, out
        assert set(created.get("labels") or []) == set(labels), f"create がラベルを付けていない: {out}"
        assert created.get("metadata") == QUESTION, f"create が metadata を付けていない: {out}"
        return created["id"]

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
        restore = self.failing("label remove human")
        try:
            with self.assertRaises(humanq.BdError) as cm:
                store.answer(self.repo, {"id": iid, "option": "a", "text": "補足"})
            self.assertEqual(getattr(cm.exception, "stage", None), "label")
            self.assertIs(getattr(cm.exception, "written", None), True)
            self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1)
            self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human", "answered"})
        finally:
            restore()
        try:
            store.answer(self.repo, {"id": iid, "option": "a", "text": "補足"})  # 送り直し
        except humanq.BdError as e:
            self.fail(f"失敗を外した後の送り直しが失敗した: {e}")
        self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1, "送り直しでコメントが二重になった")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    def test_resend_with_a_different_answer_adds_a_comment(self):
        iid = self.new_question()
        store = self.store()
        restore = self.failing("label remove human")
        try:
            with self.assertRaises(humanq.BdError):
                store.answer(self.repo, {"id": iid, "option": "a", "text": ""})
        finally:
            restore()
        try:
            store.answer(self.repo, {"id": iid, "option": "b", "text": ""})
        except humanq.BdError as e:
            self.fail(f"失敗を外した後の回答が失敗した: {e}")
        marks = [c for c in self.comments(iid) if c.startswith("[humanq]")]
        self.assertEqual(len(marks), 2)
        self.assertIn("案B", marks[-1])

    def test_label_step_is_retried_once(self):
        iid = self.new_question()
        store = self.store()
        restore = self.failing("label remove human", times=1)
        try:
            store.answer(self.repo, {"id": iid, "option": "a", "text": ""})
        except humanq.BdError as e:
            self.fail(f"1 回だけの失敗が再試行で通らなかった: {e}")
        finally:
            restore()
        self.assertEqual(self.calls("label remove human"), 2)
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    def serve(self, store: humanq.Store):
        # ポートは OS に選ばせる (port 0)。シナリオは並列のプロセスで回るので、8770〜8779 から選ぶと取り合いになる。
        # handler は Host の検査に実際のポートを使うので、bind した後に作って差し込む
        srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = srv.server_address[1]
        srv.RequestHandlerClass = humanq.make_handler(store, port, "tok", "<html></html>", "t")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        code, _, body = fetch(f"http://127.0.0.1:{port}/api/ping")      # handler が bind したポートを Host に使っている
        self.assertEqual(code, 200, body)
        return port

    def post(self, port: int, path: str, body: dict):
        return fetch(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                     {"Content-Type": "application/json", "X-Humanq-Token": "tok", "Host": f"127.0.0.1:{port}"})

    def test_http_reports_failed_stage_and_gone(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        restore = self.failing("label add answered")
        try:
            code, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a"})
        finally:
            restore()
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
        restore = self.failing("label add answered")
        try:
            _, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a"})
        finally:
            restore()
        self.assertEqual(json.loads(body).get("op"), "answer")
        self.assertEqual(json.loads(body).get("stage"), "label")
        iid2 = self.new_question()
        try:
            store.answer(self.repo, {"id": iid2, "option": "a"})
        except humanq.BdError as e:
            self.fail(f"失敗を外した後の回答が失敗した: {e}")
        restore = self.failing("label add human")
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
        restore = self.failing("label remove answered")
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
        restore = self.failing("label remove human")
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


@unittest.skipUnless(HAVE_BD, "bd が無い")
class WithBd(BdScenarios, unittest.TestCase):
    """本物の bd で回す (bd が PATH にある手元だけ)。失敗は humanq.bd を差し替えて作る。"""

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

    def setUp(self):
        found = shutil.which("bd")
        self.assertIsNotNone(found)
        self.assertNotIn(os.path.realpath(os.path.dirname(found)), FAKE_BIN_DIRS, "WithBd が偽の bd を拾っている (PATH が戻っていない)")
        real = humanq.bd
        self._rules: list[list] = []          # [語の列, 残りの回数 (None は何回でも)]
        self._calls: list[tuple] = []

        def recorded(repo, *args):
            self._calls.append(args)
            for r in self._rules:
                if argv_has(r[0], args) and (r[1] is None or r[1] > 0):
                    if r[1] is not None:
                        r[1] -= 1
                    raise humanq.BdError("boom")
            return real(repo, *args)

        humanq.bd = recorded
        self.addCleanup(setattr, humanq, "bd", real)

    def failing(self, rule: str, times: int | None = None):
        r = [rule.split(), times]
        self._rules.append(r)

        def restore():
            if r in self._rules:
                self._rules.remove(r)
        return restore

    def calls(self, rule: str) -> int:
        return sum(1 for a in self._calls if argv_has(rule.split(), a))


class WithFakeBd(BdScenarios, unittest.TestCase):
    """偽の bd (tests/fake_beads.py) で回す。本物の bd が無い環境 (CI) でも skip しない。

    偽物を置いた dir を PATH の先頭に入れるのは、テストごとの setUp で行い addCleanup で戻す (HAVE_BD はモジュールの
    読み込み時に決まるので、偽物を PATH に残したままにしない)。プロジェクトはテストごとに作り直す (偽物の init は速い)。
    失敗は env の FAKE_BEADS_FAIL で偽物に作らせ、呼び出しは FAKE_BEADS_LOG の記録で数える。"""

    @classmethod
    def setUpClass(cls):
        cls.bin_tmp = tempfile.mkdtemp(prefix="humanq-fakebin-")
        cls.bin = os.path.join(cls.bin_tmp, "bin")
        os.makedirs(cls.bin)
        install_fake(cls.bin)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.bin_tmp, ignore_errors=True)

    def setUp(self):
        leaked = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.realpath(d) in FAKE_BIN_DIRS]
        self.assertEqual(leaked, [], "前のテストの偽の bd が PATH に残っている")
        self.tmp = tempfile.mkdtemp(prefix="humanq-fakebd-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""), "FAKE_BEADS_LOG": self.log})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FAKE_BEADS_FAIL", None)
        self.assertEqual(os.path.realpath(shutil.which("bd") or ""), os.path.realpath(os.path.join(self.bin, "bd")))
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        bdc(self.repo, "init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq")

    def failing(self, rule: str, times: int | None = None):
        prev = os.environ.get("FAKE_BEADS_FAIL")
        spec = rule if times is None else f"{rule}:{times}"
        os.environ["FAKE_BEADS_FAIL"] = f"{prev};{spec}" if prev else spec

        def restore():
            if prev is None:
                os.environ.pop("FAKE_BEADS_FAIL", None)
            else:
                os.environ["FAKE_BEADS_FAIL"] = prev
        return restore

    def log_entries(self) -> list[dict]:
        try:
            lines = Path(self.log).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(x) for x in lines if x.strip()]

    def calls(self, rule: str) -> int:
        return sum(1 for e in self.log_entries() if argv_has(rule.split(), e["argv"]))

    # 今のサーバーが打つ bd の argv の 6 形を、読み → 回答 → 読み → 取り消しの順に並べたもの。
    # bd を利用している人の挙動を、後で呼び出しを作り直しても変えないための固定 (<repo> <id> <tmp> は置き換えて比べる)
    HUMAN_LIST = ["-C", "<repo>", "human", "list", "--json"]
    ANSWERED_LIST = ["-C", "<repo>", "list", "--status", "open,in_progress,blocked,deferred", "--label", "answered",
                     "--flat", "-n", "0", "--json"]
    COMMENTS = ["-C", "<repo>", "comments", "<id>", "--json"]
    COMMENT_ADD = ["-C", "<repo>", "--actor", "user/humanq", "comments", "add", "<id>", "-f", "<tmp>"]
    ADD_ANSWERED = ["-C", "<repo>", "--actor", "user/humanq", "label", "add", "<id>", "answered"]
    REMOVE_HUMAN = ["-C", "<repo>", "--actor", "user/humanq", "label", "remove", "<id>", "human"]
    ADD_HUMAN = ["-C", "<repo>", "--actor", "user/humanq", "label", "add", "<id>", "human"]
    REMOVE_ANSWERED = ["-C", "<repo>", "--actor", "user/humanq", "label", "remove", "<id>", "answered"]
    GOLDEN = [
        HUMAN_LIST, ANSWERED_LIST,                                              # 読み (回答済みは 0 件)
        HUMAN_LIST, COMMENTS, COMMENT_ADD, ADD_ANSWERED, REMOVE_HUMAN,          # 回答
        HUMAN_LIST, ANSWERED_LIST, COMMENTS,                                    # 読み (回答済みの本文を読む)
        ANSWERED_LIST, COMMENTS, COMMENT_ADD, ADD_HUMAN, REMOVE_ANSWERED,       # 取り消し
    ]

    def test_golden_argv(self):
        iid = self.new_question()
        port = self.serve(self.store())
        start = len(self.log_entries())
        items = f"http://127.0.0.1:{port}/api/items"

        def project():
            code, _, body = fetch(items)
            self.assertEqual(code, 200, body)
            ps = json.loads(body)["projects"]
            self.assertEqual(len(ps), 1, ps)
            self.assertTrue(ps[0]["ok"], ps[0])
            return ps[0]

        p = project()
        self.assertEqual([i["id"] for i in p["human"]], [iid])
        code, _, body = self.post(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a", "qsig": p["human"][0]["qsig"]})
        self.assertEqual(code, 200, body)
        p = project()
        self.assertEqual(([i["id"] for i in p["human"]], [i["id"] for i in p["answered"]]), ([], [iid]))
        self.assertTrue(p["answered"][0]["answer"].startswith("[humanq] 回答: 案A (a)"), p["answered"][0])
        code, _, body = self.post(port, "/api/undo", {"project": self.repo, "id": iid})
        self.assertEqual(code, 200, body)

        entries = self.log_entries()[start:]
        self.assertEqual({e["prog"] for e in entries}, {"bd"})
        got = []
        for e in entries:
            argv = ["<repo>" if a == self.repo else "<id>" if a == iid else a for a in e["argv"]]
            if "-f" in argv:
                k = argv.index("-f")
                self.assertLess(k + 1, len(argv), argv)
                self.assertRegex(os.path.basename(argv[k + 1]), r"^humanq-.*\.md$")
                argv[k + 1] = "<tmp>"
            got.append(argv)
        self.assertEqual(got, self.GOLDEN)

    def test_fake_has_the_quirks_of_bd_1_3_4(self):
        def run(*args):
            p = subprocess.run(["bd", "-C", self.repo, *args], capture_output=True, text=True, timeout=30)
            return p.returncode, p.stdout, p.stderr

        def as_json(out: str, what: str):
            try:
                return json.loads(out)
            except ValueError:
                self.fail(f"{what}: JSON でない: {out!r}")

        self.assertEqual(run("human", "list", "--json")[:2], (0, "null\n"), "human list は 0 件で null")
        rc, out, _ = run("list", "--status", "open", "--json")
        self.assertEqual((rc, as_json(out, "list")), (0, []), "list は 0 件で []")
        rc, _, err = run("human", "list", "--label", "x", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("unknown flag: --label", err)

        created = as_json(bdc(self.repo, "create", "plain", "-p", "2", "--json"), "create")
        self.assertIn("id", created)
        iid = created["id"]
        bdc(self.repo, "label", "add", iid, "human")
        rc, out, _ = run("human", "list", "--json")
        items = as_json(out, "human list")
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 1, items)
        self.assertNotIn("metadata", items[0], "metadata が無いチケットは key を出さない")
        self.assertEqual(items[0].get("labels"), ["human"])
        bdc(self.repo, "label", "add", iid, "answered")
        rc, out, _ = run("human", "list", "--json")
        listed = as_json(out, "human list")
        self.assertEqual(len(listed), 1, listed)
        self.assertEqual(listed[0].get("labels"), ["answered", "human"], "labels は名前の順 (付けた順ではない)")

        f = os.path.join(self.tmp, "c.md")
        Path(f).write_text("first\n")
        bdc(self.repo, "--actor", "user/humanq", "comments", "add", iid, "-f", f)
        Path(f).write_text("second\n")
        bdc(self.repo, "comments", "add", iid, "-f", f)
        cs = as_json(bdc(self.repo, "comments", iid, "--json"), "comments")
        self.assertIsInstance(cs, list)
        self.assertEqual(len(cs), 2, cs)
        self.assertEqual([c.get("text") for c in cs], ["first\n", "second\n"], "古い順")
        self.assertEqual(set(cs[0]), {"id", "issue_id", "author", "text", "created_at"})
        self.assertEqual((cs[0]["author"], cs[0]["issue_id"]), ("user/humanq", iid))
        self.assertNotEqual(cs[1]["author"], "user/humanq")
        self.assertRegex(cs[0]["id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

        rc, out, _ = run("comments", "hq-nothing", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("no issue found", str(as_json(out, "comments の error").get("error")))

        p = subprocess.run(["bd", "-C", self.tmp, "human", "list", "--json"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 1, ".beads/ の無い dir")
        self.assertIn("no beads project found", p.stderr)

        Path(self.repo, ".beads", "metadata.json").write_text(json.dumps({"database": "beads.db", "jsonl_export": "issues.jsonl"}))
        rc, _, err = run("human", "list", "--json")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("Error: historical SQLite workspace detected;"), err)

    def test_failure_rule_counts_per_project(self):
        iid = self.new_question()
        restore = self.failing("label remove human", times=1)
        try:
            for args in (["label", "remove", iid, "answered"], ["human", "list"], ["label", "add", iid, "human"]):
                p = subprocess.run(["bd", "-C", self.repo, *args], capture_output=True, text=True, timeout=30)
                self.assertEqual(p.returncode, 0, f"語の列に当たらない呼び出しを失敗させた: {args}")
            p = subprocess.run(["bd", "-C", self.repo, "--actor", "x", "label", "remove", iid, "human"], capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 1, "1 回目は失敗する")
            p = subprocess.run(["bd", "-C", self.repo, "label", "remove", iid, "human"], capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, "2 回目は通る")
        finally:
            restore()
        self.assertEqual(self.calls("label remove human"), 2)
        always = self.failing("label add answered")
        p = subprocess.run(["bd", "-C", self.repo, "label", "add", iid, "answered"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 1, "回数を付けない規則は毎回失敗させる")
        always()
        p = subprocess.run(["bd", "-C", self.repo, "label", "add", iid, "answered"], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, "元に戻した後も失敗する")


# ---- bd のシナリオを並列のプロセスで回す (道具は tests/bd_parallel.py) ---------------------------------------
#
# BdScenarios のテスト (WithBd と WithFakeBd) は、モジュールごと読み込まれたとき (discover・`-m unittest test_server`)
# に限り、このファイルを --bd-worker で起動した worker で並列に回す。各 worker は自分の setUpClass (bd init した一時の
# プロジェクト・偽の bd を置いた dir) を持つ。テストを名前で指したとき (`-m unittest test_server.WithBd` など) と
# HUMANQ_TEST_JOBS=1 のときは今までどおり直列。

JOBS_ENV = "HUMANQ_TEST_JOBS"
DEFAULT_JOBS = 3
POOL_DUMMY_ENV = "HUMANQ_TEST_POOL_DUMMY"     # 並列の道具の自己検査 (ParallelRunner) が、worker にだけ置くテスト
SCRIPT = str(Path(__file__).resolve())


def jobs_from_env() -> int:
    try:
        return int(os.environ.get(JOBS_ENV) or DEFAULT_JOBS)
    except ValueError:
        return DEFAULT_JOBS


def is_bd_scenario(t) -> bool:
    # クラスごと skip されたもの (bd が無いときの WithBd) は親に残して、親で skip を報告する (worker を起こすまでもない)
    return isinstance(t, BdScenarios) and not getattr(type(t), "__unittest_skip__", False)


def module_suite(tests, jobs: int):
    """モジュールごと読み込んだときの suite。jobs が 1 以下なら直列のまま返す。"""
    return tests if jobs <= 1 else bd_parallel.parallel_suite(tests, SCRIPT, jobs, is_bd_scenario)


def load_tests(loader, tests, pattern):
    return module_suite(tests, jobs_from_env())


if os.environ.get(POOL_DUMMY_ENV) == "1":
    class PoolDummy(unittest.TestCase):
        """ParallelRunner だけが worker に置くテスト (親では定義しないので、discover には出ない)。"""

        def test_ok(self):
            pass

        def test_fail(self):
            self.assertEqual(1, 2, "わざと落とす")

        def test_error(self):
            raise KeyError("わざと例外")

        def test_skip(self):
            self.skipTest("わざと skip")

        def test_subtest_fail(self):
            for i in range(2):
                with self.subTest(i=i):
                    self.assertEqual(i, 0)

        def test_exit(self):
            os._exit(3)                              # worker ごと落ちる

        def test_deprecation_warning(self):          # -W error なら ERROR (直列と同じ)
            warnings.warn("古い API", DeprecationWarning)

        @unittest.expectedFailure
        def test_xfail(self):
            self.assertEqual(1, 2)

        @unittest.expectedFailure
        def test_xpass(self):
            pass

        @classmethod
        def tearDownClass(cls):                      # worker の終わりに呼ばれたことを親が見る
            mark = os.environ.get("HUMANQ_TEST_POOL_MARK")
            if mark:
                Path(mark, f"teardown-{os.getpid()}").write_text("")

    class PoolDummyBrokenClass(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            mark = os.environ.get("HUMANQ_TEST_POOL_MARK")
            if mark:                                 # setUpClass が落ちたときも class cleanup が回ることを親が見る
                cls.addClassCleanup(lambda: Path(mark, f"broken-cleanup-{os.getpid()}").write_text(""))
            raise RuntimeError("setUpClass をわざと落とす")

        def test_never(self):
            pass

    class PoolDummySkippedClass(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            mark = os.environ.get("HUMANQ_TEST_POOL_MARK")
            if mark:                                 # setUpClass で skip したときも class cleanup が回ることを親が見る
                cls.addClassCleanup(lambda: Path(mark, f"skip-cleanup-{os.getpid()}").write_text(""))
            raise unittest.SkipTest("setUpClass でわざと skip")

        def test_never(self):
            pass

    class PoolDummySkippedBrokenCleanup(unittest.TestCase):
        @classmethod
        def setUpClass(cls):                         # skip しても、class cleanup の失敗は直列と同じく ERROR
            cls.addClassCleanup(lambda: 1 / 0)
            raise unittest.SkipTest("setUpClass でわざと skip")

        def test_never(self):
            pass

    class PoolDummyBrokenTeardown(unittest.TestCase):
        @classmethod
        def tearDownClass(cls):
            raise RuntimeError("tearDownClass をわざと落とす")

        def test_ok(self):
            pass

    class PoolDummyBrokenCleanup(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.addClassCleanup(lambda: 1 / 0)

        def test_ok(self):
            pass


class ParallelRunner(unittest.TestCase):
    """並列の道具が、worker の結果を取りこぼしたり別の結果に読み替えたりしないこと (落ちたテストが通ったことに
    ならない)。worker にだけ置いたテスト (PoolDummy) を、本物の道具で回して親の result を見る。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="humanq-pool-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_remote(self, names: list[str], jobs: int = 2, script: str = SCRIPT) -> unittest.TestResult:
        class Stub(unittest.TestCase):               # 親の側の元のテスト (id と表示だけ借りる)
            def runTest(self):
                pass
        self.assertEqual(len(set(names)), len(names))
        tests = unittest.TestSuite()
        stubs = {}
        for n in names:
            s = Stub()
            stubs[id(s)] = n
            tests.addTest(s)
        suite = bd_parallel.parallel_suite(tests, script, jobs, lambda t: True,
                                           {POOL_DUMMY_ENV: "1", "HUMANQ_TEST_POOL_MARK": self.tmp},
                                           name_of=lambda t: stubs[id(t)])
        self.assertIsInstance(suite, bd_parallel.ParallelSuite)
        remotes = [t for t in bd_parallel.flatten(suite) if isinstance(t, bd_parallel.RemoteTest)]
        self.assertEqual(sorted(t.name for t in remotes), sorted(names))
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, len(names))
        return result

    def fake_worker(self, body: str) -> str:
        """--bd-worker の代わりに置く script (結果の読み違いを作る)。argv[2] が名前を読む fd、argv[3] が結果を書く fd。"""
        path = os.path.join(self.tmp, "fake_worker.py")
        Path(path).write_text("import os, sys\nrd = os.fdopen(int(sys.argv[2]))\nwr = os.fdopen(int(sys.argv[3]), 'w')\n" + body)
        return path

    @staticmethod
    def names_of(pairs) -> list[str]:
        return sorted(t.name for t, _ in pairs)

    def test_each_status_is_reported_as_it_was(self):
        names = ["PoolDummy.test_ok", "PoolDummy.test_fail", "PoolDummy.test_error", "PoolDummy.test_skip",
                 "PoolDummy.test_subtest_fail", "PoolDummyBrokenClass.test_never", "PoolDummySkippedClass.test_never",
                 "PoolDummy.test_nothing", "Nowhere.test_x"]
        r = self.run_remote(names)
        self.assertEqual(self.names_of(r.failures), ["PoolDummy.test_fail", "PoolDummy.test_subtest_fail"])
        self.assertEqual(self.names_of(r.errors), ["Nowhere.test_x", "PoolDummy.test_error", "PoolDummy.test_nothing",
                                                  "PoolDummyBrokenClass.test_never"])
        # setUpClass の SkipTest は直列と同じく skip (ERROR にしない)
        self.assertEqual(sorted((t.name, why) for t, why in r.skipped),
                         [("PoolDummy.test_skip", "わざと skip"), ("PoolDummySkippedClass.test_never", "setUpClass でわざと skip")])
        marks = sorted(n.split("-")[0] for n in os.listdir(self.tmp) if "-cleanup-" in n)
        self.assertEqual(marks, ["broken", "skip"], "setUpClass が落ちた・skip したクラスの class cleanup が回っていない")
        texts = {t.name: text for t, text in r.failures + r.errors}
        self.assertIn("わざと落とす", texts["PoolDummy.test_fail"], "worker の traceback が親に届かない")
        self.assertIn("RemoteFailure", texts["PoolDummy.test_fail"])
        self.assertIn("KeyError", texts["PoolDummy.test_error"])
        self.assertIn("RemoteError", texts["PoolDummy.test_error"], "例外は ERROR のまま (FAIL にしない)")
        self.assertIn("setUpClass をわざと落とす", texts["PoolDummyBrokenClass.test_never"])

    def test_expected_failures_and_teardown(self):
        r = self.run_remote(["PoolDummy.test_xfail", "PoolDummy.test_xpass", "PoolDummy.test_ok"], jobs=1)
        self.assertEqual((r.failures, r.errors), ([], []))
        self.assertEqual(self.names_of(r.expectedFailures), ["PoolDummy.test_xfail"])
        self.assertEqual(sorted(t.name for t in r.unexpectedSuccesses), ["PoolDummy.test_xpass"])
        self.assertFalse(r.wasSuccessful(), "予期しない成功が成功として数えられた")
        marks = [n for n in os.listdir(self.tmp) if n.startswith("teardown-")]
        self.assertEqual(len(marks), 1, "worker の終わりに tearDownClass が 1 回呼ばれていない")

    def test_a_worker_that_dies_does_not_pass_its_tests(self):
        r = self.run_remote(["PoolDummy.test_exit", "PoolDummy.test_ok"], jobs=1)   # worker が落ちた後の 1 件は誰も走らせない
        self.assertEqual(r.failures, [])
        remote = [(t, x) for t, x in r.errors if isinstance(t, bd_parallel.RemoteTest)]
        self.assertEqual(self.names_of(remote), ["PoolDummy.test_exit", "PoolDummy.test_ok"])
        extra = [x for t, x in r.errors if not isinstance(t, bd_parallel.RemoteTest)]
        self.assertEqual(len(extra), 1, "異常終了した worker の rc が ERROR にならない")
        self.assertIn("rc=[3]", extra[0])
        self.assertEqual(r.skipped, [])
        texts = {t.name: text for t, text in remote}
        self.assertIn("結果を返さずに終わった", texts["PoolDummy.test_exit"])
        self.assertIn("どの worker もこのテストを走らせなかった", texts["PoolDummy.test_ok"])

    def test_a_failing_class_teardown_in_a_worker_is_an_error(self):
        # 直列 (HUMANQ_TEST_JOBS=1・クラス名指定) では tearDownClass と class cleanup の失敗は ERROR になる。並列でも同じにする
        for name in ("PoolDummyBrokenTeardown.test_ok", "PoolDummyBrokenCleanup.test_ok", "PoolDummySkippedBrokenCleanup.test_never"):
            with self.subTest(name=name):
                r = self.run_remote([name], jobs=1)
                self.assertEqual(r.failures, [])
                self.assertEqual(len(r.errors), 1, "worker の tearDownClass / class cleanup の失敗が親に届かない")
                self.assertIn("tearDownClass", str(r.errors[0][0]))
                self.assertIn("rc=[1]", r.errors[0][1])
                self.assertFalse(r.wasSuccessful())

    def test_the_worker_runs_in_the_parents_cwd(self):
        # 相対パスの env (HUMANQ_SERVER_DIR=server など) が、親と worker で同じものを指すように
        body = ("import json\nn = rd.readline().strip()\nok = os.getcwd() == %r\n"
                "wr.write(json.dumps({'name': n, 'status': 'ok' if ok else 'fail', 'text': 'worker の cwd: ' + os.getcwd()}) + '\\n')\n"
                "wr.flush()\n" % os.getcwd())
        r = self.run_remote(["PoolDummy.test_ok"], jobs=1, script=self.fake_worker(body))
        self.assertEqual(r.failures, [], "worker の cwd が親と違う")
        self.assertEqual((r.errors, r.skipped), ([], []))

    def test_a_result_that_never_comes_is_an_error(self):
        body = "import time\nrd.readline()\ntime.sleep(3)\n"           # 名前を受け取ったまま答えない
        with mock.patch.object(bd_parallel, "RESULT_WAIT", 0.5):
            r = self.run_remote(["PoolDummy.test_ok"], jobs=1, script=self.fake_worker(body))
        self.assertEqual((self.names_of(r.errors), r.failures, r.skipped), (["PoolDummy.test_ok"], [], []))
        self.assertIn("待っても結果が来ない", r.errors[0][1])

    def test_a_module_with_module_fixtures_stays_serial(self):
        # worker は setUpModule / tearDownModule を回さない。回すと、直列なら ERROR になる module の fixture の失敗が通る
        mod = types.ModuleType("humanq_pool_modfix")
        Case = type("Case", (unittest.TestCase,), {"__module__": mod.__name__, "test_x": lambda self: None})
        tests = unittest.TestSuite([Case("test_x")])
        with mock.patch.dict(sys.modules, {mod.__name__: mod}):
            for fixture in ("setUpModule", "tearDownModule"):
                with self.subTest(fixture=fixture):
                    setattr(mod, fixture, lambda: None)
                    self.assertIs(bd_parallel.parallel_suite(tests, SCRIPT, 2, lambda t: True), tests)
                    delattr(mod, fixture)
            self.assertIsInstance(bd_parallel.parallel_suite(tests, SCRIPT, 2, lambda t: True), bd_parallel.ParallelSuite)

    def test_the_worker_gets_the_interpreter_flags(self):
        # `python -W error -m unittest` の -W は env に出ない。worker の起動に渡さないと、直列なら ERROR の警告が worker では通る
        with mock.patch.object(subprocess, "_args_from_interpreter_flags", return_value=["-Werror::DeprecationWarning"]):
            r = self.run_remote(["PoolDummy.test_deprecation_warning"], jobs=1)
        self.assertEqual((self.names_of(r.errors), r.failures), (["PoolDummy.test_deprecation_warning"], []))

    def test_debug_does_not_pass_a_remote_failure(self):
        # TestSuite.debug() は結果を集めずに例外を上げる経路。RemoteTest がそこで黙って通らない
        for name, exc in (("PoolDummy.test_fail", bd_parallel.RemoteFailure), ("PoolDummy.test_error", bd_parallel.RemoteError),
                          ("PoolDummy.test_skip", unittest.SkipTest)):
            with self.subTest(name=name):
                class Stub(unittest.TestCase):
                    def runTest(self):
                        pass
                suite = bd_parallel.parallel_suite(unittest.TestSuite([Stub()]), SCRIPT, 1, lambda t: True,
                                                   {POOL_DUMMY_ENV: "1"}, name_of=lambda t, n=name: n)
                with self.assertRaises(exc):
                    suite.debug()

    def test_a_worker_that_answers_wrongly_does_not_pass(self):
        cases = {
            "別のテストの名前": 'rd.readline()\nwr.write(\'{"name": "PoolDummy.test_other", "status": "ok"}\\n\')\nwr.flush()\n',
            "JSON でない": "rd.readline()\nwr.write('ok\\n')\nwr.flush()\n",
            "status が無い": 'n = rd.readline().strip()\nwr.write(\'{"name": "%s"}\\n\' % n)\nwr.flush()\n',
        }
        for what, body in cases.items():
            with self.subTest(what=what):
                r = self.run_remote(["PoolDummy.test_ok"], jobs=1, script=self.fake_worker(body))
                self.assertEqual((self.names_of(r.errors), r.failures, r.skipped), (["PoolDummy.test_ok"], [], []))

    def test_jobs_1_keeps_the_suite_serial(self):
        tests = unittest.defaultTestLoader.loadTestsFromTestCase(WithFakeBd)
        self.assertGreater(tests.countTestCases(), 0)
        self.assertIs(module_suite(tests, 1), tests)
        self.assertIsInstance(module_suite(tests, 2), bd_parallel.ParallelSuite)
        plain = unittest.TestSuite([ParallelRunner("test_jobs_1_keeps_the_suite_serial")])
        self.assertIs(module_suite(plain, 3), plain, "BdScenarios でないテストを worker に回した")
        with mock.patch.dict(os.environ, {JOBS_ENV: "1"}):
            self.assertEqual(jobs_from_env(), 1)
        with mock.patch.dict(os.environ, {JOBS_ENV: "x"}):
            self.assertEqual(jobs_from_env(), DEFAULT_JOBS)

    def test_module_load_sends_bd_scenarios_to_workers(self):
        with mock.patch.dict(os.environ, {JOBS_ENV: "3"}):
            suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        flat = list(bd_parallel.flatten(suite))
        remote = {type(t.orig).__name__ for t in flat if isinstance(t, bd_parallel.RemoteTest)}
        local = {type(t).__name__ for t in flat if not isinstance(t, bd_parallel.RemoteTest)}
        self.assertEqual(remote, {"WithBd", "WithFakeBd"} if HAVE_BD else {"WithFakeBd"})
        self.assertIn("WithBd", remote if HAVE_BD else local, "bd が無いときの WithBd は親に残して、親で skip を報告する")
        self.assertEqual(local & {"WithFakeBd"}, set())
        self.assertIn("ParallelRunner", local)
        self.assertEqual(len(flat), suite.countTestCases())
        names = [t.name for t in flat if isinstance(t, bd_parallel.RemoteTest)]
        self.assertEqual(len(names), len(set(names)), "同じテストを 2 度配る")
        for t in flat:                               # 報告の id と表示は元のテストのまま (落ちたテストを名前で追える)
            if isinstance(t, bd_parallel.RemoteTest):
                self.assertEqual(t.name, f"{type(t.orig).__name__}.{t.orig._testMethodName}")
                self.assertTrue(t.id().endswith("." + t.name), t.id())
                self.assertEqual((t.id(), str(t)), (t.orig.id(), str(t.orig)))

    def test_a_skipped_scenario_class_stays_in_the_parent(self):
        @unittest.skip("bd が無い")
        class Skipped(BdScenarios, unittest.TestCase):
            pass

        class Plain(BdScenarios, unittest.TestCase):
            pass
        self.assertFalse(is_bd_scenario(Skipped("test_items_carry_qsig")))
        self.assertTrue(is_bd_scenario(Plain("test_items_carry_qsig")))
        self.assertFalse(is_bd_scenario(ParallelRunner("test_a_skipped_scenario_class_stays_in_the_parent")))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--bd-worker":
        sys.exit(bd_parallel.worker_main(sys.modules[__name__], int(sys.argv[2]), int(sys.argv[3])))
    unittest.main()
