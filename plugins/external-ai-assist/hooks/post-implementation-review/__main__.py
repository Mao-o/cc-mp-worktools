#!/usr/bin/env python3
"""Stop hook: 実装直後の差分を Cursor でレビューし、指摘があれば Claude に差し戻す。

差分の取得は `git diff HEAD` (tracked) + `git ls-files --others` (untracked) の
合成で、新規追加ファイル中心の変更でも取りこぼさない。

マーカーの read→判定→write は fcntl.flock で排他ロックし、同一セッション並行起動時の
カウント破綻を防ぐ。ハッシュは truncate 前の diff 全体で計算し、後半だけ変更された場合も
再レビューが走るようにしている。

発火条件:
- stop_hook_active が false (再帰防止)
- git diff (tracked + untracked) に内容がある
- 同一 diff を既にレビュー済みでない
- レビュー回数が MAX 未満
- Cursor がインストール済み

exit 0 (JSON なし): Stop を妨げない
exit 0 + {"decision": "block", "reason": ...}: Claude にレビュー結果を返し追加対応を促す
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys

import cursor

DEFAULT_MAX_REVIEWS = 2
MAX_DIFF_BYTES = 40000
MAX_UNTRACKED_FILES = 50


def log(msg: str) -> None:
    print(f"[post-implementation-review] {msg}", file=sys.stderr)


def get_max_reviews() -> int:
    raw = os.environ.get("EXTERNAL_AI_POST_REVIEW_MAX", "").strip()
    if not raw:
        return DEFAULT_MAX_REVIEWS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_REVIEWS


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


def reserve_slot(marker_file: str, current_hash: str, max_reviews: int) -> bool:
    """ロック下で原子的にスロットを確保する。

    確保成功時は count を +1 して current_hash を書き込み True を返す。並行起動時も
    `EXTERNAL_AI_POST_REVIEW_MAX` を超えた確保は起きない。レビュー結果が
    REVIEW_CLEAN / cursor.review() 失敗の場合は release_slot() で枠を戻す。
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
                    log("同一 diff でレビュー済み")
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
    """reserve_slot() で確保した枠を戻す (REVIEW_CLEAN / cursor.review() 失敗時)。

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


def _list_untracked_files(cwd: str) -> list[str]:
    """ls-files --others --exclude-standard で untracked ファイルのパス一覧を返す。"""
    try:
        listing = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    if listing.returncode != 0 or not listing.stdout:
        return []

    return [f for f in listing.stdout.split("\0") if f]


def _untracked_fingerprint(cwd: str, files: list[str]) -> str:
    """全 untracked ファイル (MAX を超える分も含む) の path:size fingerprint。

    レビュー対象として truncate された 51 番目以降のファイルが編集された場合も
    ハッシュが変化するように、diff 本文とは別に hash_source へ混ぜ込む用途。
    """
    parts: list[str] = []
    for f in files:
        try:
            size = os.path.getsize(os.path.join(cwd, f))
        except OSError:
            size = -1
        parts.append(f"{f}:{size}")
    return "\n".join(parts)


def _collect_untracked_diff(cwd: str, files: list[str]) -> str:
    """先頭 MAX_UNTRACKED_FILES 件の未追跡ファイルの diff (vs /dev/null) を連結する。"""
    if not files:
        return ""

    parts: list[str] = []
    for f in files[:MAX_UNTRACKED_FILES]:
        try:
            res = subprocess.run(
                ["git", "diff", "--no-index", "--", "/dev/null", f],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
        # git diff --no-index は差分がある場合に exit 1 を返す。stdout を採用する
        if res.stdout:
            parts.append(res.stdout)

    if len(files) > MAX_UNTRACKED_FILES:
        omitted = len(files) - MAX_UNTRACKED_FILES
        parts.append(f"\n... ({omitted} more untracked files omitted from review)\n")

    return "\n".join(parts)


def _is_inside_worktree(cwd: str) -> bool:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"


def _head_exists(cwd: str) -> bool:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return res.returncode == 0


def _get_tracked_diff(cwd: str) -> str | None:
    """tracked/staged の diff を返す。HEAD があれば `git diff HEAD`、
    なければ `git diff --cached` (初回コミット前 repo 用フォールバック)。
    """
    args = ["git", "diff", "HEAD"] if _head_exists(cwd) else ["git", "diff", "--cached"]
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log(f"git diff 実行失敗: {e}")
        return None

    if result.returncode != 0:
        log(f"git diff ({' '.join(args[1:])}) が非ゼロ終了: {result.returncode}")
        return None

    return result.stdout


def get_git_diff(cwd: str) -> tuple[str, str] | None:
    """(hash_source, truncated_for_review) を返す。

    hash_source: truncate 前の diff 全体 (ハッシュ計算用)
    truncated_for_review: cursor に渡す切り詰め済み diff
    """
    if not _is_inside_worktree(cwd):
        log("git worktree 外のため skip")
        return None

    tracked_diff = _get_tracked_diff(cwd)
    if tracked_diff is None:
        return None

    untracked_files = _list_untracked_files(cwd)
    untracked_diff = _collect_untracked_diff(cwd, untracked_files)
    untracked_fingerprint = _untracked_fingerprint(cwd, untracked_files)

    full_diff = tracked_diff
    if untracked_diff:
        if full_diff and not full_diff.endswith("\n"):
            full_diff += "\n"
        full_diff += untracked_diff

    if not full_diff.strip():
        return None

    # ハッシュ用: truncate 前の diff 全体 + 全 untracked ファイルの fingerprint。
    # omit される 51 番目以降のファイルが変更されても fingerprint が変わるので、
    # 同一 hash 扱いで skip されない。
    hash_source = full_diff
    if untracked_fingerprint:
        hash_source += "\n---\nuntracked-fingerprint:\n" + untracked_fingerprint

    encoded = full_diff.encode()
    if len(encoded) > MAX_DIFF_BYTES:
        truncated = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        truncated += "\n\n... (diff truncated for review)\n"
    else:
        truncated = full_diff

    return hash_source, truncated


def build_reason(cursor_output: str) -> str:
    return (
        "## 実装直後レビュー結果 (Cursor, 差分レビュー)\n\n"
        + cursor_output
        + "\n\n---\n\n"
        "critical な指摘があれば対応し、軽微・妥当でないと判断した指摘は"
        "理由を添えてスキップした上で作業を完了してください。"
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"stdin JSON パース失敗: {e}")
        sys.exit(0)

    if payload.get("stop_hook_active"):
        log("stop_hook_active=True によりスキップ (再帰防止)")
        sys.exit(0)

    session_id = payload.get("session_id", "")
    if not session_id:
        log("session_id が空")
        sys.exit(0)

    cwd = payload.get("cwd") or os.getcwd()

    max_reviews = get_max_reviews()
    if max_reviews <= 0:
        log("EXTERNAL_AI_POST_REVIEW_MAX=0 によりレビュー無効化")
        sys.exit(0)

    if not cursor.is_available():
        log("cursor 未インストール")
        sys.exit(0)

    diff_info = get_git_diff(cwd)
    if not diff_info:
        log("git diff (tracked + untracked) が空または取得失敗")
        sys.exit(0)
    full_diff, truncated_diff = diff_info

    current_hash = diff_hash(full_diff)

    marker_dir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "post-review-markers")
    marker_file = os.path.join(marker_dir, f"{session_id}.post.marker")

    if not reserve_slot(marker_file, current_hash, max_reviews):
        sys.exit(0)

    log(f"Cursor による実装直後レビューを実行 (diff full={len(full_diff)} chars)")
    result = cursor.review(truncated_diff)

    if not result:
        log("Cursor レビュー失敗 (fail-open、スロット戻す)")
        release_slot(marker_file, current_hash)
        sys.exit(0)

    if is_clean_review(result):
        log("Cursor: REVIEW_CLEAN (block しない、スロット戻す)")
        release_slot(marker_file, current_hash)
        sys.exit(0)

    reason = build_reason(result)

    review_file = os.path.join(
        os.environ.get("TMPDIR", "/tmp"),
        f"post-review-{session_id[:8]}.txt",
    )
    try:
        with open(review_file, "w") as f:
            f.write(reason)
        log(f"レビュー完了 → {review_file}")
    except OSError:
        log("参照コピーの保存に失敗")

    json.dump({"decision": "block", "reason": reason}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[post-implementation-review] fatal: {e}", file=sys.stderr)
