#!/usr/bin/env python3
"""humanq: この端末の beads (bd / br) のプロジェクトの判断待ち (human ラベル) を 1 つの localhost の画面に集め、
回答を各プロジェクトの bd / br に書き戻す。

usage: python3 humanq.py --state-dir DIR [--port 8765]

- 待ち受けは 127.0.0.1 だけ。ポートを取れなければ (もう誰かが動かしている) 何もせず終わる
- 対象のプロジェクトは DIR/projects.json (SessionStart hook が登録する)。リクエストのたびに読む。
  同じ beads の DB に届く登録 (br の git worktree と本体・symlink 越しの .beads) は 1 つにまとめる (registry.project_groups)
- DIR/server.json に pid・port・version を書く (hook が版の古いサーバーを止めるため。新しい版は止めない)
- DIR/token (0600) に書き込みのトークンを保存し、再起動 (版の入れ替え) しても同じ値を使う
規約 (問いの書き方・回答の形・拾い方) は plugin の README.md。依存は標準ライブラリと PATH 上の bd / br だけ。
CLI と話すコード (argv・出力の形・問いの置き場・コメントの並び) は backend.py にだけ置き、ここは CLI に依らない形の値を読む。
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend  # noqa: E402
import registry  # noqa: E402
from backend import BdError  # noqa: E402,F401  humanq.BdError として使われる (HTTP 502 の応答・テスト)

MARK = "[humanq]"
UNDO_HEAD = f"{MARK} 取り消し"
MAX_BODY = 64 * 1024
# bd (埋め込み Dolt) は同時に開くと直列化して遅くなり、そのプロジェクトのセッションの書き込みとも競る。
# プロジェクトごとに読みを 1 本にまとめ、CACHE_SEC の間は同じ結果を返す。書いたら捨てる
CACHE_SEC = 8.0
# 同じ回答の送り直しとみなす間隔。これより前の同じ本文のコメントは、前の問いへの回答かもしれないので数えない
RESEND_SEC = 600
TOKEN_FILE = "token"


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


class Stale(ValueError):
    """取り消そうとした回答が、今の最後の [humanq] コメントと違う (ほかで取り消して答え直された)。書き込みは何も起きていない。"""


class ReadOnly(ValueError):
    """bd と br のどちらで書くか決められないプロジェクトへの書き込み。CLI は打っていない。"""


# ---- 差し替え口 (中身は backend.py) ----
# bd を 1 回打つ関数。テストは humanq.bd = ... で失敗を作り、呼び出しを数える。
# BdBackend は呼ぶたびにこのモジュールの bd を引くので、差し替えはすべての読み書きに届く
bd = backend.run_bd
_BD = backend.BdBackend(run=lambda repo, *args: bd(repo, *args))
# br を 1 回打つ関数。humanq.br = ... で差し替えると、br のプロジェクトの読み書きすべてに届く
br = backend.run_br
_BR = backend.BrBackend(run=lambda repo, *args: br(repo, *args))
# プロジェクトの種別 (registry.project_kind) ごとの backend。ここに無い種別 (unknown / conflict) は読みも書きもしない
BACKENDS = {registry.BD: _BD, registry.BR: _BR}


def bd_json(repo: str, *args: str) -> list:
    """bd の --json の出力を list に読む (読み方は backend.parse_bd_list)。"""
    return _BD.json(repo, *args)


def question(meta) -> dict | None:
    """bd のチケットの metadata (dict) から問いを読む。問いの生の値の取り出しは backend、読みは question_from_raw。"""
    return question_from_raw(backend.BdBackend.question_raw(meta))


def question_from_raw(q) -> dict | None:
    """問いの生の値 (Row の question_raw: dict か JSON の文字列か None) を、画面に出す問いにする。読めなければ None。"""
    if isinstance(q, str):
        try:
            q = json.loads(q)
        except (ValueError, RecursionError):    # RecursionError は深い入れ子 (json の parser の限界。backend._loads と同じ)
            return None
    if not isinstance(q, dict) or not isinstance(q.get("ask"), str):
        return None
    raw_opts = q.get("options")
    if raw_opts is None:
        raw_opts = []
    # options は list (無い・null は選択肢なし)。list でない値や、key と label が文字列の object でない要素が 1 つでもあれば、
    # その 1 件だけを問いが未整理 (None) にする。例外にすると、同じプロジェクトのほかの判断待ちまで読めなくなる。
    # 壊れた要素だけを黙って落とすと、問いの書き手が意図したものと違う選択肢を出すことになる
    if not isinstance(raw_opts, list) or not all(
            isinstance(o, dict) and isinstance(o.get("key"), str) and isinstance(o.get("label"), str) for o in raw_opts):
        return None
    opts = [{"key": o["key"], "label": o["label"], "desc": str(o.get("desc") or "")} for o in raw_opts]
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


def shape(r: dict) -> dict:
    """backend の Row を、画面に出すチケットにする。"""
    q = question_from_raw(r["question_raw"])
    return {
        "id": r["id"],
        "title": r["title"] or "",
        "priority": r["priority"],
        "labels": r["labels"] or [],
        "created_at": (r["created_at"] or "")[:10],
        "description": r["description"] or "",
        "notes": r["notes"] or "",
        "q": q,
        "qsig": qsig(q),
    }


def marked(cs: list) -> list[dict]:
    """[humanq] のコメント (回答と取り消し) を、backend が返した順 (古い順) に。本文が文字列でないコメントは数えない
    (回答の書き込みは全部のコメントを数えるので、想定外の形の 1 件で回答を止めない)。"""
    return [c for c in cs if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].startswith(MARK)]


def asig(n: int, text: str | None) -> str:
    """回答の署名。n はそのコメントがチケットの何件目の [humanq] コメントか (1 から)、text はその本文
    (None は [humanq] のコメントが無い)。画面は回答の成功の応答と回答済みの一覧で受け取り、取り消しに付けて送る。
    サーバーは今の最後の [humanq] コメントの署名と照合し、違えば取り消さない (ほかのタブ・セッションで取り消して
    答え直された後に、前の回答の「取り消す」で今の回答を取り消さないため)。
    件数を入れるのは、同じ本文の答え直し (案 A → 取り消し → 案 A) を前の回答と分けるため。[humanq] のコメントは
    このサーバーだけがプロジェクトごとに 1 本ずつ書くので、件数は間にエージェントのコメントが挟まっても変わらない。
    回答を書いた後に bd を読み直さずに決まる (コメントの id は bd が付けるので、知るには読み直しが要る)。"""
    core = None if text is None else [n, text.strip()]
    return hashlib.sha256(json.dumps(core, ensure_ascii=False).encode()).hexdigest()[:16]


def last_of(cs: list) -> tuple[str, str]:
    """最後の [humanq] コメントの本文と、その署名。コメントが無ければ本文は空。"""
    ms = marked(cs)
    return (ms[-1]["text"], asig(len(ms), ms[-1]["text"])) if ms else ("", asig(0, None))


def last_answer(thread: list | None) -> tuple[str, str]:
    """回答済みのカードに出す、最後の [humanq] コメントの本文と署名。thread は backend の load が読んだコメント (古い順)。
    コメントを読めなければ (None) ("", "")。空の署名はどの回答の署名とも合わないので、そのカードからの取り消しは照合で断られる
    (None にすると、署名を送らない古い画面と同じに読まれて照合を素通りする)。"""
    return ("", "") if thread is None else last_of(thread)


def _prio(x: dict):
    return (x["priority"] if isinstance(x["priority"], int) else 9, x["created_at"])


def load_project(repo: str, be=None) -> dict:
    """be は読む backend (省略は bd)。"""
    human_rows, answered_rows = (be or _BD).load(repo)
    human = sorted((shape(r) for r in human_rows), key=_prio)
    answered = []
    for r in answered_rows:
        s = shape(r)
        s["answer"], s["asig"] = last_answer(r["thread"])
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


def unreadable(reason: str) -> dict:
    """bd と br のどちらで読むか決められないプロジェクト (registry.project_kind の unknown / conflict) の結果。
    どの CLI も起動しない。"""
    return {"ok": False, "writable": False, "cli": None,
            "error": f"bd と br のどちらで読むか決められない ({reason})。{registry.CLI_ENV} で指定してください"}


class Store:
    """プロジェクトごとの読みの直列化とキャッシュ。書き込みは git の repo (worktree を含む) ごと、git の外ならプロジェクトごとに 1 本ずつ。
    repo は組の代表のパス (Store.resolve)。answer / undo は一員のパスを渡されても代表のパスにしてから書く。"""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self._locks: dict[str, threading.Lock] = {}
        self._wlocks: dict[str, threading.Lock] = {}
        self._cache: dict[str, tuple[float, dict, tuple[str, str]]] = {}
        self._guard = threading.Lock()
        self._refreshing: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=4)

    def _lock(self, table: dict, repo: str) -> threading.Lock:
        with self._guard:
            return table.setdefault(repo, threading.Lock())

    def projects(self) -> list[str]:
        return registry.live_projects(self.state_dir)

    def resolve(self, repo) -> str | None:
        """登録のパス (worktree・symlink 越しなど、同じ DB に届く組の一員) を、組の代表のパスにする。未登録なら None。"""
        return registry.resolve_project(self.state_dir, repo)

    def _write_lock(self, repo: str) -> threading.Lock:
        """回答・取り消しのロック。key は git の common dir (本体と worktree で同じ)、git の外なら代表のパス。
        画面で 1 つにまとめない形 (bd の worktree など) でも、同じ repo の 2 枚のカードからの同時の回答を 1 本ずつにする。"""
        return self._lock(self._wlocks, registry.git_common_dir(repo) or repo)

    def route(self, repo: str, groups: list[dict] | None = None) -> tuple[str, str]:
        """repo を読み書きする CLI の種別 (bd / br / unknown / conflict) と理由。ファイルの印だけで決め、CLI は打たない。
        groups は読み済みの registry.project_groups (all が 1 回だけ読んで渡す。省略は読む)。"""
        return registry.project_kind(repo, registry.project_cli(self.state_dir, repo, groups))

    def project(self, repo: str, fresh: bool = False, groups: list[dict] | None = None) -> dict:
        """fresh なら、キャッシュが有効な間でも CLI で読み直す (/api/summary?fresh=1)。"""
        with self._lock(self._locks, repo):
            route = self.route(repo, groups)
            kind, reason = route
            hit = self._cache.get(repo)
            # 設定 (HUMANQ_BEADS_CLI) や印が変わったら、キャッシュの時間内でも読み直す (ファイルを見るだけで安い)
            if hit and not fresh and hit[2] == route and time.monotonic() - hit[0] < CACHE_SEC:
                return hit[1]
            be = BACKENDS.get(kind)
            if be is None:
                data = {**unreadable(reason), "human": [], "answered": []}
                self._cache[repo] = (time.monotonic(), data, route)
                return data
            # bd・br として読むプロジェクトは、読みに失敗しても writable:true (書き込みは読み直してから行う)
            try:
                data = {"ok": True, **load_project(repo, be)}
            except BdError as e:
                data = {"ok": False, "error": str(e), "human": [], "answered": []}
            except Exception as e:  # noqa: BLE001  想定外の形の出力でも、そのプロジェクトだけを読めないことにする
                data = {"ok": False, "error": f"{type(e).__name__}: {e}", "human": [], "answered": []}
            data.update(cli=kind, writable=True)
            self._cache[repo] = (time.monotonic(), data, route)
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
        # 組は 1 回だけ読む (プロジェクトごとに読み直すと、登録の数の 2 乗のファイルの読みになる)
        groups = registry.project_groups(self.state_dir)
        repos = [g["root"] for g in groups]
        datas = list(self._pool.map(lambda r: self.project(r, groups=groups), repos))  # DB が別なので並べて読める
        return [{"path": r, "name": Path(r).name, **d} for r, d in zip(repos, datas)]

    def writer(self, repo: str):
        """回答・取り消しを書き込む backend。bd と br のどちらで書くか決められないプロジェクトは、CLI を打たずに ReadOnly。"""
        kind, reason = self.route(repo)
        be = BACKENDS.get(kind)
        if be is None:
            raise ReadOnly(f"このプロジェクトは書き込めない (bd と br のどちらで書くか決められない: {reason})")
        return be

    def answer(self, repo: str, body: dict) -> str:
        """返り値は回答の署名 (asig)。成功の応答に入れ、画面はその回答の取り消しに付けて送る。"""
        iid, key = body.get("id"), body.get("option") or ""
        text = str(body.get("text") or "").strip()
        repo = self.resolve(repo) or repo
        with self._write_lock(repo):
            be = self.writer(repo)
            rows = be.pending(repo, iid)
            cur = {x["id"]: x for x in (shape(r) for r in rows)}
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
            # 送り直しても壊れない: 同じ本文のコメントが最後に残っていれば書かず、ラベルだけを直す。
            # pending が問いと一緒にコメントを読んでいれば (br)、それで判定する (読み直さない)
            thread = next((r.get("thread") for r in rows if r["id"] == iid), None)
            sig = _record(be, repo, iid, head + ("\n\n" + text if text else "") + "\n", cs=thread)
            _swap(be, repo, iid, add="answered", remove="human")
            return sig

    def undo(self, repo: str, body: dict) -> None:
        iid = body.get("id")
        seen = body.get("asig")            # 取り消す回答の署名。送らない古い画面は検査しない
        repo = self.resolve(repo) or repo
        with self._write_lock(repo):
            be = self.writer(repo)
            cur = {r["id"] for r in be.list_answered(repo)}
            if iid not in cur:
                raise Gone("反映待ちに無い (もう反映されたか、取り消し済み)")
            _record(be, repo, iid, UNDO_HEAD + "\n", expect=seen)
            _swap(be, repo, iid, add="human", remove="answered")


def _recent(comment: dict) -> bool:
    try:
        t = datetime.fromisoformat(str(comment.get("created_at")).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # 時刻が読めなければ、二重に書くよりは書かない側に倒す
    return (datetime.now(timezone.utc) - t).total_seconds() <= RESEND_SEC


def _record(be, repo: str, iid: str, text: str, expect: str | None = None, cs: list | None = None) -> str:
    """[humanq] のコメントを 1 件書く。チケットの最後のコメント (全体) が同じ本文で新しければ (送り直し) 書かない。
    最後の [humanq] コメントではなく最後のコメントを見る: 間にエージェントの問い直しのコメントが挟まっていれば、
    それは新しい問いへの回答なので書く。
    expect (取り消す回答の署名) を渡すと、今の最後の [humanq] コメントの署名と違えば何も書かずに Stale を上げる。
    照合は送り直しの判定と同じ読みで行う (CLI の呼び出しを増やさない)。
    cs は書き込みのロックの下で読んだばかりのコメント (古い順)。None なら be.comments で読む。
    返り値は、書いたコメント (送り直しなら残っているもの) の署名。回答済みの一覧の署名 (last_of) と同じ値になる。"""
    try:
        if cs is None:
            cs = be.comments(repo, iid)
        if expect is not None and expect != last_of(cs)[1]:
            raise Stale("この回答はもう最新ではない (ほかで答え直されている。画面を更新してください)")
        last = cs[-1] if cs and isinstance(cs[-1], dict) else None
        if last and (last.get("text") or "").strip() == text.strip() and _recent(last):
            return last_of(cs)[1]
        _comment(be, repo, iid, text)
        return asig(len(marked(cs)) + 1, text)
    except BdError as e:
        raise WriteError(str(e), "comment", False) from e


def _swap(be, repo: str, iid: str, add: str, remove: str) -> None:
    """ラベルを付け替える (add を付けてから remove を外す)。各段階は 1 回だけ再試行する。"""
    for op, name in (("add", add), ("remove", remove)):
        for attempt in (1, 2):
            try:
                be.label(repo, op, iid, name)
                break
            except BdError as e:
                if attempt == 2:
                    raise WriteError(str(e), "label", True) from e


def _comment(be, repo: str, iid: str, text: str) -> None:
    fd, path = tempfile.mkstemp(prefix="humanq-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        be.add_comment(repo, iid, path)
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
                # hook は自分の登録のパスで問い合わせる。worktree などで別のパスにまとめた組なら、代表のパスで読む
                repo = store.resolve(q.get("project", ""))
                if repo is None:
                    return self._json(404, {"error": "未登録のプロジェクト"})
                d = store.project(repo, fresh=True) if q.get("fresh") == "1" else store.peek(repo)
                if d is None:
                    return self._json(200, {"pending": True})
                # ok:false (読めないプロジェクト) の human は 0 でも「0 件」ではない。hook はこれを見て「読めない」と伝える。
                # cli は hook が案内の文を選ぶため (br のプロジェクトには bd のコマンドを案内しない)
                self._json(200, {"human": len(d["human"]), "answered": [x["id"] for x in d["answered"]],
                                 "ok": d.get("ok", True), "error": d.get("error"),
                                 "cli": d.get("cli"), "writable": d.get("writable")})
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
                # まとめる前の画面が送る一員のパスも、代表のパスにして同じロックの下で書く
                repo = store.resolve(body.get("project"))
                if repo is None:
                    raise ValueError("未登録のプロジェクト")
                fn = {"/api/answer": store.answer, "/api/undo": store.undo}.get(self.path)
                if fn is None:
                    return self._json(404, {"error": "not found"})
                # 決められないプロジェクトは、Store.writer が CLI を打つ前に ReadOnly で断る (下の readonly の応答)
                try:
                    sig = fn(repo, body)
                finally:
                    store.invalidate(repo)
                # 回答の成功には回答の署名 (asig) を入れる。取り消しの成功は {ok: true} だけ
                self._json(200, {"ok": True} if sig is None else {"ok": True, "asig": sig})
            except ReadOnly as e:
                self._json(400, {"error": str(e), "code": "readonly", "op": op})
            except Gone as e:
                self._json(400, {"error": str(e), "code": "gone", "op": op})
            except Changed as e:
                self._json(409, {"error": str(e), "code": "changed", "op": op})
            except Stale as e:
                self._json(409, {"error": str(e), "code": "stale", "op": op})
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
