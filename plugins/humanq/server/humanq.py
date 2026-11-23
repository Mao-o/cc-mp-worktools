#!/usr/bin/env python3
"""humanq: この端末の bd プロジェクトの判断待ち (human ラベル) を 1 つの localhost の画面に集め、
回答を各プロジェクトの bd に書き戻す。

usage: python3 humanq.py --state-dir DIR [--port 8765]

- 待ち受けは 127.0.0.1 だけ。ポートを取れなければ (もう誰かが動かしている) 何もせず終わる
- 対象のプロジェクトは DIR/projects.json (SessionStart hook が登録する)。リクエストのたびに読む
- DIR/server.json に pid・port・version を書く (hook が版の古いサーバーを止めるため。新しい版は止めない)
- DIR/token (0600) に書き込みのトークンを保存し、再起動 (版の入れ替え) しても同じ値を使う
規約 (問いの書き方・回答の形・拾い方) は plugin の README.md。依存は標準ライブラリと PATH 上の bd だけ。
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

MARK = "[humanq]"
UNDO_HEAD = f"{MARK} 取り消し"
ACTOR = "user/humanq"
# 回答済み (answered) として拾う status。`bd human list` の既定 (closed などの終わったものを除く) に揃える。
# 担当中 (in_progress) のチケットに human を付けて問うのは普通で、open だけにすると回答した途端にどの一覧からも消える
ANSWERED_STATUSES = "open,in_progress,blocked,deferred"
MAX_BODY = 64 * 1024
# bd (埋め込み Dolt) は同時に開くと直列化して遅くなり、そのプロジェクトのセッションの書き込みとも競る。
# プロジェクトごとに読みを 1 本にまとめ、CACHE_SEC の間は同じ結果を返す。書いたら捨てる
CACHE_SEC = 8.0
# 同じ回答の送り直しとみなす間隔。これより前の同じ本文のコメントは、前の問いへの回答かもしれないので数えない
RESEND_SEC = 600
TOKEN_FILE = "token"


class BdError(RuntimeError):
    pass


class WriteError(BdError):
    """書き込みの途中で失敗した。stage は失敗した段階 (comment | label)、written は回答のコメントが残っているか。"""

    def __init__(self, msg: str, stage: str, written: bool):
        super().__init__(msg)
        self.stage = stage
        self.written = written


class Gone(ValueError):
    """画面が見ている状態と違う (ほかで回答・反映済みなど)。書き込みは何も起きていない。"""


class Changed(ValueError):
    """画面が見ていた問いと、今の問いが違う。書き込みは何も起きていない。"""


def bd(repo: str, *args: str) -> str:
    try:
        p = subprocess.run(["bd", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BdError(f"bd を起動できない: {e}") from e
    if p.returncode != 0:
        raise BdError((p.stderr or p.stdout).strip()[:800] or f"bd exit {p.returncode}")
    return p.stdout


def bd_json(repo: str, *args: str) -> list:
    out = bd(repo, *args, "--json").strip()
    if not out:
        return []
    try:
        v = json.loads(out)
    except json.JSONDecodeError:
        if re.fullmatch(r"No [^\n]*", out):  # 該当 0 件のとき案内文 1 行だけを返す版がある
            return []
        raise BdError(f"bd の出力が JSON ではない: {out[:200]}") from None
    if v is None:
        return []  # 0 件の list は null を返す
    if not isinstance(v, list):
        raise BdError(f"bd の出力が list ではない ({type(v).__name__}): {out[:200]}")
    return v


def question(meta) -> dict | None:
    q = meta.get("humanq") if isinstance(meta, dict) else None
    if isinstance(q, str):
        try:
            q = json.loads(q)
        except json.JSONDecodeError:
            return None
    if not isinstance(q, dict) or not isinstance(q.get("ask"), str):
        return None
    opts = [{"key": o["key"], "label": o["label"], "desc": str(o.get("desc") or "")}
            for o in q.get("options") or []
            if isinstance(o, dict) and isinstance(o.get("key"), str) and isinstance(o.get("label"), str)]
    return {
        "kind": q.get("kind") if q.get("kind") in ("decision", "task") else "decision",
        "ask": q["ask"],
        "context": str(q.get("context") or ""),
        "options": opts,
        "recommended": q.get("recommended") if isinstance(q.get("recommended"), str) else None,
    }


def qsig(q: dict | None) -> str:
    """問いの署名。回答の意味を決めるもの (問い・背景・選択肢の key と label と並び・推奨) が変わると値が変わる。
    画面は回答に見ていた問いの署名を付けて送り、今の問いと違えば書き込まない。
    選択肢の説明 (desc) は入れない: 言い回しの直しで、見ていた回答を捨てさせないため。"""
    core = None if q is None else [q["ask"], q["context"], [[o["key"], o["label"]] for o in q["options"]], q["recommended"]]
    return hashlib.sha256(json.dumps(core, ensure_ascii=False).encode()).hexdigest()[:16]


def shape(i: dict) -> dict:
    q = question(i.get("metadata"))
    return {
        "id": i.get("id"),
        "title": i.get("title") or "",
        "priority": i.get("priority"),
        "labels": i.get("labels") or [],
        "created_at": (i.get("created_at") or "")[:10],
        "description": i.get("description") or "",
        "notes": i.get("notes") or "",
        "q": q,
        "qsig": qsig(q),
    }


def last_marked(repo: str, iid: str) -> dict | None:
    """最後の [humanq] コメント (回答か取り消し)。無ければ None。"""
    for c in reversed(bd_json(repo, "comments", iid)):
        if isinstance(c, dict) and (c.get("text") or "").startswith(MARK):
            return c
    return None


def list_answered(repo: str) -> list:
    return bd_json(repo, "list", "--status", ANSWERED_STATUSES, "--label", "answered", "--flat", "-n", "0")


def last_answer(repo: str, iid: str) -> str:
    try:
        c = last_marked(repo, iid)
    except BdError:
        return ""
    return (c.get("text") or "") if c else ""


def _prio(x: dict):
    return (x["priority"] if isinstance(x["priority"], int) else 9, x["created_at"])


def load_project(repo: str) -> dict:
    human = sorted((shape(i) for i in bd_json(repo, "human", "list")), key=_prio)
    answered = []
    for i in list_answered(repo):
        s = shape(i)
        s["answer"] = last_answer(repo, s["id"])
        answered.append(s)
    answered.sort(key=_prio)
    # 両方のラベルが付いたチケットは、書き込みの途中か、ラベルの付け替えに失敗したもの。画面が二重に出さないよう、
    # 出どころで片側にする。向きは最後の [humanq] コメントで決める: 取り消しなら判断待ち、回答 (またはコメント無し) なら回答済み。
    # 両方付いたものにだけ効く (answered の側は回答の本文を表示のためにもう読んでいるので、追加の読みは無い)
    both = {s["id"] for s in human} & {s["id"] for s in answered}
    undone = {s["id"] for s in answered if s["id"] in both and s["answer"].startswith(UNDO_HEAD)}
    answered = [s for s in answered if s["id"] not in undone]
    human = [s for s in human if s["id"] not in both or s["id"] in undone]
    return {"human": human, "answered": answered}


class Store:
    """プロジェクトごとの読みの直列化とキャッシュ。書き込みもプロジェクトごとに 1 本ずつ。"""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self._locks: dict[str, threading.Lock] = {}
        self._wlocks: dict[str, threading.Lock] = {}
        self._cache: dict[str, tuple[float, dict]] = {}
        self._guard = threading.Lock()
        self._refreshing: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=4)

    def _lock(self, table: dict, repo: str) -> threading.Lock:
        with self._guard:
            return table.setdefault(repo, threading.Lock())

    def projects(self) -> list[str]:
        return registry.live_projects(self.state_dir)

    def project(self, repo: str) -> dict:
        with self._lock(self._locks, repo):
            hit = self._cache.get(repo)
            if hit and time.monotonic() - hit[0] < CACHE_SEC:
                return hit[1]
            try:
                data = {"ok": True, **load_project(repo)}
            except BdError as e:
                data = {"ok": False, "error": str(e), "human": [], "answered": []}
            except Exception as e:  # noqa: BLE001  想定外の形の出力でも、そのプロジェクトだけを読めないことにする
                data = {"ok": False, "error": f"{type(e).__name__}: {e}", "human": [], "answered": []}
            self._cache[repo] = (time.monotonic(), data)
            return data

    def peek(self, repo: str) -> dict | None:
        """待たずに返す: キャッシュがあれば (古くても) それを返し、古ければ裏で 1 本だけ読み直す。
        無ければ None (裏で読み始める)。UserPromptSubmit の hook を bd の読みで待たせない。"""
        with self._guard:
            hit = self._cache.get(repo)
            stale = not hit or time.monotonic() - hit[0] >= CACHE_SEC
            if stale and repo not in self._refreshing:
                self._refreshing.add(repo)
                self._pool.submit(self._refresh, repo)
        return hit[1] if hit else None

    def _refresh(self, repo: str) -> None:
        try:
            self.project(repo)
        finally:
            with self._guard:
                self._refreshing.discard(repo)

    def invalidate(self, repo: str) -> None:
        with self._lock(self._locks, repo):
            self._cache.pop(repo, None)

    def all(self) -> list[dict]:
        repos = self.projects()
        datas = list(self._pool.map(self.project, repos))  # DB が別なので並べて読める
        return [{"path": r, "name": Path(r).name, **d} for r, d in zip(repos, datas)]

    def answer(self, repo: str, body: dict) -> None:
        iid, key = body.get("id"), body.get("option") or ""
        text = str(body.get("text") or "").strip()
        with self._lock(self._wlocks, repo):
            cur = {x["id"]: x for x in (shape(i) for i in bd_json(repo, "human", "list"))}
            if iid not in cur:
                raise Gone("このチケットは判断待ちに無い (画面を更新してください)")
            seen = body.get("qsig")        # 画面が見ていた問いの署名。送らない古い画面は検査しない
            if seen is not None and seen != cur[iid]["qsig"]:
                raise Changed("問いが書き換わっている (画面を更新して、見直してから送ってください)")
            q, opt = cur[iid]["q"], None
            if key:
                opt = next((o for o in (q or {}).get("options", []) if o["key"] == key), None)
                if opt is None:
                    raise ValueError("選択肢が見つからない")
            if opt is None and not text:
                raise ValueError("選択肢を選ぶか、回答を書いてください")
            head = f"{MARK} 回答: {opt['label']} ({key})" if opt else f"{MARK} 回答: (自由記述)"
            # 送り直しても壊れない: 同じ本文のコメントが最後に残っていれば書かず、ラベルだけを直す
            _record(repo, iid, head + ("\n\n" + text if text else "") + "\n")
            _swap(repo, iid, add="answered", remove="human")

    def undo(self, repo: str, body: dict) -> None:
        iid = body.get("id")
        with self._lock(self._wlocks, repo):
            cur = {i.get("id") for i in list_answered(repo)}
            if iid not in cur:
                raise Gone("反映待ちに無い (もう反映されたか、取り消し済み)")
            _record(repo, iid, UNDO_HEAD + "\n")
            _swap(repo, iid, add="human", remove="answered")


def _recent(comment: dict) -> bool:
    try:
        t = datetime.fromisoformat(str(comment.get("created_at")).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # 時刻が読めなければ、二重に書くよりは書かない側に倒す
    return (datetime.now(timezone.utc) - t).total_seconds() <= RESEND_SEC


def _record(repo: str, iid: str, text: str) -> None:
    """[humanq] のコメントを 1 件書く。チケットの最後のコメント (全体) が同じ本文で新しければ (送り直し) 書かない。
    最後の [humanq] コメントではなく最後のコメントを見る: 間にエージェントの問い直しのコメントが挟まっていれば、
    それは新しい問いへの回答なので書く。"""
    try:
        cs = bd_json(repo, "comments", iid)
        last = cs[-1] if cs and isinstance(cs[-1], dict) else None
        if last and (last.get("text") or "").strip() == text.strip() and _recent(last):
            return
        _comment(repo, iid, text)
    except BdError as e:
        raise WriteError(str(e), "comment", False) from e


def _swap(repo: str, iid: str, add: str, remove: str) -> None:
    """ラベルを付け替える (add を付けてから remove を外す)。各段階は 1 回だけ再試行する。"""
    for op, name in (("add", add), ("remove", remove)):
        for attempt in (1, 2):
            try:
                bd(repo, "--actor", ACTOR, "label", op, iid, name)
                break
            except BdError as e:
                if attempt == 2:
                    raise WriteError(str(e), "label", True) from e


def _comment(repo: str, iid: str, text: str) -> None:
    fd, path = tempfile.mkstemp(prefix="humanq-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        bd(repo, "--actor", ACTOR, "comments", "add", iid, "-f", path)
    finally:
        os.unlink(path)


def make_handler(store: Store, port: int, token: str, page: str, version: str):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    # frame-ancestors は default-src に含まれない。ほかのサイトが iframe に入れて回答を押させるのを止める
    csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
           "connect-src 'self'; img-src data:; frame-ancestors 'none'")

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") in hosts:  # DNS rebinding 対策
                return True
            self._send(421, b"bad host", "text/plain")
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            path, _, query = self.path.partition("?")
            if path == "/":
                self._send(200, page.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            elif path == "/api/ping":
                self._json(200, {"app": "humanq", "version": version, "pid": os.getpid()})
            elif path == "/api/items":
                self._json(200, {"projects": store.all()})
            elif path == "/api/summary":
                # hook 用。既定はキャッシュを待たずに返す (UserPromptSubmit を遅くしない)。fresh=1 は読み直す
                q = {k: _unquote(v) for k, v in (p.split("=", 1) for p in query.split("&") if "=" in p)}
                repo = q.get("project", "")
                if repo not in store.projects():
                    return self._json(404, {"error": "未登録のプロジェクト"})
                d = store.project(repo) if q.get("fresh") == "1" else store.peek(repo)
                if d is None:
                    return self._json(200, {"pending": True})
                self._json(200, {"human": len(d["human"]), "answered": [x["id"] for x in d["answered"]]})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return
            if self.headers.get("X-Humanq-Token") != token:
                return self._json(403, {"error": "token"})
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(415, {"error": "content-type"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > MAX_BODY:
                return self._json(413, {"error": "size"})
            op = {"/api/answer": "answer", "/api/undo": "undo"}.get(self.path)   # 失敗の応答に入れる (画面の文言が分かれる)
            try:
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise ValueError("body")
                repo = body.get("project")
                if repo not in store.projects():
                    raise ValueError("未登録のプロジェクト")
                fn = {"/api/answer": store.answer, "/api/undo": store.undo}.get(self.path)
                if fn is None:
                    return self._json(404, {"error": "not found"})
                try:
                    fn(repo, body)
                finally:
                    store.invalidate(repo)
                self._json(200, {"ok": True})
            except Gone as e:
                self._json(400, {"error": str(e), "code": "gone", "op": op})
            except Changed as e:
                self._json(409, {"error": str(e), "code": "changed", "op": op})
            except (ValueError, json.JSONDecodeError) as e:
                self._json(400, {"error": str(e), "op": op})
            except BdError as e:
                self._json(502, {"error": str(e), "op": op, "stage": getattr(e, "stage", None),
                                 "written": getattr(e, "written", False)})
            except Exception as e:  # noqa: BLE001  応答が無いまま切れるより、500 の JSON を返す
                self._json(500, {"error": f"サーバーの内部エラー: {type(e).__name__}: {e}", "op": op})

    return H


def _unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)


def load_token(state_dir: str) -> str:
    """書き込みのトークン。state dir のファイル (0600) に保存し、再起動しても同じ値を使う。

    サーバーの入れ替えで値が変わると、開いたままの画面の回答がすべて 403 になる。GET / は誰にでもトークン入りの
    ページを返すので、ファイルに置いても露出は増えない (同じ端末の同じユーザーだけが読める)。
    ポートを取れたプロセスだけが呼ぶこと (取れなかったものが書き換えないため)。
    読めない・壊れているファイル (UTF-8 でない、ディレクトリ、など) は作り直す。保存できなくても起動を止めず、
    その起動のあいだだけメモリ上のトークンで動く (ログに 1 行)。"""
    path = os.path.join(state_dir, TOKEN_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{20,}", tok):
            return tok
    except (OSError, ValueError):      # UnicodeDecodeError は ValueError
        pass
    tok = secrets.token_urlsafe(24)
    tmp = None
    try:
        os.makedirs(state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".tmp-token-")  # mkstemp は 0600 で作る
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        os.replace(tmp, path)
    except OSError as e:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        print(f"humanq: トークンを保存できない。この起動のあいだだけ保存せずに動く: {type(e).__name__}: {e}", file=sys.stderr)
    return tok


def main() -> None:
    here = Path(__file__).resolve()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--port", type=int, default=registry.DEFAULT_PORT)
    a = ap.parse_args()
    version = registry.plugin_version()
    page = (here.parent / "humanq.html").read_text(encoding="utf-8")
    store = Store(a.state_dir)
    try:
        # 先にポートを取る。トークンのファイルを読み書きするのは、取れたプロセスだけ
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), BaseHTTPRequestHandler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            return  # 別のセッションが先に起動した。ポートが排他の役をする
        raise
    srv.RequestHandlerClass = make_handler(store, a.port, load_token(a.state_dir), page, version)
    registry.write_server_info(a.state_dir, {"pid": os.getpid(), "port": a.port, "version": version})
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
