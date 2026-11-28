#!/usr/bin/env python3
"""humanq の hook。beads (bd / br) を使うプロジェクト (cwd から上に .beads/ がある) でだけ動き、それ以外は何もしない。

    humanq_hook.py session-start   (SessionStart)
        プロジェクトを登録し、画面のサーバーが動いていなければ切り離して起動する (版が古ければ入れ替える。新しければそのまま使う)。
        判断待ちと、画面で回答済みの件数を Claude に伝える (保留中は判断待ちに数えず、保留中と保留から戻った件数を 1 行で添える。
        情報待ち (ユーザーが回答に必要な情報が足りないと返したもの) も判断待ちに数えず、エージェントの番として ID と手順を 1 行で添える)
    humanq_hook.py prompt-submit   (UserPromptSubmit)
        このセッションで前に伝えた後に画面で回答されたもの・情報待ちになったものがあれば、Claude に伝える。
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
    書き換わった。入っている)、保留中か情報待ちかを読み残している判断待ち (unread。入っている)。どれも 0 なら None。"""
    n, m, u = _count(s.get("snoozed")), _count(s.get("returned")), _count(s.get("unread"))
    parts = []
    if n:
        parts.append(f"ユーザーが保留にした判断待ち {n} 件 (判断待ちの件数に入れていない。回答待ちのままなので、"
                     "問いを取り下げたり、答えを推測して進めたりしない)")
    if m:
        parts.append(f"保留から判断待ちに戻ったもの {m} 件 (戻る日が来たか、保留の後に問いが書き換わった。判断待ちの件数に入っている)")
    if u:
        # 読み残しの間は、情報待ちの ID も summary の needinfo から抜ける。読み終えた後の UserPromptSubmit が、増えた ID として伝える
        parts.append(f"判断待ちの件数のうち {u} 件は、保留中か情報待ちかを確認中 (サーバーの起動直後の読み残し。保留中・情報待ちのものが"
                     "混じっていて、次の読みで減る。保留中かもしれないチケットに取りかかったり、答えを推測して進めたりしない。"
                     "情報待ち (エージェントの番) と分かったものは、読み終えた後のプロンプトで ID を伝える)")
    return "humanq: " + "。".join(parts) + "。" if parts else None


def needinfo_ids(s: dict) -> list[str] | None:
    """summary の needinfo (情報待ちのチケットの ID の list。判断待ちの件数に入っていない) の、空でない文字列の ID。
    list でなければ (古いサーバーは返さない・形が崩れている) None (情報待ちが無いのか分からない)。"""
    v = s.get("needinfo")
    return [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else None


def _needinfo_cmds(s: dict) -> tuple[str, str]:
    """(足りない情報の読み方, ラベルの外し方)。プロジェクトの CLI のコマンドだけを出す。"""
    if _is_br(s):
        return ("`br comments <id> --json` を (created_at, id) の昇順に並べ直した最後の `[humanq] 情報待ち`",
                "`br label remove <id> needs-info`")
    return "`bd comments <id>` の最後の `[humanq] 情報待ち`", "`bd label remove <id> needs-info`"


def needinfo_line(s: dict) -> str | None:
    """情報待ち (エージェントの番) の 1 行。needinfo_ids が 0 件 (か読めない) なら None。"""
    ids = needinfo_ids(s)
    if not ids:
        return None
    read, drop = _needinfo_cmds(s)
    return (f"humanq: 情報待ち {len(ids)} 件 (エージェントの番): {', '.join(ids)}。ユーザーが判断待ちの画面で、回答に必要な情報が"
            f"足りないと返した問い (判断待ちの件数に入れていない)。{read} (作者 user/humanq) の補足が足りない情報。調べてチケット (本文・"
            f"notes・問いの背景) に足し、問いを書き直してから {drop} で外す (書き直した問いは判断待ちに戻る。問いの無いチケットは問いを書く。本文や notes に足すだけでは問いが変わらず、同じ問いのまま戻って画面は知らせない)。調べても分からないときも、"
            f"分からなかったこと (何を探したか) をコメントと問いの背景に書いて問いを書き直し、ラベルを外す (問いは取り下げない)。"
            f"手順は {README} の「回答を拾う」")


def needinfo_added_line(s: dict, ids: list[str]) -> str:
    """UserPromptSubmit で伝える、前に伝えた後で情報待ちになったもの (ids) の 1 行。読み残しが解けて情報待ちと分かったものも含む。"""
    read, drop = _needinfo_cmds(s)
    return (f"humanq: 前に伝えた後で、情報待ち (エージェントの番) になったものが {len(ids)} 件ある: {', '.join(ids)}。ユーザーが"
            f"回答に必要な情報が足りないと返した問い (判断待ちの件数に入れていない)。{read} (作者 user/humanq) の補足が足りない情報。"
            f"今の作業に関わるなら、調べてチケットに足し、問いを書き直してから {drop} で外す (問いは取り下げない。手順は {README} の"
            f"「回答を拾う」)")


# セッションごとの覚え (sessions/<session>.json は回答済み、sessions/<session>.needinfo.json は情報待ち)。session の ID は
# 英数字と - _ だけに直すので、"." を含む覚えの名前は、別のセッションの回答済みの覚えと重ならない
NEEDINFO_SEEN = ".needinfo"


def _seen_path(sd: str, session_id: str, kind: str = "") -> str:
    d = os.path.join(sd, "sessions")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80] or "unknown"
    return os.path.join(d, safe + kind + ".json")


def _remember(sd: str, session_id: str, now: list[str], kind: str = "", keep: bool = False) -> list[str]:
    """前に伝えた集合と比べて、新しいもの (now の並びのまま) を返し、今の集合を覚える。kind は覚えの種類 ("" は回答済み・
    NEEDINFO_SEEN は情報待ち)。keep なら前に伝えたものも覚えに残す (今の集合が欠けているかもしれない読み: 読み残しの間の情報待ち。
    欠けたものを忘れると、読み終えた後に同じ ID を「増えた」と伝え直す)。"""
    path = _seen_path(sd, session_id, kind)
    try:
        with open(path, encoding="utf-8") as f:
            seen = set(json.load(f))
    except (OSError, ValueError, TypeError, RecursionError):   # RecursionError は深い入れ子、TypeError は数などの記憶のファイル。壊れたファイルと同じに空として読む
        seen = set()
    # 文字列でない要素 (壊れた記憶のファイル) は覚え直さない (sorted が型の混在で落ちる)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(set(now) | ({x for x in seen if isinstance(x, str)} if keep else set())), f)
    return [a for a in now if a not in seen]


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
    session = inp.get("session_id") or "unknown"
    _remember(sd, session, answered)
    ni = needinfo_ids(s)
    if ni is not None:
        # 下の情報待ちの 1 行で全部を伝える。読み残しの間に抜けた ID は覚えないので、読み終えた後の UserPromptSubmit が伝える
        _remember(sd, session, ni, NEEDINFO_SEEN)
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
    ni = needinfo_line(s)
    if ni:
        lines.append(ni)
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
    sd, session = registry.state_dir(), inp.get("session_id") or "unknown"
    new = _remember(sd, session, s.get("answered") or [])
    lines = []
    if new and _is_br(s):
        lines.append(f"humanq: 前に伝えた後で、answered ラベルの付いたチケットが {len(new)} 件増えた: {', '.join(new)}。" + BR_PICKUP)
    elif new:
        lines.append(f"humanq: 前に伝えた後で、ユーザーが判断待ちの画面で {len(new)} 件に回答した: {', '.join(new)}。"
                     f"`bd comments <id>` の最後の `[humanq] 回答` を読み、今の作業に関わるなら反映する (手順は {README})。")
    ni = needinfo_ids(s)
    if ni is not None:                    # needinfo を返さない古いサーバーでは、覚えた集合を消さない
        # 読み残し (unread) の間は情報待ちの ID が抜けうるので、前に伝えたものを忘れない (読み終えた後に伝え直さない)
        added = _remember(sd, session, ni, NEEDINFO_SEEN, keep=_count(s.get("unread")) > 0)
        if added:
            lines.append(needinfo_added_line(s, added))
    if lines:
        _out("UserPromptSubmit", "\n".join(lines))


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
