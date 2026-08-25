#!/usr/bin/env python3
"""実装直後の差分を Cursor でレビューし、指摘があれば Claude に差し戻す hook 群。

**レビュー対象は「前回 Stop がレビュー対象として消費した時点以降に、このセッションが
変更したファイル」だけ**。作業ツリー全体の `git diff HEAD` は使わない。同一ディレクトリで
複数セッションが動くと、一行も編集していないセッションが隣のセッションの編集を
5〜10 分かけてレビューしてしまうため。

3 つの phase を 1 エントリポイントで捌く (hooks.json から `--phase` で振り分け):

| phase | hook | 役割 |
|---|---|---|
| `pre-tool` | PreToolUse(Bash) | Bash 実行前の `git status` スナップショットを保存 |
| `post-tool` | PostToolUse(Write/Edit/NotebookEdit, Bash) | 変更パスを pending に積む |
| `stop` | Stop | pending を claim してレビュー、結果を配信 |

Bash にも張るのは、`sed -i` / フォーマッタ / スクリプト生成による変更を
Write/Edit だけ見ていると取りこぼすため。実行前後の `git status` を突き合わせれば
Bash 経由の変更も「どのセッションがやったか」付きで拾える。

ターン境界を UserPromptSubmit ではなく「前回 Stop の消費時点」で定義する理由、
in-flight 予約と TTL 回収の設計は state.py の docstring を参照。
外部に送らないファイルの判定 (既定除外 glob / 追加 glob / CODE_ONLY) は exclusion.py。

## 0.6.0 で入れた「頻度と待ち時間」の制御

Stop は編集のあった全ターンで発火し、最大 `cursor.timeout_sec()` 秒ブロックする。
0.5.0 は利用者向けの出力が一切無く (stderr は debug log 止まり)、最大 11 分の無言に
なっていた。次で調整・可視化する:

| 環境変数 | 既定 | 効果 |
|---|---|---|
| `EXTERNAL_AI_POST_REVIEW` | `1` | この hook 自体の on/off |
| `EXTERNAL_AI_POST_REVIEW_TIMEOUT` | `300` | cursor の timeout (上限 600) |
| `EXTERNAL_AI_POST_REVIEW_MIN_LINES` | `0` | 変更行数がこれ未満のターンは見送り |
| `EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC` | `0` | 前回レビュー完了から N 秒は見送り |

見送り (`MIN_LINES` / `COOLDOWN_SEC`) では **pending を消費しない**ので、貯まった
変更は次に走るレビューへまとめて載る。所要時間と結果は `systemMessage` に出す。

exit 0 (JSON なし): Stop を妨げない
exit 0 + {"systemMessage": ...}: 完了要約 / 除外・繰り越し・見送りの通知 (Stop を妨げない)
exit 0 + {"decision": "block", "reason": ..., "systemMessage": ...}: レビュー結果を返す
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

# hooks/_common を解決するため、hook 内モジュールより先に hooks/ を sys.path に載せる
# (plugin root 内の相対配置なので ${CLAUDE_PLUGIN_ROOT} が cache コピーでも壊れない)。
_HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _common import hooklog, notify, sentinel, settings  # noqa: E402

import cursor  # noqa: E402
import exclusion  # noqa: E402
import gitscan  # noqa: E402
import state  # noqa: E402
import stategc  # noqa: E402

# 1 回のレビューで cursor に渡す diff 合計の上限。ファイル (セクション) 単位で積み上げ、
# 収まらないファイルは **送らずに pending へ戻す** (hash も記録しない)。結合後に末尾を切る
# 方式だと、切り落とされたファイルが「レビュー済み」扱いになり以後再掲されなかった (0.4.1 まで)。
MAX_DIFF_BYTES = 40000

# 1 ファイルの diff 上限。これを超えるファイルは先頭だけを `(truncated)` 付きで送り、
# **この場合だけ** hash を記録する (先頭は見ているので、変わらない限り再掲しない)。
# MAX_DIFF_BYTES 以下でなければならない (先頭のファイルが必ず収まる = 永久繰り越しが無い)。
# 合計の 80% にしているのは、切り詰めは「その diff の末尾を二度と見ない」恒久的な損失で、
# 繰り越しは「次ターンまで待つ」だけの遅延なので、1 ファイルはなるべく丸ごと送るため。
MAX_FILE_DIFF_BYTES = 32000

# 1 回のレビューで diff を取るパス数の上限。溢れた分は捨てずに pending へ戻し、
# 次の Stop でレビューする (silent truncation にしない)。
MAX_REVIEW_PATHS = 60

# パス単位 diff 収集の時間予算。Stop の hook timeout 690s のうち cursor が上限 600s
# (`cursor.MAX_TIMEOUT_SEC`。既定は 300s だが env で伸ばせるので上限で見る) +
# kill 猶予 15s (3 × KILL_GRACE_SEC) を使うため、git に回せるのは約 75s。他の git 呼び出し
# (rev-parse 2 × 2 + ls-files 10 × 2 [symlink_map と untracked_among] + 予算判定後に走る
# 最後の 1 パスの path_diff 5) を引いた残りに収まるよう決めている
# (式は tests/test_review_set.py::TestTimeoutBudgets で固定。合計 59s)。
COLLECT_BUDGET_SEC = 30

# systemMessage / stderr に列挙するファイル名の上限 (それ以上は件数だけ)
MAX_LISTED_NAMES = 10

_EDIT_TOOLS = ("Write", "Edit", "NotebookEdit")

ENV_ENABLED = "EXTERNAL_AI_POST_REVIEW"
ENV_LEGACY_MAX = "EXTERNAL_AI_POST_REVIEW_MAX"
ENV_BASH_TRACKING = "EXTERNAL_AI_POST_REVIEW_BASH_TRACKING"
ENV_MIN_LINES = "EXTERNAL_AI_POST_REVIEW_MIN_LINES"
ENV_COOLDOWN = "EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC"


log = hooklog.make_logger("post-implementation-review")

# フェンス / 装飾 / 「指摘なし」の前置き 1 文を許容する判定 (規則は _common/sentinel.py)
is_clean_review = sentinel.is_clean_review


def review_enabled() -> bool:
    """`EXTERNAL_AI_POST_REVIEW=0` で無効化。

    v0.2.0 の `EXTERNAL_AI_POST_REVIEW_MAX` はレビュー回数の予算だったが、ターン
    スコープ化で意味を失ったため撤廃した。ただし `=0` を「hook の無効化スイッチ」
    として使っている既存環境があるので、その用法だけは互換のため生かしている
    (0 以外の数値は無視 = 回数制限は掛からない)。**撤廃済みの死んだ別名**なので、
    新しい変数が設定されていればそちらが勝つ。exitplan-review の
    `EXTERNAL_AI_REVIEW_MAX` は現役の回数予算なので AND で効き、扱いが違う。
    """
    if settings.raw(ENV_ENABLED):
        return settings.flag(ENV_ENABLED, default=True)
    return settings.raw(ENV_LEGACY_MAX) != "0"


def bash_tracking_enabled() -> bool:
    return settings.flag(ENV_BASH_TRACKING, default=True)


def diff_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_reason(cursor_output: str) -> str:
    return (
        "## 実装直後レビュー結果 (Cursor, 差分レビュー)\n\n"
        + cursor_output
        + "\n\n---\n\n"
        "critical な指摘があれば対応し、軽微・妥当でないと判断した指摘は"
        "理由を添えてスキップした上で作業を完了してください。"
    )


# --------------------------------------------------------------------------
# PreToolUse(Bash) / PostToolUse
# --------------------------------------------------------------------------


def handle_pre_tool(payload: dict) -> None:
    if payload.get("tool_name") != "Bash" or not bash_tracking_enabled():
        return
    session_id = payload.get("session_id") or ""
    tool_use_id = payload.get("tool_use_id") or ""
    if not session_id or not tool_use_id:
        return
    root = gitscan.worktree_root(payload.get("cwd") or os.getcwd())
    if not root:
        return
    state.save_bash_snapshot(session_id, tool_use_id, gitscan.status_snapshot(root))


def handle_post_tool(payload: dict) -> None:
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    tool_name = payload.get("tool_name") or ""
    cwd = payload.get("cwd") or os.getcwd()

    if tool_name in _EDIT_TOOLS:
        paths = _edited_paths(payload.get("tool_input") or {}, cwd)
        if paths:
            state.record_pending(session_id, paths)
        return

    if tool_name == "Bash":
        _record_bash_changes(payload, session_id, cwd)


def _edited_paths(tool_input: dict, cwd: str) -> list[str]:
    """編集系ツールの入力から対象パスを取り出す。

    Write / Edit は `file_path` に絶対パスが安定して入る (CLI 2.1.233 実測)。
    NotebookEdit は現環境に非搭載だが、搭載環境で `notebook_path` を使う可能性が
    あるため両方見る。MultiEdit は現環境に存在しないので matcher からも外している。
    """
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return []
    return [raw if os.path.isabs(raw) else os.path.join(cwd, raw)]


def _record_bash_changes(payload: dict, session_id: str, cwd: str) -> None:
    """Bash 実行前後の status スナップショット差分を pending に積む。"""
    if not bash_tracking_enabled():
        return
    tool_use_id = payload.get("tool_use_id") or ""
    if not tool_use_id:
        return
    pre = state.pop_bash_snapshot(session_id, tool_use_id)
    if pre is None:
        return  # PreToolUse が動かなかった (git 外・timeout) → 属性付けを諦める
    root = gitscan.worktree_root(cwd)
    if not root:
        return
    changed = gitscan.changed_between(pre, gitscan.status_snapshot(root))
    if changed:
        state.record_pending(session_id, [os.path.join(root, rel) for rel in changed])


# --------------------------------------------------------------------------
# Stop
# --------------------------------------------------------------------------


def handle_stop(payload: dict) -> None:
    if payload.get("stop_hook_active"):
        log("stop_hook_active=True によりスキップ (再帰防止)")
        return

    session_id = payload.get("session_id") or ""
    if not session_id:
        log("session_id が空")
        return

    stategc.gc_stale()

    if not review_enabled():
        log("EXTERNAL_AI_POST_REVIEW=0 によりレビュー無効化")
        return
    if not cursor.is_available():
        log("cursor 未インストール")
        return

    cwd = payload.get("cwd") or os.getcwd()
    root = gitscan.worktree_root(cwd)
    if not root:
        log("git worktree 外のため skip")
        return

    # cooldown は claim の**前**に見る (claim すると pending を消費してしまう)。
    # cursor lock も取らない — ロックを取らずに済むなら他セッションを待たせない。
    cooled = _cooldown_notice(session_id)
    if cooled:
        log(cooled)
        json.dump(_with_notices({}, [cooled]), sys.stdout, ensure_ascii=False)
        return

    # cursor lock を先に取る。取れなければ claim もしないので pending は温存される。
    # state lock は claim_pending() の内側で完結し、cursor 実行中は保持しない。
    with state.cursor_lock(root) as acquired:
        if not acquired:
            log("同一作業ツリーで別セッションがレビュー中のため skip (pending は温存)")
            return
        output = _review_claim(payload, session_id, root)

    if output:
        json.dump(output, sys.stdout, ensure_ascii=False)


def _cooldown_notice(session_id: str) -> str | None:
    """cooldown 中なら利用者向けの一文を返す (そうでなければ None)。

    `EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC` は「前回レビュー完了から N 秒未満なら
    今回は走らせない」。**pending は消費しない**ので、貯まった変更は cooldown 明けの
    Stop でまとめて 1 回のレビューに載る。

    pending が空のターンでは黙る。編集していないターンまで毎回通知すると、
    通知そのものがノイズになって読まれなくなる。

    **副作用として、pending が空で in-flight だけが TTL 超過している場合は cooldown を
    素通りして `claim_pending()` の回収経路に入る**。cooldown (`> IN_FLIGHT_TTL_SEC` の
    設定時のみ起きる) より「kill されたレビューを取りこぼさない」ほうを優先する。
    """
    cooldown = settings.count(ENV_COOLDOWN, 0)
    if cooldown <= 0:
        return None
    remaining = cooldown - (time.time() - state.last_review_at(session_id))
    if remaining <= 0:
        return None
    waiting = state.pending_count(session_id)
    if not waiting:
        return None
    return (
        f"前回レビューから {cooldown} 秒 ({ENV_COOLDOWN}) 未満のためレビューを見送り: "
        f"{waiting} ファイルは残り約 {int(remaining)} 秒後のターンでまとめてレビューします"
    )


def _review_claim(payload: dict, session_id: str, root: str) -> dict:
    """claim を取り、**何が起きても握りっぱなしにしない**ことを保証する薄い外枠。

    claim を取った後で例外が出ると、in-flight にエントリが残ったまま hook が死ぬ。
    復元されるのは TTL (`IN_FLIGHT_TTL_SEC` = 900s) 超過後の Stop なので、その間
    このセッションの変更は pending へ戻らず**レビューが 15 分沈黙する**。呼び出し元の
    fail-open (`__main__` の `except Exception`) はプロセスを守るだけで状態は戻さない。

    実際の経路: `EXTERNAL_AI_POST_REVIEW_TIMEOUT` に非有限値が入ると
    `Popen.communicate(timeout=nan)` が `ValueError` を投げる (この穴は
    `_common/settings.py` 側でも塞いだが、**例外の出どころを 1 つ塞ぐより
    「claim は必ず戻る」を構造で保証するほうが強い**)。

    復元は `claimed` 全件に対して行う (レビュー済みのものが混ざっても、hash 一致で
    次回の `_collect_diffs` が落とすので二重レビューにはならない)。除外済みパスが
    戻っても、除外は claim のたびに再適用されるので外部に送られることはない。
    """
    claim = state.claim_pending(session_id)
    if claim is None:
        log("このセッションが変更したファイルが無いため skip")
        return {}
    claim_id, claimed = claim
    try:
        return _run_review(session_id, root, claim_id, claimed)
    except Exception as e:
        log(f"レビュー中に例外 (claim を pending へ戻す): {e}")
        state.restore_claim(session_id, claim_id, claimed)
        return _with_notices(
            {},
            [
                f"レビューを完了できませんでした ({type(e).__name__})。"
                f"{len(claimed)} ファイルは次のレビューに持ち越します"
            ],
        )


def _run_review(session_id: str, root: str, claim_id: str, claimed: list[str]) -> dict:
    """除外 → diff 収集 → cursor → 状態確定。stdout に出す JSON (無ければ {}) を返す。

    利用者向けの通知 (除外・繰り越し・切り詰め) は `systemMessage` にまとめる (block 時は
    `decision` / `reason` と同居させる。公式 docs の共通フィールドで Stop でも有効)。
    systemMessage が表示されない環境でも stderr に同じ内容を残し、除外そのものは通知の
    配信に依存しない。

    ここから送出された例外は `_review_claim` が拾って claim を復元する。
    """
    notices: list[str] = []

    rels, overflow, excluded = _resolve_paths(root, claimed, exclusion.load_policy())
    if excluded:
        # 除外は恒久: pending にも reviewed にも残さない。ファイル名は出すが内容は出さない
        notices.append(
            f"{len(excluded)} ファイルを外部 AI レビューから除外 (内容は送信していません): "
            + _list_names(f"{name} ({reason})" for name, reason in excluded)
        )
    if overflow:
        notices.append(
            f"{len(overflow)} ファイルは 1 回あたり {MAX_REVIEW_PATHS} 件の上限により"
            "次ターンに繰り越し: " + _list_names(_rel_names(root, overflow))
        )

    batch = _collect_diffs(root, rels, state.reviewed_hashes(session_id))
    # 繰り越しは捨てずに pending へ戻す (次の Stop でレビューされる)。claim 順を保って 1 回で
    # 積む: 予算超過 (rels の途中) → 時間切れ (rels の末尾) → 上限超過 (rels の外) の順
    carried = batch.deferred + overflow
    if carried:
        state.record_pending(session_id, carried)
    if batch.deferred_time:
        notices.append(
            f"{len(batch.deferred_time)} ファイルは git diff の時間予算超過により"
            "次ターンに繰り越し: " + _list_names(_rel_names(root, batch.deferred_time))
        )
    if batch.deferred_size:
        notices.append(
            f"{len(batch.deferred_size)} ファイルは diff 合計 {MAX_DIFF_BYTES // 1000} KB の"
            "予算に収まらないため次ターンに繰り越し (レビュー済みにはしません): "
            + _list_names(_rel_names(root, batch.deferred_size))
        )
    if batch.truncated:
        notices.append(
            f"{len(batch.truncated)} ファイルは diff が {MAX_FILE_DIFF_BYTES // 1000} KB を"
            "超えるため先頭のみ送信 (truncated): "
            + _list_names(f"{rel} ({size} bytes)" for rel, size in batch.truncated)
        )
    for notice in notices:
        log(notice)

    if not batch.sections:
        log("レビュー対象の差分が無い (空 diff / 前回と同一 / 除外のみ) ため skip")
        state.complete_claim(session_id, claim_id, {})
        return _with_notices({}, notices)

    # しきい値は「実際に送る diff」で測る (除外・繰り越し後の量が課金に対応するため)
    min_lines = settings.count(ENV_MIN_LINES, 0)
    changed_lines = _count_changed_lines(batch.sections)
    if min_lines > 0 and changed_lines < min_lines:
        # 消費せず pending に戻す (cursor 失敗時と同じ経路。hash も記録しない)
        state.restore_claim(session_id, claim_id, batch.submitted)
        notices.insert(
            0,
            f"変更 {changed_lines} 行が {ENV_MIN_LINES}={min_lines} に満たないため"
            f"レビューを見送り: {len(batch.submitted)} ファイルは次のレビューにまとめます",
        )
        log(notices[0])
        return _with_notices({}, notices)

    diff_text = "\n".join(batch.sections)
    log(f"Cursor によるレビューを実行 ({len(batch.submitted)} ファイル, {len(diff_text)} chars)")
    started = time.monotonic()
    result = cursor.review(diff_text)
    elapsed = time.monotonic() - started
    state.mark_review_done(session_id)
    summary = f"差分レビュー完了 ({notify.format_elapsed(elapsed)}, {len(batch.submitted)} ファイル)"

    if not result:
        log("Cursor レビュー失敗 (fail-open、pending に戻す)")
        state.restore_claim(session_id, claim_id, batch.submitted)
        notices.insert(0, f"{summary} → 結果を取得できず (timeout / 失敗)。次ターンに持ち越し")
        return _with_notices({}, notices)

    if is_clean_review(result):
        log("Cursor: REVIEW_CLEAN (block しない、レビュー済みとして確定)")
        state.complete_claim(session_id, claim_id, batch.hashes)
        notices.insert(0, f"{summary} → 指摘なし")
        return _with_notices({}, notices)

    reason = build_reason(result)
    state.complete_claim(session_id, claim_id, batch.hashes)
    _save_review_copy(session_id, reason)
    notices.insert(0, f"{summary} → 指摘あり (Claude に対応を依頼しました)")
    return _with_notices({"decision": "block", "reason": reason}, notices)


def _count_changed_lines(sections: list[str]) -> int:
    """diff の追加・削除行数を数える。

    しきい値の単位を「ファイル数」ではなく行数にしているのは、typo 1 行の修正と
    1 ファイル 300 行の書き換えを区別したいのが本設定の主旨だから。

    **接頭辞でファイルヘッダを判別してはいけない**。`sections` の 1 要素は
    `gitscan.path_diff` が返す 1 ファイル分の diff なので、`--- a/path` /
    `+++ b/path` は必ず最初の `@@` より前に来る。逆に `@@` より後ろでは:

    - `-- コメント` (SQL / Lua / Haskell) を削除した行が `--- コメント`
    - `++ 何か` を追加した行が `+++ 何か`

    になり、`"--- "` / `"+++ "` で弾くと中身の行まで落ちる。「SQL のコメント行
    だけ消したターン」が 0 行と数えられ、`MIN_LINES` に引っかかって実質的な変更が
    黙って skip される (最初の実装は `"---"` / `"+++"`、次が `"--- "` / `"+++ "`。
    どちらも中身の行と区別できていなかった — 接頭辞では原理的に無理)。

    **最初の `@@` 以降だけを数える**のが唯一の正確な方法。hunk ヘッダ自体は `@`
    始まりなので数に入らず、中身がどんな文字列でも誤判定しない。`@@` を含まない
    section (binary 差分など) は 0 行。
    """
    total = 0
    for section in sections:
        in_hunk = False
        for line in section.splitlines():
            if not in_hunk:
                # ヘッダ領域 (`diff --git` / `index` / `---` / `+++`) を読み飛ばす
                in_hunk = line.startswith("@@")
                continue
            if line.startswith(("+", "-")):
                total += 1
    return total


def _with_notices(output: dict, notices: list[str]) -> dict:
    """利用者向け通知を `systemMessage` に載せる (組み立ては両 review hook 共通)。

    書いてよい内容の線引き (要約と件数のみ。レビュー本文・diff は出さない) は
    `_common/notify.py` の docstring を正典とする。
    """
    message = notify.compose("post-implementation-review", notices)
    if message:
        output["systemMessage"] = message
    return output


def _list_names(names) -> str:
    items = list(names)
    shown = ", ".join(items[:MAX_LISTED_NAMES])
    if len(items) > MAX_LISTED_NAMES:
        shown += f", 他 {len(items) - MAX_LISTED_NAMES} 件"
    return shown


def _rel_names(root: str, abs_paths: list[str]) -> list[str]:
    return [gitscan.to_relative(root, p) or p for p in abs_paths]


def _resolve_paths(
    root: str, claimed: list[str], policy: exclusion.Policy
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """claim したパスを作業ツリー相対に正規化し、(rels, overflow_abs, excluded) を返す。

    作業ツリー外の絶対パスはここで落ちる (復元もしない — 残すと毎 Stop 走査され続ける)。
    ディレクトリも落とす: 入れ子の git リポジトリは `git status -uall` でも `dir/` の
    まま出てくるため、v0.3.0 が書いた state に残っている可能性がある。

    除外 (exclusion.Policy) もここで当てる。除外されたパスは作業ツリー外と同じく
    **復元しない・hash も記録しない** (恒久除外)。上限 (MAX_REVIEW_PATHS) の手前で除外する
    ので、除外ファイルが枠を食ったり overflow として pending に戻ったりしない。
    判定には実体 (realpath 相対。git に渡すのもこれ)、**lexical なパス** (root だけ
    realpath で同定し、配下の symlink 構成要素名はそのまま残したもの)、**別名** (repo 内の
    symlink を列挙し、実体がその target 配下なら `link + 残り` を生成) のすべてを渡す —
    `credentials/` → `ordinary/` のような symlink ディレクトリ経由の claim や機密名の
    リンクを、どの名前が機密に見えても外部に送らない (安全側に倒す)。別名が要るのは
    Bash 経由の変更: `git status` は実体名しか返さないので lexical 名が claim に現れない。

    順序は claim 順 (= pending に積まれた順) を保つ。前回 Stop が繰り越した (pending に
    戻した) パスは次ターンの先頭に来るので、予算超過で繰り越されたファイルが新しい編集に
    毎回追い越されて永久に残ることがない。
    """
    root_real = os.path.realpath(root).rstrip(os.sep) or os.sep
    symlinks = gitscan.symlink_map(root)
    # 実体 (realpath 相対) ごとに判定候補を集める。同じ実体が別名 (symlink) で複数回 claim
    # されていても、どの名前が機密に見えるかを全部見てから判定する
    candidates: dict[str, list[str]] = {}
    for path in claimed:
        rel = gitscan.to_relative(root, path)
        if not rel or os.path.isdir(os.path.join(root, rel)):
            continue
        names = candidates.setdefault(rel, [rel])
        lexical = _lexical_relative(root_real, path)
        for name in [lexical, *exclusion.expand_aliases(rel, symlinks)]:
            if name and name not in names:
                names.append(name)

    rels: list[str] = []
    excluded: list[tuple[str, str]] = []
    for rel, names in candidates.items():
        hit = policy.explain(names)
        if hit is not None:
            excluded.append(hit)  # (当たった名前, 理由)
            continue
        rels.append(rel)
    overflow = [os.path.join(root, r) for r in rels[MAX_REVIEW_PATHS:]]
    return rels[:MAX_REVIEW_PATHS], overflow, excluded


def _lexical_relative(root_real: str, path: str) -> str | None:
    """claim されたパスを、root 配下の symlink を解決せずに (lexical に) root 相対へ変換する。

    `to_relative` (全体を realpath) だと `credentials/` → `ordinary/` のような symlink
    ディレクトリ経由の claim が `ordinary/data.json` になり、除外判定から `credentials` が
    消える (Codex PR レビュー P1)。親ディレクトリだけ realpath する方式も同じ穴があった。
    ここでは **root の別名** (`/tmp` → `/private/tmp`、symlink された親ディレクトリ) だけを
    realpath で同定し、その下の構成要素は名前のまま残す。祖先を浅い方から試して最初に root と
    一致したところで切るので、root 配下に root 自身へ戻る symlink があっても途中の名前は残る。
    root の別名が見つからなければ None (作業ツリー外)。
    """
    parts = os.path.normpath(path).split(os.sep)
    for cut in range(1, len(parts)):
        prefix = os.sep.join(parts[:cut]) or os.sep
        if os.path.realpath(prefix) == root_real:
            return os.sep.join(parts[cut:]) or None
    return None


class ReviewBatch:
    """`_collect_diffs` の結果。submitted / hashes は cursor に渡すファイルだけ。

    dataclass にしていないのは、テストが `__main__.py` を `sys.modules` 未登録のまま
    `exec_module` で読むため (`from __future__ import annotations` 下の dataclass は
    モジュール名前空間の解決で落ちる)。
    """

    def __init__(self) -> None:
        self.sections: list[str] = []
        self.submitted: list[str] = []  # 絶対パス
        self.hashes: dict[str, str] = {}
        self.deferred_time: list[str] = []  # 時間予算で未処理 (絶対パス)
        self.deferred_size: list[str] = []  # 合計バイト予算で未送信 (絶対パス)
        self.truncated: list[tuple[str, int]] = []  # (rel, 切り詰め前の bytes)

    @property
    def deferred(self) -> list[str]:
        """pending へ戻す絶対パス (未レビュー)。claim 順 = バイト予算 (途中) → 時間切れ (末尾)。"""
        return self.deferred_size + self.deferred_time


def _collect_diffs(root: str, rels: list[str], reviewed: dict[str, str]) -> ReviewBatch:
    """パスごとに diff を取り、予算に収まるものだけを ReviewBatch に積む。

    前回レビュー時と同一 hash のパスは載せない。差分が空のパス (commit 済み・revert
    済み) も載せない。どちらも submitted に入らないので、cursor 失敗時にも復元されず
    そのまま消える。

    **予算はファイル単位で当てる**:

    - 1 ファイルが MAX_FILE_DIFF_BYTES を超える → 先頭だけを `(truncated)` 付きで送り、
      hash は全文で記録する (変わらない限り再掲しない。変われば切り詰めた形で再掲)
    - 積み上げ合計が MAX_DIFF_BYTES を超えるファイル → 送らず deferred_size へ
      (hash を記録しないので次ターンにそのまま再掲される)。後続の小さいファイルは
      予算が残っていれば送る (first-fit)

    COLLECT_BUDGET_SEC を超えた時点で打ち切り、未処理パスを deferred_time として返す。
    Stop 全体の hook timeout (690s) のうち cursor が上限 600s + kill 猶予 15s を使うため、
    git に使える時間は約 75s しかない。1 パス 5s × 60 パスでは足が出るので、経過時間で
    頭を押さえる。deferred は捨てずに pending へ戻す。
    """
    untracked = gitscan.untracked_among(root, rels)
    has_head = gitscan.head_exists(root)

    batch = ReviewBatch()
    used = 0
    deadline = time.monotonic() + COLLECT_BUDGET_SEC

    for index, rel in enumerate(rels):
        if time.monotonic() > deadline:
            batch.deferred_time = [os.path.join(root, r) for r in rels[index:]]
            break

        text = gitscan.path_diff(root, rel, rel in untracked, has_head)
        if not text.strip():
            continue
        abs_path = os.path.join(root, rel)
        digest = diff_hash(text)  # hash は切り詰め前の全文で取る
        if reviewed.get(abs_path) == digest:
            continue

        full_size = len(text.encode())
        if full_size > MAX_FILE_DIFF_BYTES:
            text = _truncate_section(text, MAX_FILE_DIFF_BYTES)
        size = len(text.encode())
        separator = 1 if batch.sections else 0  # "\n".join の区切り分
        if used + separator + size > MAX_DIFF_BYTES:
            batch.deferred_size.append(abs_path)
            continue

        batch.sections.append(text)
        batch.submitted.append(abs_path)
        batch.hashes[abs_path] = digest
        used += separator + size
        if full_size > MAX_FILE_DIFF_BYTES:
            batch.truncated.append((rel, full_size))
    return batch


_TRUNCATED_MARKER = (
    "\n... (truncated for review: only the first part of this file's diff is shown; "
    "{full} bytes in total)\n"
)


def _truncate_section(text: str, limit: int) -> str:
    """1 ファイル分の diff を marker 込みで limit バイト以下に切り詰める。

    行の途中で切ると diff の hunk が壊れて読みにくいので、可能なら最後の改行で切る
    (ただし半分未満まで戻るほど長い行なら byte 境界で切る)。UTF-8 の途中で切れた
    バイトは捨てる。
    """
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    marker = _TRUNCATED_MARKER.format(full=len(encoded))
    budget = max(limit - len(marker.encode()), 0)
    cut = encoded[:budget]
    newline = cut.rfind(b"\n")
    if newline >= budget // 2:
        cut = cut[:newline]
    return cut.decode("utf-8", errors="ignore") + marker


def _save_review_copy(session_id: str, reason: str) -> None:
    path = state.review_copy_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(reason)
        log(f"レビュー完了 → {path}")
    except OSError:
        log("参照コピーの保存に失敗")


# --------------------------------------------------------------------------


def parse_phase(argv: list[str]) -> str:
    if "--phase" in argv:
        idx = argv.index("--phase")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return "stop"


def main(argv: list[str] | None = None) -> None:
    phase = parse_phase(argv if argv is not None else sys.argv[1:])
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"stdin JSON パース失敗: {e}")
        return
    if not isinstance(payload, dict):
        return

    if phase == "pre-tool":
        handle_pre_tool(payload)
    elif phase == "post-tool":
        handle_post_tool(payload)
    else:
        handle_stop(payload)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:  # hook が例外で Claude Code を止めないよう fail-open
        log(f"fatal: {e}")
