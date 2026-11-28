"""hook (hooks/humanq_hook.py) が JSON として読む 3 つの入力が壊れていても、hook が落ちずに「壊れた入力」と同じ扱いになること。

    python3 -m unittest discover tests

- stdin (main): 読めなければ {} で進める
- サーバーの応答 (_get): 読めなければ None (サーバーを使えない扱い)
- 前に伝えた回答済みの記憶のファイル (_remember): 読めなければ空 (今の集合を覚え直す)

深く入れ子の JSON (`[` を何万個も並べたものなど) は json の parser が RecursionError を上げる。ValueError (壊れた JSON) と
同じ結果になることを、入力ごとに、壊れた JSON を対照にして確かめる。例外が漏れる版は、テスト側で self.fail に変えて
FAIL として数える (ERROR にしない)。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_server as ts  # noqa: E402  helper だけ借りる (TestCase は名前空間に入れない)

registry = ts.registry

DEEP = {                                    # json の parser の再帰の限界を超える入れ子
    "深い入れ子 (閉じない)": "[" * 200000,
    "深い入れ子 (閉じる)": "[" * 100000 + "]" * 100000,
    "深い入れ子 (object)": '{"a":' * 100000,
}
BROKEN = {"JSON でない": "not json", "空": ""}  # 壊れた入力の対照 (ValueError)


class Fixed:
    """GET に body をそのまま返す 127.0.0.1 の使い捨てのサーバー (hits は受けた GET の数)。
    hang なら、Content-Length に足りない 1 バイトだけを送って止まる (読みの途中の時間切れ)。"""

    def __init__(self, body: bytes, hang: bool = False):
        outer = self
        self.hits = 0
        self.release = threading.Event()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                self.send_response(200)
                self.send_header("Content-Length", "1000" if hang else str(len(body)))
                self.end_headers()
                if hang:
                    self.wfile.write(b"[")
                    self.wfile.flush()
                    outer.release.wait(10)
                    return
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()   # shutdown を待たせない

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class FailingStdin:
    """読もうとすると OSError を上げる stdin。"""

    def read(self, *a):
        raise OSError("stdin を読めない")


class HookBrokenInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="humanq-hookin-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sd = os.path.join(self.tmp, "state")
        os.makedirs(self.sd)
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, ".beads"))
        env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.sd})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("humanq_hook_input_test", ts.HOOK_PATH)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.n = 0

    def serve(self, body: str, hang: bool = False) -> Fixed:
        """hook の向き先 (URL と PORT) をこのサーバーにする。"""
        srv = Fixed(body.encode(), hang)
        self.addCleanup(srv.close)
        self.hook.PORT = srv.port
        self.hook.URL = f"http://127.0.0.1:{srv.port}/"
        return srv

    def quietly(self, what: str, fn):
        """fn の結果。例外が漏れたら、ERROR ではなく FAIL にする。"""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            self.fail(f"{what}で例外が漏れた: {type(e).__name__}: {str(e)[:80]}")

    # ---- サーバーの応答 (_get) ----
    def test_get_reads_an_unreadable_response_as_none(self):
        for what, body in {**DEEP, **BROKEN}.items():
            with self.subTest(what):
                srv = self.serve(body)
                self.assertIsNone(self.quietly(what, lambda: self.hook._get("/api/ping", 5)))
                self.assertEqual(srv.hits, 1, "応答を受けていない (読めなかったのではなく、つながらなかった)")

    def test_get_gives_up_on_a_response_that_stops_midway(self):
        # 応答の途中で止まる: r.read() の時間切れ (URLError に包まれない OSError)
        srv = self.serve("", hang=True)
        self.assertIsNone(self.quietly("途中で止まる応答", lambda: self.hook._get("/api/ping", 0.3)))
        self.assertEqual(srv.hits, 1)

    def test_get_gives_up_when_nothing_listens(self):
        srv = Fixed(b"{}")
        port = srv.port
        srv.close()
        self.hook.URL = f"http://127.0.0.1:{port}/"
        self.assertIsNone(self.quietly("つながらない", lambda: self.hook._get("/api/ping", 1)))

    def test_get_still_reads_a_valid_response(self):
        srv = self.serve('{"app": "humanq", "version": "1.2.3"}')
        self.assertEqual(self.hook._get("/api/ping", 5), {"app": "humanq", "version": "1.2.3"})
        self.assertEqual(srv.hits, 1)

    def outcome(self, body: str) -> dict:
        """body を返すサーバーに対する、ensure_server と summary と session_start の結果。"""
        srv = self.serve(body)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = {
                "ensure_server": self.quietly("ensure_server", lambda: self.hook.ensure_server(self.sd)),
                "summary": self.quietly("summary", lambda: self.hook.summary(self.root, 5, fresh=True)),
            }
            with mock.patch.object(self.hook, "ensure_server", lambda sd: "running"):
                self.quietly("session_start", lambda: self.hook.session_start({"cwd": self.root, "session_id": "s1"}))
        got["session_start"] = out.getvalue().replace(str(srv.port), "PORT")      # サーバーごとにポートが違う
        self.assertGreaterEqual(srv.hits, 3, "サーバーに届いていない")
        return got

    def test_a_deep_response_is_treated_like_a_broken_one(self):
        base = self.outcome(BROKEN["JSON でない"])
        # 対照が「サーバーを使えない」扱いになっていること (対照どうしの一致が、同じ誤りの一致にならないように固定する)
        self.assertEqual((base["ensure_server"], base["summary"]), ("port-busy", None))
        self.assertIn("判断待ちの画面 (http://127.0.0.1:PORT/) を使えない (サーバー: running)", base["session_start"])
        for what, body in DEEP.items():
            with self.subTest(what):
                self.assertEqual(self.outcome(body), base)

    # ---- 記憶のファイル (_remember) ----
    # 回答済みの覚え (kind "") と、情報待ちの覚え (kind NEEDINFO_SEEN) は同じ読み方
    KINDS = {"回答済み": "", "情報待ち": None}

    def kind(self, name: str) -> str:
        return self.hook.NEEDINFO_SEEN if self.KINDS[name] is None else self.KINDS[name]

    def remembered(self, content: str | None, kind: str = "", keep: bool = False) -> tuple[list[str], str]:
        """記憶のファイルに content を置いて (None は置かない) _remember(["a", "b"]) を呼ぶ。(返り値, 呼んだ後のファイルの中身)。"""
        self.n += 1
        sid = f"s{self.n}"
        path = self.hook._seen_path(self.sd, sid, kind)
        if content is not None:
            Path(path).write_text(content, encoding="utf-8")
        new = self.quietly("_remember", lambda: self.hook._remember(self.sd, sid, ["a", "b"], kind, keep))
        return new, Path(path).read_text(encoding="utf-8")

    # 読めない・形の崩れた記憶: 壊れた JSON・深い入れ子と、JSON としては読めるが集合にならないもの (数・null・入れ子の list)
    SHAPELESS = {"数": "5", "null": "null", "真偽値": "true", "入れ子の list": "[[1], [2]]"}

    def test_remember_reads_an_unreadable_file_as_empty(self):
        for name in self.KINDS:
            for keep in (False, True):
                for what, content in {**DEEP, **BROKEN, **self.SHAPELESS, "ファイルが無い": None}.items():
                    with self.subTest(kind=name, keep=keep, what=what):
                        new, after = self.remembered(content, self.kind(name), keep)
                        self.assertEqual(new, ["a", "b"], "読めない記憶は空 (全部が新しい)")
                        self.assertEqual(after, '["a", "b"]', "今の集合を覚え直す")

    def test_remember_still_reads_a_valid_file(self):
        for name in self.KINDS:
            with self.subTest(kind=name):
                new, after = self.remembered('["a"]', self.kind(name))
                self.assertEqual(new, ["b"])
                self.assertEqual(after, '["a", "b"]')
                # keep (読み残しの間の情報待ち) は、前に伝えたもののうち今の集合に無いものも残す。文字列でない要素は覚え直さない
                new, after = self.remembered('["c", "a", 3, null]', self.kind(name), keep=True)
                self.assertEqual(new, ["b"])
                self.assertEqual(after, '["a", "b", "c"]')
                new, after = self.remembered('["c", "a"]', self.kind(name), keep=False)
                self.assertEqual((new, after), (["b"], '["a", "b"]'), "keep でないのに、今の集合に無いものを残した")

    def test_the_two_memories_are_separate_files_that_no_session_name_reaches(self):
        # 回答済みの覚えと情報待ちの覚えは別のファイル。session の ID を直した名前は "." を含まないので、別のセッションの
        # 回答済みの覚えが、あるセッションの情報待ちの覚えと同じファイルにならない
        a = self.hook._seen_path(self.sd, "s.needinfo")
        n = self.hook._seen_path(self.sd, "s", self.hook.NEEDINFO_SEEN)
        self.assertEqual(os.path.dirname(a), os.path.dirname(n))
        self.assertEqual((os.path.basename(a), os.path.basename(n)), ("s_needinfo.json", "s.needinfo.json"))

    # ---- stdin (main) ----
    def run_main(self, event: str, stdin) -> list:
        """main を stdin で呼び、session_start / prompt_submit が受け取った入力の list を返す (本体は呼ばない)。"""
        seen: list = []
        err = io.StringIO()
        with mock.patch.object(self.hook, "session_start", lambda inp: seen.append(inp)), \
                mock.patch.object(self.hook, "prompt_submit", lambda inp: seen.append(inp)), \
                mock.patch.object(sys, "argv", ["humanq_hook.py", event]), \
                mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stderr(err):
            self.quietly("main", self.hook.main)
        self.assertEqual(err.getvalue(), "")
        return seen

    def test_main_proceeds_with_an_empty_input_when_stdin_is_unreadable(self):
        forms = {what: lambda s=s: io.StringIO(s) for what, s in {**DEEP, **BROKEN}.items()}
        forms["stdin を読めない"] = FailingStdin
        for event in ("session-start", "prompt-submit"):
            for what, make in forms.items():
                with self.subTest(event=event, what=what):
                    self.assertEqual(self.run_main(event, make()), [{}])

    def test_main_still_passes_a_valid_input(self):
        for event in ("session-start", "prompt-submit"):
            with self.subTest(event=event):
                self.assertEqual(self.run_main(event, io.StringIO('{"cwd": "/x", "session_id": "s"}')),
                                 [{"cwd": "/x", "session_id": "s"}])

    def test_the_hook_process_exits_zero_on_a_deep_stdin(self):
        # 本物の hook のプロセスの終わり方 (exit code)。cwd は .beads の無い dir にして、本体は何もせずに戻る
        cwd = os.path.join(self.tmp, "plain")
        os.makedirs(cwd)
        if registry.find_bd_root(cwd) is not None:
            self.skipTest("tmp の親に .beads がある")
        for event in ("session-start", "prompt-submit"):
            with self.subTest(event=event):
                p = subprocess.run([sys.executable, str(ts.HOOK_PATH), event], input=DEEP["深い入れ子 (閉じない)"], text=True,
                                   capture_output=True, cwd=cwd, timeout=60)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
