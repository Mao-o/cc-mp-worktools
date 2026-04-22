"""PreToolUse hook の verify() 成功結果を短期キャッシュする。

同一プロジェクトで `gh pr list && gh pr view && gh pr comment ...` のように
短時間に連打されるケースで `gh auth status` / `aws sts` / `gcloud config ...`
などを毎回呼び直すコストを削減する。検証成功のみキャッシュし、失敗 (deny 発生)
は常に再検証する。

キャッシュ無効化:
- TTL (既定 30 秒) を過ぎた
- accounts.local.json の mtime が変わった
- キャッシュファイルが存在しない or JSON 破損

保存先: $TMPDIR/cc-mp-verify-cloud-account/<sha256>.json
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

_CACHE_TTL_SEC = 30


def _cache_dir() -> Path | None:
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    p = Path(base) / "cc-mp-verify-cloud-account"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return p


def _cache_path(key: str) -> Path | None:
    base = _cache_dir()
    if base is None:
        return None
    return base / f"{key}.json"


def _cache_key(service_name: str, project_dir: str, expected) -> str:
    material = json.dumps(
        {"svc": service_name, "pd": project_dir, "exp": expected},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def get_success(
    service_name: str, project_dir: str, expected, accounts_mtime: float
) -> bool:
    """検証成功が短期キャッシュにあれば True を返す。"""
    path = _cache_path(_cache_key(service_name, project_dir, expected))
    if path is None or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("accounts_mtime") != accounts_mtime:
        return False
    ts = data.get("timestamp", 0)
    if time.time() - ts > _CACHE_TTL_SEC:
        return False
    return bool(data.get("success"))


def set_success(
    service_name: str, project_dir: str, expected, accounts_mtime: float
) -> None:
    """検証成功をキャッシュする。書き込み失敗は無視 (キャッシュはベストエフォート)。"""
    path = _cache_path(_cache_key(service_name, project_dir, expected))
    if path is None:
        return
    data = {
        "success": True,
        "accounts_mtime": accounts_mtime,
        "timestamp": time.time(),
    }
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
