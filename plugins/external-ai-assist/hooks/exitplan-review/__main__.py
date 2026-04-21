#!/usr/bin/env python3
"""PreToolUse(ExitPlanMode) hook: Cursor + Codex でプランをクロスレビューし、
セッション内で最大 MAX_REVIEWS 回ブロックする。

- Cursor: 既存コードベース整合観点 (primary)
- Codex: 要件・アーキ観点 (補完)

両方のレビュアーを並列実行し、critical な指摘があった場合のみ Claude に block を返す。
全レビュアーが REVIEW_CLEAN を返した場合、または全失敗の場合は fail-open (exit 0)。

マーカーの read→判定→write は fcntl.flock で排他ロック。
同一セッション並行起動時のカウント破綻を防ぐ。

exit 0 (JSON なし): ブロックしない (clean / レビュー済み / 両方失敗 / エラー)
exit 0 + JSON stdout: decision:block で差し戻し
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import codex
import cursor

REVIEWERS = [cursor, codex]

DEFAULT_MAX_REVIEWS = 2
_HEADERS = {
    "cursor": "## Cursor レビュー (既存コードベース整合観点)",
    "codex": "## Codex レビュー (要件・アーキ観点)",
}


def log(msg: str) -> None:
    print(f"[exitplan-review] {msg}", file=sys.stderr)


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


def reserve_slot(marker_file: str, current_hash: str, max_reviews: int) -> bool:
    """ロック下で原子的にスロットを確保する。

    確保成功時は count を +1 して current_hash を書き込み True を返す。並行起動時も
    `EXTERNAL_AI_REVIEW_MAX` を超えた確保は起きない。レビュー結果が REVIEW_CLEAN /
    reviewer 失敗の場合は release_slot() で枠を戻す。
    """
    try:
        os.makedirs(os.path.dirname(marker_file), exist_ok=True)
        with open(marker_file, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                content = f.read().strip()
                lines = content.split("\n") if content else []
                saved_hash = lines[0] if lines else ""
                try:
                    count = int(lines[1]) if len(lines) > 1 else 0
                except ValueError:
                    count = 0

                if count >= max_reviews:
                    log(f"レビュー回数上限 ({max_reviews}) に達した")
                    return False
                if saved_hash == current_hash:
                    log("同一内容でレビュー済み")
                    return False

                f.seek(0)
                f.truncate()
                f.write(f"{current_hash}\n{count + 1}")
                f.flush()
                return True
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
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
        with open(marker_file, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                content = f.read().strip()
                lines = content.split("\n") if content else []
                saved_hash = lines[0] if lines else ""
                try:
                    count = int(lines[1]) if len(lines) > 1 else 0
                except ValueError:
                    count = 0

                new_count = max(0, count - 1)
                new_hash = "" if saved_hash == reserved_hash else saved_hash

                f.seek(0)
                f.truncate()
                f.write(f"{new_hash}\n{new_count}")
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        log(f"マーカー read/write 失敗: {e}")


def run_reviewers(plan_text: str) -> dict[str, str]:
    """利用可能なレビュアーを並列実行し、clean でない結果のみ {name: output} で返す。"""
    active = [r for r in REVIEWERS if r.is_available()]
    if not active:
        return {}

    overall_timeout = max(r.TIMEOUT_SEC for r in active) + 60
    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        future_map = {pool.submit(r.review, plan_text): r for r in active}
        try:
            for future in as_completed(future_map, timeout=overall_timeout):
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
        except Exception as e:
            log(f"並列実行エラー: {e}")

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
        print(f"[exitplan-review] fatal: {e}", file=sys.stderr)
