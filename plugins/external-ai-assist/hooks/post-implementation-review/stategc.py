"""$TMPDIR に残る post-implementation-review の一時ファイルの GC。

v0.2.0 以前は GC が無く、`$TMPDIR/post-review-markers/` にセッションごとの marker が
無期限に溜まっていた。新形式の状態ファイルに加えて旧形式の残骸も TTL 超過分だけ
掃除する (旧版が並行稼働していても、書き込み直後のファイルは消さない)。

状態機械そのもの (state.py) とは変更理由が別 — 掃除ポリシーの調整でロック順序や
claim の正しさに触りたくないため、モジュールを分けている。
"""
from __future__ import annotations

import os
import time

from _common import flock

from state import BASH_SNAPSHOT_TTL_SEC, STATE_TTL_SEC, _tmp_root, state_root

# 旧実装 (v0.2.0 以前) が残した一時ファイル。
_LEGACY_MARKER_DIR = "post-review-markers"
_LEGACY_REVIEW_PREFIX = "post-review-"


def gc_stale(now: float | None = None) -> int:
    """TTL 超過の状態ファイル・スナップショット・レビュー結果を削除し、件数を返す。

    Stop でのみ呼ぶ (PostToolUse ごとに走らせるほどの緊急性はない)。

    あわせて `state_root()` の権限を retrofit する (内部バックログ)。0.8.x 以前は
    ディレクトリを既定 umask で作っていたため、旧バージョンからアップグレードした
    環境では既存ディレクトリが 0o700 になっていない。新規作成分は
    `_common.flock` 側 (`_makedirs_private`) が締めるので、ここでは
    「既に存在する」ディレクトリの締め直しだけを行う。
    """
    flock.harden_dir(state_root())
    now = time.time() if now is None else now
    removed = 0
    for path, ttl in _gc_candidates():
        try:
            if now - os.path.getmtime(path) > ttl:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    _prune_empty_dirs()
    return removed


def _gc_candidates() -> list[tuple[str, int]]:
    """(パス, 適用する TTL) の一覧。bashsnap だけ短い TTL を当てる。"""
    paths: list[tuple[str, int]] = []
    root = state_root()
    bashsnap_dir = os.path.join(root, "bashsnap")
    for base, _dirs, files in os.walk(root):
        ttl = BASH_SNAPSHOT_TTL_SEC if base == bashsnap_dir else STATE_TTL_SEC
        paths.extend((os.path.join(base, name), ttl) for name in files)

    legacy_dir = os.path.join(_tmp_root(), _LEGACY_MARKER_DIR)
    if os.path.isdir(legacy_dir):
        try:
            paths.extend(
                (os.path.join(legacy_dir, n), STATE_TTL_SEC)
                for n in os.listdir(legacy_dir)
            )
        except OSError:
            pass

    tmp = _tmp_root()
    try:
        for name in os.listdir(tmp):
            if name.startswith(_LEGACY_REVIEW_PREFIX) and name.endswith(".txt"):
                paths.append((os.path.join(tmp, name), STATE_TTL_SEC))
    except OSError:
        pass
    return paths


def _prune_empty_dirs() -> None:
    for base, dirs, _files in os.walk(state_root(), topdown=False):
        for name in dirs:
            try:
                os.rmdir(os.path.join(base, name))
            except OSError:
                pass
    try:
        os.rmdir(os.path.join(_tmp_root(), _LEGACY_MARKER_DIR))
    except OSError:
        pass
