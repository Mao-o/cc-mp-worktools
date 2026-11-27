#!/usr/bin/env python3
"""humanq の hook。beads (bd / br) を使うプロジェクト (cwd から上に .beads/ がある) でだけ動き、それ以外は何もしない。

    humanq_hook.py session-start   (SessionStart)
        プロジェクトを登録し、画面のサーバーが動いていなければ切り離して起動する (版が古ければ入れ替える。新しければそのまま使う)。
        判断待ちと、画面で回答済みの件数を Claude に伝える (保留中は判断待ちに数えず、保留中と保留から戻った件数を 1 行で添える)
    humanq_hook.py prompt-submit   (UserPromptSubmit)
        このセッションで前に伝えた後に画面で回答されたものがあれば、Claude に伝える。
        サーバーのキャッシュを見るだけで bd は読まない (応答を遅くしない)

どちらも失敗してもセッションを止めない (exit 0)。サーバーは 1 台を全プロジェクト・全セッションで共有し、
127.0.0.1 のポートを取れたものだけが残る (同時に起動しても 1 台になる)。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import registry  # noqa: E402

PORT = int(os.environ.get("HUMANQ_PORT") or registry.DEFAULT_PORT)
URL = f"http://127.0.0.1:{PORT}/"
README = ROOT / "README.md"


def _get(path: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(URL.rstrip("/") + path, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError, RecursionError):    # RecursionError は深い入れ子の応答 (json の parser の限界)
        return None


def _version_key(v) -> tuple[int, ...] | None:
    """'0.1.5' -> (0, 1, 5)。数字の組として読めなければ None。"""
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return None


def ensure_server(sd: str) -> str:
    """動いている humanq を使う。無い・版が古い・版が読めないなら起動する (動いているのが自分より新しい版ならそのまま使う)。
    返り値は状態の 1 語。

    新しい版を止めない理由: plugin を上げたあとも、上げる前から動いているセッションは旧版の hook で SessionStart を出す。
    それが版の違いだけで新しいサーバーを旧版に入れ替えると、開いている画面の書き込みのトークンが変わって 403 になる。
    (この判断を持たない版の hook には効かない。旧版のセッションが終わるまで、入れ替わりは起こりうる)"""
    want = registry.plugin_version()
    ping = _get("/api/ping", 0.5)
    if ping and ping.get("app") == "humanq":
        have, mine = _version_key(ping.get("version")), _version_key(want)
        if ping.get("version") == want or (have is not None and mine is not None and have > mine):
            return "running"
    if ping and ping.get("app") == "humanq":
        pid = ping.get("pid")
        info = registry.read_server_info(sd)
        if isinstance(pid, int) and info.get("pid") == pid:  # 自分たちが起動したものだけ止める
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(20):
                if _get("/api/ping", 0.2) is None:
                    break
                time.sleep(0.1)
    elif ping is None and _port_taken():
        return "port-busy"
    log = open(os.path.join(sd, "server.log"), "a", encoding="utf-8")
    subprocess.Popen([sys.executable, str(ROOT / "server" / "humanq.py"), "--state-dir", sd, "--port", str(PORT)],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    for _ in range(30):
        p = _get("/api/ping", 0.2)
        if p and p.get("app") == "humanq":
            return "started"
        time.sleep(0.1)
    return "starting"


def _port_taken() -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def summary(root: str, timeout: float, fresh: bool = False) -> dict | None:
    s = _get("/api/summary?project=" + urllib.parse.quote(root, safe="") + ("&fresh=1" if fresh else ""), timeout)
    return None if not s or s.get("pending") else s


def _is_br(s: dict) -> bool:
    """summary がサーバーの br として読んだプロジェクトか。cli を返さない古いサーバーは br を読まないので bd。"""
    return s.get("cli") == registry.BR


# br の回答の拾い方。br comments の生の並びは決まっていない (JSONL から取り込み直すと古い回答が最後に来る) ので、
# (created_at, id) の昇順に並べ直してから最後の [humanq] コメントを読む
BR_PICKUP = ("回答済みは、`br comments <id> --json` を (created_at, id) の昇順に並べ直し、最後の `[humanq] 回答` "
             "(作者 user/humanq。その後に `[humanq] 取り消し` があれば飛ばす) を読んでチケットに反映し、"
             "`br label remove <id> answered` で外す。生の並びの最後を読まない。")


def _count(v) -> int:
    """summary の件数。数でない値 (古いサーバーは key を返さない・真偽値・負の数) は 0。"""
    return v if type(v) is int and v > 0 else 0


def snooze_line(s: dict) -> str | None:
    """保留の件数の 1 行。保留中 (判断待ちの件数に入れていない) と、保留から判断待ちに戻ったもの (戻る日が来た・問いが
    書き換わった。入っている)。どちらも 0 なら None。"""
    n, m = _count(s.get("snoozed")), _count(s.get("returned"))
    parts = []
    if n:
        parts.append(f"ユーザーが保留にした判断待ち {n} 件 (判断待ちの件数に入れていない。回答待ちのままなので、"
                     "問いを取り下げたり、答えを推測して進めたりしない)")
    if m:
        parts.append(f"保留から判断待ちに戻ったもの {m} 件 (戻る日が来たか、保留の後に問いが書き換わった。判断待ちの件数に入っている)")
    return "humanq: " + "。".join(parts) + "。" if parts else None


def _seen_path(sd: str, session_id: str) -> str:
    d = os.path.join(sd, "sessions")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80] or "unknown"
    return os.path.join(d, safe + ".json")


def _remember(sd: str, session_id: str, answered: list[str]) -> list[str]:
    """前に伝えた回答済みの集合と比べて、新しいものを返し、今の集合を覚える。"""
    path = _seen_path(sd, session_id)
    try:
        with open(path, encoding="utf-8") as f:
            seen = set(json.load(f))
    except (OSError, ValueError, RecursionError):   # RecursionError は深い入れ子の記憶のファイル。壊れたファイルと同じに空として読む
        seen = set()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(answered), f)
    return [a for a in answered if a not in seen]


def _out(event: str, context: str, system: str | None = None) -> None:
    o: dict = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    if system:
        o["systemMessage"] = system
    print(json.dumps(o, ensure_ascii=False))


def session_start(inp: dict) -> None:
    root = registry.find_bd_root(inp.get("cwd") or os.getcwd())
    if not root:
        return
    sd = registry.state_dir()
    raw = os.environ.get(registry.CLI_ENV)
    cli, valid = registry.cli_setting(raw)
    warn = [] if valid else [f"humanq: {registry.CLI_ENV}={raw!r} は bd / br / auto のどれでもないので auto として扱う。"]
    registry.register(sd, root, cli)
    state = ensure_server(sd)
    s = summary(root, 8.0, fresh=True) if state in ("running", "started") else None
    if s is None:
        _out("SessionStart", "\n".join([f"humanq: 判断待ちの画面 ({URL}) を使えない (サーバー: {state})。"
                                        f"原因は {os.path.join(sd, 'server.log')} にある", *warn]))
        return
    if s.get("ok") is False:
        # 読めなかったときの human は 0 でも「0 件」ではない。件数を言わずに読めないと伝える
        _out("SessionStart", "\n".join([f"humanq: このプロジェクトの判断待ちを読めない: {s.get('error')}", *warn]))
        return
    answered = s.get("answered") or []
    _remember(sd, inp.get("session_id") or "unknown", answered)
    ids = f" ({', '.join(answered)})" if answered else ""
    if _is_br(s) and s.get("writable"):
        # br のプロジェクト。br のコマンドで案内する (bd のコマンドは出さない)
        lines = [f"humanq: このプロジェクト (br) の判断待ちは {URL} の画面にまとまっていて、ユーザーは手が空いたときにそこで答える。"
                 f"今は判断待ち {s.get('human', 0)} 件・answered ラベルの付いたもの {len(answered)} 件{ids}。",
                 "ユーザーが同席していない作業 (自律ループ・長い作業の途中) で方針判断が要るときは、br のチケットに human ラベルと "
                 "agent_context の top-level key humanq (問い・背景・選択肢・推奨) を書いておけば画面に出る。"
                 "`br show <id> --json` で今の agent_context を読み、ほかの key を残したまま humanq を足した JSON をファイルに書き、"
                 "`br update <id> --agent-context @<file>` で入れる (値を丸ごと置き換える。短くなる書き換えは --force が要る)。"
                 f"書き方と回答の拾い方は {README} にある。同席している会話では、今までどおりその場で聞く。"]
        if answered:
            lines.append(BR_PICKUP)
    elif _is_br(s):
        # br を読みだけで返す古いサーバー (writable:false) が動いている。件数だけを伝える
        lines = [f"humanq: このプロジェクト (br) の判断待ちは {URL} の画面に出る (動いている画面のサーバーは br のプロジェクトを"
                 f"読み取りだけで扱い、画面からは回答できない)。今は判断待ち {s.get('human', 0)} 件・answered ラベルの付いたもの "
                 f"{len(answered)} 件{ids}。"]
    else:
        lines = [
            f"humanq: このプロジェクト (bd) の判断待ちは {URL} の画面にまとまっていて、ユーザーは手が空いたときにそこで答える。"
            f"今は判断待ち {s.get('human', 0)} 件・画面で回答済みで未反映 {len(answered)} 件{ids}。",
            "ユーザーが同席していない作業 (自律ループ・長い作業の途中) で方針判断が要るときは、bd のチケットに human ラベルと "
            "metadata の humanq (問い・背景・選択肢・推奨) を書いておけば画面に出る。書き方と回答の拾い方は "
            f"{README} にある。同席している会話では、今までどおりその場で聞く。",
        ]
        if answered:
            lines.append("回答済みは、`bd comments <id>` の最後の `[humanq] 回答` (作者 user/humanq) を読んでチケットに反映し、"
                         "`bd label remove <id> answered` で外す。")
    sn = snooze_line(s)
    if sn:
        lines.append(sn)
    msg = (f"humanq: 判断待ち {s.get('human', 0)} 件 / 回答済み {len(answered)} 件 → {URL}"
           if s.get("human") or answered else None)
    _out("SessionStart", "\n".join([*lines, *warn]), msg)


def prompt_submit(inp: dict) -> None:
    root = registry.find_bd_root(inp.get("cwd") or os.getcwd())
    if not root:
        return
    s = summary(root, 1.5)
    if not s or s.get("ok") is False:     # 読めなかったときの answered は [] でも「無い」ではない。覚えた集合を消さない
        return
    new = _remember(registry.state_dir(), inp.get("session_id") or "unknown", s.get("answered") or [])
    if new and _is_br(s):
        _out("UserPromptSubmit", f"humanq: 前に伝えた後で、answered ラベルの付いたチケットが {len(new)} 件増えた: {', '.join(new)}。"
                                 + BR_PICKUP)
    elif new:
        _out("UserPromptSubmit",
             f"humanq: 前に伝えた後で、ユーザーが判断待ちの画面で {len(new)} 件に回答した: {', '.join(new)}。"
             f"`bd comments <id>` の最後の `[humanq] 回答` を読み、今の作業に関わるなら反映する (手順は {README})。")


def main() -> None:
    try:
        inp = json.load(sys.stdin)
    except (ValueError, OSError, RecursionError):   # RecursionError は深い入れ子の stdin。読めない入力と同じに {} で進める
        inp = {}
    try:
        {"session-start": session_start, "prompt-submit": prompt_submit}[sys.argv[1]](inp)
    except Exception as e:  # noqa: BLE001  hook はセッションを止めない
        print(f"humanq hook: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
