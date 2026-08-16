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

exit 0 (JSON なし): Stop を妨げない
exit 0 + {"decision": "block", "reason": ...}: Claude にレビュー結果を返し追加対応を促す
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import cursor
import gitscan
import state
import stategc

MAX_DIFF_BYTES = 40000

# 1 回のレビューで diff を取るパス数の上限。溢れた分は捨てずに pending へ戻し、
# 次の Stop でレビューする (silent truncation にしない)。
MAX_REVIEW_PATHS = 60

# パス単位 diff 収集の時間予算。Stop の hook timeout 660s のうち cursor が最大 600s を
# 使うため、git に回せるのは約 60s。他の git 呼び出し (rev-parse 2 + ls-files 10 +
# rev-parse 2) を引いた残りに収まるよう決めている。
COLLECT_BUDGET_SEC = 30

_EDIT_TOOLS = ("Write", "Edit", "NotebookEdit")


def log(msg: str) -> None:
    print(f"[post-implementation-review] {msg}", file=sys.stderr)


def review_enabled() -> bool:
    """`EXTERNAL_AI_POST_REVIEW=0` で無効化。

    v0.2.0 の `EXTERNAL_AI_POST_REVIEW_MAX` はレビュー回数の予算だったが、ターン
    スコープ化で意味を失ったため撤廃した。ただし `=0` を「hook の無効化スイッチ」
    として使っている既存環境があるので、その用法だけは互換のため生かしている
    (0 以外の数値は無視 = 回数制限は掛からない)。
    """
    raw = os.environ.get("EXTERNAL_AI_POST_REVIEW", "").strip().lower()
    if raw:
        return raw not in ("0", "false", "off", "no")
    return os.environ.get("EXTERNAL_AI_POST_REVIEW_MAX", "").strip() != "0"


def bash_tracking_enabled() -> bool:
    raw = os.environ.get("EXTERNAL_AI_POST_REVIEW_BASH_TRACKING", "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def diff_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def is_clean_review(text: str) -> bool:
    """REVIEW_CLEAN sentinel が単独で返されているときのみ True。

    LLM が REVIEW_CLEAN + 後続指摘を混在させた出力を clean 扱いして critical feedback を
    silently drop することを避けるため、「非空行が 1 行のみで、その行が REVIEW_CLEAN」を
    厳密に要求する。
    """
    stripped = text.strip()
    if not stripped:
        return True
    non_empty_lines = [line for line in stripped.split("\n") if line.strip()]
    if len(non_empty_lines) != 1:
        return False
    only_line = non_empty_lines[0].strip().strip("`*#").strip()
    return only_line.upper() == "REVIEW_CLEAN"


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

    # cursor lock を先に取る。取れなければ claim もしないので pending は温存される。
    # state lock は claim_pending() の内側で完結し、cursor 実行中は保持しない。
    with state.cursor_lock(root) as acquired:
        if not acquired:
            log("同一作業ツリーで別セッションがレビュー中のため skip (pending は温存)")
            return
        _review_claim(payload, session_id, root)


def _review_claim(payload: dict, session_id: str, root: str) -> None:
    claim = state.claim_pending(session_id)
    if claim is None:
        log("このセッションが変更したファイルが無いため skip")
        return
    claim_id, claimed = claim

    rels, overflow = _resolve_paths(root, claimed)
    if overflow:
        # 溢れた分は捨てずに pending へ戻す (次の Stop でレビューされる)
        log(f"{len(overflow)} パスを次回に繰り越し (1 回あたり {MAX_REVIEW_PATHS} 件上限)")
        state.record_pending(session_id, overflow)

    sections, submitted, hashes, deferred = _collect_diffs(
        root, rels, state.reviewed_hashes(session_id)
    )
    if deferred:
        state.record_pending(session_id, deferred)
    if not sections:
        log("レビュー対象の差分が無い (空 diff / 前回と同一) ため skip")
        state.complete_claim(session_id, claim_id, {})
        return

    diff_text = _truncate("\n".join(sections))
    log(f"Cursor によるレビューを実行 ({len(submitted)} ファイル, {len(diff_text)} chars)")
    result = cursor.review(diff_text)

    if not result:
        log("Cursor レビュー失敗 (fail-open、pending に戻す)")
        state.restore_claim(session_id, claim_id, submitted)
        return

    if is_clean_review(result):
        log("Cursor: REVIEW_CLEAN (block しない、レビュー済みとして確定)")
        state.complete_claim(session_id, claim_id, hashes)
        return

    reason = build_reason(result)
    state.complete_claim(session_id, claim_id, hashes)
    _save_review_copy(session_id, reason)
    json.dump({"decision": "block", "reason": reason}, sys.stdout, ensure_ascii=False)


def _resolve_paths(root: str, claimed: list[str]) -> tuple[list[str], list[str]]:
    """claim したパスを作業ツリー相対に正規化し、上限超過分を絶対パスで切り出す。

    作業ツリー外の絶対パスはここで落ちる (復元もしない — 残すと毎 Stop 走査され続ける)。
    """
    rels: list[str] = []
    for path in claimed:
        rel = gitscan.to_relative(root, path)
        if rel and rel not in rels:
            rels.append(rel)
    rels.sort()
    overflow = [os.path.join(root, r) for r in rels[MAX_REVIEW_PATHS:]]
    return rels[:MAX_REVIEW_PATHS], overflow


def _collect_diffs(
    root: str, rels: list[str], reviewed: dict[str, str]
) -> tuple[list[str], list[str], dict[str, str], list[str]]:
    """パスごとに diff を取り、(sections, submitted_abs, hashes, deferred_abs) を返す。

    前回レビュー時と同一 hash のパスは載せない。差分が空のパス (commit 済み・revert
    済み) も載せない。どちらも submitted に入らないので、cursor 失敗時にも復元されず
    そのまま消える。

    COLLECT_BUDGET_SEC を超えた時点で打ち切り、未処理パスを deferred として返す。
    Stop 全体の hook timeout (660s) のうち cursor が最大 600s を使うため、git に
    使える時間は約 60s しかない。1 パス 5s × 60 パスでは足が出るので、経過時間で
    頭を押さえる。deferred は捨てずに pending へ戻す。
    """
    untracked = gitscan.untracked_among(root, rels)
    has_head = gitscan.head_exists(root)

    sections: list[str] = []
    submitted: list[str] = []
    hashes: dict[str, str] = {}
    deadline = time.monotonic() + COLLECT_BUDGET_SEC

    for index, rel in enumerate(rels):
        if time.monotonic() > deadline:
            deferred = [os.path.join(root, r) for r in rels[index:]]
            log(f"git diff の時間予算超過。{len(deferred)} パスを次回に繰り越し")
            return sections, submitted, hashes, deferred

        text = gitscan.path_diff(root, rel, rel in untracked, has_head)
        if not text.strip():
            continue
        abs_path = os.path.join(root, rel)
        digest = diff_hash(text)
        if reviewed.get(abs_path) == digest:
            continue
        sections.append(text)
        submitted.append(abs_path)
        hashes[abs_path] = digest
    return sections, submitted, hashes, []


def _truncate(diff_text: str) -> str:
    encoded = diff_text.encode()
    if len(encoded) <= MAX_DIFF_BYTES:
        return diff_text
    truncated = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    return truncated + "\n\n... (diff truncated for review)\n"


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
        print(f"[post-implementation-review] fatal: {e}", file=sys.stderr)
