#!/usr/bin/env python3
"""PreToolUse(ExitPlanMode) hook: Cursor + Codex でプランをクロスレビューし、
セッション内で最大 MAX_REVIEWS 回ブロックする。

- Cursor: 既存コードベース整合観点 (primary)
- Codex: 要件・アーキ観点 (補完)

両方のレビュアーを並列実行し、critical な指摘があった場合のみ Claude に block を返す。
全レビュアーが REVIEW_CLEAN を返した場合、または全失敗の場合は fail-open (exit 0)。

マーカーの read→判定→write は flock で排他ロック (`_common.flock`)。
同一セッション並行起動時のカウント破綻を防ぐ。

exit 0 (JSON なし): ブロックしない (clean / レビュー済み / 両方失敗 / エラー)
exit 0 + JSON stdout: decision:block で差し戻し
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# hooks/_common を解決するため、hook 内モジュールより先に hooks/ を sys.path に載せる
# (plugin root 内の相対配置なので ${CLAUDE_PLUGIN_ROOT} が cache コピーでも壊れない)。
_HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _common import flock, hooklog, sentinel  # noqa: E402

import codex  # noqa: E402
import cursor  # noqa: E402

REVIEWERS = [cursor, codex]

DEFAULT_MAX_REVIEWS = 2
_HEADERS = {
    "cursor": "## Cursor レビュー (既存コードベース整合観点)",
    "codex": "## Codex レビュー (要件・アーキ観点)",
}

log = hooklog.make_logger("exitplan-review")

# フェンス / 装飾 / 「指摘なし」の前置き 1 文を許容する判定 (規則は _common/sentinel.py)
is_clean_review = sentinel.is_clean_review


def get_max_reviews() -> int:
    raw = os.environ.get("EXTERNAL_AI_REVIEW_MAX", "").strip()
    if not raw:
        return DEFAULT_MAX_REVIEWS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_REVIEWS


def plan_hash(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _read_marker(f) -> tuple[str, int]:
    """マーカー本文 (1 行目 hash / 2 行目 count) を読む。壊れていれば空扱い。

    release_slot() が hash を空にすると本文は `"\\n<count>"` になる。0.3.1 までは全体を
    strip() してから分割していたため、この形を `["<count>"]` と誤読して hash=<count> /
    count=0 になり、枠を 1 つ戻しただけで上限カウントがリセットされていた。行単位で
    読んで 1 行目 (空) を hash として扱う。
    """
    lines = [line.strip() for line in flock.read_all(f).split("\n")]
    saved_hash = lines[0] if lines else ""
    try:
        count = int(lines[1]) if len(lines) > 1 and lines[1] else 0
    except ValueError:
        count = 0
    return saved_hash, count


def reserve_slot(marker_file: str, current_hash: str, max_reviews: int) -> bool:
    """ロック下で原子的にスロットを確保する。

    確保成功時は count を +1 して current_hash を書き込み True を返す。並行起動時も
    `EXTERNAL_AI_REVIEW_MAX` を超えた確保は起きない。レビュー結果が REVIEW_CLEAN /
    reviewer 失敗の場合は release_slot() で枠を戻す。
    """
    try:
        with flock.locked_file(marker_file) as f:
            saved_hash, count = _read_marker(f)
            if count >= max_reviews:
                log(f"レビュー回数上限 ({max_reviews}) に達した")
                return False
            if saved_hash == current_hash:
                log("同一内容でレビュー済み")
                return False
            flock.rewrite(f, f"{current_hash}\n{count + 1}")
            return True
    except OSError as e:
        log(f"マーカー read/write 失敗: {e}")
        return False


def release_slot(marker_file: str, reserved_hash: str) -> None:
    """reserve_slot() で確保した枠を戻す (REVIEW_CLEAN / reviewer 失敗時)。

    - count を -1 (0 未満にはしない)
    - saved_hash がまだ自分 (reserved_hash) なら空に戻す
    - 他プロセスが追い越して saved_hash を上書きしていれば hash は触らない
    """
    try:
        with flock.locked_file(marker_file) as f:
            saved_hash, count = _read_marker(f)
            new_hash = "" if saved_hash == reserved_hash else saved_hash
            flock.rewrite(f, f"{new_hash}\n{max(0, count - 1)}")
    except OSError as e:
        log(f"マーカー read/write 失敗: {e}")


def run_reviewers(plan_text: str) -> dict[str, str]:
    """利用可能なレビュアーを並列実行し、clean でない結果のみ {name: output} で返す。

    全体 timeout は置かない。各レビュアーは自前の TIMEOUT_SEC で必ず返り、かつ
    `ThreadPoolExecutor` の with 終端は全 future の完了を待つため、`as_completed` に
    timeout を渡しても実質効かない (0.3.1 までの overall_timeout は dead logic だった)。
    """
    active = [r for r in REVIEWERS if r.is_available()]
    if not active:
        return {}

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        future_map = {pool.submit(r.review, plan_text): r for r in active}
        for future in as_completed(future_map):
            reviewer = future_map[future]
            try:
                result = future.result()
            except Exception as e:
                log(f"{reviewer.NAME} 失敗: {e}")
                continue
            if not result:
                log(f"{reviewer.NAME}: 結果なし")
                continue
            if is_clean_review(result):
                log(f"{reviewer.NAME}: REVIEW_CLEAN")
                continue
            results[reviewer.NAME] = result
    return results


def build_reason(results: dict[str, str]) -> str:
    sections = []
    for reviewer in REVIEWERS:
        name = reviewer.NAME
        if name in results:
            header = _HEADERS.get(name, f"## {name} レビュー")
            sections.append(f"{header}\n\n{results[name]}")

    return (
        "## クロスレビュー結果 (ExitPlanMode)\n\n"
        + "\n\n".join(sections)
        + "\n\n---\n\n"
        "レビュー指摘を踏まえてプランを見直し、再度 ExitPlanMode を呼んでください。"
        "既に対処済み・妥当でない指摘は無視して構いません。"
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"stdin JSON パース失敗: {e}")
        sys.exit(0)

    if payload.get("tool_name") != "ExitPlanMode":
        sys.exit(0)

    session_id = payload.get("session_id", "")
    if not session_id:
        log("session_id が空")
        sys.exit(0)

    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        log(f"tool_input が dict ではない: {type(tool_input).__name__}")
        sys.exit(0)

    plan_text = tool_input.get("plan", "")
    if not isinstance(plan_text, str) or not plan_text.strip():
        log("plan が空または非文字列")
        sys.exit(0)

    plan_stripped = plan_text.strip()

    max_reviews = get_max_reviews()
    if max_reviews <= 0:
        log("EXTERNAL_AI_REVIEW_MAX=0 によりレビュー無効化")
        sys.exit(0)

    active_names = [r.NAME for r in REVIEWERS if r.is_available()]
    if not active_names:
        log("利用可能なレビュアーなし (cursor/codex 未インストール)")
        sys.exit(0)

    marker_dir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "plan-review-markers")
    marker_file = os.path.join(marker_dir, f"{session_id}.exitplan.marker")
    current_hash = plan_hash(plan_stripped)

    if not reserve_slot(marker_file, current_hash, max_reviews):
        sys.exit(0)

    log(f"レビュー実行: {', '.join(active_names)}")
    results = run_reviewers(plan_stripped)

    if not results:
        log("全レビュアーが REVIEW_CLEAN または結果なし (block しない、スロット戻す)")
        release_slot(marker_file, current_hash)
        sys.exit(0)

    reason = build_reason(results)

    review_file = os.path.join(
        os.environ.get("TMPDIR", "/tmp"),
        f"plan-review-{session_id[:8]}.txt",
    )
    try:
        with open(review_file, "w") as f:
            f.write(reason)
        log(f"レビュー完了 ({', '.join(results.keys())}) → {review_file}")
    except OSError:
        log("参照コピーの保存に失敗")

    json.dump({"decision": "block", "reason": reason}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:
        log(f"fatal: {e}")
