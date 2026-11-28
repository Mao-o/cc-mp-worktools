"""humanq のサーバー側 (server/humanq.py) のテスト。

    python3 -m unittest discover -s tests

bd・br を通す読み書きのシナリオ (BdScenarios) は 3 通りで回す:
- WithFakeBd: 偽の bd (tests/fake_beads.py) で回す。本物の bd が無い環境 (CI) でも skip しない。
  サーバーが打つ bd の argv の列も、ここで固定する (test_golden_argv)
- WithBd: 本物の bd で回す。env HUMANQ_TEST_BD を立てたときだけ走り、既定では skip する (本物は遅い。判定は
  tests/real_cli.py、契約テストと共通)。bd を上げたら手元で env を立てて回す。一時ディレクトリに git init + bd init した
  使い捨てのプロジェクトで行う (本物の bd の DB には書かない)。この脚は PATH の bd を打つので、env の値が起動コマンドの文字列でも、
  打つのは PATH の bd (--version が通ることだけ確かめる)
- WithFakeBr: 偽の br (同じ fake_beads.py を br の名前で置く) で、同じシナリオを回す。コメントは新しい順で返る。
  サーバーが打つ br の argv の列もここで固定する
モジュールごと読み込んだとき (discover など) は、これらのシナリオを HUMANQ_TEST_JOBS 個 (既定 3) のプロセスで
並列に回す (下の「並列のプロセスで回す」。1 で直列)。
サーバーのプロセスを起動する確認も、シナリオの中の HTTP も、OS が選んだポートで行う (同じマシンで suite を同時に回しても取り合わない)。
HUMANQ_SERVER_DIR で別の server/ を試せる。
"""
from __future__ import annotations

import atexit
import contextlib
import importlib.util
import io
import json
import os
import queue
import re
import shlex
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
import urllib.parse
import urllib.request
import warnings
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SERVER_DIR = Path(os.environ.get("HUMANQ_SERVER_DIR") or HERE.parent / "server")
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(HERE))                   # bd_parallel (`-m unittest tests.test_server` のように親から指したときも読む)
import bd_parallel  # noqa: E402
import fake_cli  # noqa: E402
import humanq  # noqa: E402
import real_cli  # noqa: E402
import registry  # noqa: E402

HAVE_BD = real_cli.real_command("bd") is not None     # env HUMANQ_TEST_BD を立てたときだけ (空なら bd を起動しない)
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


FAKE_BIN_DIRS: set[str] = set()        # 偽の bd を置いた dir (realpath)。WithBd がそこの bd を拾っていないことを確かめる


def install_fake(bin_dir: str, prog: str = "bd") -> str:
    """tests/fake_beads.py を bin_dir に prog の名前で置く。偽物は argv[0] の basename で振る舞いを決めるので、
    その名前で置く (置き方は tests/fake_cli.py。今の python で起動し、元のファイルの mode に頼らない)。"""
    dst = fake_cli.install(bin_dir, prog)
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


class AnswerSig(unittest.TestCase):
    """回答の署名 (humanq.asig / last_of) の性質。bd を通す照合はシナリオ (BdScenarios) の側で見る。"""

    def test_the_sig_tells_apart_what_the_count_alone_cannot(self):
        self.assertNotEqual(humanq.asig(1, "[humanq] 回答: 案A (a)"), humanq.asig(1, "[humanq] 回答: 案B (b)"),
                            "件数が同じでも本文が違えば別の回答 (コメントが消された・並びが崩れたときに取り違えない)")
        self.assertNotEqual(humanq.asig(1, "[humanq] 回答: 案A (a)"), humanq.asig(3, "[humanq] 回答: 案A (a)"),
                            "本文が同じでも件数が違えば別の回答 (同じ案での答え直し)")
        # 書いた本文と bd から読み直した本文で、前後の空白 (末尾の改行) だけが違っても同じ回答 (送り直しの判定と同じ扱い)
        self.assertEqual(humanq.asig(2, "[humanq] 回答: 案A (a)\n"), humanq.asig(2, "[humanq] 回答: 案A (a)"))

    def test_last_of_counts_only_humanq_comments(self):
        mark = "[humanq] 回答: 案A (a)\n"
        cs = [{"text": "エージェントのメモ"}, {"text": "[humanq] 回答: 案B (b)\n"}, {"text": humanq.UNDO_HEAD + "\n"},
              {"text": "問い直した"}, {"text": mark}, {"text": "後のメモ"}]
        self.assertEqual(humanq.last_of(cs), (mark, humanq.asig(3, mark)))
        self.assertEqual(humanq.last_of([{"text": "メモだけ"}]), ("", humanq.asig(0, None)))
        self.assertNotEqual(humanq.asig(0, None), "", "コメントが無いときの署名が、読めなかったときの空の署名と同じ")

    def test_comments_of_an_unexpected_shape_are_not_counted(self):
        # 本文が文字列でない・dict でない要素は数えない (例外で回答や一覧を止めない)
        mark = "[humanq] 回答: 案A (a)\n"
        for odd in ({"text": 5}, {"text": ["[humanq] x"]}, {"text": None}, {}, "[humanq] 文字列だけ", None):
            with self.subTest(odd=odd):
                try:
                    got = humanq.last_of([{"text": mark}, odd])
                except Exception as e:  # noqa: BLE001
                    self.fail(f"想定外の形のコメントで例外: {type(e).__name__}: {e}")
                self.assertEqual(got, (mark, humanq.asig(1, mark)))


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


class HookInstructionsByCli(unittest.TestCase):
    """hook が Claude に出す指示文 (問いの置き方・回答の拾い方) に、プロジェクトの CLI (bd / br) のコマンドだけが入ること。
    サーバーと CLI は打たず、summary (サーバーの応答) を差し替える。"""

    BD_CMDS = ("`bd ", "bd comments", "bd label")
    BR_CMDS = ("`br ", "br comments", "br label")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="humanq-hookcli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sd = os.path.join(self.tmp, "state")
        os.makedirs(self.sd)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, ".beads"))
        env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.sd})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("humanq_hook_cli_test", HOOK_PATH)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)

    def run_raw(self, fn: str, summary: dict, session: str = "s1") -> dict:
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(self.hook, "ensure_server", lambda sd: "running"), \
                mock.patch.object(self.hook, "summary", lambda *a, **kw: dict(summary)), \
                contextlib.redirect_stdout(buf):
            try:
                getattr(self.hook, fn)({"cwd": self.root, "session_id": session})
            except Exception as e:  # noqa: BLE001  崩れた summary で hook が止まるのを、走っていない ERROR でなく失敗にする
                self.fail(f"hook が例外を出した: {type(e).__name__}: {e} (summary: {summary!r:.200})")
        out = buf.getvalue()
        self.assertTrue(out.strip(), "hook の出力が無い")
        return json.loads(out)

    def run_hook(self, fn: str, summary: dict, session: str = "s1") -> str:
        return self.run_raw(fn, summary, session)["hookSpecificOutput"]["additionalContext"]

    def assertOnly(self, ctx: str, want: tuple[str, ...], other: tuple[str, ...]):
        self.assertEqual(len(want), 3)
        for w in want:
            self.assertIn(w, ctx)
        for o in other:
            self.assertNotIn(o, ctx, f"別の CLI のコマンド ({o}) が入っている")

    def test_bd_project_gets_bd_commands_only(self):
        ctx = self.run_hook("session_start", {"ok": True, "cli": "bd", "writable": True, "human": 1, "answered": ["x-1"]})
        self.assertOnly(ctx, ("metadata の humanq", "`bd comments <id>`", "`bd label remove <id> answered`"), self.BR_CMDS)
        self.assertNotIn("agent_context", ctx)

    def test_old_server_without_cli_is_read_as_bd(self):
        ctx = self.run_hook("session_start", {"ok": True, "human": 0, "answered": ["x-1"]})
        self.assertOnly(ctx, ("metadata の humanq", "`bd comments <id>`", "`bd label remove <id> answered`"), self.BR_CMDS)

    def test_br_project_gets_br_commands_only(self):
        ctx = self.run_hook("session_start", {"ok": True, "cli": "br", "writable": True, "human": 1, "answered": ["x-1"]})
        self.assertOnly(ctx, ("`br update <id> --agent-context @<file>`", "`br comments <id> --json`",
                              "`br label remove <id> answered`"), self.BD_CMDS)
        self.assertIn("(created_at, id) の昇順", ctx, "br のコメントは並べ直してから最後を読む")
        self.assertIn("`br show <id> --json`", ctx)
        self.assertNotIn("metadata", ctx)

    def test_br_project_without_answers_still_gets_the_asking_instructions(self):
        ctx = self.run_hook("session_start", {"ok": True, "cli": "br", "writable": True, "human": 0, "answered": []})
        self.assertIn("`br update <id> --agent-context @<file>`", ctx)
        self.assertNotIn("br label remove", ctx, "回答済みが無いのに拾い方を出した")
        for w in self.BD_CMDS:
            self.assertNotIn(w, ctx)

    def test_unreadable_project_gets_no_commands(self):
        # conflict / unknown: サーバーは ok:false, cli:null で返す。件数と設定の案内だけで、どの CLI のコマンドも出さない
        err = f"bd と br のどちらで読むか決められない (理由)。{registry.CLI_ENV} で指定してください"
        ctx = self.run_hook("session_start", {"ok": False, "cli": None, "writable": False, "error": err, "human": 0, "answered": []})
        self.assertIn(registry.CLI_ENV, ctx)
        for w in self.BD_CMDS + self.BR_CMDS + ("agent_context", "metadata"):
            self.assertNotIn(w, ctx)

    SNOOZED = "ユーザーが保留にした判断待ち {} 件 (判断待ちの件数に入れていない。回答待ちのままなので、問いを取り下げたり、答えを推測して進めたりしない)"
    RETURNED = "保留から判断待ちに戻ったもの {} 件 (戻る日が来たか、保留の後に問いが書き換わった。判断待ちの件数に入っている)"

    def snooze_lines(self, ctx: str) -> list[str]:
        return [ln for ln in ctx.splitlines() if "保留" in ln]

    def test_session_start_adds_one_snooze_line_on_both_clis(self):
        for cli in ("bd", "br"):
            base = {"ok": True, "cli": cli, "writable": True, "human": 2, "answered": []}
            cases = {
                "保留と戻ったもの": ({"snoozed": 3, "returned": 1},
                                     f"humanq: {self.SNOOZED.format(3)}。{self.RETURNED.format(1)}。"),
                "保留だけ": ({"snoozed": 1, "returned": 0}, f"humanq: {self.SNOOZED.format(1)}。"),
                "戻ったものだけ": ({"snoozed": 0, "returned": 2}, f"humanq: {self.RETURNED.format(2)}。"),
            }
            for what, (extra, line) in cases.items():
                with self.subTest(cli=cli, what=what):
                    ctx = self.run_hook("session_start", {**base, **extra})
                    self.assertEqual(self.snooze_lines(ctx), [line])
                    self.assertIn("判断待ち 2 件", ctx, "判断待ちの件数は summary の human のまま (保留はサーバーが除いてある)")

    UNREAD = ("判断待ちの件数のうち {} 件は、保留中か情報待ちかを確認中 (サーバーの起動直後の読み残し。保留中・情報待ちのものが混じっていて、次の読みで減る。"
              "保留中かもしれないチケットに取りかかったり、答えを推測して進めたりしない。情報待ち (エージェントの番) と分かったものは、"
              "読み終えた後のプロンプトで ID を伝える)")

    def test_session_start_says_how_many_waiting_are_still_being_checked_for_a_snooze(self):
        for cli in ("bd", "br"):
            base = {"ok": True, "cli": cli, "writable": True, "human": 20, "answered": []}
            cases = {
                "読み残しだけ": ({"unread": 16}, f"humanq: {self.UNREAD.format(16)}。"),
                "保留と読み残し": ({"snoozed": 5, "unread": 16}, f"humanq: {self.SNOOZED.format(5)}。{self.UNREAD.format(16)}。"),
                "全部": ({"snoozed": 5, "returned": 1, "unread": 16},
                         f"humanq: {self.SNOOZED.format(5)}。{self.RETURNED.format(1)}。{self.UNREAD.format(16)}。"),
            }
            for what, (extra, line) in cases.items():
                with self.subTest(cli=cli, what=what):
                    ctx = self.run_hook("session_start", {**base, **extra})
                    self.assertEqual(self.snooze_lines(ctx), [line])
                    self.assertIn("判断待ち 20 件", ctx, "判断待ちの件数は summary の human のまま")

    def test_unread_that_is_not_a_positive_number_adds_no_line(self):
        base = {"ok": True, "cli": "bd", "writable": True, "human": 3, "answered": []}
        for what, extra in {"0 件": {"unread": 0}, "古いサーバー (key が無い)": {}, "文字列": {"unread": "3"}, "真偽値": {"unread": True},
                            "負の数": {"unread": -1}, "小数": {"unread": 1.5}, "入れ子": {"unread": [2]}}.items():
            with self.subTest(what):
                self.assertEqual(self.snooze_lines(self.run_hook("session_start", {**base, **extra})), [])

    def test_no_snooze_line_without_snoozes_or_from_an_old_server(self):
        base = {"ok": True, "cli": "bd", "writable": True, "human": 1, "answered": []}
        for what, extra in {"0 件": {"snoozed": 0, "returned": 0}, "古いサーバー (key が無い)": {},
                            "数でない値": {"snoozed": "3", "returned": None}, "真偽値": {"snoozed": True, "returned": True},
                            "負の数": {"snoozed": -1, "returned": -2}, "小数": {"snoozed": 2.0, "returned": 1.5},
                            "入れ子": {"snoozed": [3], "returned": {"n": 1}}}.items():
            with self.subTest(what):
                self.assertEqual(self.snooze_lines(self.run_hook("session_start", {**base, **extra})), [])

    def needinfo_lines(self, ctx: str) -> list[str]:
        return [ln for ln in ctx.splitlines() if "情報待ち" in ln and "エージェントの番" in ln]

    def test_session_start_adds_one_needinfo_line_with_the_ids_and_the_commands_of_the_cli(self):
        own = {"bd": ("`bd comments <id>` の最後の `[humanq] 情報待ち`", "`bd label remove <id> needs-info`"),
               "br": ("`br comments <id> --json` を (created_at, id) の昇順に並べ直した最後の `[humanq] 情報待ち`",
                      "`br label remove <id> needs-info`")}
        for cli, other in (("bd", self.BR_CMDS), ("br", self.BD_CMDS)):
            with self.subTest(cli=cli):
                ctx = self.run_hook("session_start", {"ok": True, "cli": cli, "writable": True, "human": 2, "answered": [],
                                                      "snoozed": 1, "needinfo": ["x-1", "x-2"]})
                lines = self.needinfo_lines(ctx)
                self.assertEqual(len(lines), 1, ctx)
                self.assertTrue(lines[0].startswith("humanq: 情報待ち 2 件 (エージェントの番): x-1, x-2。"), lines[0])
                for w in own[cli]:
                    self.assertIn(w, lines[0])
                for o in other:
                    self.assertNotIn(o, ctx, f"別の CLI のコマンド ({o}) が入っている")
                self.assertIn("問いを書き直してから", lines[0])
                self.assertIn("問いの無いチケットは問いを書く", lines[0])
                self.assertIn("判断待ち 2 件", ctx, "判断待ちの件数は summary の human のまま (情報待ちはサーバーが除いてある)")
                self.assertEqual(len(self.snooze_lines(ctx)), 1, "保留の行と情報待ちの行が混ざった")
                self.assertNotIn("情報待ち", self.snooze_lines(ctx)[0])

    def test_no_needinfo_line_without_ids_or_from_an_old_server(self):
        base = {"ok": True, "cli": "bd", "writable": True, "human": 1, "answered": []}
        for what, extra in {"0 件": {"needinfo": []}, "古いサーバー (key が無い)": {}, "文字列": {"needinfo": "x-1"},
                            "数": {"needinfo": 2}, "object": {"needinfo": {"x-1": 1}}, "文字列でない要素だけ": {"needinfo": [5, None, "", ["x"]]}}.items():
            with self.subTest(what):
                self.assertEqual(self.needinfo_lines(self.run_hook("session_start", {**base, **extra})), [])
        ctx = self.run_hook("session_start", {**base, "needinfo": [5, "x-3", None]})
        self.assertEqual([ln.split("。")[0] for ln in self.needinfo_lines(ctx)], ["humanq: 情報待ち 1 件 (エージェントの番): x-3"])

    def test_the_user_message_does_not_count_snoozes(self):
        o = self.run_raw("session_start", {"ok": True, "cli": "bd", "writable": True, "human": 0, "answered": [], "snoozed": 4})
        self.assertNotIn("systemMessage", o, "保留だけのときにユーザーへ判断待ちを知らせた")
        o = self.run_raw("session_start", {"ok": True, "cli": "bd", "writable": True, "human": 1, "answered": [], "snoozed": 4}, "s2")
        self.assertEqual(o.get("systemMessage"), f"humanq: 判断待ち 1 件 / 回答済み 0 件 → {self.hook.URL}")

    def test_prompt_submit_follows_the_cli(self):
        ctx = self.run_hook("prompt_submit", {"ok": True, "cli": "br", "writable": True, "human": 0, "answered": ["x-2"]})
        self.assertOnly(ctx, ("x-2", "`br comments <id> --json`", "(created_at, id) の昇順"), self.BD_CMDS)
        ctx = self.run_hook("prompt_submit", {"ok": True, "cli": "bd", "writable": True, "human": 0, "answered": ["x-3"]}, "s2")
        self.assertOnly(ctx, ("x-3", "`bd comments <id>`", "[humanq] 回答"), self.BR_CMDS)

    # ---- UserPromptSubmit で伝える、前に伝えた後で情報待ちになったもの ----

    def run_quiet(self, fn: str, summary: dict, session: str = "s1") -> str:
        """run_hook と同じだが、出力が無ければ "" (伝えるものが無い)。"""
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(self.hook, "ensure_server", lambda sd: "running"), \
                mock.patch.object(self.hook, "summary", lambda *a, **kw: dict(summary)), \
                contextlib.redirect_stdout(buf):
            try:
                getattr(self.hook, fn)({"cwd": self.root, "session_id": session})
            except Exception as e:  # noqa: BLE001  走っていない ERROR でなく失敗にする
                self.fail(f"hook が例外を出した: {type(e).__name__}: {e} (summary: {summary!r:.200})")
        out = buf.getvalue()
        if not out.strip():
            return ""
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1, f"hook の出力が JSON 1 行でない: {out!r:.300}")
        return json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]

    ADDED = "humanq: 前に伝えた後で、情報待ち (エージェントの番) になったものが {} 件ある: {}。"

    def added_lines(self, ctx: str) -> list[str]:
        return [ln for ln in ctx.splitlines() if "情報待ち (エージェントの番) になったもの" in ln]

    def s(self, cli="bd", answered=(), needinfo=(), **extra) -> dict:
        return {"ok": True, "cli": cli, "writable": True, "human": 1, "answered": list(answered), "needinfo": list(needinfo), **extra}

    def test_prompt_submit_tells_only_the_needinfo_added_since_it_last_told_with_the_commands_of_the_cli(self):
        own = {"bd": ("`bd comments <id>` の最後の `[humanq] 情報待ち`", "`bd label remove <id> needs-info`"),
               "br": ("`br comments <id> --json` を (created_at, id) の昇順に並べ直した最後の `[humanq] 情報待ち`",
                      "`br label remove <id> needs-info`")}
        for cli, other in (("bd", self.BR_CMDS), ("br", self.BD_CMDS)):
            with self.subTest(cli=cli):
                sess = f"tell-{cli}"
                self.run_hook("session_start", self.s(cli, needinfo=["x-1"]), sess)        # SessionStart で x-1 を伝えた
                self.assertEqual(self.run_quiet("prompt_submit", self.s(cli, needinfo=["x-1"]), sess), "", "伝えた情報待ちを伝え直した")
                ctx = self.run_quiet("prompt_submit", self.s(cli, needinfo=["x-1", "x-2", "x-3"]), sess)
                lines = self.added_lines(ctx)
                self.assertEqual(len(lines), 1, ctx)
                self.assertTrue(lines[0].startswith(self.ADDED.format(2, "x-2, x-3")), lines[0])
                for w in own[cli]:
                    self.assertIn(w, lines[0])
                for o in other:
                    self.assertNotIn(o, ctx, f"別の CLI のコマンド ({o}) が入っている")
                self.assertIn("問いを書き直してから", lines[0])
                self.assertIn("問いは取り下げない", lines[0])
                self.assertEqual(self.run_quiet("prompt_submit", self.s(cli, needinfo=["x-1", "x-2", "x-3"]), sess), "",
                                 "伝えた後の同じ情報待ちを、もう一度伝えた")
                # 判断待ちに戻ったもの (x-2) が、もう一度情報待ちになったら伝え直す
                self.assertEqual(self.run_quiet("prompt_submit", self.s(cli, needinfo=["x-1", "x-3"]), sess), "")
                ctx = self.run_quiet("prompt_submit", self.s(cli, needinfo=["x-1", "x-2", "x-3"]), sess)
                self.assertEqual([ln.split("。")[0] + "。" for ln in self.added_lines(ctx)], [self.ADDED.format(1, "x-2")])

    def test_prompt_submit_tells_new_answers_and_new_needinfo_in_one_output(self):
        self.run_hook("session_start", self.s(answered=["x-1"]), "both")
        ctx = self.run_quiet("prompt_submit", self.s(answered=["x-1", "x-4"], needinfo=["x-1"]), "both")
        self.assertEqual(len(ctx.splitlines()), 2, ctx)
        self.assertIn("1 件に回答した: x-4", ctx.splitlines()[0])
        # 回答済みと情報待ちの覚えは別: 回答済みとして伝えた x-1 (取り消して情報待ちにした) も、情報待ちとしては伝える
        self.assertEqual([ln.split("。")[0] + "。" for ln in self.added_lines(ctx)], [self.ADDED.format(1, "x-1")])

    def test_needinfo_hidden_by_unread_at_session_start_is_told_on_a_later_prompt(self):
        # サーバーの起動直後の読み残し: 情報待ちの x-2 は summary の needinfo から抜けて、判断待ちの件数と unread に入る
        ctx = self.run_hook("session_start", self.s(needinfo=["x-1"], human=2, unread=1), "late")
        self.assertIn("情報待ち (エージェントの番) と分かったものは、読み終えた後のプロンプトで ID を伝える", ctx)
        self.assertEqual(self.run_quiet("prompt_submit", self.s(needinfo=["x-1"], unread=1), "late"), "", "読み終える前に伝えた")
        ctx = self.run_quiet("prompt_submit", self.s(needinfo=["x-1", "x-2"]), "late")            # 読み終えた
        self.assertEqual([ln.split("。")[0] + "。" for ln in self.added_lines(ctx)], [self.ADDED.format(1, "x-2")])

    def test_needinfo_told_before_is_kept_while_unread_and_not_told_again(self):
        # 伝えた後でサーバーが入れ替わり、読み残しの間は伝えた x-1 が needinfo から抜ける。読み終えた後に「増えた」と伝え直さない
        self.run_hook("session_start", self.s(needinfo=["x-1"]), "swap")
        self.assertEqual(self.run_quiet("prompt_submit", self.s(needinfo=[], unread=1), "swap"), "")
        self.assertEqual(self.run_quiet("prompt_submit", self.s(needinfo=[], unread=1), "swap"), "", "読み残しが続いた読みで伝えた")
        self.assertEqual(self.run_quiet("prompt_submit", self.s(needinfo=["x-1"]), "swap"), "", "読み残しの後に、伝えた情報待ちを伝え直した")
        # 読み残しが無い読みで抜けたもの (判断待ちに戻った) は忘れるので、もう一度情報待ちになれば伝える
        self.assertEqual(self.run_quiet("prompt_submit", self.s(needinfo=[]), "swap"), "")
        ctx = self.run_quiet("prompt_submit", self.s(needinfo=["x-1"]), "swap")
        self.assertEqual([ln.split("。")[0] + "。" for ln in self.added_lines(ctx)], [self.ADDED.format(1, "x-1")])

    def test_prompt_submit_keeps_the_told_needinfo_when_the_summary_cannot_say(self):
        # needinfo を返さない古いサーバー・list でない needinfo・読めなかったプロジェクトは、情報待ちが無いのか分からない。覚えを消さない
        self.run_hook("session_start", self.s(needinfo=["x-1"]), "keep")
        cases = {"古いサーバー (key が無い)": {"ok": True, "cli": "bd", "human": 0, "answered": []},
                 "文字列": {**self.s(), "needinfo": "x-1"}, "数": {**self.s(), "needinfo": 3}, "null": {**self.s(), "needinfo": None},
                 "object": {**self.s(), "needinfo": {"x-1": 1}},
                 "読めない": {"ok": False, "cli": "bd", "error": "boom", "human": 0, "answered": [], "needinfo": []}}
        for what, summary in cases.items():
            with self.subTest(what):
                self.assertEqual(self.added_lines(self.run_quiet("prompt_submit", summary, "keep")), [])
                self.assertEqual(self.run_quiet("prompt_submit", self.s(needinfo=["x-1"]), "keep"), "", "覚えた情報待ちを消した")

    def test_session_start_without_needinfo_from_an_old_server_keeps_the_told_needinfo(self):
        self.run_hook("session_start", self.s(needinfo=["x-1"]), "old")
        self.run_hook("session_start", {"ok": True, "cli": "bd", "human": 1, "answered": []}, "old")   # 古いサーバーに入れ替わった
        self.assertEqual(self.run_quiet("prompt_submit", self.s(needinfo=["x-1"]), "old"), "", "古いサーバーの SessionStart で覚えを消した")


class BdScenarios:
    """bd・br を通す読み書きのシナリオ。本物の bd (WithBd)・偽の bd (WithFakeBd)・偽の br (WithFakeBr) で回す。

    サブクラスは self.repo と self.tmp (init 済みの使い捨てのプロジェクトと、その置き場) と、次の 2 つを用意する:
      failing(rule, times=None): CLI の呼び出しのうち、argv に rule の語がこの順で現れるものを失敗させる
                                  (times を付けると最初の times 回だけ)。返り値を呼ぶと元に戻す
      calls(rule): setUp から後に、argv に rule の語がこの順で現れた CLI の呼び出しの数
    チケットを作る・読む・書き換える helper (new_question から comments_unreadable_once まで) は bd の形で書いてあり、
    br のクラスは上書きする。シナリオの本文は CLI に依らない"""

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
        """コメントの本文 (古い順)。"""
        return [c["text"] for c in json.loads(bdc(self.repo, "comments", iid, "--json") or "[]")]

    def labels(self, iid: str) -> set[str]:
        return set(json.loads(bdc(self.repo, "show", iid, "--json"))[0].get("labels") or [])

    def label(self, iid: str, op: str, name: str) -> None:
        bdc(self.repo, "label", op, iid, name)

    def set_status(self, iid: str, status: str) -> None:
        bdc(self.repo, "update", iid, "--status", status)

    def close(self, iid: str) -> None:
        bdc(self.repo, "close", iid)

    def backend(self):
        """load_project に渡す backend (None は bd)。"""
        return None

    def comments_unreadable_once(self, iid: str | None = None):
        """次の 1 回のコメントの読み (画面の一覧を作るときの読み) を、読めない状態にする。iid を渡すと、そのチケットの読みだけ
        (共有のプロジェクトの WithBd では、前のテストの回答済みのコメントが先に読まれる)。返り値を呼ぶと元に戻す。"""
        return self.failing("comments" + (f" {iid}" if iid else ""), times=1)

    def store(self) -> humanq.Store:
        sd = os.path.join(self.tmp, "state")
        os.makedirs(sd, exist_ok=True)
        registry.register(sd, self.repo)
        return humanq.Store(sd)

    def write(self, fn, body: dict, raises: type | None = None):
        """Store の書き込み (store.answer / store.undo) を呼ぶ。想定外の例外は FAIL にする (実装の変異で別の型の例外が
        漏れたとき、テストが走っていない ERROR に数えない)。raises を渡すと、その型の例外が上がることを確かめて返す。"""
        try:
            fn(self.repo, body)
        except Exception as e:  # noqa: BLE001
            if raises is not None and isinstance(e, raises):
                return e
            self.fail(f"{fn.__name__} が想定外の例外を上げた: {type(e).__name__}: {e}")
        if raises is not None:
            self.fail(f"{fn.__name__} が {raises.__name__} を上げなかった")
        return None

    def load(self) -> dict:
        """humanq.load_project を呼ぶ。想定外の例外は FAIL にする (write と同じ理由)。"""
        try:
            return humanq.load_project(self.repo, self.backend())
        except Exception as e:  # noqa: BLE001
            self.fail(f"load_project が例外を上げた: {type(e).__name__}: {e}")

    def test_human_and_answered_both_listed_only_as_answered(self):
        iid = self.new_question(labels=("human", "answered"))
        data = self.load()
        self.assertNotIn(iid, [i["id"] for i in data["human"]])
        self.assertIn(iid, [i["id"] for i in data["answered"]])

    def test_resend_after_partial_write_keeps_one_comment(self):
        iid = self.new_question()
        self.add_comment(iid, "エージェントのメモ: 問いを書いた")   # 回答より古いコメント (並びが崩れて返る CLI で、最新を取り違えない)
        store = self.store()
        restore = self.failing("label remove human")
        try:
            e = self.write(store.answer, {"id": iid, "option": "a", "text": "補足"}, raises=humanq.BdError)
            self.assertEqual(getattr(e, "stage", None), "label")
            self.assertIs(getattr(e, "written", None), True)
            self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1)
            self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human", "answered"})
        finally:
            restore()
        self.write(store.answer, {"id": iid, "option": "a", "text": "補足"})  # 送り直し (失敗を外した後)
        self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1, "送り直しでコメントが二重になった")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    def test_resend_with_a_different_answer_adds_a_comment(self):
        iid = self.new_question()
        store = self.store()
        restore = self.failing("label remove human")
        try:
            self.write(store.answer, {"id": iid, "option": "a", "text": ""}, raises=humanq.BdError)
        finally:
            restore()
        self.write(store.answer, {"id": iid, "option": "b", "text": ""})        # 失敗を外した後の回答
        marks = [c for c in self.comments(iid) if c.startswith("[humanq]")]
        self.assertEqual(len(marks), 2)
        self.assertIn("案B", marks[-1])

    def test_label_step_is_retried_once(self):
        iid = self.new_question()
        store = self.store()
        restore = self.failing("label remove human", times=1)
        try:
            self.write(store.answer, {"id": iid, "option": "a", "text": ""})   # 1 回だけの失敗は再試行で通る
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
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()   # shutdown を待たせない
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

        def odd(repo, *rest):
            raise AttributeError("'str' object has no attribute 'get'")

        humanq.load_project = odd
        try:
            d = store.project(self.repo)
            self.assertFalse(d["ok"])
            self.assertIn("error", d)
            self.assertEqual(store.all()[0]["ok"], False)
        finally:
            humanq.load_project = real

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
        found = [i for i in self.load()[key] if i["id"] == iid]
        self.assertEqual(len(found), 1, f"{iid} が {key} に 1 件ない: {found}")
        return found[0]

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

    # ---- /api/summary の fresh (キャッシュの有効期限の内でも読み直す) ----

    def summary(self, port: int, fresh: bool = False) -> dict:
        url = f"http://127.0.0.1:{port}/api/summary?project={urllib.parse.quote(self.repo, safe='')}" + ("&fresh=1" if fresh else "")
        code, _, raw = fetch(url)
        self.assertEqual(code, 200, raw)
        out = json.loads(raw)
        self.assertEqual(out.get("ok"), True, out)
        return out

    def test_fresh_summary_rereads_instead_of_returning_the_cache(self):
        self.new_question()
        port = self.serve(self.store())
        before = self.summary(port, fresh=True)["human"]            # キャッシュを埋める
        self.new_question()                                         # キャッシュの有効期限 (8 秒) の内に増やす
        self.assertEqual(self.summary(port, fresh=True)["human"], before + 1, "fresh=1 なのにキャッシュを返した")

    def test_plain_polling_stays_on_the_cache_and_only_fresh_starts_the_cli(self):
        self.new_question()
        port = self.serve(self.store())
        self.summary(port, fresh=True)
        filled = self.calls("list")
        self.assertGreater(filled, 0, "fresh=1 が CLI を打っていない")
        for _ in range(3):
            code, _, raw = fetch(f"http://127.0.0.1:{port}/api/items")
            self.assertEqual(code, 200, raw)
            self.summary(port)
        self.assertEqual(self.calls("list"), filled, "fresh でないポーリングが CLI を打った (キャッシュを使っていない)")
        self.summary(port, fresh=True)
        self.assertGreater(self.calls("list"), filled, "fresh=1 がキャッシュの内で読み直さなかった")

    # ---- 問いの options の形が崩れていても、そのチケット 1 件だけが問いが未整理になる ----

    BROKEN_OPTIONS = {"数": 5, "真偽値": True, "偽": False, "文字列": "案A", "object": {"key": "a", "label": "案A"},
                      "要素が数": [1, 2], "要素が文字列": ["a"], "key の欠け": [{"label": "案A"}], "label の欠け": [{"key": "a"}],
                      "key が数": [{"key": 1, "label": "案A"}], "正しい要素と壊れた要素": [{"key": "a", "label": "案A"}, 7]}

    def test_a_malformed_options_makes_only_that_question_unstructured(self):
        good = self.new_question()
        for name, options in self.BROKEN_OPTIONS.items():
            with self.subTest(options=name):
                bad = self.new_question()
                self.set_question(bad, options=options)
                data = self.store().project(self.repo, fresh=True)
                self.assertEqual(data.get("ok"), True, f"1 件の崩れた問いで、プロジェクトの全部が読めなくなった: {data.get('error')}")
                by_id = {i["id"]: i for i in data["human"]}
                self.assertIn(bad, by_id, "崩れた問いのチケットが判断待ちから消えた")
                self.assertIsNone(by_id[bad]["q"], "崩れた options の問いを、問いとして出した")
                self.assertIn(good, by_id, "同じプロジェクトのほかの判断待ちが消えた")
                self.assertIsNotNone(by_id[good]["q"])
                self.close(bad)                                     # 次の回の判断待ちに残さない

    def test_answer_for_a_malformed_question_takes_free_text_only(self):
        iid = self.new_question()
        self.set_question(iid, options=5)
        store = self.store()
        self.write(store.answer, {"id": iid, "option": "a"}, raises=ValueError)        # 選択肢は無い
        self.write(store.answer, {"id": iid, "text": "自由記述で答える"})
        self.assertIn(iid, [i["id"] for i in self.load()["answered"]])

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
        self.write(store.answer, {"id": iid2, "option": "a"})                 # 失敗を外した後の回答
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
        self.write(store.answer, {"id": iid, "option": "a"})
        restore = self.failing("label remove answered")
        try:
            e = self.write(store.undo, {"id": iid}, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual((e.stage, e.written), ("label", True))
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human", "answered"})
        data = self.load()
        self.assertIn(iid, [i["id"] for i in data["human"]], "取り消したチケットが判断待ちに出ない")
        self.assertNotIn(iid, [i["id"] for i in data["answered"]])

    def test_both_labels_after_a_partial_answer_stay_answered(self):
        iid = self.new_question()
        store = self.store()
        restore = self.failing("label remove human")
        try:
            self.write(store.answer, {"id": iid, "option": "a"}, raises=humanq.WriteError)
        finally:
            restore()
        data = self.load()
        self.assertNotIn(iid, [i["id"] for i in data["human"]])
        self.assertIn(iid, [i["id"] for i in data["answered"]])

    # 9. 送り直しとみなすのは、最後のコメント全体が同じ回答のときだけ
    def test_answer_after_the_question_was_asked_again_is_recorded(self):
        iid = self.new_question()
        store = self.store()
        self.write(store.answer, {"id": iid, "option": "a"})
        self.add_comment(iid, "問いを書き直した: 案を 1 つ足した")             # README の手順 4
        self.label(iid, "add", "human")
        self.label(iid, "remove", "answered")
        self.write(store.answer, {"id": iid, "option": "a"})                 # 10 分以内に同じ選択肢
        texts = self.comments(iid)
        self.assertEqual(len(texts), 3, texts)
        self.assertTrue(texts[-1].startswith("[humanq] 回答: 案A (a)"), "問い直しの後の回答が記録されていない")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    # 10. 担当中などの open 以外のチケット
    def test_answered_includes_tickets_that_are_not_open(self):
        for status in ("in_progress", "blocked", "hooked"):         # hooked は bd 1.3.4 の組み込み (bd human list に出る)
            with self.subTest(status=status):
                iid = self.new_question()
                self.set_status(iid, status)
                store = self.store()
                self.assertIn(iid, [i["id"] for i in self.load()["human"]])
                self.write(store.answer, {"id": iid, "option": "a"})
                data = self.load()
                self.assertIn(iid, [i["id"] for i in data["answered"]], "回答したチケットがどの一覧にも出ない")
                self.assertNotIn(iid, [i["id"] for i in data["human"]])
                self.write(store.undo, {"id": iid})                          # 取り消しも Gone にならない
                self.assertIn(iid, [i["id"] for i in self.load()["human"]])

    def test_human_and_answered_both_on_an_in_progress_ticket_show_once_as_answered(self):
        iid = self.new_question(labels=("human", "answered"))
        self.set_status(iid, "in_progress")
        data = self.load()
        self.assertNotIn(iid, [i["id"] for i in data["human"]])
        self.assertIn(iid, [i["id"] for i in data["answered"]])

    def test_closed_tickets_are_not_listed_as_answered(self):
        iid = self.new_question(labels=("answered",))
        self.close(iid)
        self.assertNotIn(iid, [i["id"] for i in self.load()["answered"]])

    # ---- 0.7.0: 取り消しの照合 (回答の署名 asig) ----
    # 画面は回答の成功の応答と回答済みの一覧で回答の署名を受け取り、取り消しに付けて送る。サーバーは今の最後の
    # [humanq] コメントの署名と照合し、違えば 409 stale で何も書かない

    def post_json(self, port: int, path: str, body: dict) -> tuple[int, dict]:
        code, _, raw = self.post(port, path, body)
        try:
            j = json.loads(raw)
        except ValueError:
            self.fail(f"{path} の応答が JSON でない: {code} {raw[:200]!r}")
        self.assertIsInstance(j, dict, raw)
        return code, j

    def answer_sig(self, port: int, iid: str, option: str) -> str:
        code, j = self.post_json(port, "/api/answer", {"project": self.repo, "id": iid, "option": option})
        self.assertEqual(code, 200, j)
        self.assertIsInstance(j.get("asig"), str, f"回答の成功の応答に署名が無い: {j}")
        self.assertTrue(j["asig"], j)
        return j["asig"]

    def undo_http(self, port: int, iid: str, **extra) -> tuple[int, dict]:
        return self.post_json(port, "/api/undo", {"project": self.repo, "id": iid, **extra})

    def answered_now(self, port: int, iid: str) -> dict:
        code, _, raw = fetch(f"http://127.0.0.1:{port}/api/items")
        self.assertEqual(code, 200, raw)
        ps = json.loads(raw)["projects"]
        self.assertEqual(len(ps), 1, ps)
        found = [i for i in ps[0]["answered"] if i["id"] == iid]
        self.assertEqual(len(found), 1, ps[0])
        return found[0]

    def test_undo_from_a_stale_answer_does_not_undo_the_answer_given_since(self):
        # 案 A で答える → 別のタブで取り消して案 B で答え直す → 最初のタブに残った案 A のトーストの「取り消す」
        iid = self.new_question()
        port = self.serve(self.store())
        sig_a = self.answer_sig(port, iid, "a")
        code, j = self.undo_http(port, iid, asig=sig_a)                      # 別のタブ (その時点では今の回答)
        self.assertEqual(code, 200, j)
        sig_b = self.answer_sig(port, iid, "b")
        self.assertNotEqual(sig_a, sig_b)
        before = self.comments(iid)
        writes = (self.calls("comments add"), self.calls("label"))
        code, j = self.undo_http(port, iid, asig=sig_a)
        self.assertEqual((code, j.get("code"), j.get("op")), (409, "stale", "undo"), j)
        self.assertEqual(self.comments(iid), before, "署名の違う取り消しがコメントを書いた")
        self.assertEqual((self.calls("comments add"), self.calls("label")), writes, "署名の違う取り消しが bd に書き込んだ")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"}, "今の回答 (案 B) が取り消された")
        self.assertTrue(before and before[-1].startswith("[humanq] 回答: 案B (b)"), before)
        code, j = self.undo_http(port, iid, asig=sig_b)                      # 今の回答の署名なら取り消せる
        self.assertEqual(code, 200, j)
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})

    def test_undo_from_a_stale_answer_with_the_same_text_is_refused(self):
        # 案 A → 取り消し → 案 A で答え直す: 本文が同じでも別の回答なので、前の回答の署名では取り消さない
        iid = self.new_question()
        port = self.serve(self.store())
        sig1 = self.answer_sig(port, iid, "a")
        code, j = self.undo_http(port, iid, asig=sig1)
        self.assertEqual(code, 200, j)
        sig2 = self.answer_sig(port, iid, "a")
        self.assertNotEqual(sig1, sig2, "同じ本文で答え直した回答が、前の回答と同じ署名になる")
        n = len(self.comments(iid))
        code, j = self.undo_http(port, iid, asig=sig1)
        self.assertEqual((code, j.get("code")), (409, "stale"), j)
        self.assertEqual(len(self.comments(iid)), n, "署名の違う取り消しがコメントを書いた")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    def test_answered_items_carry_the_sig_the_answer_returned(self):
        # 回答済みのカードの取り消しも同じ照合を通る: 一覧の asig は回答の応答の asig と同じで、それで取り消せる。
        # エージェントのコメントが回答の前と後に挟まっても変わらない (署名が数えるのは [humanq] のコメントだけ)
        iid = self.new_question()
        port = self.serve(self.store())
        self.add_comment(iid, "エージェントのメモ: 問いを書いた")
        sig = self.answer_sig(port, iid, "a")
        self.add_comment(iid, "エージェントのメモ: 回答を読む前の作業")
        self.assertEqual(self.answered_now(port, iid).get("asig"), sig)
        code, j = self.undo_http(port, iid, asig=sig)
        self.assertEqual(code, 200, j)
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})

    def test_an_answered_ticket_without_a_humanq_comment_is_undone_by_its_card(self):
        # answered だけが付き [humanq] のコメントが無いチケットも、一覧の署名で取り消せる。
        # 一覧でコメントを読めなかったときの署名は、コメントが無いときの署名とも合わない
        iid = self.new_question(labels=("answered",))
        port = self.serve(self.store())
        restore = self.comments_unreadable_once()
        try:
            unread = self.answered_now(port, iid)
        finally:
            restore()
        code, j = self.undo_http(port, iid, asig=unread.get("asig"))
        self.assertEqual((code, j.get("code")), (409, "stale"), (unread, j))
        it = self.answered_now(port, iid)
        self.assertIsInstance(it.get("asig"), str, it)
        code, j = self.undo_http(port, iid, asig=it["asig"])
        self.assertEqual(code, 200, j)
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})

    def test_a_resent_answer_returns_the_sig_of_the_comment_already_there(self):
        # 途中で失敗した回答の送り直し (コメントは書かずにラベルだけ直す) も、残っている回答の署名を返す
        iid = self.new_question()
        port = self.serve(self.store())
        restore = self.failing("label remove human")
        try:
            code, j = self.post_json(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a"})
        finally:
            restore()
        self.assertEqual((code, j.get("stage")), (502, "label"), j)
        sig = self.answer_sig(port, iid, "a")
        self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1, "送り直しでコメントが二重になった")
        self.assertEqual(self.answered_now(port, iid).get("asig"), sig, "送り直しの応答の署名が、残っている回答の署名と違う")

    def test_an_answer_whose_comments_could_not_be_read_is_not_undone_by_its_card(self):
        # 一覧を作るときに回答のコメントを読めなかったチケットには、どの回答とも合わない署名を付ける
        # (null だと「署名を送らない古い画面」と読まれて照合を素通りする)
        iid = self.new_question()
        port = self.serve(self.store())
        self.answer_sig(port, iid, "a")
        restore = self.comments_unreadable_once()
        try:
            it = self.answered_now(port, iid)
        finally:
            restore()
        self.assertEqual(it.get("answer"), "", it)
        self.assertIsInstance(it.get("asig"), str, it)
        code, j = self.undo_http(port, iid, asig=it["asig"])
        self.assertEqual((code, j.get("code")), (409, "stale"), j)
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})

    def test_undo_without_asig_is_still_accepted(self):
        # 署名を送らない古い画面 (更新の前から開いたままのタブ) の取り消しは、今までどおり受け付ける
        iid = self.new_question()
        port = self.serve(self.store())
        self.answer_sig(port, iid, "a")
        code, j = self.undo_http(port, iid)
        self.assertEqual(code, 200, j)
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})

    # ---- 保留 (snooze): コメント 1 件 + snoozed のラベル。status と担当は変えない ----

    def set_assignee(self, iid: str, who: str) -> None:
        bdc(self.repo, "update", iid, "--assignee", who)

    def state(self, iid: str) -> tuple:
        """チケットの (status, 担当)。保留が変えないことを確かめる。"""
        shown = json.loads(bdc(self.repo, "show", iid, "--json"))
        self.assertIsInstance(shown, list, shown)
        self.assertEqual(len(shown), 1, shown)
        return shown[0].get("status"), shown[0].get("assignee")

    @staticmethod
    def day(n: int) -> str:
        return (humanq.today() + timedelta(days=n)).isoformat()

    def marks(self, iid: str) -> list[str]:
        """[humanq] のコメントの本文 (古い順。末尾の空白は落とす: 本物の CLI が末尾の改行を保つかに依らない)。"""
        return [c.rstrip() for c in self.comments(iid) if c.startswith("[humanq]")]

    def ids(self, data: dict, key: str, mine=None) -> list:
        """data[key] の id (並びのまま)。mine を渡すと、その id だけ (共有のプロジェクトの WithBd では、前のテストのチケットが残る)。"""
        return [i["id"] for i in data[key] if mine is None or i["id"] in mine]

    def pick(self, data: dict, key: str, iid: str) -> dict:
        found = [i for i in data[key] if i["id"] == iid]
        self.assertEqual(len(found), 1, f"{iid} が {key} に 1 件ない: {[i['id'] for i in data[key]]}")
        return found[0]

    def http_project(self, port: int) -> dict:
        code, _, raw = fetch(f"http://127.0.0.1:{port}/api/items")
        self.assertEqual(code, 200, raw)
        ps = json.loads(raw)["projects"]
        self.assertEqual(len(ps), 1, ps)
        self.assertTrue(ps[0]["ok"], ps[0])
        return ps[0]

    def test_snooze_until_a_date_moves_the_question_out_of_the_pending_list(self):
        iid, other = self.new_question(), self.new_question()
        self.set_status(iid, "in_progress")                  # エージェントが担当中の問い (status を変えると担当が崩れる)
        self.set_assignee(iid, "agent-x")
        before = self.state(iid)
        self.assertEqual(before, ("in_progress", "agent-x"))
        store = self.store()
        q = self.item(iid)["qsig"]
        self.write(store.snooze, {"id": iid, "until": self.day(3), "note": " 仕様の確認待ち \n", "qsig": q})
        self.assertEqual(self.state(iid), before, "保留で status か担当が変わった")
        self.assertEqual(self.labels(iid) & {"human", "answered", "snoozed"}, {"human", "snoozed"})
        self.assertEqual(self.marks(iid), [f"[humanq] 保留: {self.day(3)} に戻る (qsig: {q})\n\n仕様の確認待ち"])
        data = self.load()
        mine = (iid, other)
        self.assertEqual((self.ids(data, "human", mine), self.ids(data, "snoozed", mine)), ([other], [iid]),
                         "保留した問いが判断待ちに残った")
        self.assertEqual(self.pick(data, "snoozed", iid)["snooze"],
                         {"until": self.day(3), "note": "仕様の確認待ち", "expired": False, "changed": False})
        self.assertIsNone(self.pick(data, "human", other)["snooze"])
        self.assertIsNotNone(self.pick(data, "snoozed", iid)["q"], "保留中のチケットも問いを持つ")

    def test_snooze_without_a_date_and_unsnooze(self):
        iid = self.new_question()
        self.set_assignee(iid, "agent-y")
        before = self.state(iid)
        store = self.store()
        self.write(store.snooze, {"id": iid, "until": None})               # qsig を送らない画面でも、今の問いの署名を書く
        q = self.item(iid, "snoozed")["qsig"]
        self.assertEqual(self.marks(iid), [f"[humanq] 保留: 期限なし (qsig: {q})"])
        data = self.load()
        self.assertEqual(self.ids(data, "human", (iid,)), [])
        self.assertEqual(self.pick(data, "snoozed", iid)["snooze"], {"until": None, "note": "", "expired": False, "changed": False})
        with mock.patch.object(humanq, "today", lambda: date(9999, 12, 30)):       # 期限なしは何年経っても戻らない
            self.assertEqual(self.ids(self.load(), "snoozed", (iid,)), [iid])
        self.write(store.unsnooze, {"id": iid})
        self.assertEqual(self.marks(iid), [f"[humanq] 保留: 期限なし (qsig: {q})", "[humanq] 保留の解除"])
        self.assertEqual(self.labels(iid) & {"human", "snoozed"}, {"human"}, "解除で snoozed が外れていない")
        data = self.load()
        self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "snoozed", (iid,))), ([iid], []))
        self.assertEqual(self.state(iid), before, "保留と解除で status か担当が変わった")

    def test_a_snooze_returns_to_the_pending_list_on_its_date(self):
        iid = self.new_question()
        store = self.store()
        until = self.day(1)                                  # 明日に戻る: 今日は保留、明日の読みから判断待ち
        self.write(store.snooze, {"id": iid, "until": until, "note": "明日見る"})
        self.assertEqual(self.ids(self.load(), "snoozed", (iid,)), [iid], "戻る日の前の日に保留から外れた")
        tomorrow = humanq.today() + timedelta(days=1)
        with mock.patch.object(humanq, "today", lambda: tomorrow):
            data = self.load()
            self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "snoozed", (iid,))), ([iid], []),
                             "戻る日の当日に判断待ちに戻らない")
            self.assertEqual(self.pick(data, "human", iid)["snooze"],
                             {"until": until, "note": "明日見る", "expired": True, "changed": False})
            self.assertIn("snoozed", self.labels(iid), "読むだけでラベルを外した (ラベルは次の回答で外す)")
            self.write(store.answer, {"id": iid, "option": "a"})       # 戻った問いに答えると、保留のラベルも外れる
        self.assertEqual(self.labels(iid) & {"human", "answered", "snoozed"}, {"answered"})
        data = self.load()
        self.assertEqual([self.ids(data, k, (iid,)) for k in ("answered", "human", "snoozed")], [[iid], [], []])

    def test_pressing_snooze_twice_writes_one_comment(self):
        iid = self.new_question()
        store = self.store()
        body = {"id": iid, "until": self.day(7), "note": "2 回押した"}
        self.write(store.snooze, body)
        adds = self.calls("comments add")
        self.write(store.snooze, dict(body))
        self.assertEqual(self.calls("comments add"), adds, "2 回目の保留がコメントを書いた")
        self.assertEqual(len(self.marks(iid)), 1)
        self.write(store.unsnooze, {"id": iid})
        self.write(store.unsnooze, {"id": iid})                         # 解除の 2 回押しも、解除のコメントは 1 件
        self.assertEqual(self.marks(iid)[1:], ["[humanq] 保留の解除"])

    def test_a_snooze_whose_label_step_failed_is_resent_without_a_second_comment(self):
        iid = self.new_question()
        store = self.store()
        body = {"id": iid, "until": self.day(2)}
        restore = self.failing("label add snoozed")
        try:
            e = self.write(store.snooze, body, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual((e.stage, e.written), ("label", True))
        self.assertEqual(self.calls("label add snoozed"), 2, "ラベルの段階を 1 回だけ再試行していない")
        self.assertNotIn("snoozed", self.labels(iid))
        self.assertEqual(self.ids(self.load(), "human", (iid,)), [iid], "ラベルの無い保留を保留中に出した (ラベルが読む印)")
        self.write(store.snooze, body)                                  # 送り直し
        self.assertEqual(len(self.marks(iid)), 1, "送り直しで保留のコメントが二重になった")
        self.assertEqual(self.ids(self.load(), "snoozed", (iid,)), [iid])
        restore = self.failing("label remove snoozed")
        try:
            e = self.write(store.unsnooze, {"id": iid}, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual((e.stage, e.written), ("label", True))
        self.assertEqual(self.ids(self.load(), "human", (iid,)), [iid], "解除のコメントの後は、ラベルが残っていても判断待ち")
        self.write(store.unsnooze, {"id": iid})
        self.assertEqual(self.marks(iid)[1:], ["[humanq] 保留の解除"], "解除の送り直しでコメントが二重になった")
        self.assertNotIn("snoozed", self.labels(iid))

    def test_answered_tickets_cannot_be_snoozed(self):
        store = self.store()
        port = self.serve(store)
        answered = self.new_question(labels=("answered",))
        both = self.new_question(labels=("human", "answered"))         # 画面には回答済みとして出る
        for iid in (answered, both):
            with self.subTest(iid=iid):
                code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": iid, "until": None})
                self.assertEqual((code, j.get("code"), j.get("op")), (400, "gone", "snooze"), j)
                self.assertEqual(self.comments(iid), [], "回答済みのチケットに保留を書いた")
                self.assertNotIn("snoozed", self.labels(iid))
        # 取り消しのラベルの付け替えに失敗した両ラベルのチケット (最後が取り消し) は、判断待ちに出ているので保留できる
        iid = self.new_question()
        self.write(store.answer, {"id": iid, "option": "a"})
        restore = self.failing("label remove answered")
        try:
            self.write(store.undo, {"id": iid}, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human", "answered"})
        self.write(store.snooze, {"id": iid, "until": None})
        data = self.load()
        mine = (iid, answered, both)
        self.assertEqual((self.ids(data, "snoozed", mine), sorted(self.ids(data, "answered", mine))), ([iid], sorted([answered, both])),
                         "保留のコメントの後の両ラベルのチケットを回答済みに出した")
        self.write(store.unsnooze, {"id": iid})
        data = self.load()
        self.assertEqual((self.ids(data, "human", mine), sorted(self.ids(data, "answered", mine))), ([iid], sorted([answered, both])),
                         "保留の解除のコメントの後の両ラベルのチケットを回答済みに出した")

    # 壊れた body: どれも 400 で、CLI に何も書かない
    BAD_SNOOZE = {
        "until が無い": {}, "until が数": {"until": 5}, "until が真偽値": {"until": True}, "until が list": {"until": []},
        "until が object": {"until": {}}, "until が深い入れ子": {"until": [[[[[[[[["x"]]]]]]]]]},
        "月が 13": {"until": "2099-13-01"}, "2 月 30 日": {"until": "2099-02-30"}, "桁が足りない": {"until": "2099-1-1"},
        "区切りなし": {"until": "20991201"}, "全角の数字": {"until": "２０９９-１２-０１"}, "アラビア・インドの数字": {"until": "٢٠٩٩-١٢-٠١"},
        "NUL": {"until": "2099-12-01\x00"}, "後ろの空白": {"until": "2099-12-01 "}, "時刻つき": {"until": "2099-12-01T00:00"},
        "年が 0": {"until": "0000-01-01"}, "過去": {"until": "2000-01-01"},
        "note が数": {"until": None, "note": 5}, "note が list": {"until": None, "note": ["x"]}, "note が object": {"until": None, "note": {"a": 1}},
        "id が無い": {"id": None, "until": None}, "id が数": {"id": 5, "until": None}, "id が空": {"id": "", "until": None},
        "id が list": {"id": ["x"], "until": None},
    }

    def test_snooze_rejects_a_broken_body_without_writing(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        writes = (self.calls("comments add"), self.calls("label"))
        for name, extra in self.BAD_SNOOZE.items():
            with self.subTest(body=name):
                body = {"project": self.repo, "id": iid, **extra}
                code, j = self.post_json(port, "/api/snooze", body)
                self.assertEqual((code, j.get("op")), (400, "snooze"), j)
                self.assertNotIn("code", j, f"形の誤りを gone などと取り違えた: {j}")
                if "id" in extra:
                    code, j = self.post_json(port, "/api/unsnooze", body)
                    self.assertEqual((code, j.get("op")), (400, "unsnooze"), j)
        # 形の誤り (YYYY-MM-DD でない) と、形は合うが日付として読めない・今日以前の日付 (戻る日は明日以降) は、別の文で断る
        for until, head in (("2099-02-30", "until が日付として読めない"), ("2099-12-01 ", "until は YYYY-MM-DD か null"),
                            ("٢٠٩٩-١٢-٠١", "until は YYYY-MM-DD か null"), ("2000-01-01", "戻る日 (2000-01-01) が今日以前"),
                            (self.day(-1), f"戻る日 ({self.day(-1)}) が今日以前"), (self.day(0), f"戻る日 ({self.day(0)}) が今日以前")):
            with self.subTest(until=until):
                code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": iid, "until": until})
                self.assertEqual(code, 400, j)
                self.assertTrue(str(j.get("error")).startswith(head), j)
        # body そのものが深い入れ子 (json の parser の限界) でも 400 で返す
        raw_body = ("[" * 30000 + "]" * 30000).encode()
        code, _, raw = fetch(f"http://127.0.0.1:{port}/api/snooze", raw_body,
                             {"Content-Type": "application/json", "X-Humanq-Token": "tok", "Host": f"127.0.0.1:{port}"})
        self.assertEqual(code, 400, raw)
        self.assertEqual((self.calls("comments add"), self.calls("label")), writes, "壊れた body で書き込んだ")
        self.assertEqual(self.comments(iid), [])

    def test_snooze_for_a_changed_question_is_not_recorded(self):
        iid = self.new_question()
        port = self.serve(self.store())
        seen = self.item(iid)["qsig"]
        self.set_question(iid, ask="書き直した問い")
        code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": iid, "until": None, "qsig": seen})
        self.assertEqual((code, j.get("code"), j.get("op")), (409, "changed", "snooze"), j)
        self.assertEqual(self.comments(iid), [])
        self.assertNotIn("snoozed", self.labels(iid))
        code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": iid, "until": None, "qsig": self.item(iid)["qsig"]})
        self.assertEqual(code, 200, j)

    def counts(self, port: int) -> tuple:
        s = self.summary(port, fresh=True)
        return s.get("human"), s.get("snoozed"), s.get("returned")

    def test_snooze_over_http_and_the_counts_leave_out_snoozed(self):
        port = self.serve(self.store())
        h0, s0, r0 = self.counts(port)                       # 共有のプロジェクト (WithBd) では前のテストのチケットが残る
        a, b, c = self.new_question(), self.new_question(), self.new_question()
        for iid, until in ((a, self.day(3)), (b, self.day(1))):
            code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": iid, "until": until, "note": "n"})
            self.assertEqual((code, j), (200, {"ok": True}))
        self.assertEqual(self.counts(port), (h0 + 1, s0 + 2, r0))
        p = self.http_project(port)
        self.assertEqual((self.ids(p, "human", (a, b, c)), self.ids(p, "snoozed", (a, b, c))), ([c], [b, a]), "保留中は期日の早い順")
        tomorrow = humanq.today() + timedelta(days=1)
        with mock.patch.object(humanq, "today", lambda: tomorrow):
            h1, s1, r1 = self.counts(port)
        self.assertEqual((h1 - h0, r1 - r0), (2, 1), "戻る日の来た b が判断待ちと「戻った」に数えられていない")
        self.assertLessEqual(s1, s0 + 1)
        code, j = self.post_json(port, "/api/unsnooze", {"project": self.repo, "id": a})
        self.assertEqual((code, j), (200, {"ok": True}))
        self.assertEqual(self.counts(port)[:2], (h0 + 2, s0 + 1))
        code, j = self.post_json(port, "/api/unsnooze", {"project": self.repo, "id": "hq-nothing"})
        self.assertEqual((code, j.get("code"), j.get("op")), (400, "gone", "unsnooze"), j)
        restore = self.failing("label add snoozed")
        try:
            code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": c, "until": None})
        finally:
            restore()
        self.assertEqual((code, j.get("op"), j.get("stage"), j.get("written")), (502, "snooze", "label", True), j)

    def test_snoozed_tickets_come_in_the_order_of_their_dates_and_no_date_last(self):
        x, y, z = self.new_question(), self.new_question(), self.new_question()
        store = self.store()
        for iid, until in ((x, None), (y, self.day(5)), (z, self.day(1))):
            self.write(store.snooze, {"id": iid, "until": until})
        self.assertEqual(self.ids(self.load(), "snoozed", (x, y, z)), [z, y, x], "期日の早い順・期限なしは最後でない")

    def test_a_snoozed_ticket_whose_comments_cannot_be_read_is_shown_as_pending(self):
        iid = self.new_question()
        store = self.store()
        self.write(store.snooze, {"id": iid, "until": self.day(5)})
        restore = self.comments_unreadable_once(iid)
        try:
            data = self.load()
        finally:
            restore()
        self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "snoozed", (iid,))), ([iid], []),
                         "期日を読めない保留を判断待ちから隠した")
        self.assertIsNone(self.pick(data, "human", iid)["snooze"])
        self.assertEqual(self.ids(self.load(), "snoozed", (iid,)), [iid], "読めるようになったら保留中に戻る")

    # ---- 保留は問いに結び付く (保留のコメントの問いの署名) ----

    def read(self, store: humanq.Store) -> dict:
        """サーバーの読み (Store.project、fresh。保留の thread の覚えを使う)。読めたことを確かめる。"""
        try:
            data = store.project(store.resolve(self.repo) or self.repo, fresh=True)     # HTTP と同じく代表のパスで読む
        except Exception as e:  # noqa: BLE001
            self.fail(f"Store.project が例外を上げた: {type(e).__name__}: {e}")
        self.assertTrue(data.get("ok"), data)
        return data

    def test_a_new_question_after_the_question_was_taken_off_is_not_hidden_by_an_old_snooze(self):
        # 期限なしで保留 → エージェントが画面の外で片付けて human を外す → 同じチケットで新しく問う
        iid = self.new_question()
        store = self.store()
        self.write(store.snooze, {"id": iid, "until": None})
        self.assertEqual(self.ids(self.read(store), "snoozed", (iid,)), [iid])
        self.label(iid, "remove", "human")
        self.set_question(iid, ask="まったく別の新しい問い")
        self.label(iid, "add", "human")
        for data in (self.load(), self.read(store)):
            self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "snoozed", (iid,))), ([iid], []),
                             "新しい問いが古い保留に隠れた")
            self.assertEqual(self.pick(data, "human", iid)["snooze"], {"until": None, "note": "", "expired": False, "changed": True})

    def test_a_rewritten_question_brings_the_snooze_back_and_a_desc_edit_does_not(self):
        iid, other = self.new_question(), self.new_question()
        store = self.store()
        port = self.serve(store)
        for i in (iid, other):
            self.write(store.snooze, {"id": i, "until": self.day(5), "note": "待ち"})
        h0, s0, r0 = self.counts(port)
        # 選択肢の説明 (desc) だけの直しは問いの署名に入らないので、保留のまま
        opts = json.loads(json.dumps(QUESTION))["humanq"]["options"]
        opts[0]["desc"] = "言い回しだけ直した"
        self.set_question(other, options=opts)
        self.set_question(iid, ask="前提が変わったので問い直す")
        data = self.read(store)
        self.assertEqual((self.ids(data, "human", (iid, other)), self.ids(data, "snoozed", (iid, other))), ([iid], [other]))
        self.assertEqual(self.pick(data, "human", iid)["snooze"], {"until": self.day(5), "note": "待ち", "expired": False, "changed": True})
        self.assertEqual(self.counts(port), (h0 + 1, s0 - 1, r0 + 1), "書き換わって戻った保留を「戻った」に数えていない")
        # 戻ったカードからもう一度保留すれば、今の問いで保留になる
        self.write(store.snooze, {"id": iid, "until": None, "qsig": self.pick(data, "human", iid)["qsig"]})
        self.assertEqual(self.ids(self.read(store), "snoozed", (iid, other)), [other, iid])

    # ---- 回答と保留のラベル ----

    def test_answer_of_a_returned_snooze_whose_label_removal_failed_is_resent(self):
        iid = self.new_question()
        store = self.store()
        self.write(store.snooze, {"id": iid, "until": self.day(1)})
        tomorrow = humanq.today() + timedelta(days=1)
        with mock.patch.object(humanq, "today", lambda: tomorrow):
            self.assertEqual(self.ids(self.read(store), "human", (iid,)), [iid])        # 戻った (覚えを作る)
            restore = self.failing("label remove snoozed")
            try:
                e = self.write(store.answer, {"id": iid, "option": "a"}, raises=humanq.WriteError)
            finally:
                restore()
            self.assertEqual((e.stage, e.written), ("label", True))
            self.assertEqual(self.labels(iid) & {"human", "answered", "snoozed"}, {"human", "snoozed"},
                             "保留のラベルを外す前に、ラベルを付け替えた")
            data = self.read(store)
            self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "snoozed", (iid,))), ([iid], []),
                             "回答のコメントの後のチケットを保留として隠した (書き込みの後に覚えを捨てていない)")
            self.write(store.answer, {"id": iid, "option": "a"})                       # 送り直しで残りを済ませる
        self.assertEqual(self.labels(iid) & {"human", "answered", "snoozed"}, {"answered"})
        self.assertEqual(len([m for m in self.marks(iid) if m.startswith("[humanq] 回答")]), 1, "送り直しで回答が二重になった")

    def test_answer_removes_only_a_snoozed_label_that_humanq_put(self):
        # 利用者が自分の意味で付けた snoozed (humanq の保留のコメントが無い) は、回答で外さない
        own = self.new_question(labels=("human", "snoozed"))
        store = self.store()
        self.assertEqual(self.ids(self.load(), "human", (own,)), [own], "コメントの無い snoozed を保留中に出した")
        self.write(store.answer, {"id": own, "option": "a"})
        self.assertEqual(self.labels(own) & {"human", "answered", "snoozed"}, {"answered", "snoozed"}, "humanq が付けていない snoozed を外した")
        # 保留の解除でラベルを外し損ねたもの (最後の [humanq] コメントが解除) は、humanq が付けたラベルなので外す
        left = self.new_question()
        self.write(store.snooze, {"id": left, "until": None})
        restore = self.failing("label remove snoozed")
        try:
            self.write(store.unsnooze, {"id": left}, raises=humanq.WriteError)
        finally:
            restore()
        self.assertIn("snoozed", self.labels(left))
        self.write(store.answer, {"id": left, "option": "a"})
        self.assertEqual(self.labels(left) & {"human", "answered", "snoozed"}, {"answered"})
        # 取り消して答え直した (最後の [humanq] コメントが取り消し) チケットに後から付いた snoozed も、humanq のものではない
        again = self.new_question()
        self.write(store.answer, {"id": again, "option": "a"})
        self.write(store.undo, {"id": again})
        self.label(again, "add", "snoozed")
        self.write(store.answer, {"id": again, "option": "b"})
        self.assertIn("snoozed", self.labels(again))

    def test_answer_and_undo_reject_an_id_that_is_not_a_string(self):
        self.new_question()
        port = self.serve(self.store())
        writes = (self.calls("comments add"), self.calls("label"))
        for path in ("/api/answer", "/api/undo"):
            for name, iid in {"list": ["x"], "数": 5, "object": {"a": 1}, "null": None, "空": ""}.items():
                with self.subTest(path=path, id=name):
                    code, j = self.post_json(port, path, {"project": self.repo, "id": iid, "option": "a"})
                    self.assertEqual((code, j.get("op")), (400, path.rsplit("/", 1)[1]), j)
                    self.assertNotIn("code", j, f"形の誤りを gone などと取り違えた: {j}")
        self.assertEqual((self.calls("comments add"), self.calls("label")), writes, "壊れた id で書き込んだ")

    def test_items_carry_the_server_today(self):
        # 画面は期間の選択と日付の下限を、ブラウザの TZ ではなくサーバーの今日で数える
        port = self.serve(self.store())
        for day in (date(2031, 1, 31), date(2032, 2, 29)):
            with self.subTest(day=day), mock.patch.object(humanq, "today", lambda: day):
                code, _, raw = fetch(f"http://127.0.0.1:{port}/api/items")
                self.assertEqual(code, 200, raw)
                self.assertEqual(json.loads(raw).get("today"), day.isoformat())

    # ---- 情報待ち (needinfo): コメント 1 件 + needs-info のラベル。human・status・担当は変えない ----
    # エージェントは足りない情報をチケットに足し、問いを書き直してから needs-info を外す (README の「回答を拾う」)

    HQ_LABELS = {"human", "answered", "snoozed", "needs-info"}

    def test_needinfo_moves_the_question_out_of_the_pending_list(self):
        iid, other = self.new_question(), self.new_question()
        self.set_status(iid, "in_progress")                  # エージェントが担当中の問い (status を変えると担当が崩れる)
        self.set_assignee(iid, "agent-x")
        before = self.state(iid)
        store = self.store()
        q = self.item(iid)["qsig"]
        self.write(store.needinfo, {"id": iid, "note": " どの環境の話か\n本番か検証か \n", "qsig": q})
        self.assertEqual(self.state(iid), before, "情報待ちで status か担当が変わった")
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"human", "needs-info"})
        self.assertEqual(self.marks(iid), [f"[humanq] 情報待ち (qsig: {q})\n\nどの環境の話か\n本番か検証か"])
        for data in (self.load(), self.read(store)):
            mine = (iid, other)
            self.assertEqual([self.ids(data, k, mine) for k in ("human", "needinfo", "snoozed", "answered")], [[other], [iid], [], []],
                             "情報待ちにした問いが判断待ちに残った")
            got = self.pick(data, "needinfo", iid)
            self.assertEqual((got["needinfo"], got["qsig"]), ({"note": "どの環境の話か\n本番か検証か"}, q))
            self.assertIsNotNone(got["q"], "情報待ちのチケットも問いを持つ")

    def test_after_the_agent_adds_the_information_and_rewrites_the_question_it_is_pending_again(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        self.write(store.needinfo, {"id": iid, "note": "見積もりの根拠"})
        old = self.pick(self.read(store), "needinfo", iid)["qsig"]
        # エージェント: 足りない情報を背景に足して問いを書き直し、needs-info を外す
        self.set_question(iid, context="見積もりの根拠: 過去 3 件の実績から 2 日")
        self.label(iid, "remove", "needs-info")
        for data in (self.load(), self.read(store)):
            self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "needinfo", (iid,))), ([iid], []),
                             "書き直した問いが判断待ちに戻らない")
            now = self.pick(data, "human", iid)
            self.assertNotEqual(now["qsig"], old)
            self.assertIsNone(now["snooze"])
        code, j = self.post_json(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a", "qsig": now["qsig"]})
        self.assertEqual(code, 200, j)
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"answered"})

    def test_a_rewritten_question_whose_label_was_left_comes_back_and_the_answer_removes_the_label(self):
        iid, kept = self.new_question(), self.new_question()
        store = self.store()
        port = self.serve(store)
        for i in (iid, kept):
            self.write(store.needinfo, {"id": i, "note": "n"})
        s0 = self.summary(port, fresh=True)
        opts = json.loads(json.dumps(QUESTION))["humanq"]["options"]
        opts[0]["desc"] = "言い回しだけ直した"
        self.set_question(kept, options=opts)                # 選択肢の説明 (desc) だけの直しは問いの署名に入らない: 情報待ちのまま
        self.set_question(iid, context="足した情報")          # 問いを書き直したが、needs-info を外し忘れた
        data = self.read(store)
        self.assertEqual((self.ids(data, "human", (iid, kept)), self.ids(data, "needinfo", (iid, kept))), ([iid], [kept]))
        self.assertIn("needs-info", self.labels(iid), "読むだけでラベルを外した (ラベルは次の回答で外す)")
        s1 = self.summary(port, fresh=True)
        self.assertEqual((s1["human"] - s0["human"], [x for x in s1["needinfo"] if x in (iid, kept)]), (1, [kept]))
        self.write(store.answer, {"id": iid, "option": "a"})
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"answered"}, "humanq が付けた needs-info を回答で外していない")

    def test_answer_whose_needs_info_removal_failed_is_resent(self):
        iid = self.new_question()
        store = self.store()
        self.write(store.needinfo, {"id": iid, "note": "n"})
        self.set_question(iid, context="足した情報")          # 書き直したが、ラベルを外し忘れた
        restore = self.failing("label remove needs-info")
        try:
            e = self.write(store.answer, {"id": iid, "option": "a"}, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual((e.stage, e.written), ("label", True))
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"human", "needs-info"}, "情報待ちのラベルを外す前に、ラベルを付け替えた")
        data = self.read(store)
        self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "needinfo", (iid,))), ([iid], []),
                         "回答のコメントの後のチケットを情報待ちとして隠した")
        self.write(store.answer, {"id": iid, "option": "a"})                       # 送り直しで残りを済ませる
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"answered"})
        self.assertEqual(len([m for m in self.marks(iid) if m.startswith("[humanq] 回答")]), 1, "送り直しで回答が二重になった")

    def test_answer_removes_only_a_needs_info_label_that_humanq_put(self):
        # 利用者が自分の意味で付けた needs-info (humanq の情報待ちのコメントが無い) は、情報待ちに出さず、回答で外さない
        own = self.new_question(labels=("human", "needs-info"))
        store = self.store()
        self.assertEqual(self.ids(self.load(), "human", (own,)), [own], "コメントの無い needs-info を情報待ちに出した")
        self.write(store.answer, {"id": own, "option": "a"})
        self.assertEqual(self.labels(own) & self.HQ_LABELS, {"answered", "needs-info"}, "humanq が付けていない needs-info を外した")
        # 取り消して答え直した (最後の [humanq] コメントが取り消し) チケットに後から付いた needs-info も、humanq のものではない
        again = self.new_question()
        self.write(store.answer, {"id": again, "option": "a"})
        self.write(store.undo, {"id": again})
        self.label(again, "add", "needs-info")
        self.write(store.answer, {"id": again, "option": "b"})
        self.assertIn("needs-info", self.labels(again))

    def test_the_last_humanq_comment_decides_between_a_snooze_and_needinfo(self):
        # 保留した後に (前の画面から) 情報待ちにした: 最後の [humanq] コメントは情報待ちなので、情報待ちに出す。保留の解除は何も書かない
        iid = self.new_question()
        store = self.store()
        self.write(store.snooze, {"id": iid, "until": None})
        self.write(store.needinfo, {"id": iid, "note": "n"})
        data = self.read(store)
        self.assertEqual([self.ids(data, k, (iid,)) for k in ("human", "snoozed", "needinfo")], [[], [], [iid]])
        adds = self.calls("comments add")
        self.write(store.unsnooze, {"id": iid})
        self.assertEqual(self.calls("comments add"), adds, "保留でないチケットの解除で、解除のコメントを書いた")
        self.assertEqual(self.ids(self.read(store), "needinfo", (iid,)), [iid])

    # 保留と情報待ちは続けて書かれうる (戻る日が来たカードを情報待ちにする・情報待ちのカードを前の画面から保留する)。
    # どの順で経由しても、回答の後に humanq が付けたラベル (snoozed・needs-info) は残らない
    # S 保留 (明日まで) / D 戻る日が来る / U 保留の解除 / N 情報待ち / R 問いを書き直して needs-info を外す
    # L 問いを書き直したが needs-info を外し忘れる / W 情報待ちの取り下げ / A 回答 / K 取り消し
    HQ_ORDERS = {
        "保留 → 戻る → 情報待ち → 書き直して外す": "SDNR", "保留 → 戻る → 情報待ち → 外し忘れ": "SDNL",
        "情報待ち → 保留 → 戻る": "NSD", "情報待ち → 外し忘れ → 保留 → 解除": "NLSU", "情報待ち → 外し忘れ → 保留 → 戻る": "NLSD",
        "情報待ち → 書き直して外す → 保留 → 解除": "NRSU", "保留 → 情報待ち": "SN", "保留 → 情報待ち → 保留": "SNS",
        "保留 → 解除": "SU", "情報待ち": "N", "保留 → 戻る": "SD", "保留 → 解除 → 情報待ち": "SUN",
        "回答 → 取り消し → 保留 → 戻る": "AKSD", "回答 → 取り消し → 情報待ち": "AKN", "回答 → 取り消し → 情報待ち → 保留 → 戻る": "AKNSD",
        "情報待ち → 取り下げ": "NW", "情報待ち → 取り下げ → 情報待ち": "NWN", "保留 → 情報待ち → 取り下げ": "SNW",
        "情報待ち → 外し忘れ → 取り下げ": "NLW", "情報待ち → 取り下げ → 保留 → 戻る": "NWSD",
    }

    def run_hq_order(self, order: str) -> str:
        """order の順に状態を経由させ、まだ回答していないチケットの ID を返す (時計は D で進める)。"""
        iid = self.new_question()
        store = self.store()
        clock = [humanq.today()]
        n = 0
        with mock.patch.object(humanq, "today", lambda: clock[0]):
            for step in order:
                n += 1
                if step == "S":
                    self.write(store.snooze, {"id": iid, "until": self.day(1)})
                elif step == "D":
                    clock[0] = clock[0] + timedelta(days=2)
                elif step == "U":
                    self.write(store.unsnooze, {"id": iid})
                elif step == "N":
                    self.write(store.needinfo, {"id": iid, "note": "n"})
                elif step == "W":
                    self.write(store.unneedinfo, {"id": iid})
                elif step in "RL":
                    self.set_question(iid, context=f"足した情報 {n}")
                    if step == "R" and "needs-info" in self.labels(iid):
                        self.label(iid, "remove", "needs-info")
                elif step == "A":
                    self.write(store.answer, {"id": iid, "option": "a"})
                elif step == "K":
                    self.write(store.undo, {"id": iid})
                else:
                    self.fail(f"知らない段 {step}")
        self.hq_clock = clock[0]
        return iid

    def test_answer_removes_the_labels_humanq_put_whatever_order_the_states_were_passed(self):
        for name, order in self.HQ_ORDERS.items():
            with self.subTest(order=name):
                iid = self.run_hq_order(order)
                with mock.patch.object(humanq, "today", lambda: self.hq_clock):
                    self.write(self.store().answer, {"id": iid, "option": "b"})
                self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"answered"}, f"回答の後に humanq のラベルが残った ({order})")

    def test_resent_answer_removes_both_labels_humanq_put(self):
        # 回答のラベルの段階が失敗して送り直す (最後のコメントがこの回答) ときも、前の回答より後の保留・情報待ちを両方見る
        for rule, order in (("label remove snoozed", "SDNR"), ("label remove needs-info", "SDNL"), ("label remove needs-info", "NSD")):
            with self.subTest(rule=rule, order=order):
                iid = self.run_hq_order(order)
                store = self.store()
                with mock.patch.object(humanq, "today", lambda: self.hq_clock):
                    restore = self.failing(rule)
                    try:
                        e = self.write(store.answer, {"id": iid, "option": "a"}, raises=humanq.WriteError)
                    finally:
                        restore()
                    self.assertEqual((e.stage, e.written), ("label", True))
                    self.write(store.answer, {"id": iid, "option": "a"})
                self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"answered"})
                self.assertEqual(len([m for m in self.marks(iid) if m.startswith("[humanq] 回答")]), 1, "送り直しで回答が二重になった")

    def test_answer_keeps_a_label_the_user_put_after_the_previous_answer_whatever_order(self):
        # 前の回答より前の保留・情報待ちは見ない: 回答 → 取り消し の後に利用者が付けたラベルは、保留や情報待ちを経由した後でも外さない
        for order in ("SNAK", "NSAK", "SDNLAK"):
            with self.subTest(order=order):
                iid = self.run_hq_order(order)
                self.label(iid, "add", "snoozed")
                self.label(iid, "add", "needs-info")
                self.write(self.store().answer, {"id": iid, "option": "b"})
                self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"answered", "snoozed", "needs-info"})

    BAD_NEEDINFO = {
        "note が無い": {}, "note が空": {"note": ""}, "note が空白だけ": {"note": " \n\t "}, "note が数": {"note": 5},
        "note が null": {"note": None}, "note が list": {"note": ["x"]}, "note が object": {"note": {"a": 1}},
        "note が真偽値": {"note": True},
        "id が無い": {"id": None, "note": "n"}, "id が数": {"id": 5, "note": "n"}, "id が空": {"id": "", "note": "n"},
        "id が list": {"id": ["x"], "note": "n"},
    }

    def test_needinfo_rejects_a_broken_body_without_writing(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        writes = (self.calls("comments add"), self.calls("label"))
        for name, extra in self.BAD_NEEDINFO.items():
            with self.subTest(body=name):
                code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, **extra})
                self.assertEqual((code, j.get("op")), (400, "needinfo"), j)
                self.assertNotIn("code", j, f"形の誤りを gone などと取り違えた: {j}")
        # 文字列でないものと、空 (空白だけを含む) は別の文で断る
        for note, head in ((5, "note (足りない情報) を文字列で"), ("", "note (足りない情報) が空"), ("  \n", "note (足りない情報) が空")):
            with self.subTest(note=note):
                code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, "note": note})
                self.assertEqual(code, 400, j)
                self.assertTrue(str(j.get("error")).startswith(head), j)
        self.assertEqual((self.calls("comments add"), self.calls("label")), writes, "壊れた body で書き込んだ")
        self.assertEqual(self.comments(iid), [])

    def test_needinfo_for_a_changed_question_an_answered_or_a_missing_ticket_is_not_recorded(self):
        iid = self.new_question()
        store = self.store()
        port = self.serve(store)
        seen = self.item(iid)["qsig"]
        self.set_question(iid, ask="書き直した問い")
        code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, "note": "n", "qsig": seen})
        self.assertEqual((code, j.get("code"), j.get("op")), (409, "changed", "needinfo"), j)
        self.assertEqual(self.comments(iid), [])
        self.assertNotIn("needs-info", self.labels(iid))
        answered = self.new_question(labels=("answered",))
        both = self.new_question(labels=("human", "answered"))          # 画面には回答済みとして出る
        for i in (answered, both, "hq-nothing"):
            with self.subTest(iid=i):
                code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": i, "note": "n"})
                self.assertEqual((code, j.get("code"), j.get("op")), (400, "gone", "needinfo"), j)
        for i in (answered, both):
            self.assertEqual(self.comments(i), [], "回答済みのチケットに情報待ちを書いた")
            self.assertNotIn("needs-info", self.labels(i))
        code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, "note": "n", "qsig": self.item(iid)["qsig"]})
        self.assertEqual((code, j), (200, {"ok": True}))

    def test_a_needinfo_whose_label_step_failed_is_resent_and_pressing_twice_writes_one_comment(self):
        iid = self.new_question()
        store = self.store()
        body = {"id": iid, "note": "2 回押した"}
        restore = self.failing("label add needs-info")
        try:
            e = self.write(store.needinfo, body, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual((e.stage, e.written), ("label", True))
        self.assertEqual(self.calls("label add needs-info"), 2, "ラベルの段階を 1 回だけ再試行していない")
        self.assertNotIn("needs-info", self.labels(iid))
        self.assertEqual(self.ids(self.load(), "human", (iid,)), [iid], "ラベルの無い情報待ちを情報待ちに出した (ラベルが読む印)")
        self.write(store.needinfo, body)                               # 送り直し
        self.write(store.needinfo, dict(body))                         # 2 回押し
        self.assertEqual(len(self.marks(iid)), 1, "送り直し・2 回押しで情報待ちのコメントが二重になった")
        self.assertEqual(self.ids(self.load(), "needinfo", (iid,)), [iid])

    def test_needinfo_over_http_and_the_summary(self):
        port = self.serve(self.store())
        base = self.summary(port, fresh=True)               # 共有のプロジェクト (WithBd) では前のテストのチケットが残る
        a, b = self.new_question(), self.new_question()
        code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": a, "note": "n"})
        self.assertEqual((code, j), (200, {"ok": True}))
        s = self.summary(port, fresh=True)
        self.assertEqual((s["human"] - base["human"], s["snoozed"] - base["snoozed"]), (1, 0), "情報待ちを判断待ちか保留中に数えた")
        self.assertEqual([x for x in s["needinfo"] if x in (a, b)], [a])
        p = self.http_project(port)
        self.assertEqual((self.ids(p, "human", (a, b)), self.ids(p, "needinfo", (a, b))), ([b], [a]))
        self.assertEqual(self.pick(p, "needinfo", a)["needinfo"], {"note": "n"})
        restore = self.failing("label add needs-info")
        try:
            code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": b, "note": "n"})
        finally:
            restore()
        self.assertEqual((code, j.get("op"), j.get("stage"), j.get("written")), (502, "needinfo", "label", True), j)

    def test_a_needinfo_ticket_whose_comments_cannot_be_read_is_shown_as_pending(self):
        iid = self.new_question()
        store = self.store()
        self.write(store.needinfo, {"id": iid, "note": "n"})
        restore = self.comments_unreadable_once(iid)
        try:
            data = self.load()
        finally:
            restore()
        self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "needinfo", (iid,))), ([iid], []),
                         "足りない情報を読めない情報待ちを判断待ちから隠した")
        self.assertEqual(data["unread"], 1, "読めない情報待ちを読み残しに数えていない")
        self.assertEqual(self.ids(self.load(), "needinfo", (iid,)), [iid], "読めるようになったら情報待ちに戻る")

    # ---- 情報待ちの取り下げ (unneedinfo): 取り下げのコメント 1 件を書いて needs-info を外す (保留の解除と同じ形)。
    # 押し間違えた・ユーザーが自分で情報を見つけたときに、同じ問いのまま判断待ちに戻す。human・status・担当は変えない ----

    UNNEEDINFO = "[humanq] 取り下げ: 情報待ち"

    def test_unneedinfo_returns_the_same_question_to_the_pending_list(self):
        iid, other = self.new_question(), self.new_question()
        self.set_status(iid, "in_progress")
        self.set_assignee(iid, "agent-z")
        before = self.state(iid)
        store = self.store()
        q = self.item(iid)["qsig"]
        self.write(store.needinfo, {"id": iid, "note": "押し間違えた"})
        self.write(store.needinfo, {"id": other, "note": "n"})
        self.write(store.unneedinfo, {"id": iid})
        self.assertEqual(self.marks(iid), [f"[humanq] 情報待ち (qsig: {q})\n\n押し間違えた", self.UNNEEDINFO])
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"human"}, "取り下げで needs-info が外れていない")
        self.assertEqual(self.labels(other) & self.HQ_LABELS, {"human", "needs-info"}, "ほかの情報待ちのラベルを外した")
        self.assertEqual(self.state(iid), before, "取り下げで status か担当が変わった")
        for data in (self.load(), self.read(store)):
            mine = (iid, other)
            self.assertEqual([self.ids(data, k, mine) for k in ("human", "needinfo", "snoozed", "answered")], [[iid], [other], [], []],
                             "取り下げた問いが判断待ちに戻らない")
            got = self.pick(data, "human", iid)
            self.assertEqual((got["qsig"], got["snooze"]), (q, None), "取り下げた問いが同じ問いのまま戻っていない")
        self.write(store.answer, {"id": iid, "option": "a", "qsig": q})
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"answered"})

    def test_an_unneedinfo_whose_label_step_failed_is_pending_and_the_resend_or_the_answer_removes_the_label(self):
        iid, kept = self.new_question(), self.new_question()
        store = self.store()
        for i in (iid, kept):
            self.write(store.needinfo, {"id": i, "note": "n"})
        restore = self.failing("label remove needs-info")
        try:
            e = self.write(store.unneedinfo, {"id": iid}, raises=humanq.WriteError)
            self.write(store.unneedinfo, {"id": kept}, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual((e.stage, e.written), ("label", True))
        self.assertEqual(self.calls("label remove needs-info"), 4, "ラベルの段階を 1 回だけ再試行していない")
        self.assertEqual(self.labels(iid) & self.HQ_LABELS, {"human", "needs-info"})
        data = self.read(store)
        self.assertEqual((sorted(self.ids(data, "human", (iid, kept))), self.ids(data, "needinfo", (iid, kept))),
                         (sorted([iid, kept]), []), "取り下げのコメントの後のチケットを、ラベルが残っているので情報待ちに隠した")
        adds = self.calls("comments add")
        self.write(store.unneedinfo, {"id": iid})                       # 送り直し: コメントは書かずにラベルだけ外す
        self.assertEqual(self.calls("comments add"), adds, "取り下げの送り直しでコメントを書いた")
        self.assertEqual(self.marks(iid)[1:], [self.UNNEEDINFO])
        self.assertNotIn("needs-info", self.labels(iid))
        self.assertEqual(self.calls("label remove needs-info"), 5)
        self.write(store.unneedinfo, {"id": iid})                       # 2 回押し: 何も書かない
        self.assertEqual((self.calls("comments add"), self.calls("label remove needs-info")), (adds, 5), "取り下げの 2 回押しで書いた")
        # 送り直さずに答えても、humanq が付けて残った needs-info は回答で外す
        self.write(store.answer, {"id": kept, "option": "a"})
        self.assertEqual(self.labels(kept) & self.HQ_LABELS, {"answered"}, "取り下げの後に残った needs-info を回答で外していない")

    def test_unneedinfo_of_a_pending_ticket_that_is_not_waiting_for_info_writes_nothing(self):
        # まだ情報待ちにしていない判断待ちと、取り下げの後にエージェントのコメントが足されたもの (最後のコメントが取り下げでないので、
        # 送り直しの判定では止まらない) への取り下げは、コメントもラベルも書かない
        fresh, back = self.new_question(), self.new_question()
        store = self.store()
        self.write(store.needinfo, {"id": back, "note": "n"})
        self.write(store.unneedinfo, {"id": back})
        self.add_comment(back, "エージェントのメモ\n")
        adds, removes = self.calls("comments add"), self.calls("label remove")
        for iid in (fresh, back):
            with self.subTest(iid=iid):
                self.write(store.unneedinfo, {"id": iid})
        self.assertEqual((self.calls("comments add"), self.calls("label remove")), (adds, removes), "情報待ちでない判断待ちに取り下げを書いた")
        self.assertEqual((self.marks(fresh), self.marks(back)[1:]), ([], [self.UNNEEDINFO]))
        self.assertEqual(self.labels(fresh) & self.HQ_LABELS, {"human"})

    def test_a_ticket_with_both_labels_whose_last_humanq_comment_is_an_unneedinfo_is_pending(self):
        # 回答・取り消しのラベルの付け替えの途中で human と answered が両方付いたチケットは、最後の [humanq] コメントで向きを決める
        iid = self.new_question(labels=("human", "answered"))
        self.add_comment(iid, "[humanq] 回答: 案A (a)\n")
        self.add_comment(iid, self.UNNEEDINFO + "\n")
        data = self.load()
        self.assertEqual((self.ids(data, "human", (iid,)), self.ids(data, "answered", (iid,))), ([iid], []),
                         "最後が取り下げのチケットを回答済みに出した")

    def test_unneedinfo_over_http_and_for_a_ticket_that_is_not_pending(self):
        iid, b = self.new_question(), self.new_question()
        store = self.store()
        port = self.serve(store)
        for i in (iid, b):
            code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": i, "note": "n"})
            self.assertEqual((code, j), (200, {"ok": True}))
        base = self.summary(port, fresh=True)
        self.assertIn(iid, base["needinfo"])
        code, j = self.post_json(port, "/api/unneedinfo", {"project": self.repo, "id": iid})
        self.assertEqual((code, j), (200, {"ok": True}))
        s = self.summary(port, fresh=True)
        self.assertEqual(([x for x in s["needinfo"] if x in (iid, b)], s["human"] - base["human"]), ([b], 1),
                         "取り下げた問いが判断待ちの件数に戻らない")
        p = self.http_project(port)
        self.assertEqual((self.ids(p, "human", (iid, b)), self.ids(p, "needinfo", (iid, b))), ([iid], [b]))
        answered = self.new_question(labels=("answered",))
        for i in (answered, "hq-nothing"):
            with self.subTest(iid=i):
                code, j = self.post_json(port, "/api/unneedinfo", {"project": self.repo, "id": i})
                self.assertEqual((code, j.get("code"), j.get("op")), (400, "gone", "unneedinfo"), j)
        self.assertEqual(self.comments(answered), [])
        for name, extra in {"id が無い": {}, "id が数": {"id": 5}, "id が空": {"id": ""}, "id が list": {"id": [b]}}.items():
            with self.subTest(body=name):
                code, j = self.post_json(port, "/api/unneedinfo", {"project": self.repo, **extra})
                self.assertEqual((code, j.get("op")), (400, "unneedinfo"), j)
                self.assertNotIn("code", j, f"形の誤りを gone などと取り違えた: {j}")
        restore = self.failing("label remove needs-info")
        try:
            code, j = self.post_json(port, "/api/unneedinfo", {"project": self.repo, "id": b})
        finally:
            restore()
        self.assertEqual((code, j.get("op"), j.get("stage"), j.get("written")), (502, "unneedinfo", "label", True), j)

    # ---- UserPromptSubmit の hook: セッションの途中で情報待ちになったものを、サーバーのキャッシュから伝える ----

    def hook_for(self, port: int):
        """このプロジェクトの state dir と、このサーバーの port に向けた hook のモジュール (サーバーの起動だけ差し替える)。"""
        env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": os.path.join(self.tmp, "state"), "HUMANQ_PORT": str(port)})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location(f"humanq_hook_scenario_{port}", HOOK_PATH)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        hook.ensure_server = lambda sd: "running"
        return hook

    def hook_says(self, hook, fn: str, session: str) -> str:
        """hook の fn を呼び、Claude に渡す文 (出力が無ければ "")。例外は FAIL にする。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                getattr(hook, fn)({"cwd": self.repo, "session_id": session})
            except Exception as e:  # noqa: BLE001
                self.fail(f"hook の {fn} が例外を出した: {type(e).__name__}: {e}")
        out = buf.getvalue().strip()
        if not out:
            return ""
        self.assertEqual(len(out.splitlines()), 1, out)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def told_cmds(self, line: str, cli: str) -> list[str]:
        """hook の 1 行が勧めるコマンド (`` ` `` で囲んだもののうち CLI の名前で始まるもの)。どれもそのプロジェクトの CLI のもの。"""
        cmds = [c for c in re.findall(r"`([^`]+)`", line) if c.split()[:1] in (["bd"], ["br"])]
        self.assertEqual(len(cmds), 2, f"読み方と外し方の 2 つでない: {cmds}")
        self.assertEqual({c.split()[0] for c in cmds}, {cli}, cmds)
        return cmds

    def run_told(self, cmd: str, iid: str) -> str:
        """hook が勧めたコマンドの <id> を iid に差し替え、1 文字も加工せずに shell の規則で分けてプロジェクトの dir で打つ。"""
        self.assertIn("<id>", cmd)
        argv = shlex.split(cmd.replace("<id>", iid), comments=True)
        p = subprocess.run(argv, cwd=self.repo, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, f"{argv}: {p.stderr or p.stdout}")
        return p.stdout

    def test_prompt_submit_tells_a_needinfo_marked_during_the_session_once_and_the_told_commands_reach_the_ticket(self):
        a, b = self.new_question(), self.new_question()
        store = self.store()
        port = self.serve(store)
        hook = self.hook_for(port)
        cli = self.summary(port, fresh=True)["cli"]
        sess = f"mid-{type(self).__name__}-{self.n}"         # 共有のプロジェクト (WithBd) の前のテストのセッションと分ける
        self.hook_says(hook, "session_start", sess)          # 前のテストの情報待ちは、ここで伝えて覚える
        self.assertEqual(self.hook_says(hook, "prompt_submit", sess), "")
        for i in (a, b):
            code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": i, "note": f"足りない {i}"})
            self.assertEqual((code, j), (200, {"ok": True}))
        self.summary(port, fresh=True)                       # 画面の読み (hook が見るキャッシュを埋める。hook は CLI を打たない)
        ctx = self.hook_says(hook, "prompt_submit", sess)
        lines = [ln for ln in ctx.splitlines() if "情報待ち (エージェントの番) になったもの" in ln]
        self.assertEqual(len(lines), 1, ctx)
        head = lines[0].split("。")[0]
        self.assertIn("になったものが 2 件ある: ", head)
        self.assertEqual(sorted(head.split("件ある: ", 1)[1].split(", ")), sorted([a, b]), "前のテストの情報待ちも伝えた、か a・b を伝えない")
        self.assertEqual(self.hook_says(hook, "prompt_submit", sess), "", "伝えた情報待ちを次のプロンプトでも伝えた")
        # 勧めたコマンドを、<id> を差し替えて打つ: 読み方は a の足りない情報を出し、外し方は a のラベルだけを外す
        read, drop = self.told_cmds(lines[0], cli)
        out = self.run_told(read, a)
        self.assertIn(f"足りない {a}", out)
        self.assertIn("[humanq] 情報待ち (qsig: ", out)
        self.assertNotIn(f"足りない {b}", out, "読み方が別のチケットのコメントを出した")
        self.run_told(drop, a)
        self.assertEqual((self.labels(a) & self.HQ_LABELS, self.labels(b) & self.HQ_LABELS), ({"human"}, {"human", "needs-info"}),
                         "外し方が名前どおりのチケットのラベルを外していない")
        # SessionStart の情報待ちの 1 行が勧めるコマンドも同じ (新しいセッション)
        self.summary(port, fresh=True)
        ctx = self.hook_says(hook, "session_start", sess + "-2")
        lines = [ln for ln in ctx.splitlines() if ln.startswith("humanq: 情報待ち ") and "(エージェントの番)" in ln]
        self.assertEqual(len(lines), 1, ctx)
        head = lines[0].split("。")[0]
        self.assertIn("(エージェントの番): ", head)
        told = head.split("(エージェントの番): ", 1)[1].split(", ")
        self.assertIn(b, told)
        self.assertNotIn(a, told, "ラベルを外して判断待ちに戻ったものを情報待ちと伝えた")
        read, drop = self.told_cmds(lines[0], cli)
        self.assertIn(f"足りない {b}", self.run_told(read, b))
        self.run_told(drop, b)
        self.assertNotIn("needs-info", self.labels(b))


class BdOnly:
    """bd のプロジェクトだけのシナリオ (WithBd と WithFakeBd。br には bd の --json の形が無い)。"""

    repo: str

    def test_answered_includes_a_ticket_with_a_custom_status(self):
        """status.custom で宣言した独自の status (category が wip か未指定) のチケットは bd human list に出る。回答した後も、
        回答済みの一覧に出る (status を列挙して読むと、列挙に無い status はどの一覧からも消える)。"""
        bdc(self.repo, "config", "set", "status.custom", "waiting,tuning:wip")
        for status in ("waiting", "tuning"):
            with self.subTest(status=status):
                iid = self.new_question()
                self.set_status(iid, status)
                store = self.store()
                self.assertIn(iid, [i["id"] for i in self.load()["human"]])
                self.write(store.answer, {"id": iid, "option": "a"})
                data = self.load()
                self.assertIn(iid, [i["id"] for i in data["answered"]], "独自の status のチケットがどの一覧にも出ない")
                self.assertNotIn(iid, [i["id"] for i in data["human"]])

    # ---- 保留の thread の覚え: bd は保留 1 件ごとに bd comments を打つので、読むたびには打たない ----

    def comment_reads(self, ids) -> int:
        """ids のチケットのコメントの読み (bd comments <id> --json。書き込みの comments add は数えない) の数。"""
        return sum(self.calls(f"comments {i} --json") for i in ids)

    def test_reads_after_the_first_do_not_read_the_snoozed_comments_again(self):
        store = self.store()
        port = self.serve(store)
        ids = [self.new_question() for _ in range(4)]
        for i in ids:
            self.write(store.snooze, {"id": i, "until": None, "note": "n"})
        first = self.summary(port, fresh=True)
        n = self.comment_reads(ids)
        for _ in range(2):                                   # hook の fresh=1 の読みも、画面の読みも覚えを使う
            self.assertEqual(self.summary(port, fresh=True)["snoozed"], first["snoozed"])
            # 並びは見ない (期限なし・同じ優先度で、本物の bd の作成日は秒の単位なので同じ値になりうる)
            self.assertEqual(sorted(self.ids(self.http_project(port), "snoozed", ids)), sorted(ids))
        self.assertEqual(self.comment_reads(ids), n, "2 回目からの読みで、保留のコメントを読み直した")
        # このサーバーの書き込み (保留の補足を書き換える) は、そのチケットだけ読み直す
        self.write(store.snooze, {"id": ids[0], "until": None, "note": "書き換えた補足"})
        n = self.comment_reads(ids)
        data = self.read(store)
        self.assertEqual(self.comment_reads(ids), n + 1, "書いたチケットだけを読み直していない")
        self.assertEqual(self.pick(data, "snoozed", ids[0])["snooze"]["note"], "書き換えた補足", "書き込みの後も古い覚えを使った")
        # 解除のラベルの段階の失敗 (ラベルが残る) も、覚えを捨てるので判断待ちに出る
        restore = self.failing("label remove snoozed")
        try:
            self.write(store.unsnooze, {"id": ids[1]}, raises=humanq.WriteError)
        finally:
            restore()
        self.assertEqual(self.ids(self.read(store), "human", ids), [ids[1]], "解除のコメントの後のチケットを、古い覚えで保留に隠した")
        # ほかで変わったラベルと問いは、コメントを読まずに一覧で分かる
        self.label(ids[2], "remove", "snoozed")
        self.set_question(ids[3], ask="書き直した問い")
        n = self.comment_reads(ids)
        data = self.read(store)
        self.assertEqual(self.comment_reads(ids), n, "ラベルと問いの変化を見るのにコメントを読んだ")
        self.assertEqual((sorted(self.ids(data, "human", ids)), self.ids(data, "snoozed", ids)), (sorted(ids[1:]), [ids[0]]))
        self.assertIsNone(self.pick(data, "human", ids[2])["snooze"])
        self.assertTrue(self.pick(data, "human", ids[3])["snooze"]["changed"])

    def test_snoozes_left_unread_by_the_time_limit_are_pending_and_read_behind(self):
        store = self.store()
        ids = [self.new_question() for _ in range(3)]
        for i in ids:
            self.write(store.snooze, {"id": i, "until": None})
        store._threads.budget = 0.0                         # 時間の上限に当たって、1 件も読めない読み
        data = self.read(store)
        self.assertEqual((sorted(self.ids(data, "human", ids)), self.ids(data, "snoozed", ids)), (sorted(ids), []),
                         "期日を読めていない保留を隠した")
        for i in ids:
            self.assertIsNone(self.pick(data, "human", i)["snooze"])
        key = store.resolve(self.repo) or self.repo
        deadline = time.monotonic() + 30                    # 裏の読みが読み終えるのを待つ
        while time.monotonic() < deadline and (store._warming or not all(store._threads.get(key, i)[1] for i in ids)):
            time.sleep(0.05)
        self.assertEqual(store._warming, set(), "裏の読みが終わらない")
        n = self.comment_reads(ids)
        # 裏の読みはキャッシュを古い扱いにする: キャッシュの時間の内の読み (画面のポーリング) も、覚えで作り直す
        cached = store.project(key)
        self.assertTrue(cached.get("ok"), cached)
        self.assertEqual(sorted(self.ids(cached, "snoozed", ids)), sorted(ids), "裏で読んだ後も、キャッシュの古い結果 (判断待ちに出したもの) を返した")
        self.assertEqual(sorted(self.ids(self.read(store), "snoozed", ids)), sorted(ids), "裏で読んだ覚えを次の読みが使っていない")
        self.assertEqual(self.comment_reads(ids), n)

    def test_summary_counts_the_snoozes_left_unread_and_the_hook_says_so(self):
        store = self.store()
        port = self.serve(store)
        base = self.summary(port, fresh=True)               # 共有のプロジェクトでは、前のテストの保留が残る
        self.assertEqual(base.get("unread"), 0, "読み残しが無いのに unread を数えた")
        stale = self.new_question()                         # 保留のラベルだけ残った (コメントは読めて、保留ではない) 判断待ち
        self.label(stale, "add", "snoozed")
        base = self.summary(port, fresh=True)               # stale を読んで覚えさせる。読めているので unread に数えない
        self.assertEqual(base.get("unread"), 0, "コメントを読めていて保留でないものを読み残しに数えた")
        ids = [self.new_question() for _ in range(3)]
        for i in ids:
            self.write(store.snooze, {"id": i, "until": None})
        self.new_question()                                 # 保留でない判断待ちは、コメントを読まないので読み残しに数えない
        store._threads.budget = 0.0                         # 時間の上限に当たって、書いた 3 件のコメントを読めない読み
        s = self.summary(port, fresh=True)
        self.assertEqual((s.get("unread"), s.get("human") - base["human"], s.get("snoozed") - base["snoozed"]),
                         (3, 4, 0), "読み残した保留は判断待ちに出して、unread に数える")
        spec = importlib.util.spec_from_file_location("humanq_hook_unread_test", HOOK_PATH)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        self.assertIn("判断待ちの件数のうち 3 件は、保留中か情報待ちかを確認中", hook.snooze_line(s) or "", "summary の件数が hook の文面に出ない")
        key = store.resolve(self.repo) or self.repo
        deadline = time.monotonic() + 30                    # 裏の読みが読み終えるのを待つ
        while time.monotonic() < deadline and (store._warming or not all(store._threads.get(key, i)[1] for i in ids)):
            time.sleep(0.05)
        s = self.summary(port, fresh=True)
        self.assertEqual((s.get("unread"), s.get("snoozed") - base["snoozed"], s.get("human")), (0, 3, base["human"] + 1),
                         "読み終えた後も unread が残った")

    def test_a_needinfo_left_unread_at_session_start_is_told_on_a_prompt_after_the_read(self):
        # サーバーの起動直後の読み残し: 情報待ちの ID は summary の needinfo から抜け、判断待ちの件数と unread に入る。
        # SessionStart の hook はそれを確認中と言い、読み終えた後の UserPromptSubmit が情報待ちとして伝える
        store = self.store()
        port = self.serve(store)
        iid = self.new_question()
        self.write(store.needinfo, {"id": iid, "note": "n"})          # 書き込みはそのチケットの覚えを捨てる
        store._threads.budget = 0.0                                  # 時間の上限に当たって、コメントを読めない読み
        hook = self.hook_for(port)
        sess = f"late-{type(self).__name__}-{self.n}"
        ctx = self.hook_says(hook, "session_start", sess)
        unread = [ln for ln in ctx.splitlines() if "保留中か情報待ちかを確認中" in ln]
        self.assertEqual(len(unread), 1, ctx)
        self.assertIn("情報待ち (エージェントの番) と分かったものは、読み終えた後のプロンプトで ID を伝える", unread[0])
        told = [ln.split("。")[0].split("(エージェントの番): ", 1)[-1].split(", ") for ln in ctx.splitlines()
                if ln.startswith("humanq: 情報待ち ")]
        self.assertNotIn(iid, sum(told, []), "前提: 読み残した情報待ちを SessionStart で伝えた")
        key = store.resolve(self.repo) or self.repo
        deadline = time.monotonic() + 30                            # 裏の読みが読み終えるのを待つ
        while time.monotonic() < deadline and (store._warming or not store._threads.get(key, iid)[1]):
            time.sleep(0.05)
        self.assertEqual(store._warming, set(), "裏の読みが終わらない")
        self.summary(port, fresh=True)                               # 読み終えた後の画面の読み (hook が見るキャッシュを埋める)
        ctx = self.hook_says(hook, "prompt_submit", sess)
        lines = [ln for ln in ctx.splitlines() if "情報待ち (エージェントの番) になったもの" in ln]
        self.assertEqual(len(lines), 1, ctx)
        head = lines[0].split("。")[0]
        self.assertIn("件ある: ", head)
        self.assertIn(iid, head.split("件ある: ", 1)[1].split(", "), "読み残しの後に情報待ちと分かったものを伝えない")

    def test_a_read_behind_racing_a_resnooze_does_not_memorise_the_old_comment(self):
        ids = [self.new_question() for _ in range(2)]
        store = self.store()
        for i in ids:
            self.write(store.snooze, {"id": i, "until": None, "note": "old"})
        store._threads.budget = 0.0                         # 全部読み残して、裏の読みに回す
        be = humanq.BACKENDS["bd"]
        real = be.comments
        entered, go = threading.Event(), threading.Event()

        def slow(repo, iid):
            out = real(repo, iid)                           # 書き込みの前のコメント (補足 old)
            if iid == ids[0] and threading.current_thread().name.startswith("ThreadPoolExecutor") and not go.is_set():
                entered.set()
                go.wait(10)
            return out
        with mock.patch.object(be, "comments", slow):
            self.read(store)
            self.assertTrue(entered.wait(10), "裏の読みが始まらない")
            store._threads.budget = 120.0
            self.write(store.snooze, {"id": ids[0], "until": None, "note": "new"})   # ラベルは残る
            go.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and store._warming:
                time.sleep(0.05)
        self.assertEqual(self.pick(self.read(store), "snoozed", ids[0])["snooze"]["note"], "new", "裏の読みが古いコメントで覚え直した")

    def test_a_fresh_read_waiting_behind_a_read_returns_that_read(self):
        # SessionStart の hook (fresh=1) が画面の読みの最中に来たら、その読みの結果を受け取り、もう 1 回分を読まない
        self.new_question()
        store = self.store()
        key = store.resolve(self.repo) or self.repo
        real = humanq.bd
        started = threading.Event()
        lists = []

        def slow(repo, *args):
            if list(args[:2]) == ["human", "list"]:
                lists.append(args)
                started.set()
                time.sleep(1.0)
            return real(repo, *args)
        got: dict = {}

        def poll():
            try:
                got["poll"] = store.project(key)
            except Exception as e:  # noqa: BLE001
                got["poll"] = e
        with mock.patch.object(humanq, "bd", slow):
            th = threading.Thread(target=poll)
            th.start()
            try:
                self.assertTrue(started.wait(10), "画面の読みが始まらない")
                fresh = store.project(key, fresh=True)
            finally:
                th.join(30)
        self.assertIsInstance(got.get("poll"), dict, got)
        self.assertTrue(got["poll"].get("ok"), got["poll"])
        self.assertEqual(len(lists), 1, "fresh の読みが、待っていた間に終わった読みの後に、もう一度読んだ")
        self.assertIs(fresh, got["poll"], "fresh の読みが、待っていた間に終わった読みの結果を返していない")

    # ---- 回答・取り消し・読みあたりの bd の起動の数: 回答済みの件数に比例して増えない ----
    # 回答済みのコメントは覚え (ThreadMemo) から読む。0.18.1 までは読みのたびに回答済み 1 件ごとに bd comments を打ち
    # (N+1)、回答済みが 20 件なら回答の後の読みは 23 回、取り消しの後と普段の読みは 22 回だった (bd は 1 回 0.2〜0.3 秒)
    CLI_RUNS = {"回答": 5, "回答の後の読み": 3, "取り消し": 5, "取り消しの後の読み": 2, "普段の読み": 2}

    def test_cli_runs_per_answer_undo_and_read_do_not_grow_with_the_answered(self):
        made = 0
        for n in (0, 5, 20):
            for _ in range(n - made):
                self.new_question(labels=("answered",))     # 回答済み (コメントの中身は打つ数に効かない)
            made = n
            with self.subTest(answered=n):
                store = self.store()
                iid = self.new_question()
                self.read(store)                            # 最初の読み (回答済みのコメントを読んで覚える。ここは件数に比例する)
                steps = (("回答", lambda: self.write(store.answer, {"id": iid, "option": "a"})),
                         ("回答の後の読み", lambda: self.read(store)),
                         ("取り消し", lambda: self.write(store.undo, {"id": iid})),
                         ("取り消しの後の読み", lambda: self.read(store)),
                         ("普段の読み", lambda: self.read(store)))
                got = {}
                for name, step in steps:
                    before = self.calls("")                 # 空の規則は全部の呼び出しに当たる
                    step()
                    got[name] = self.calls("") - before
                self.assertEqual(got, self.CLI_RUNS, f"回答済みが {n} 件のときの bd の起動の数")

    def test_an_answer_rewritten_elsewhere_is_picked_up_after_a_refused_undo(self):
        # 回答済みのコメントは覚えから読むので、ほかで書かれた [humanq] のコメント (別の端末で取り消して答え直した、など) は
        # 最大 10 分遅れて出る。遅れて出た古いカードからの取り消しは照合で断られ、覚えを捨てるので、次の読みで今の回答が出る
        iid = self.new_question()
        store = self.store()
        self.write(store.answer, {"id": iid, "option": "a"})
        old = self.pick(self.read(store), "answered", iid)
        self.add_comment(iid, "[humanq] 取り消し\n")             # ほかの端末の humanq (ラベルは回答済みに戻っている)
        self.add_comment(iid, "[humanq] 回答: 案B (b)\n")
        self.assertEqual(self.pick(self.read(store), "answered", iid)["asig"], old["asig"], "覚えの内なのにコメントを読み直した")
        self.write(store.undo, {"id": iid, "asig": old["asig"]}, raises=humanq.Stale)
        now = self.pick(self.read(store), "answered", iid)
        self.assertTrue(now["answer"].startswith("[humanq] 回答: 案B (b)"), f"断った取り消しの後も古い覚えを使った: {now}")
        self.assertNotEqual(now["asig"], old["asig"])
        self.write(store.undo, {"id": iid, "asig": now["asig"]})
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})

    def test_bd_json_rejects_non_list(self):
        def listed(*args):
            try:
                return humanq.bd_json(self.repo, *args)
            except Exception as e:  # noqa: BLE001  実装の変異で上がった例外を、走っていない ERROR にしない
                self.fail(f"bd_json が例外を上げた: {type(e).__name__}: {e}")

        real = humanq.bd
        try:
            humanq.bd = lambda repo, *a: '{"issues": []}'
            with self.assertRaises(humanq.BdError):
                humanq.bd_json(self.repo, "list")
            humanq.bd = lambda repo, *a: "Update available: bd 9.9\n[]"
            with self.assertRaises(humanq.BdError):
                humanq.bd_json(self.repo, "list")
            humanq.bd = lambda repo, *a: "No human-needed beads found.\n"
            self.assertEqual(listed("human", "list"), [])
            humanq.bd = lambda repo, *a: "null\n"
            self.assertEqual(listed("human", "list"), [])
        finally:
            humanq.bd = real


@unittest.skipUnless(HAVE_BD, real_cli.skip_reason("bd"))
class WithBd(BdScenarios, BdOnly, unittest.TestCase):
    """本物の bd で回す (env HUMANQ_TEST_BD を立てた手元だけ)。失敗は humanq.bd を差し替えて作る。"""

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

    def store(self) -> humanq.Store:
        # 共有のプロジェクトには前のテストの保留が溜まり、本物の bd では 1 回の読みの時間の上限 (コメントの読み 2 秒) に当たる。
        # 当たると、残りを裏で読むたびに件数がテストの途中で動くので、上限を広げる (上限を当てるテストは自分で 0 にする)
        store = super().store()
        store._threads.budget = 120.0
        return store

    def failing(self, rule: str, times: int | None = None):
        r = [rule.split(), times]
        self._rules.append(r)

        def restore():
            if r in self._rules:
                self._rules.remove(r)
        return restore

    def calls(self, rule: str) -> int:
        return sum(1 for a in self._calls if argv_has(rule.split(), a))


class FakeLog:
    """偽の CLI (tests/fake_beads.py) の失敗の注入と呼び出しの記録 (WithFakeBd と WithFakeBr)。
    失敗は env の FAKE_BEADS_FAIL で偽物に作らせ、呼び出しは FAKE_BEADS_LOG の記録 (self.log) で数える。"""

    log: str

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


class WithFakeBd(FakeLog, BdScenarios, BdOnly, unittest.TestCase):
    """偽の bd (tests/fake_beads.py) で回す。本物の bd が無い環境 (CI) でも skip しない。

    偽物を置いた dir を PATH の先頭に入れるのは、テストごとの setUp で行い addCleanup で戻す (HAVE_BD はモジュールの
    読み込み時に決まるので、偽物を PATH に残したままにしない)。プロジェクトはテストごとに作り直す (偽物の init は速い)。"""

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

    # 今のサーバーが打つ bd の argv の 8 形を、読み → 回答 → 読み → 読み → 取り消し → 読みの順に並べたもの。
    # bd を利用している人の挙動を、後で呼び出しを作り直しても変えないための固定 (<repo> <id> <tmp> は置き換えて比べる)。
    # 読みはどれもキャッシュの切れた読み (画面は 15 秒ごとに読み、キャッシュは 8 秒)
    HUMAN_LIST = ["-C", "<repo>", "human", "list", "--json"]
    ANSWERED_LIST = ["-C", "<repo>", "list", "--label", "answered", "--flat", "-n", "0", "--json"]    # status では絞らない
    COMMENTS = ["-C", "<repo>", "comments", "<id>", "--json"]
    COMMENT_ADD = ["-C", "<repo>", "--actor", "user/humanq", "comments", "add", "<id>", "-f", "<tmp>"]
    ADD_ANSWERED = ["-C", "<repo>", "--actor", "user/humanq", "label", "add", "<id>", "answered"]
    REMOVE_HUMAN = ["-C", "<repo>", "--actor", "user/humanq", "label", "remove", "<id>", "human"]
    ADD_HUMAN = ["-C", "<repo>", "--actor", "user/humanq", "label", "add", "<id>", "human"]
    REMOVE_ANSWERED = ["-C", "<repo>", "--actor", "user/humanq", "label", "remove", "<id>", "answered"]
    GOLDEN = [
        HUMAN_LIST, ANSWERED_LIST,                                              # 読み (回答済みは 0 件)
        HUMAN_LIST, COMMENTS, COMMENT_ADD, ADD_ANSWERED, REMOVE_HUMAN,          # 回答
        HUMAN_LIST, ANSWERED_LIST, COMMENTS,                                    # 読み (回答したチケットの覚えは捨ててあるので、本文を読む)
        HUMAN_LIST, ANSWERED_LIST,                                              # 次の読み: 回答済みの本文は覚えから読み、COMMENTS を
                                                                                # 打たない (0.18.1 までは打ち、回答済み 1 件ごとに 1 回増えた)
        ANSWERED_LIST, COMMENTS, COMMENT_ADD, ADD_HUMAN, REMOVE_ANSWERED,       # 取り消し
        HUMAN_LIST, ANSWERED_LIST,                                              # 読み (取り消したチケットは回答済みに無い)
    ]

    def test_golden_argv(self):
        iid = self.new_question()
        port = self.serve(self.store())
        cache = mock.patch.object(humanq, "CACHE_SEC", 0.0)                    # どの読みもキャッシュの切れた読みにする
        cache.start()
        self.addCleanup(cache.stop)
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
        code, j = self.post_json(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a", "qsig": p["human"][0]["qsig"]})
        self.assertEqual(code, 200, j)
        p = project()
        self.assertEqual(([i["id"] for i in p["human"]], [i["id"] for i in p["answered"]]), ([], [iid]))
        self.assertTrue(p["answered"][0]["answer"].startswith("[humanq] 回答: 案A (a)"), p["answered"][0])
        # 取り消しは画面と同じく回答の署名を付けて送る。照合は回答の応答・一覧・取り消しの読みに相乗りし、bd の呼び出しを増やさない
        self.assertEqual(p["answered"][0].get("asig"), j.get("asig"), (p["answered"][0], j))
        again = project()                                                     # 覚えから作った回答済みのカードも同じ
        self.assertEqual(again["answered"], p["answered"], "覚えから作った回答済みのカードが、読んで作ったものと違う")
        code, j = self.undo_http(port, iid, asig=j.get("asig"))
        self.assertEqual(code, 200, j)
        p = project()
        self.assertEqual(([i["id"] for i in p["human"]], [i["id"] for i in p["answered"]]), ([iid], []))

        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN)

    ADD_SNOOZED = ["-C", "<repo>", "--actor", "user/humanq", "label", "add", "<id>", "snoozed"]
    REMOVE_SNOOZED = ["-C", "<repo>", "--actor", "user/humanq", "label", "remove", "<id>", "snoozed"]
    # 保留 → 読み (保留のラベルの付いた判断待ちのコメントを読む) → 解除 → 読み (ラベルが無いのでコメントは読まない)。
    # status を変える呼び出し (update・defer) は無い
    GOLDEN_SNOOZE = [
        HUMAN_LIST, COMMENTS, COMMENT_ADD, ADD_SNOOZED,
        HUMAN_LIST, ANSWERED_LIST, COMMENTS,
        HUMAN_LIST, COMMENTS, COMMENT_ADD, REMOVE_SNOOZED,
        HUMAN_LIST, ANSWERED_LIST,
    ]

    def test_golden_argv_of_snooze(self):
        iid = self.new_question()
        port = self.serve(self.store())
        start = len(self.log_entries())
        code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": iid, "until": self.day(1), "note": "n"})
        self.assertEqual(code, 200, j)
        self.assertEqual([i["id"] for i in self.http_project(port)["snoozed"]], [iid])
        code, j = self.post_json(port, "/api/unsnooze", {"project": self.repo, "id": iid})
        self.assertEqual(code, 200, j)
        self.assertEqual([i["id"] for i in self.http_project(port)["human"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_SNOOZE)

    ADD_NEEDINFO = ["-C", "<repo>", "--actor", "user/humanq", "label", "add", "<id>", "needs-info"]
    # 情報待ち → 読み (情報待ちのラベルの付いた判断待ちのコメントを読む)。human を外す・status を変える呼び出しは無い。
    # エージェントが問いを書き直してラベルを外した後の読みは、コメントを読まない
    GOLDEN_NEEDINFO = [HUMAN_LIST, COMMENTS, COMMENT_ADD, ADD_NEEDINFO, HUMAN_LIST, ANSWERED_LIST, COMMENTS]
    GOLDEN_NEEDINFO_BACK = [HUMAN_LIST, ANSWERED_LIST]

    def test_golden_argv_of_needinfo(self):
        iid = self.new_question()
        port = self.serve(self.store())
        cache = mock.patch.object(humanq, "CACHE_SEC", 0.0)                    # どの読みもキャッシュの切れた読みにする
        cache.start()
        self.addCleanup(cache.stop)
        start = len(self.log_entries())
        code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, "note": "n"})
        self.assertEqual(code, 200, j)
        self.assertEqual([i["id"] for i in self.http_project(port)["needinfo"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_NEEDINFO)
        self.set_question(iid, context="足した情報")
        self.label(iid, "remove", "needs-info")
        start = len(self.log_entries())
        self.assertEqual([i["id"] for i in self.http_project(port)["human"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_NEEDINFO_BACK)

    REMOVE_NEEDINFO = ["-C", "<repo>", "--actor", "user/humanq", "label", "remove", "<id>", "needs-info"]
    # 情報待ちの取り下げ → 読み (ラベルが無いのでコメントは読まない)。human を外す・status を変える呼び出しは無い
    GOLDEN_UNNEEDINFO = [HUMAN_LIST, COMMENTS, COMMENT_ADD, REMOVE_NEEDINFO, HUMAN_LIST, ANSWERED_LIST]

    def test_golden_argv_of_unneedinfo(self):
        iid = self.new_question()
        port = self.serve(self.store())
        cache = mock.patch.object(humanq, "CACHE_SEC", 0.0)                    # どの読みもキャッシュの切れた読みにする
        cache.start()
        self.addCleanup(cache.stop)
        code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, "note": "n"})
        self.assertEqual(code, 200, j)
        start = len(self.log_entries())
        code, j = self.post_json(port, "/api/unneedinfo", {"project": self.repo, "id": iid})
        self.assertEqual((code, j), (200, {"ok": True}))
        self.assertEqual([i["id"] for i in self.http_project(port)["human"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_UNNEEDINFO)

    def normalized_argv(self, entries: list[dict], iid: str) -> list[list[str]]:
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
        return got

    def test_a_stale_undo_only_reads(self):
        # 署名の違う取り消しは、反映待ちの一覧とコメントを読むだけで、bd に何も書かない
        iid = self.new_question()
        port = self.serve(self.store())
        sig_a = self.answer_sig(port, iid, "a")
        code, j = self.undo_http(port, iid, asig=sig_a)
        self.assertEqual(code, 200, j)
        self.answer_sig(port, iid, "b")
        start = len(self.log_entries())
        code, j = self.undo_http(port, iid, asig=sig_a)
        self.assertEqual((code, j.get("code")), (409, "stale"), j)
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), [self.ANSWERED_LIST, self.COMMENTS])

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


class WithFakeBr(FakeLog, BdScenarios, unittest.TestCase):
    """偽の br (tests/fake_beads.py を br の名前で置いたもの) で、bd と同じシナリオ (BdScenarios) を回す。

    偽の br はコメントを新しい順で返す (本物の並びは決まっていない) ので、古い順を前提にした送り直しの判定・取り消しの
    照合・両ラベルの向きは、ここで落ちる。偽物の時刻は秒単位なので、1 つのテストのコメントはたいてい同じ秒に入り、
    並びは id でだけ決まる (test_comments_in_the_same_second_are_ordered_by_id は時刻を揃えて確かめる)。
    偽の bd も同じ dir に置く (br のプロジェクトで bd を打ったら記録に残り、tearDown で落ちる。本物の bd を拾わない)。
    チケットの helper は br の形 (-C が無く cwd から .beads を探す・問いは agent_context・ラベルは位置引数) で上書きする。"""

    @classmethod
    def setUpClass(cls):
        cls.bin_tmp = tempfile.mkdtemp(prefix="humanq-fakebr-bin-")
        cls.bin = os.path.join(cls.bin_tmp, "bin")
        os.makedirs(cls.bin)
        for prog in ("bd", "br"):
            install_fake(cls.bin, prog)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.bin_tmp, ignore_errors=True)

    def setUp(self):
        leaked = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.realpath(d) in FAKE_BIN_DIRS]
        self.assertEqual(leaked, [], "前のテストの偽の CLI が PATH に残っている")
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-fakebr-"))     # 偽物が記録する cwd は realpath
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""), "FAKE_BEADS_LOG": self.log})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FAKE_BEADS_FAIL", None)
        for prog in ("bd", "br"):
            self.assertEqual(os.path.realpath(shutil.which(prog) or ""), os.path.realpath(os.path.join(self.bin, prog)))
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        self.brc("init", "--prefix", "hq")
        Path(self.repo, ".beads", ".gitignore").write_text(".br_history/\n*-fsqlite-ns-gate\n", encoding="utf-8")   # br init が書く印
        self.assertEqual(registry.project_kind(self.repo)[0], registry.BR)

    def tearDown(self):
        entries = self.log_entries()
        self.assertEqual({e["prog"] for e in entries} - {"br"}, set(), "br のプロジェクトで bd を打った")
        self.assertEqual({e["cwd"] for e in entries} - {self.repo}, set(), "br をプロジェクトの外の dir で打った")

    # ---- チケットの helper (br の形) ----
    def brc(self, *args: str) -> str:
        p = subprocess.run(["br", *args], cwd=self.repo, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, f"br {args}: {p.stderr or p.stdout}")
        return p.stdout

    def write_ctx(self, name: str, q: dict) -> str:
        path = os.path.join(self.tmp, name)
        Path(path).write_text(json.dumps(q), encoding="utf-8")
        return "@" + path

    def new_question(self, labels=("human",)) -> str:
        type(self).n += 1
        created = json.loads(self.brc("create", f"question {self.n}", "-p", "1", "-l", ",".join(labels), "--json"))
        self.assertIsInstance(created, dict, created)
        self.assertIn("id", created, created)
        self.assertEqual(set(created.get("labels") or []), set(labels), f"create がラベルを付けていない: {created}")
        self.brc("update", created["id"], "--agent-context", self.write_ctx(f"ctx{self.n}.json", QUESTION))
        return created["id"]

    def comments(self, iid: str) -> list[str]:
        """コメントの本文 (古い順)。偽の br の並びは新しい順なので、単調に増える id で並べ直す (製品の並べ替えは使わない)。"""
        cs = json.loads(self.brc("comments", iid, "--json"))
        self.assertIsInstance(cs, list, cs)
        for c in cs:
            self.assertIsInstance(c.get("id") if isinstance(c, dict) else None, int, cs)
        return [c["text"] for c in sorted(cs, key=lambda c: c["id"])]

    def labels(self, iid: str) -> set[str]:
        shown = json.loads(self.brc("show", iid, "--json"))
        self.assertIsInstance(shown, list, shown)
        self.assertEqual(len(shown), 1, shown)
        return set(shown[0].get("labels") or [])

    def set_question(self, iid: str, **changes) -> None:
        q = json.loads(json.dumps(QUESTION))
        q["humanq"].update(changes)
        self.brc("update", iid, "--agent-context", self.write_ctx(f"ctx-{iid}.json", q))

    def add_comment(self, iid: str, text: str) -> None:
        f = os.path.join(self.tmp, "comment.md")
        Path(f).write_text(text, encoding="utf-8")
        self.brc("comments", "add", iid, "-f", f)

    def label(self, iid: str, op: str, name: str) -> None:
        self.brc("label", op, iid, name)

    def set_status(self, iid: str, status: str) -> None:
        self.brc("update", iid, "--status", status)

    def set_assignee(self, iid: str, who: str) -> None:
        self.brc("update", iid, "--assignee", who)

    def state(self, iid: str) -> tuple:
        shown = json.loads(self.brc("show", iid, "--json"))
        self.assertIsInstance(shown, list, shown)
        self.assertEqual(len(shown), 1, shown)
        return shown[0].get("status"), shown[0].get("assignee")

    def close(self, iid: str) -> None:
        self.brc("close", iid)

    def backend(self):
        return humanq._BR

    def comments_unreadable_once(self, iid: str | None = None):
        """次の 1 回の show の comments を list でない値にする (br はコメントを show で読む。exit で失敗させると
        プロジェクト全体が読めなくなるので、そのチケットだけ読めない形を作る)。iid は使わない (プロジェクトはテストごと)。
        返り値を呼ぶと元に戻す。"""
        real, left = humanq.br, [1]

        def odd(repo, *args):
            out = real(repo, *args)
            if args[:1] == ("show",) and left[0] > 0:
                left[0] -= 1
                items = json.loads(out)
                for d in items:
                    d["comments"] = {"text": "list ではない"}
                return json.dumps(items)
            return out

        humanq.br = odd

        def restore():
            humanq.br = real
        self.addCleanup(restore)
        return restore

    def same_second(self, iid: str) -> None:
        """iid のコメントの created_at を、すべて最初のコメントの値にする (偽の br の状態を直接書き換える)。並びは id でしか決まらない。"""
        path = Path(self.repo, ".beads", "fake-state.json")
        state = json.loads(path.read_text(encoding="utf-8"))
        cs = state["issues"][iid]["comments"]
        self.assertGreaterEqual(len(cs), 2, cs)
        for c in cs:
            c["created_at"] = cs[0]["created_at"]
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def call(self, fn, body: dict):
        """Store の書き込みの返り値を返す。想定外の例外は FAIL にする。"""
        try:
            return fn(self.repo, body)
        except Exception as e:  # noqa: BLE001
            self.fail(f"{fn.__name__} が想定外の例外を上げた: {type(e).__name__}: {e}")

    # ---- br だけのシナリオ ----
    def test_a_warning_on_exit_zero_is_logged_and_the_answer_still_succeeds(self):
        """br は JSONL の自動 export に失敗しても exit 0 で stderr に警告を出す。回答は DB に入っているので成功のまま、
        警告は 1 回の書き込みの呼び出しごとにサーバーの stderr に残る (残さないと、JSONL に入っていないことが誰にも見えない)。"""
        warn = "Warning: Mutation succeeded, but automatic JSONL export failed: disk full"
        iid = self.new_question()
        store = self.store()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"FAKE_BEADS_WARN": warn}), contextlib.redirect_stderr(err):
            self.write(store.answer, {"id": iid, "option": "a"})
        logged = [ln for ln in err.getvalue().splitlines() if "humanq: br: " in ln]
        self.assertGreaterEqual(len(logged), 2, err.getvalue())             # コメントとラベルの書き込みのたび
        self.assertEqual(set(logged), {"humanq: br: " + warn})
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"}, "警告で回答の書き込みが途中で止まった")
        self.assertTrue(self.comments(iid)[-1].startswith("[humanq] 回答: 案A (a)"))

    def test_comments_in_the_same_second_are_ordered_by_id(self):
        store = self.store()
        # (a) 回答より前のメモと同じ秒の回答: 送り直しても回答のコメントは 1 件のまま
        iid = self.new_question()
        self.add_comment(iid, "エージェントのメモ")
        restore = self.failing("label remove human")
        try:
            self.write(store.answer, {"id": iid, "option": "a"}, raises=humanq.WriteError)
        finally:
            restore()
        self.same_second(iid)
        self.write(store.answer, {"id": iid, "option": "a"})
        self.assertEqual(len([c for c in self.comments(iid) if c.startswith("[humanq]")]), 1, "同じ秒の並びを取り違えて二重に書いた")
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"answered"})
        # (b) 回答の後の問い直しが同じ秒: 同じ選択肢の回答を、新しい回答として書く
        iid = self.new_question()
        self.write(store.answer, {"id": iid, "option": "a"})
        self.add_comment(iid, "問いを書き直した")
        self.label(iid, "add", "human")
        self.label(iid, "remove", "answered")
        self.same_second(iid)
        self.write(store.answer, {"id": iid, "option": "a"})
        texts = self.comments(iid)
        self.assertEqual(len(texts), 3, texts)
        self.assertTrue(texts[-1].startswith("[humanq] 回答: 案A (a)"), "同じ秒の問い直しの後の回答が書かれていない")
        # (c) 同じ秒の 回答 → 取り消し → 回答: 一覧の署名は最後の回答のもの。前の回答の署名では取り消さない
        iid = self.new_question()
        sig1 = self.call(store.answer, {"id": iid, "option": "a"})
        self.call(store.undo, {"id": iid, "asig": sig1})
        sig2 = self.call(store.answer, {"id": iid, "option": "a"})
        self.same_second(iid)
        self.assertEqual(self.item(iid, "answered").get("asig"), sig2)
        self.write(store.undo, {"id": iid, "asig": sig1}, raises=humanq.Stale)
        self.write(store.undo, {"id": iid, "asig": sig2})
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})

    # サーバーが打つ br の argv の列 (読み → 回答 → 読み → 取り消し)。<id> <tmp> は置き換えて比べる。cwd はどれもプロジェクト
    LIST_HUMAN = ["list", "-l", "human", "--limit", "0", "--json"]
    LIST_ANSWERED = ["list", "-l", "answered", "--limit", "0", "--json"]
    SHOW = ["show", "<id>", "--json"]
    COMMENT_ADD = ["--actor", "user/humanq", "comments", "add", "<id>", "-f", "<tmp>"]
    ADD_ANSWERED = ["--actor", "user/humanq", "label", "add", "<id>", "answered"]
    REMOVE_HUMAN = ["--actor", "user/humanq", "label", "remove", "<id>", "human"]
    ADD_HUMAN = ["--actor", "user/humanq", "label", "add", "<id>", "human"]
    REMOVE_ANSWERED = ["--actor", "user/humanq", "label", "remove", "<id>", "answered"]
    GOLDEN = [
        LIST_HUMAN, LIST_ANSWERED, SHOW,                                        # 読み (判断待ちの問い)
        LIST_HUMAN, SHOW, COMMENT_ADD, ADD_ANSWERED, REMOVE_HUMAN,              # 回答 (show 1 回で問いとコメント)
        LIST_HUMAN, LIST_ANSWERED, SHOW,                                        # 読み (回答済みの本文)
        LIST_ANSWERED, SHOW, COMMENT_ADD, ADD_HUMAN, REMOVE_ANSWERED,           # 取り消し
    ]

    def normalized_argv(self, entries: list[dict], iid: str) -> list[list[str]]:
        self.assertEqual({e["prog"] for e in entries}, {"br"})
        self.assertEqual({e["cwd"] for e in entries}, {self.repo})
        got = []
        for e in entries:
            argv = ["<id>" if a == iid else a for a in e["argv"]]
            if "-f" in argv:
                k = argv.index("-f")
                self.assertLess(k + 1, len(argv), argv)
                self.assertRegex(os.path.basename(argv[k + 1]), r"^humanq-.*\.md$")
                argv[k + 1] = "<tmp>"
            got.append(argv)
        return got

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
            self.assertEqual((ps[0]["ok"], ps[0]["cli"], ps[0]["writable"]), (True, "br", True), ps[0])
            return ps[0]

        p = project()
        self.assertEqual([i["id"] for i in p["human"]], [iid])
        code, j = self.post_json(port, "/api/answer", {"project": self.repo, "id": iid, "option": "a", "qsig": p["human"][0]["qsig"]})
        self.assertEqual(code, 200, j)
        p = project()
        self.assertEqual(([i["id"] for i in p["human"]], [i["id"] for i in p["answered"]]), ([], [iid]))
        self.assertEqual(p["answered"][0]["answer"], "[humanq] 回答: 案A (a)\n", p["answered"][0])
        self.assertEqual(p["answered"][0].get("asig"), j.get("asig"), (p["answered"][0], j))
        code, j = self.undo_http(port, iid, asig=j.get("asig"))
        self.assertEqual(code, 200, j)
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN)

    ADD_SNOOZED = ["--actor", "user/humanq", "label", "add", "<id>", "snoozed"]
    REMOVE_SNOOZED = ["--actor", "user/humanq", "label", "remove", "<id>", "snoozed"]
    # 保留 → 読み → 解除 → 読み。保留の期日のコメントは、読みの show (1 回) から取る (プロセスは増えない)。status を変える呼び出しは無い
    GOLDEN_SNOOZE = [
        LIST_HUMAN, SHOW, COMMENT_ADD, ADD_SNOOZED,
        LIST_HUMAN, LIST_ANSWERED, SHOW,
        LIST_HUMAN, SHOW, COMMENT_ADD, REMOVE_SNOOZED,
        LIST_HUMAN, LIST_ANSWERED, SHOW,
    ]

    def test_golden_argv_of_snooze(self):
        iid = self.new_question()
        port = self.serve(self.store())
        start = len(self.log_entries())
        code, j = self.post_json(port, "/api/snooze", {"project": self.repo, "id": iid, "until": self.day(1), "note": "n"})
        self.assertEqual(code, 200, j)
        self.assertEqual([i["id"] for i in self.http_project(port)["snoozed"]], [iid])
        code, j = self.post_json(port, "/api/unsnooze", {"project": self.repo, "id": iid})
        self.assertEqual(code, 200, j)
        self.assertEqual([i["id"] for i in self.http_project(port)["human"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_SNOOZE)

    ADD_NEEDINFO = ["--actor", "user/humanq", "label", "add", "<id>", "needs-info"]
    # 情報待ち → 読み。足りない情報のコメントは、読みの show (1 回) から取る (プロセスは増えない)。human を外す・status を変える呼び出しは無い
    GOLDEN_NEEDINFO = [LIST_HUMAN, SHOW, COMMENT_ADD, ADD_NEEDINFO, LIST_HUMAN, LIST_ANSWERED, SHOW]
    GOLDEN_NEEDINFO_BACK = [LIST_HUMAN, LIST_ANSWERED, SHOW]

    def test_golden_argv_of_needinfo(self):
        iid = self.new_question()
        port = self.serve(self.store())
        cache = mock.patch.object(humanq, "CACHE_SEC", 0.0)
        cache.start()
        self.addCleanup(cache.stop)
        start = len(self.log_entries())
        code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, "note": "n"})
        self.assertEqual(code, 200, j)
        self.assertEqual([i["id"] for i in self.http_project(port)["needinfo"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_NEEDINFO)
        self.set_question(iid, context="足した情報")
        self.label(iid, "remove", "needs-info")
        start = len(self.log_entries())
        self.assertEqual([i["id"] for i in self.http_project(port)["human"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_NEEDINFO_BACK)

    REMOVE_NEEDINFO = ["--actor", "user/humanq", "label", "remove", "<id>", "needs-info"]
    # 情報待ちの取り下げ → 読み。取り下げの前のコメントは show (1 回) から取る。human を外す・status を変える呼び出しは無い
    GOLDEN_UNNEEDINFO = [LIST_HUMAN, SHOW, COMMENT_ADD, REMOVE_NEEDINFO, LIST_HUMAN, LIST_ANSWERED, SHOW]

    def test_golden_argv_of_unneedinfo(self):
        iid = self.new_question()
        port = self.serve(self.store())
        cache = mock.patch.object(humanq, "CACHE_SEC", 0.0)
        cache.start()
        self.addCleanup(cache.stop)
        code, j = self.post_json(port, "/api/needinfo", {"project": self.repo, "id": iid, "note": "n"})
        self.assertEqual(code, 200, j)
        start = len(self.log_entries())
        code, j = self.post_json(port, "/api/unneedinfo", {"project": self.repo, "id": iid})
        self.assertEqual((code, j), (200, {"ok": True}))
        self.assertEqual([i["id"] for i in self.http_project(port)["human"]], [iid])
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), self.GOLDEN_UNNEEDINFO)

    def test_a_stale_undo_only_reads(self):
        # 署名の違う取り消しは、回答済みの一覧とそのチケットの show を読むだけで、br に何も書かない
        iid = self.new_question()
        port = self.serve(self.store())
        sig_a = self.answer_sig(port, iid, "a")
        code, j = self.undo_http(port, iid, asig=sig_a)
        self.assertEqual(code, 200, j)
        self.answer_sig(port, iid, "b")
        start = len(self.log_entries())
        code, j = self.undo_http(port, iid, asig=sig_a)
        self.assertEqual((code, j.get("code")), (409, "stale"), j)
        self.assertEqual(self.normalized_argv(self.log_entries()[start:], iid), [self.LIST_ANSWERED, self.SHOW])

    def test_the_written_comment_and_labels_have_the_shape_of_the_readme(self):
        # README「回答の形」: 作者 user/humanq、1 行目 [humanq] 回答: <label> (<key>)、補足は空行の後、末尾の改行も保つ。
        # ラベルは answered を足してから human を外す。取り消しは [humanq] 取り消し で、answered を human に戻す
        iid = self.new_question()
        store = self.store()
        self.write(store.answer, {"id": iid, "option": "b", "text": "補足の 1 行"})
        self.write(store.undo, {"id": iid})
        cs = json.loads(self.brc("comments", iid, "--json"))
        self.assertIsInstance(cs, list, cs)
        self.assertEqual(len(cs), 2, cs)
        cs.sort(key=lambda c: c["id"])
        self.assertEqual([(c["author"], c["text"]) for c in cs],
                         [("user/humanq", "[humanq] 回答: 案B (b)\n\n補足の 1 行\n"), ("user/humanq", "[humanq] 取り消し\n")])
        self.assertEqual(self.labels(iid) & {"human", "answered"}, {"human"})
        labels = [e["argv"][3:] for e in self.log_entries() if e["argv"][2:3] == ["label"]]
        self.assertEqual(labels, [["add", iid, "answered"], ["remove", iid, "human"], ["add", iid, "human"], ["remove", iid, "answered"]])


# ---- bd のシナリオを並列のプロセスで回す (道具は tests/bd_parallel.py) ---------------------------------------
#
# BdScenarios のテスト (WithBd・WithFakeBd・WithFakeBr) は、モジュールごと読み込まれたとき (discover・`-m unittest test_server`)
# に限り、このファイルを --bd-worker で起動した worker で並列に回す。各 worker は自分の setUpClass (bd init した一時の
# プロジェクト・偽の bd を置いた dir) を持つ。テストを名前で指したとき (`-m unittest test_server.WithBd` など) と
# HUMANQ_TEST_JOBS=1 のときは今までどおり直列。
# discover (load_tests に pattern が渡る) では、worker を読み込みの時点で起こす。test_server は discover の並び (名前の順) で
# 最後のモジュールなので、run の時点で起こすと、親がほかのモジュールを直列に回している間 worker が動かない。
# モジュールを名前で指したとき (`-m unittest tests.test_server`。pattern は None) は、今までどおり run の時点で起こす。

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
    suite = module_suite(tests, jobs_from_env())
    if pattern is not None and isinstance(suite, bd_parallel.ParallelSuite):
        suite.pool.start()
        # 読み込んだまま走らせずに終わったとき (failfast で手前のモジュールで止まった・一覧だけを取る道具) も、
        # 配っていない名前を捨てて worker を回収する (走らせたときは ParallelSuite.run が先に回収している)
        atexit.register(suite.pool.close)
    return suite


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

    def run_remote(self, names: list[str], jobs: int = 2, script: str = SCRIPT, prestart: bool = False) -> unittest.TestResult:
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
        self.pool = suite.pool
        remotes = [t for t in bd_parallel.flatten(suite) if isinstance(t, bd_parallel.RemoteTest)]
        self.assertEqual(sorted(t.name for t in remotes), sorted(names))
        if prestart:                                 # discover と同じく、読み込みの時点で起こしてから run する (load_tests)
            suite.pool.start()
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
        # 名前を受け取ったまま、待つ上限 (RESULT_WAIT) より長く答えない。close は worker が終わるのを待つので、黙る時間が
        # そのまま所要に乗る。上限との差 (0.8 秒) に worker の起動の分が加わる
        body = "import time\nrd.readline()\ntime.sleep(1)\n"
        with mock.patch.object(bd_parallel, "RESULT_WAIT", 0.2):
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
        self.assertEqual(remote, {"WithBd", "WithFakeBd", "WithFakeBr"} if HAVE_BD else {"WithFakeBd", "WithFakeBr"})
        self.assertIn("WithBd", remote if HAVE_BD else local, "bd が無いときの WithBd は親に残して、親で skip を報告する")
        self.assertEqual(local & {"WithFakeBd", "WithFakeBr"}, set())
        self.assertIn("ParallelRunner", local)
        self.assertEqual(len(flat), suite.countTestCases())
        names = [t.name for t in flat if isinstance(t, bd_parallel.RemoteTest)]
        self.assertEqual(len(names), len(set(names)), "同じテストを 2 度配る")
        for t in flat:                               # 報告の id と表示は元のテストのまま (落ちたテストを名前で追える)
            if isinstance(t, bd_parallel.RemoteTest):
                self.assertEqual(t.name, f"{type(t.orig).__name__}.{t.orig._testMethodName}")
                self.assertTrue(t.id().endswith("." + t.name), t.id())
                self.assertEqual((t.id(), str(t)), (t.orig.id(), str(t.orig)))

    def test_discover_starts_the_workers_while_loading(self):
        # discover は load_tests に pattern を渡す。そのときだけ読み込みの時点で worker を起こし (親がほかのモジュールを
        # 回している間に回す)、走らせずに終わっても atexit で回収する。モジュールを名前で指したとき (pattern は None) は
        # run の時点で起こす。直列 (HUMANQ_TEST_JOBS=1) では起こさず、suite もそのまま返す
        mod = sys.modules[__name__]
        for jobs, pattern, started in (("3", "test*.py", True), ("3", None, False), ("1", "test*.py", False)):
            with self.subTest(jobs=jobs, pattern=pattern):
                loader = unittest.TestLoader()
                with mock.patch.dict(os.environ, {JOBS_ENV: jobs}), \
                        mock.patch.object(bd_parallel.Pool, "start", autospec=True) as start, \
                        mock.patch.object(atexit, "register") as register:
                    suite = loader.loadTestsFromModule(mod, pattern=pattern)
                self.assertEqual(loader.errors, [], "load_tests が例外を上げた")
                classes = {type(t.orig if isinstance(t, bd_parallel.RemoteTest) else t).__name__ for t in bd_parallel.flatten(suite)}
                self.assertIn("WithFakeBd", classes)
                if jobs == "1":
                    self.assertNotIsInstance(suite, bd_parallel.ParallelSuite)
                    self.assertEqual((start.call_args_list, register.call_args_list), ([], []))
                    continue
                self.assertIsInstance(suite, bd_parallel.ParallelSuite)
                self.assertEqual(start.call_args_list, [mock.call(suite.pool)] if started else [],
                                 "読み込みの時点で worker を起こすかが、pattern の有無と合わない")
                self.assertEqual(register.call_args_list, [mock.call(suite.pool.close)] if started else [],
                                 "読み込みの時点で起こした worker を、走らせずに終わったときに回収しない")

    def test_workers_started_while_loading_are_not_started_again(self):
        # discover は読み込みの時点で worker を起こし、ParallelSuite.run がもう一度 start を呼ぶ。2 度目で worker を
        # 増やさず、先に起こした worker の結果をそのまま報告する
        r = self.run_remote(["PoolDummy.test_ok", "PoolDummy.test_fail", "PoolDummy.test_skip"], jobs=2, prestart=True)
        self.assertEqual(len(self.pool.procs), 2, "2 度目の start で worker を増やした")
        self.assertEqual(self.names_of(r.failures), ["PoolDummy.test_fail"])
        self.assertEqual((r.errors, sorted(t.name for t, _ in r.skipped)), ([], ["PoolDummy.test_skip"]))

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
