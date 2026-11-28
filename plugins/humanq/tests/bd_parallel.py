"""test_server の一部のテストを、別のプロセスで並列に回す道具 (discover の対象ではない)。

本物の bd は 1 回の呼び出しに 0.2〜0.5 秒かかり (embedded Dolt を開く)、bd を通すシナリオは全体で 200 回を超えて打つ。
直列だと suite が 90 秒を超えるので、test_server は bd のシナリオを、テストのファイル自身を --bd-worker で起動した
worker に 1 件ずつ配る。テストの中身・assert・件数は直列のときと同じで、worker はそれぞれ自分の setUpClass を持ち、
worker の間で状態を分け合わない。親は worker の結果 (成功・FAIL・ERROR・skip) を、元のテストの id でそのまま報告する。
テストの外の失敗 (tearDownClass・class cleanup の失敗、worker の異常終了) は worker の rc に出し、親はそれを
ERROR 1 件として足す (直列の unittest が tearDownClass の失敗を ERROR にするのと同じく、成功にしない)。

worker への名前 ("<クラス名>.<メソッド名>") と worker からの結果 (1 行の JSON) は専用の pipe で受け渡す
(テストの中の bd やサーバーが stdin / stdout に触れても、やり取りに混ざらない)。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import unittest

RESULT_WAIT = 900                               # 1 件の結果を待つ上限 (秒)


class RemoteFailure(AssertionError):
    """worker で assertion が落ちた。本文は worker の traceback。"""


class RemoteError(Exception):
    """worker でテストが走りきらなかった (例外・setUpClass の失敗・worker の異常終了)。"""


def _exc_info(cls, text: str):
    try:
        raise cls("\n" + text)
    except cls:
        return sys.exc_info()


def summarize(r: unittest.TestResult) -> tuple[str, str]:
    """1 件を走らせた TestResult を (status, 本文) にまとめる。subTest は複数の失敗を持ちうるので、本文はつなげる。"""
    if r.errors:
        return "error", "\n".join(t for _, t in r.errors + r.failures)
    if r.failures:
        return "fail", "\n".join(t for _, t in r.failures)
    if r.unexpectedSuccesses:
        return "unexpected", ""
    if r.expectedFailures:
        return "xfail", "\n".join(t for _, t in r.expectedFailures)
    if r.skipped:
        return "skip", r.skipped[0][1]
    return "ok", ""


class Pool:
    """名前の列を、jobs 個の worker (script を --bd-worker で起動したもの) に 1 件ずつ配って結果を集める。"""

    def __init__(self, script: str, names: list[str], jobs: int, env_extra: dict | None = None):
        self.script = script
        self.names = list(names)
        self.jobs = max(1, min(jobs, len(self.names)))
        self.env = dict(os.environ, **(env_extra or {}))
        self.results: dict[str, dict] = {}
        self.events = {n: threading.Event() for n in self.names}
        self.lock = threading.Lock()
        self.todo: queue.Queue = queue.Queue()
        for n in self.names:
            self.todo.put(n)
        self.procs: list[subprocess.Popen] = []
        self.threads: list[threading.Thread] = []

    def _set(self, name: str, rec: dict) -> None:
        with self.lock:
            self.results.setdefault(name, rec)
        self.events[name].set()

    def start(self) -> None:
        """worker を起こす。起こしてあれば何もしない (discover は読み込みの時点で起こし、ParallelSuite.run がもう一度呼ぶ)。"""
        if self.procs:
            return
        for _ in range(self.jobs):
            to_w_r, to_w_w = os.pipe()
            from_w_r, from_w_w = os.pipe()
            # cwd は親のまま (相対パスの env が親と同じものを指す)。script は絶対パスなので sys.path[0] は script の dir
            # -W error などの起動の flag も渡す (渡さないと、直列なら ERROR になる警告が worker では通る)
            p = subprocess.Popen([sys.executable, *subprocess._args_from_interpreter_flags(), self.script, "--bd-worker", str(to_w_r), str(from_w_w)],
                                 stdin=subprocess.DEVNULL, pass_fds=(to_w_r, from_w_w), env=self.env)
            os.close(to_w_r)
            os.close(from_w_w)
            self.procs.append(p)
            t = threading.Thread(target=self._drive, args=(p, os.fdopen(to_w_w, "w", encoding="utf-8"),
                                                          os.fdopen(from_w_r, "r", encoding="utf-8")), daemon=True)
            self.threads.append(t)
            t.start()
        threading.Thread(target=self._finish, daemon=True).start()

    def _drive(self, p: subprocess.Popen, w, r) -> None:
        try:
            while True:
                try:
                    name = self.todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    w.write(name + "\n")
                    w.flush()
                    line = r.readline()
                except OSError:
                    line = ""
                if not line:
                    self._set(name, {"status": "error", "text": f"worker が結果を返さずに終わった (rc={p.poll()})"})
                    return
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = {"name": None}
                if rec.get("name") != name:
                    self._set(name, {"status": "error", "text": f"worker の結果が頼んだテストと違う: {line[:200]!r}"})
                    return
                self._set(name, rec)
        finally:
            for f in (w, r):
                try:
                    f.close()
                except OSError:
                    pass

    def _finish(self) -> None:
        for t in self.threads:
            t.join()
        for n in self.names:                         # どの worker も走らせなかったもの (worker が全部落ちた)
            if not self.events[n].is_set():
                self._set(n, {"status": "error", "text": "どの worker もこのテストを走らせなかった"})

    def result(self, name: str) -> dict:
        if not self.events[name].wait(RESULT_WAIT):
            return {"status": "error", "text": f"{RESULT_WAIT} 秒待っても結果が来ない"}
        return self.results[name]

    def close(self) -> list[int]:
        """worker を回収し、0 以外で終わった worker の rc を返す (tearDownClass・class cleanup の失敗、異常終了、時間切れの kill)。"""
        while True:                                  # まだ配っていないものは配らない (failfast などで途中で止まったとき)
            try:
                self.todo.get_nowait()
            except queue.Empty:
                break
        for t in self.threads:
            t.join(RESULT_WAIT)
        for p in self.procs:
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        return [p.returncode for p in self.procs if p.returncode != 0]


class RemoteTest(unittest.TestCase):
    """worker で走らせたテストの結果を、元のテストとして親の result に報告する (id と表示は元のテストのまま)。"""

    def __init__(self, orig: unittest.TestCase, pool: Pool, name: str):
        super().__init__("_remote")
        self.orig, self.pool, self.name = orig, pool, name

    def _remote(self):
        pass

    def id(self):
        return self.orig.id()

    def __str__(self):
        return str(self.orig)

    def shortDescription(self):
        return self.orig.shortDescription()

    def debug(self):                                 # TestSuite.debug() の経路。worker の結果を例外として上げる (黙って通さない)
        rec = self.pool.result(self.name)
        st, text = rec.get("status"), rec.get("text", "")
        if st == "skip":
            raise unittest.SkipTest(text)
        if st not in ("ok", "xfail"):
            raise (RemoteFailure if st == "fail" else RemoteError)("\n" + (text or f"worker の結果: {rec!r}"))

    def run(self, result=None):
        result.startTest(self)
        try:
            rec = self.pool.result(self.name)
            st, text = rec.get("status"), rec.get("text", "")
            if st == "ok":
                result.addSuccess(self)
            elif st == "fail":
                result.addFailure(self, _exc_info(RemoteFailure, text))
            elif st == "skip":
                result.addSkip(self, text)
            elif st == "xfail":
                result.addExpectedFailure(self, _exc_info(RemoteFailure, text))
            elif st == "unexpected":
                result.addUnexpectedSuccess(self)
            else:
                result.addError(self, _exc_info(RemoteError, text or f"worker の結果が読めない: {rec!r}"))
            if hasattr(result, "addDuration"):           # 3.12 から (--durations)
                result.addDuration(self, float(rec.get("elapsed") or 0.0))
        finally:
            result.stopTest(self)
        return result


class ParallelSuite(unittest.TestSuite):
    """先に worker を起こし (起こしてあればそのまま)、親では並列にしないテストを走らせ、最後に worker の結果を順に報告する。"""

    def __init__(self, tests, pool: Pool):
        super().__init__(tests)
        self.pool = pool

    def run(self, result, debug=False):
        self.pool.start()
        try:
            return super().run(result, debug)
        finally:
            bad = self.pool.close()
            if bad:                                  # 直列なら tearDownClass の失敗は ERROR になる。並列でも黙って通さない
                result.addError(unittest.suite._ErrorHolder("tearDownClass (bd_parallel の worker)"),
                                _exc_info(RemoteError, f"worker が 0 以外で終わった (rc={bad})。tearDownClass・class cleanup の失敗か、"
                                                       "worker の異常終了 (traceback は worker の stderr)"))


def flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t


def default_name(t: unittest.TestCase) -> str:
    return f"{type(t).__name__}.{t._testMethodName}"


def parallel_suite(tests, script: str, jobs: int, wanted, env_extra: dict | None = None, name_of=default_name):
    """tests のうち wanted(t) が真のものを worker に回す suite を作る。回すものが無ければ tests をそのまま返す。"""
    flat = list(flatten(tests))
    remote = [t for t in flat if wanted(t)]
    mods = {sys.modules.get(type(t).__module__) for t in remote}
    if not remote or any(hasattr(m, "setUpModule") or hasattr(m, "tearDownModule") for m in mods):
        return tests                             # worker は module 単位の fixture を回さないので、直列と結果が分かれる形は並列にしない
    names = [name_of(t) for t in remote]
    pool = Pool(script, names, jobs, env_extra)
    local = [t for t in flat if not wanted(t)]
    return ParallelSuite(local + [RemoteTest(t, pool, n) for t, n in zip(remote, names)], pool)


def _class_cleanups(cls) -> bool:
    """cls の class cleanup を回す。直列の unittest と同じく、失敗は例外にせず tearDown_exceptions に溜まるので、
    それを stderr に出し、1 件でもあれば False を返す。"""
    cls.doClassCleanups()
    excs = getattr(cls, "tearDown_exceptions", [])
    for exc in excs:
        traceback.print_exception(*exc)
    return not excs


def worker_main(mod, in_fd: int, out_fd: int) -> int:
    """--bd-worker: 名前を 1 行ずつ受け取り、mod のそのテストを走らせて結果を 1 行の JSON で返す。
    setUpClass はクラスごとに最初の 1 件の前に 1 回、tearDownClass は最後にまとめて呼ぶ (直列のときと同じ)。
    setUpClass の SkipTest はそのクラスの全テストを skip にし、setUpClass が落ちたときも class cleanup は回す。
    tearDownClass か class cleanup が落ちたら rc 1 で終わる (親が ERROR にする)。"""
    rd = os.fdopen(in_fd, "r", encoding="utf-8")
    wr = os.fdopen(out_fd, "w", encoding="utf-8")
    ready: list[type] = []
    broken: dict[type, str] = {}
    skipped: dict[type, str] = {}
    torn = True
    try:
        while True:
            line = rd.readline()
            if not line:
                break
            name = line.strip()
            elapsed = 0.0
            cls_name, _, meth = name.partition(".")
            cls = getattr(mod, cls_name, None)
            if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)) or not meth:
                status, text = "error", f"テストが見つからない: {name}"
            else:
                if cls not in ready and cls not in broken and cls not in skipped:
                    try:
                        cls.setUpClass()
                        ready.append(cls)
                    except unittest.SkipTest as e:   # 直列と同じく skip (ERROR にしない)
                        skipped[cls] = str(e)
                        torn = _class_cleanups(cls) and torn
                    except Exception:
                        broken[cls] = "setUpClass が失敗した:\n" + traceback.format_exc()
                        torn = _class_cleanups(cls) and torn
                if cls in skipped:
                    status, text = "skip", skipped[cls]
                elif cls in broken:
                    status, text = "error", broken[cls]
                else:
                    r = unittest.TestResult()
                    t0 = time.perf_counter()
                    try:
                        cls(meth).run(r)
                        status, text = summarize(r)
                    except Exception:
                        status, text = "error", traceback.format_exc()
                    elapsed = time.perf_counter() - t0
            wr.write(json.dumps({"name": name, "status": status, "text": text, "elapsed": elapsed}) + "\n")
            wr.flush()
    finally:
        # 直列のときと同じく、tearDownClass が落ちても class cleanup は回す
        for cls in reversed(ready):
            try:
                cls.tearDownClass()
            except Exception:
                torn = False
                traceback.print_exc()
            torn = _class_cleanups(cls) and torn
        rd.close()
        wr.close()
    return 0 if torn else 1                          # finally の中の return は 3.14 で SyntaxWarning になるので外に置く
