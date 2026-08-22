"""PreToolUse hook の verify() 成功結果を短期キャッシュする。

同一プロジェクトで `gh pr list && gh pr view && gh pr comment ...` のように
短時間に連打されるケースで `gh auth status` / `aws sts` / `gcloud config ...`
などを毎回呼び直すコストを削減する。検証成功のみキャッシュし、失敗 (deny 発生)
は常に再検証する。

キャッシュ無効化:
- TTL (既定 30 秒) を過ぎた
- accounts.local.json の mtime が変わった
- キャッシュファイルが存在しない or JSON 破損
- アカウント状態を変えうるコマンド (`gh auth switch` / `gcloud config set` /
  `firebase use <x>` / `kubectl config use-context` / `aws sso login` 等) を
  dispatcher が検出した時点で、その service の entry を全て破棄する
  (`invalidate(service_name)`)。CLI のアカウント状態はマシン全体で共有される
  (hosts.yml / gcloud 設定 / configstore / kubeconfig / SSO token cache) ため、
  project_dir や inline env で絞らず service 単位で破棄する

保存先: $TMPDIR/cc-mp-verify-cloud-account/<service>-<sha256>.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

_CACHE_TTL_SEC = 30

_SERVICE_TAG_RE = re.compile(r"[^A-Za-z0-9_-]")


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


def _service_tag(service_name: str) -> str:
    """ファイル名の prefix に使う service 名 (英数字・`_`・`-` 以外は `_` に置換)。

    `invalidate()` が service 単位で glob できるよう、key の先頭に置く。
    """
    return _SERVICE_TAG_RE.sub("_", service_name) or "_"


def _cache_key(service_name: str, project_dir: str, expected, inline_env=None) -> str:
    material = json.dumps(
        {
            "svc": service_name,
            "pd": project_dir,
            "exp": expected,
            "env": inline_env or {},
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_service_tag(service_name)}-{digest}"


def get_success(
    service_name: str,
    project_dir: str,
    expected,
    accounts_mtime: float,
    inline_env=None,
) -> bool:
    """検証成功が短期キャッシュにあれば True を返す。

    inline_env (コマンド行頭の `AWS_PROFILE=...` 等) が異なれば検証結果も
    変わりうるためキーに含める。env 差で別エントリになり、profile A の成功が
    profile B で誤って allow されることを防ぐ。
    """
    path = _cache_path(_cache_key(service_name, project_dir, expected, inline_env))
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
    service_name: str,
    project_dir: str,
    expected,
    accounts_mtime: float,
    inline_env=None,
) -> None:
    """検証成功をキャッシュする。書き込み失敗は無視 (キャッシュはベストエフォート)。"""
    path = _cache_path(_cache_key(service_name, project_dir, expected, inline_env))
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


def invalidate(service_name: str) -> int:
    """service の成功 cache を project_dir / expected / inline env を問わず全て破棄する。

    アカウント状態を変えうるコマンドの PreToolUse 時点で dispatcher が呼ぶ。
    破棄した entry 数を返す (削除失敗・cache dir 不在は 0 扱いで例外にしない)。
    """
    base = _cache_dir()
    if base is None:
        return 0
    removed = 0
    try:
        entries = list(base.glob(f"{_service_tag(service_name)}-*.json"))
    except OSError:
        return 0
    for path in entries:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
