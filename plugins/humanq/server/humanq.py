#!/usr/bin/env python3
"""humanq: この端末の beads (bd / br) のプロジェクトの判断待ち (human ラベル) を 1 つの localhost の画面に集め、
回答 (と保留・情報待ち) を各プロジェクトの bd / br に書き戻す。

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
import contextlib
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
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend  # noqa: E402
import registry  # noqa: E402
from backend import BdError  # noqa: E402,F401  humanq.BdError として使われる (HTTP 502 の応答・テスト)

MARK = "[humanq]"
UNDO_HEAD = f"{MARK} 取り消し"
# 保留 (README の「回答の形」): 1 行目が `[humanq] 保留: <YYYY-MM-DD> に戻る (qsig: <問いの署名>)` か
# `[humanq] 保留: 期限なし (qsig: <問いの署名>)`。日付は判断待ちに戻る日 (その日の読みから判断待ちに出す)。
# 問いの署名は保留したときの問いのもので、今の問いの署名と違えば保留とは読まない (問い直された問いを古い保留で隠さない)。
# 解除は `[humanq] 保留の解除`。正本はコメントで、ラベル (SNOOZE_LABEL) はコメントを読むチケットを絞る印。status は変えない
SNOOZE_HEAD = f"{MARK} 保留: "
SNOOZE_NO_DATE = "期限なし"
UNSNOOZE_HEAD = f"{MARK} 保留の解除"
SNOOZE_LABEL = backend.SNOOZE_LABEL
_SNOOZE_LINE = re.compile(re.escape(SNOOZE_HEAD) + r"(?:([0-9]{4}-[0-9]{2}-[0-9]{2}) に戻る|" + SNOOZE_NO_DATE
                          + r") \(qsig: ([0-9a-f]{16})\)")
# 情報待ち (README の「回答の形」): ユーザーが「回答に必要な情報が足りない」とエージェントに返したもの。1 行目が
# `[humanq] 情報待ち (qsig: <問いの署名>)` で、空行の後に足りない情報 (必須) が続く。問いの署名は返したときの問いのもので、今の問いの
# 署名と違えば (エージェントが情報を足して問いを書き直した) 情報待ちとは読まない。正本はコメントで、ラベル (NEEDINFO_LABEL) は
# コメントを読むチケットを絞る印。human・status・担当は変えない。ラベルは、問いを書き直したエージェントが外す
NEEDINFO_HEAD = f"{MARK} 情報待ち"
NEEDINFO_LABEL = backend.NEEDINFO_LABEL
_NEEDINFO_LINE = re.compile(re.escape(NEEDINFO_HEAD) + r" \(qsig: ([0-9a-f]{16})\)")
# 判断待ちの側で書く [humanq] コメント (取り消し・保留・保留の解除・情報待ち)。両方のラベルのチケットの最後の [humanq] コメントが
# これなら判断待ちに出す (回答かコメント無しなら回答済み)
PENDING_SIDE = (UNDO_HEAD, SNOOZE_HEAD, UNSNOOZE_HEAD, NEEDINFO_HEAD)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MAX_BODY = 64 * 1024
# bd (埋め込み Dolt) は同時に開くと直列化して遅くなり、そのプロジェクトのセッションの書き込みとも競る。
# プロジェクトごとに読みを 1 本にまとめ、CACHE_SEC の間は同じ結果を返す。書いたら捨てる
CACHE_SEC = 8.0
# 回答済みと保留・情報待ちの thread の覚え (backend.ThreadMemo)。THREAD_MEMO_SEC は覚えを使う上限 (このサーバーの書き込みは、その場で
# 捨てる)、SNOOZE_READ_SEC は 1 回の読みで保留・情報待ちの thread を CLI で読む時間の上限 (回答済みには当てない)。SessionStart の hook は
# 件数の読みを 8 秒で打ち切る。画面の読みの最中なら、その読みの結果を受け取る (Store.project の fresh)。上限は一覧と回答済みの
# コメントの読みの後に数え始めるので、1 回の読みはその分だけ 2 秒より長い (回答済みのコメントを読むのは、覚えの無い・古いものだけ)
THREAD_MEMO_SEC = 600.0
SNOOZE_READ_SEC = 2.0
# 時間の上限で読めなかった保留の thread を裏で読むとき、1 件ごとに読みのロックを放す間 (待っている読みを先に通す)
WARM_GAP_SEC = 0.05
# 同じ回答の送り直しとみなす間隔。これより前の同じ本文のコメントは、前の問いへの回答かもしれないので数えない
RESEND_SEC = 600
TOKEN_FILE = "token"
# 見通し (README の「画面の見通し」。読むだけ)。判断待ちの読み (/api/items・/api/summary・hook) とは別の API (/api/overview) で、
# 画面が見通しのタブを開いたときだけ読む。読みは 1 プロジェクトあたり 2 プロセス (状態別の件数と ready)。キャッシュは判断待ちとは
# 別に OVERVIEW_CACHE_SEC (書き込みの後は判断待ちのキャッシュと一緒に捨てる: 回答で answered が付いたものを ready から外す)
OVERVIEW_CACHE_SEC = 60.0
READY_TOP = 5
# 状態別の件数の並び: 組み込みの status を作業の順に → ほかの status (名前の順) → 終わった status。並べるのは status の名前だけで、
# ラベル (plugin: や lane: など) は並びに使わない
STATUS_ORDER = ("open", "in_progress", "blocked", "hooked", "deferred", "draft", "pinned")
DONE_STATUSES = ("closed", "tombstone")


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
    """[humanq] のコメント (回答・取り消し・保留・保留の解除・情報待ち) を、backend が返した順 (古い順) に。本文が文字列でないコメントは数えない
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


def today() -> date:
    """保留の戻る日と比べる今日 (サーバーのローカルの日付)。画面は /api/items の today でこの日付を受け取り、期間の
    選択と日付の下限に使う (ブラウザの TZ では決めない)。テストは humanq.today を差し替える。"""
    return date.today()


def snooze_line(until: str | None, sig: str) -> str:
    """保留のコメントの 1 行目。until は判断待ちに戻る日 (YYYY-MM-DD) か None (期限なし)、sig は今の問いの署名。"""
    return SNOOZE_HEAD + (f"{until} に戻る" if until else SNOOZE_NO_DATE) + f" (qsig: {sig})"


def snooze_of(thread) -> dict | None:
    """thread (古い順の Comment) の最後の [humanq] コメントが保留なら {until, note, qsig}。until は判断待ちに戻る日
    (YYYY-MM-DD) か None (期限なし)、note は補足 (空行の後の本文。無ければ "")、qsig は保留したときの問いの署名。
    最後の [humanq] コメントが保留でない (回答・取り消し・保留の解除)・保留の 1 行目の形が崩れている (問いの署名が無いものを
    含む)・日付として読めない・thread を読めない (None) ときは None (保留と確かめられないものは、判断待ちに出す側に倒す)。"""
    if not isinstance(thread, list):
        return None
    ms = marked(thread)
    if not ms:
        return None
    first, _, rest = ms[-1]["text"].partition("\n")
    m = _SNOOZE_LINE.fullmatch(first.rstrip())
    if m is None:
        return None
    until = m.group(1)
    if until is not None:
        try:
            until = date.fromisoformat(until).isoformat()
        except ValueError:
            return None
    return {"until": until, "note": rest.strip(), "qsig": m.group(2)}


def needinfo_line(sig: str) -> str:
    """情報待ちのコメントの 1 行目。sig は今の問いの署名。"""
    return f"{NEEDINFO_HEAD} (qsig: {sig})"


def needinfo_of(thread) -> dict | None:
    """thread (古い順の Comment) の最後の [humanq] コメントが情報待ちなら {note, qsig}。note は足りない情報 (空行の後の本文。
    無ければ "")、qsig は情報待ちにしたときの問いの署名。最後の [humanq] コメントが情報待ちでない (回答・取り消し・保留など)・
    1 行目の形が崩れている (問いの署名が無いものを含む)・thread を読めない (None) ときは None (情報待ちと確かめられないものは、
    判断待ちに出す側に倒す)。"""
    if not isinstance(thread, list):
        return None
    ms = marked(thread)
    if not ms:
        return None
    first, _, rest = ms[-1]["text"].partition("\n")
    m = _NEEDINFO_LINE.fullmatch(first.rstrip())
    if m is None:
        return None
    return {"note": rest.strip(), "qsig": m.group(1)}


def _expired(sn: dict) -> bool:
    """保留の戻る日が来たか (戻る日の当日から判断待ちに出す。前の日まで保留)。期限なしは来ない。"""
    return sn["until"] is not None and date.fromisoformat(sn["until"]) <= today()


def _prio(x: dict):
    return (x["priority"] if isinstance(x["priority"], int) else 9, x["created_at"])


def _snooze_order(x: dict):
    """保留中の並び: 戻る日の早いもの (期限なしは最後) → 優先度 → 作成日。"""
    u = x["snooze"]["until"]
    return (u is None, u or "", *_prio(x))


def load_project(repo: str, be=None, memo: backend.ThreadMemo | None = None) -> dict:
    """be は読む backend (省略は bd)、memo は回答済みと保留・情報待ちの thread の覚え (Store が渡す。省略は覚えずに全部読む)。
    判断待ち (human)・回答済み (answered)・保留中 (snoozed)・情報待ち (needinfo) を返す。判断待ちと保留中の各チケットには snooze が付く:
    判断待ちは None か、保留から戻ったもの ({until, note, expired, changed}。expired は戻る日が来た、changed は保留の後に
    問いが書き換わった。少なくとも一方が true)。保留中は {until, note, expired: false, changed: false}。情報待ちの各チケットには
    needinfo ({note}。note は足りない情報) が付く (並びは判断待ちと同じ優先度 → 作成日)。"""
    human_rows, answered_rows = (be or _BD).load(repo, memo)
    human = sorted(((shape(r), r) for r in human_rows), key=lambda x: _prio(x[0]))
    answered = []
    for r in answered_rows:
        s = shape(r)
        s["answer"], s["asig"] = last_answer(r["thread"])
        answered.append(s)
    answered.sort(key=_prio)
    # 両方のラベルが付いたチケットは、書き込みの途中か、ラベルの付け替えに失敗したもの。画面が二重に出さないよう、
    # 出どころで片側にする。向きは最後の [humanq] コメントで決める: 取り消し (と、判断待ちの側でだけ書く保留・保留の解除)
    # なら判断待ち、回答 (またはコメント無し) なら回答済み。
    # 両方付いたものにだけ効く (answered の側は回答の本文を表示のためにもう読んでいるので、追加の読みは無い)
    both = {s["id"] for s, _ in human} & {s["id"] for s in answered}
    undone = {s["id"] for s in answered if s["id"] in both and s["answer"].startswith(PENDING_SIDE)}
    answered = [s for s in answered if s["id"] not in undone]
    # 情報待ち: ラベルが付いていて、最後の [humanq] コメントが情報待ちで、情報待ちにしたときの問いが今の問いと同じものだけを
    # 判断待ちから外す。エージェントが情報を足して問いを書き直したもの (問いの署名が違う) は、ラベルが残っていても判断待ちに出す。
    # 保留: ラベルが付いていて、最後の [humanq] コメントが保留で、戻る日が来ておらず、保留したときの問いが今の問いと同じ
    # (問いの署名が同じ) ものだけを判断待ちから外す。戻る日が来たもの・問いが書き換わったものは、判断待ちに戻して出す
    # (ラベルは次の回答で外す)。どちらも最後の [humanq] コメントで決めるので、1 件が両方になることは無い
    waiting, snoozed, needinfo, unread = [], [], [], 0
    for s, r in human:
        if s["id"] in both and s["id"] not in undone:
            continue
        thread = r.get("thread")
        ni = needinfo_of(thread) if backend.labeled(r, NEEDINFO_LABEL) else None
        if ni is not None and ni["qsig"] == s["qsig"]:
            needinfo.append({**s, "needinfo": {"note": ni["note"]}})
            continue
        labeled = backend.labeled(r, SNOOZE_LABEL)
        sn = snooze_of(thread) if labeled else None
        if sn is None:
            # 保留か情報待ちのラベルが付いているのにコメントを読めていない (thread が None: 1 回の読みの時間の上限に間に合わなかった・
            # 読めなかった) 判断待ちは、保留か情報待ちか確かめられないので判断待ちに出している。その件数を unread に数える
            # (読めていて保留・情報待ちでなくなったものは数えない)
            if backend.watched(r) and not isinstance(thread, list):
                unread += 1
            waiting.append({**s, "snooze": None})
            continue
        why = {"until": sn["until"], "note": sn["note"], "expired": _expired(sn), "changed": sn["qsig"] != s["qsig"]}
        if why["expired"] or why["changed"]:
            waiting.append({**s, "snooze": why})
        else:
            snoozed.append({**s, "snooze": why})
    snoozed.sort(key=_snooze_order)
    return {"human": waiting, "answered": answered, "snoozed": snoozed, "needinfo": needinfo, "unread": unread}


def empty() -> dict:
    """読めなかったプロジェクトの一覧 (どれも空の list。呼ぶたびに新しい list)。"""
    return {"human": [], "answered": [], "snoozed": [], "needinfo": []}


def unreadable(reason: str) -> dict:
    """bd と br のどちらで読むか決められないプロジェクト (registry.project_kind の unknown / conflict) の結果。
    どの CLI も起動しない。"""
    return {"ok": False, "writable": False, "cli": None,
            "error": f"bd と br のどちらで読むか決められない ({reason})。{registry.CLI_ENV} で指定してください"}


# ---- 見通し (読むだけ) ----

def _status_key(name: str) -> tuple:
    if name in STATUS_ORDER:
        return (0, STATUS_ORDER.index(name), "")
    if name in DONE_STATUSES:
        return (2, DONE_STATUSES.index(name), "")
    return (1, 0, name)


def overview_of(raw: dict, pending: set, answered: set) -> dict:
    """読んだ見通し (raw: {ok, cli, counts, ready} か {ok: false, error, cli}) を、画面に出す形にする。
    pending と answered は判断待ちの読みのキャッシュにある id (判断待ち・保留中・情報待ちと、回答済み。CLI は打たない)。
    ready は answered のラベルの付いたもの (と、回答済みの一覧にあるもの) を除き、優先度 (数でなければ 9) → 作成日 → id の順に
    並べて上位 READY_TOP 件。human のラベルの付いたもの (と、判断待ちの一覧にあるもの) は human: true (画面は判断待ちのカードへの
    リンクにする)。ほかのラベルは解釈せず、文字列のものをそのまま渡す。ready_total は除いた後の件数。"""
    if not raw.get("ok"):
        return {"ok": False, "error": raw.get("error"), "cli": raw.get("cli"),
                "counts": [], "total": 0, "ready": [], "ready_total": 0}
    counts = sorted(raw["counts"], key=lambda c: _status_key(c[0]))
    rows = [r for r in raw["ready"] if not backend.labeled(r, "answered") and r["id"] not in answered]
    rows.sort(key=lambda r: (r["priority"] if type(r["priority"]) is int else 9, str(r["created_at"] or ""), r["id"]))
    return {"ok": True, "cli": raw.get("cli"),
            "counts": [{"status": s, "count": n} for s, n in counts], "total": sum(n for _, n in counts),
            "ready": [_ready_item(r, pending) for r in rows[:READY_TOP]], "ready_total": len(rows)}


def _ready_item(r: dict, pending: set) -> dict:
    labels = [x for x in r["labels"] if isinstance(x, str)] if isinstance(r["labels"], list) else []
    return {"id": r["id"], "title": r["title"] if isinstance(r["title"], str) else "",
            "priority": r["priority"] if type(r["priority"]) is int else None,
            "status": r["status"] if isinstance(r["status"], str) else "",
            "labels": labels, "created_at": str(r["created_at"] or "")[:10],
            "human": "human" in labels or r["id"] in pending}


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
        # 回答済みと保留・情報待ちの thread の覚え。書き込み (answer / undo / snooze / unsnooze / needinfo) はそのチケットの覚えを捨てる
        self._threads = backend.ThreadMemo(THREAD_MEMO_SEC, SNOOZE_READ_SEC)
        self._warming: set[str] = set()
        self._warm_pool = ThreadPoolExecutor(max_workers=1)     # 画面の読み (_pool) の枠を裏の読みで埋めない
        # 見通しの読み (状態別の件数と ready) のキャッシュ。判断待ちのキャッシュ (_cache) とは別で、長め (OVERVIEW_CACHE_SEC)
        self._ocache: dict[str, tuple[float, dict, tuple[str, str]]] = {}

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
        """fresh なら、キャッシュが有効な間でも CLI で読み直す (/api/summary?fresh=1)。ただし問い合わせの後に終わった読み
        (このロックを待っていた間に終わった画面の読み) の結果はそのまま返す。fresh でも、回答済みと保留の thread は覚え
        (ThreadMemo) から使う (読み直すのは一覧と、覚えの無い・古いコメント)。"""
        asked = time.monotonic()
        with self._lock(self._locks, repo):
            route = self.route(repo, groups)
            kind, reason = route
            hit = self._cache.get(repo)
            # 設定 (HUMANQ_BEADS_CLI) や印が変わったら、キャッシュの時間内でも読み直す (ファイルを見るだけで安い)。
            # fresh でも、問い合わせの後に終わった読みの結果は返す (SessionStart の hook が画面の読みの後ろで、もう 1 回分を
            # 読まない。2 回分を待つと、保留と回答済みのコメントの読みの分だけ 8 秒の打ち切りに近づく)
            if hit and hit[2] == route and (hit[0] >= asked if fresh else time.monotonic() - hit[0] < CACHE_SEC):
                return hit[1]
            be = BACKENDS.get(kind)
            if be is None:
                data = {**unreadable(reason), **empty()}
                self._cache[repo] = (time.monotonic(), data, route)
                return data
            # bd・br として読むプロジェクトは、読みに失敗しても writable:true (書き込みは読み直してから行う)
            try:
                data = {"ok": True, **load_project(repo, be, self._threads)}
            except BdError as e:
                data = {"ok": False, "error": str(e), **empty()}
            except Exception as e:  # noqa: BLE001  想定外の形の出力でも、そのプロジェクトだけを読めないことにする
                data = {"ok": False, "error": f"{type(e).__name__}: {e}", **empty()}
            data.update(cli=kind, writable=True)
            self._cache[repo] = (time.monotonic(), data, route)
            self._warm_later(repo, be, self._threads.take_left(repo))
            return data

    def _warm_later(self, repo: str, be, ids: list[str]) -> None:
        """時間の上限で読めなかった保留・情報待ちの thread を裏で読む (プロジェクトごとに 1 本)。読み終えたら、キャッシュを古い扱いに
        して、次の読みが覚えで作り直すようにする。"""
        if not ids:
            return
        with self._guard:
            if repo in self._warming:
                return
            self._warming.add(repo)
        self._warm_pool.submit(self._warm, repo, be, ids)

    def _warm(self, repo: str, be, ids: list[str]) -> None:
        try:
            for iid in ids:
                # 1 件ごとに読みのロックを取って放す (hook の件数の読みが、裏の読みの全部を待たない)
                with self._lock(self._locks, repo):
                    if self._threads.get(repo, iid)[1]:
                        continue                        # 間に表の読みが読んだ
                    gen = self._threads.gen(repo)
                    try:
                        thread = be.comments(repo, iid)
                    except BdError:
                        continue
                    self._threads.put(repo, iid, thread, gen)
                time.sleep(WARM_GAP_SEC)
            with self._lock(self._locks, repo):
                hit = self._cache.get(repo)
                if hit:
                    self._cache[repo] = (float("-inf"), *hit[1:])
        except Exception as e:  # noqa: BLE001  裏の読みの失敗で、サーバーを止めない (次の表の読みが読み直す)
            print(f"humanq: 保留・情報待ちのコメントを裏で読めなかった: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        finally:
            with self._guard:
                self._warming.discard(repo)

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
        """書き込みの後に、判断待ちと見通しのキャッシュを捨てる。"""
        with self._lock(self._locks, repo):
            self._cache.pop(repo, None)
            self._ocache.pop(repo, None)

    def all(self) -> list[dict]:
        # 組は 1 回だけ読む (プロジェクトごとに読み直すと、登録の数の 2 乗のファイルの読みになる)
        groups = registry.project_groups(self.state_dir)
        repos = [g["root"] for g in groups]
        datas = list(self._pool.map(lambda r: self.project(r, groups=groups), repos))  # DB が別なので並べて読める
        return [{"path": r, "name": Path(r).name, **d} for r, d in zip(repos, datas)]

    # ---- 見通し (読むだけ。/api/overview だけが呼ぶ。/api/items・/api/summary・hook は呼ばない) ----

    def overviews(self) -> list[dict]:
        """登録したプロジェクトごとの見通し (並びは all と同じ)。"""
        groups = registry.project_groups(self.state_dir)
        repos = [g["root"] for g in groups]
        datas = list(self._pool.map(lambda r: self.overview(r, groups=groups), repos))
        return [{"path": r, "name": Path(r).name, **d} for r, d in zip(repos, datas)]

    def overview(self, repo: str, groups: list[dict] | None = None) -> dict:
        """1 プロジェクトの見通し (overview_of の形)。読み (状態別の件数 → ready) は判断待ちの読みと同じロックの下で 1 本ずつ行い
        (bd の埋め込み Dolt を同時に開かない)、OVERVIEW_CACHE_SEC の間は同じ読みを使う。設定や印が変わったら読み直す。
        ready から回答済みを除き、判断待ちに印を付けるのは毎回 (判断待ちの読みのキャッシュの id も使う。CLI は打たない)。"""
        with self._lock(self._locks, repo):
            route = self.route(repo, groups)
            hit = self._ocache.get(repo)
            if hit and hit[2] == route and time.monotonic() - hit[0] < OVERVIEW_CACHE_SEC:
                raw = hit[1]
            else:
                raw = self._read_overview(repo, route)
                self._ocache[repo] = (time.monotonic(), raw, route)
        return overview_of(raw, *self._known(repo))

    def _read_overview(self, repo: str, route: tuple[str, str]) -> dict:
        kind, reason = route
        be = BACKENDS.get(kind)
        if be is None:
            return unreadable(reason)
        try:
            raw = {"ok": True, "counts": be.status_counts(repo), "ready": be.ready(repo)}
        except BdError as e:
            raw = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001  想定外の形の出力でも、そのプロジェクトの見通しだけを読めないことにする
            raw = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        raw["cli"] = kind
        return raw

    def _known(self, repo: str) -> tuple[set, set]:
        """判断待ちの読みのキャッシュ (古くてもよい。CLI は打たない) にある (判断待ち・保留中・情報待ちの id, 回答済みの id)。
        ready の行にラベルが無い CLI でも、判断待ちのリンクと回答済みの除外を効かせるため。キャッシュが無ければ空 (読めなかった
        プロジェクトのキャッシュは、どの一覧も空)。"""
        hit = self._cache.get(repo)
        if hit is None:
            return set(), set()
        d = hit[1]
        return {x["id"] for x in d["human"] + d["snoozed"] + d["needinfo"]}, {x["id"] for x in d["answered"]}

    def writer(self, repo: str):
        """回答・取り消し・保留・保留の解除・情報待ちを書き込む backend。bd と br のどちらで書くか決められないプロジェクトは、CLI を打たずに ReadOnly。"""
        kind, reason = self.route(repo)
        be = BACKENDS.get(kind)
        if be is None:
            raise ReadOnly(f"このプロジェクトは書き込めない (bd と br のどちらで書くか決められない: {reason})")
        return be

    @contextlib.contextmanager
    def _writing(self, repo: str, iid: str):
        """書き込みのロックの下で書き、終わったら (失敗しても) そのチケットの thread の覚え (回答済み・保留・情報待ち) を捨てる。
        断った書き込み (Gone・Stale など) も捨てる: 古い覚えの署名で断られたカードが、次の読みで読み直した署名で出直す。"""
        with self._write_lock(repo):
            try:
                yield
            finally:
                self._threads.drop(repo, iid)

    def answer(self, repo: str, body: dict) -> str:
        """返り値は回答の署名 (asig)。成功の応答に入れ、画面はその回答の取り消しに付けて送る。"""
        iid, key = _body_id(body), body.get("option") or ""
        text = str(body.get("text") or "").strip()
        repo = self.resolve(repo) or repo
        with self._writing(repo, iid):
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
            body_text = head + ("\n\n" + text if text else "") + "\n"
            # 送り直しても壊れない: 同じ本文のコメントが最後に残っていれば書かず、ラベルだけを直す。
            # pending が問いと一緒にコメントを読んでいれば (br)、それで判定する (読み直さない)
            row = next(r for r in rows if r["id"] == iid)
            cs = row.get("thread")
            snoozed = backend.labeled(row, SNOOZE_LABEL)
            asked = backend.labeled(row, NEEDINFO_LABEL)
            if (snoozed or asked) and cs is None:
                cs = _comments(be, repo, iid)          # 保留・情報待ちのラベルを humanq が付けたかを、回答の前のコメントで見る
            # 保留のラベルを外すのは、この回答の前の最後の [humanq] コメントが保留か保留の解除のとき、情報待ちのラベルを外すのは
            # 情報待ちのとき (humanq が付けたラベル) だけ。利用者が自分の意味で付けた snoozed・needs-info は外さない
            # (情報待ちのラベルが残ったまま回答できるのは、エージェントが問いを書き直してラベルを外し忘れたとき)
            drop_label = snoozed and _put_by_humanq(cs, body_text, (SNOOZE_HEAD, UNSNOOZE_HEAD))
            drop_ask = asked and _put_by_humanq(cs, body_text, (NEEDINFO_HEAD,))
            sig = _record(be, repo, iid, body_text, cs=cs)
            # 保留・情報待ちのラベルは付け替えより先に外す: 外し損ねてもチケットは判断待ちに残り (回答のコメントが最後の [humanq]
            # コメントなので保留・情報待ちとは読まない)、画面からの送り直しで、回答を書かずに残りを済ませる
            if drop_label:
                _label(be, repo, iid, "remove", SNOOZE_LABEL)
            if drop_ask:
                _label(be, repo, iid, "remove", NEEDINFO_LABEL)
            _swap(be, repo, iid, add="answered", remove="human")
            return sig

    def undo(self, repo: str, body: dict) -> None:
        iid = _body_id(body)
        seen = body.get("asig")            # 取り消す回答の署名。送らない古い画面は検査しない
        repo = self.resolve(repo) or repo
        with self._writing(repo, iid):
            be = self.writer(repo)
            cur = {r["id"] for r in be.list_answered(repo)}
            if iid not in cur:
                raise Gone("反映待ちに無い (もう反映されたか、取り消し済み)")
            _record(be, repo, iid, UNDO_HEAD + "\n", expect=seen)
            _swap(be, repo, iid, add="human", remove="answered")

    def _pending_row(self, be, repo: str, iid: str) -> tuple[dict, dict, list]:
        """保留・保留の解除・情報待ちが書く前の読み直し: (判断待ちの Row, その問いの shape, コメント (古い順))。判断待ちに無ければ Gone。
        コメントは pending が読んでいればそれ (br)、無ければ comments で読む (読めなければ comment の段階の WriteError)。"""
        rows = be.pending(repo, iid)
        row = next((r for r in rows if r["id"] == iid), None)
        if row is None:
            raise Gone("このチケットは判断待ちに無い (画面を更新してください)")
        cs = row.get("thread")
        if cs is None:
            cs = _comments(be, repo, iid)
        return row, shape(row), cs

    def snooze(self, repo: str, body: dict) -> None:
        """判断待ちを保留する: `[humanq] 保留: <戻る日> に戻る (qsig: <問いの署名>)` (期限なしは `[humanq] 保留: 期限なし
        (qsig: <問いの署名>)`) のコメントと保留のラベル。status と担当は変えない。body の until は判断待ちに戻る日
        (YYYY-MM-DD。明日以降) か null (期限なし)、note は補足 (任意)、qsig は画面が見ていた問いの署名 (違えば Changed)。
        コメントには今の問いの署名を書く (問いが書き換わったら、保留とは読まない)。回答済み (answered。両ラベルで最後が
        取り消しのものを除く) は Gone。同じ本文のコメントが最後にあれば書かない (送り直し・2 回押し)。"""
        iid = _body_id(body)
        until = _body_until(body)
        note = body.get("note", "")
        if not isinstance(note, str):
            raise ValueError("note (補足) は文字列で送ってください")
        note = note.strip()
        repo = self.resolve(repo) or repo
        with self._writing(repo, iid):
            be = self.writer(repo)
            row, cur, cs = self._pending_row(be, repo, iid)
            seen = body.get("qsig")
            if seen is not None and seen != cur["qsig"]:
                raise Changed("問いが書き換わっている (画面を更新して、見直してから送ってください)")
            # 回答済み (answered のラベル) は保留しない。両方のラベルで最後の [humanq] コメントが判断待ちの側のもの (取り消しなど)
            # なら、画面には判断待ちに出ている (load_project と同じ向き)
            if backend.labeled(row, "answered") and not last_of(cs)[0].startswith(PENDING_SIDE):
                raise Gone("回答済みのチケットは保留できない (画面を更新してください)")
            _record(be, repo, iid, snooze_line(until, cur["qsig"]) + ("\n\n" + note if note else "") + "\n", cs=cs)
            _label(be, repo, iid, "add", SNOOZE_LABEL)

    def unsnooze(self, repo: str, body: dict) -> None:
        """保留を解除する: 最後の [humanq] コメントが保留 (戻る日が来たもの・問いが書き換わったものを含む) なら
        `[humanq] 保留の解除` を書き、保留のラベルが付いていれば外す。どちらでもなければ何も書かない (送り直し・ほかで
        解除済み)。問いの署名は照合しない (解除は問いの意味に依らない)。判断待ち (human のラベル) に無ければ Gone。"""
        iid = _body_id(body)
        repo = self.resolve(repo) or repo
        with self._writing(repo, iid):
            be = self.writer(repo)
            row, _, cs = self._pending_row(be, repo, iid)
            if snooze_of(cs) is not None:
                _record(be, repo, iid, UNSNOOZE_HEAD + "\n", cs=cs)
            if backend.labeled(row, SNOOZE_LABEL):
                _label(be, repo, iid, "remove", SNOOZE_LABEL)

    def needinfo(self, repo: str, body: dict) -> None:
        """判断待ちを情報待ちにする (回答に必要な情報が足りないと、エージェントに返す): `[humanq] 情報待ち (qsig: <問いの署名>)` と
        空行の後の足りない情報のコメントと、情報待ちのラベル。human・status・担当は変えない。body の note は足りない情報 (必須。
        空白だけは断る)、qsig は画面が見ていた問いの署名 (違えば Changed)。コメントには今の問いの署名を書く (エージェントが問いを
        書き直したら、情報待ちとは読まない)。回答済み (answered。両ラベルで最後が取り消しなどのものを除く) は Gone。同じ本文の
        コメントが最後にあれば書かない (送り直し・2 回押し)。"""
        iid = _body_id(body)
        note = _body_note(body)
        repo = self.resolve(repo) or repo
        with self._writing(repo, iid):
            be = self.writer(repo)
            row, cur, cs = self._pending_row(be, repo, iid)
            seen = body.get("qsig")
            if seen is not None and seen != cur["qsig"]:
                raise Changed("問いが書き換わっている (画面を更新して、見直してから送ってください)")
            # 回答済み (answered のラベル) は情報待ちにしない。向きは保留と同じ (load_project と同じ)
            if backend.labeled(row, "answered") and not last_of(cs)[0].startswith(PENDING_SIDE):
                raise Gone("回答済みのチケットは情報待ちにできない (画面を更新してください)")
            _record(be, repo, iid, needinfo_line(cur["qsig"]) + "\n\n" + note + "\n", cs=cs)
            _label(be, repo, iid, "add", NEEDINFO_LABEL)


def _body_id(body: dict) -> str:
    iid = body.get("id")
    if not isinstance(iid, str) or not iid:
        raise ValueError("id (チケットの ID) を文字列で送ってください")
    return iid


def _body_until(body: dict) -> str | None:
    """判断待ちに戻る日。key until が必須で、値は YYYY-MM-DD (明日以降。戻る日の当日から判断待ちに出すので、今日は
    保留にならない) か null (期限なし)。"""
    if "until" not in body:
        raise ValueError("until (判断待ちに戻る日。期限なしは null) がない")
    u = body["until"]
    if u is None:
        return None
    if not isinstance(u, str) or not _DATE.fullmatch(u):
        raise ValueError("until は YYYY-MM-DD か null で送ってください")
    try:
        d = date.fromisoformat(u)
    except ValueError:
        raise ValueError(f"until が日付として読めない: {u}") from None
    if d <= today():
        raise ValueError(f"戻る日 ({u}) が今日以前 (戻る日は明日以降)")
    return d.isoformat()


def _body_note(body: dict) -> str:
    """情報待ちの足りない情報。key note が必須で、前後の空白を除いて空でない文字列 (何が足りないかが無いと、エージェントが
    調べられない)。返すのは前後の空白を除いたもの。"""
    note = body.get("note")
    if not isinstance(note, str):
        raise ValueError("note (足りない情報) を文字列で送ってください")
    note = note.strip()
    if not note:
        raise ValueError("note (足りない情報) が空 (何が足りないかを書いてください)")
    return note


def _comments(be, repo: str, iid: str) -> list:
    """書き込みの前の読み直しでコメントを読む。読めなければ comment の段階の WriteError (何も書いていない)。"""
    try:
        return be.comments(repo, iid)
    except BdError as e:
        raise WriteError(str(e), "comment", False) from e


def _resent(cs: list, text: str) -> bool:
    """送り直しか: チケットの最後のコメント (全体) が同じ本文で新しい。本文が文字列でないコメントは、同じ本文とみなさない。"""
    last = cs[-1] if cs and isinstance(cs[-1], dict) else None
    lt = last.get("text") if last else None
    return isinstance(lt, str) and lt.strip() == text.strip() and _recent(last)


def _put_by_humanq(cs: list, text: str, heads: tuple[str, ...]) -> bool:
    """回答 (本文 text) の前の最後の [humanq] コメントが heads のどれかで始まるか (そのラベルを humanq が付けた)。保留のラベルは
    保留か保留の解除、情報待ちのラベルは情報待ちを渡す。送り直しのとき (最後のコメントがこの回答) は、その前の [humanq] コメントを見る。"""
    ms = marked(cs)
    if _resent(cs, text):
        ms = ms[:-1]
    return bool(ms) and ms[-1]["text"].startswith(heads)


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
        if _resent(cs, text):
            return last_of(cs)[1]
        _comment(be, repo, iid, text)
        return asig(len(marked(cs)) + 1, text)
    except BdError as e:
        raise WriteError(str(e), "comment", False) from e


def _label(be, repo: str, iid: str, op: str, name: str) -> None:
    """ラベルを 1 つ付ける (op は add) か外す (remove)。1 回だけ再試行し、2 回とも失敗したら label の段階の WriteError
    (コメントは書き込み済み)。"""
    for attempt in (1, 2):
        try:
            be.label(repo, op, iid, name)
            return
        except BdError as e:
            if attempt == 2:
                raise WriteError(str(e), "label", True) from e


def _swap(be, repo: str, iid: str, add: str, remove: str) -> None:
    """ラベルを付け替える (add を付けてから remove を外す)。各段階は 1 回だけ再試行する。"""
    for op, name in (("add", add), ("remove", remove)):
        _label(be, repo, iid, op, name)


def _comment(be, repo: str, iid: str, text: str) -> None:
    fd, path = tempfile.mkstemp(prefix="humanq-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        be.add_comment(repo, iid, path)
    finally:
        os.unlink(path)


# 書き込みの API のパスと、それを書く Store のメソッドの名前 (失敗の応答の op にも入れる)
WRITE_OPS = {"/api/answer": "answer", "/api/undo": "undo", "/api/snooze": "snooze", "/api/unsnooze": "unsnooze",
             "/api/needinfo": "needinfo"}


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
                # today はサーバーの今日 (保留の戻る日と比べる日)。画面は期間の選択と日付の下限をこれで数える
                self._json(200, {"projects": store.all(), "today": today().isoformat()})
            elif path == "/api/overview":
                # 見通し (読むだけ)。画面が見通しのタブを開いたときだけ読む。/api/items (15 秒ごとのポーリング)・/api/summary
                # (hook) とは別で、どちらも見通しを読まない。project を付けると、そのプロジェクト (代表のパス) だけ
                q = _query(query)
                if "project" not in q:
                    return self._json(200, {"projects": store.overviews()})
                repo = store.resolve(q["project"])
                if repo is None:
                    return self._json(404, {"error": "未登録のプロジェクト"})
                self._json(200, {"projects": [{"path": repo, "name": Path(repo).name, **store.overview(repo)}]})
            elif path == "/api/summary":
                # hook 用。既定はキャッシュを待たずに返す (UserPromptSubmit を遅くしない)。fresh=1 は読み直す
                q = _query(query)
                # hook は自分の登録のパスで問い合わせる。worktree などで別のパスにまとめた組なら、代表のパスで読む
                repo = store.resolve(q.get("project", ""))
                if repo is None:
                    return self._json(404, {"error": "未登録のプロジェクト"})
                d = store.project(repo, fresh=True) if q.get("fresh") == "1" else store.peek(repo)
                if d is None:
                    return self._json(200, {"pending": True})
                # ok:false (読めないプロジェクト) の human は 0 でも「0 件」ではない。hook はこれを見て「読めない」と伝える。
                # cli は hook が案内の文を選ぶため (br のプロジェクトには bd のコマンドを案内しない)。
                # human は保留中・情報待ちを数えない。snoozed は保留中の件数、returned は保留から判断待ちに戻った件数 (戻る日が来た・
                # 問いが書き換わった。human に入っている。判断待ちの snooze が null でないもの)。
                # unread は、保留か情報待ちのラベルが付いているのにコメントを読めておらず判断待ちに出している件数 (human に入っている。
                # サーバーの起動直後の読み残し)。hook は「うち N 件は保留中か情報待ちかを確認中」と伝える。
                # needinfo は情報待ち (エージェントの番) のチケットの ID (human には入っていない)。hook はエージェントに調べさせる
                self._json(200, {"human": len(d["human"]), "answered": [x["id"] for x in d["answered"]],
                                 "snoozed": len(d.get("snoozed") or []),
                                 "returned": sum(1 for x in d["human"] if x.get("snooze")),
                                 "unread": d.get("unread") or 0,
                                 "needinfo": [x["id"] for x in d.get("needinfo") or []],
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
            op = WRITE_OPS.get(self.path)        # 失敗の応答に入れる (画面の文言が分かれる)
            try:
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise ValueError("body")
                # まとめる前の画面が送る一員のパスも、代表のパスにして同じロックの下で書く
                repo = store.resolve(body.get("project"))
                if repo is None:
                    raise ValueError("未登録のプロジェクト")
                fn = getattr(store, op) if op else None
                if fn is None:
                    return self._json(404, {"error": "not found"})
                # 決められないプロジェクトは、Store.writer が CLI を打つ前に ReadOnly で断る (下の readonly の応答)
                try:
                    sig = fn(repo, body)
                finally:
                    store.invalidate(repo)
                # 回答の成功には回答の署名 (asig) を入れる。取り消し・保留・保留の解除・情報待ちの成功は {ok: true} だけ
                self._json(200, {"ok": True} if sig is None else {"ok": True, "asig": sig})
            except ReadOnly as e:
                self._json(400, {"error": str(e), "code": "readonly", "op": op})
            except Gone as e:
                self._json(400, {"error": str(e), "code": "gone", "op": op})
            except Changed as e:
                self._json(409, {"error": str(e), "code": "changed", "op": op})
            except Stale as e:
                self._json(409, {"error": str(e), "code": "stale", "op": op})
            except (ValueError, json.JSONDecodeError, RecursionError) as e:    # RecursionError は body の深い入れ子
                self._json(400, {"error": str(e) or type(e).__name__, "op": op})
            except BdError as e:
                self._json(502, {"error": str(e), "op": op, "stage": getattr(e, "stage", None),
                                 "written": getattr(e, "written", False)})
            except Exception as e:  # noqa: BLE001  応答が無いまま切れるより、500 の JSON を返す
                self._json(500, {"error": f"サーバーの内部エラー: {type(e).__name__}: {e}", "op": op})

    return H


def _unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)


def _query(query: str) -> dict[str, str]:
    """URL の query を {key: 値} にする (値は % を戻す。= の無い項目は捨てる。同じ key は後の値)。"""
    return {k: _unquote(v) for k, v in (p.split("=", 1) for p in query.split("&") if "=" in p)}


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
