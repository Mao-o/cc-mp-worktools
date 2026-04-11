#!/usr/bin/env python3
"""PreToolUse (ExitPlanMode) フック:
Codex CLI でプランをレビューし、セッション内で最大2回ブロックする。

exit 0 (JSON なし): ブロックしない（レビュー済み・エラー時フォールスルー）
exit 0 + JSON stdout: decision:block でブロック（レビュー結果を注入）
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys

MAX_REVIEWS = 2


def log(msg: str) -> None:
    print(f"[exitplan-review-codex] {msg}", file=sys.stderr)


def plan_hash(text: str) -> str:
    """プラン内容の正規化ハッシュ（先頭 2000 文字、空白正規化）"""
    normalized = " ".join(text[:2000].split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def read_marker(path: str) -> tuple[str, int]:
    """マーカーファイルを読み込み (ハッシュ, レビュー回数) を返す"""
    try:
        lines = open(path).read().strip().split("\n")
        saved_hash = lines[0] if lines else ""
        count = int(lines[1]) if len(lines) > 1 else 0
        return saved_hash, count
    except (OSError, ValueError):
        return "", 0


def write_marker(path: str, h: str, count: int) -> None:
    """マーカーファイルにハッシュとレビュー回数を書き込む"""
    try:
        with open(path, "w") as f:
            f.write(f"{h}\n{count}")
    except OSError as e:
        log(f"マーカー書き込み失敗: {e}")


def main() -> None:
    # ── 依存コマンド確認 ──
    if not shutil.which("codex"):
        log("codex コマンドが見つかりません")
        sys.exit(0)

    # ── stdin パース ──
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"stdin JSON パース失敗: {e}")
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    if tool_name != "ExitPlanMode":
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

    # ── マーカーチェック ──
    marker_dir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "plan-review-markers")
    os.makedirs(marker_dir, exist_ok=True)

    current_hash = plan_hash(plan_stripped)
    marker_file = os.path.join(marker_dir, f"{session_id}.exitplan.marker")

    saved_hash, review_count = read_marker(marker_file)

    if review_count >= MAX_REVIEWS:
        log(f"レビュー回数上限 ({MAX_REVIEWS}) に達した")
        sys.exit(0)

    if saved_hash == current_hash:
        log("同一内容でレビュー済み")
        sys.exit(0)

    # ── Codex レビュー実行 ──
    log("Codex にプランレビューを依頼中...")

    review_prompt = (
        "以下の <stdin> に記載された実装プランをレビューして。\n"
        "- 問題点・矛盾・リスクを指摘\n"
        "- 見落としているエッジケースや依存関係を指摘\n"
        "- 改善案があれば提示\n"
        "箇条書きで簡潔にまとめること。"
    )

    try:
        result = subprocess.run(
            ["codex", "exec", "-s", "read-only", "--ephemeral", review_prompt],
            input=plan_stripped,
            capture_output=True,
            text=True,
            timeout=1500,
        )
    except subprocess.TimeoutExpired:
        log("Codex レビューがタイムアウト")
        sys.exit(0)
    except (FileNotFoundError, OSError) as e:
        log(f"codex 実行失敗: {e}")
        sys.exit(0)

    review = result.stdout.strip()
    if not review:
        log("レビュー結果が空")
        sys.exit(0)

    # ── マーカー書き込み ──
    write_marker(marker_file, current_hash, review_count + 1)

    # ── 人間参照用コピー ──
    review_file = os.path.join(
        os.environ.get("TMPDIR", "/tmp"),
        f"plan-review-{session_id[:8]}.txt",
    )
    try:
        with open(review_file, "w") as f:
            f.write(f"## Codex によるプランレビュー (ExitPlanMode)\n\n{review}\n")
        log(f"レビュー完了 → {review_file}")
    except OSError:
        log("参照コピーの保存に失敗")

    # ── ブロック応答 ──
    output = {
        "decision": "block",
        "reason": (
            "## Codex によるプランレビュー\n\n"
            f"{review}\n\n"
            "レビューを反映した上で、再度 ExitPlanMode を呼んでください。"
        ),
    }
    json.dump(output, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
