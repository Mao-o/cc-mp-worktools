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
  dispatcher が検出した時点 (PreToolUse) と、そのコマンドの実行後 (PostToolUse)
  の 2 回、`invalidate(service_name)` で service の **epoch を進め**、既存 entry を
  全て削除する。CLI のアカウント状態はマシン全体で共有される (hosts.yml /
  gcloud 設定 / configstore / kubeconfig / SSO token cache) ため、project_dir や
  inline env で絞らず service 単位で破棄する

epoch (tombstone): 削除だけだと、切替 hook と並行して走った別 hook が旧状態を
検証して新しい entry を書き、切替後に TTL 残り分だけ通ってしまう。そのため entry
には verify 開始時点の epoch を記録し、読む側は entry の epoch が現在と違えば無視、
書く側は開始時と現在の epoch が違えば書かない。PostToolUse で実行後にも epoch を
進めるので、実行完了前に開始した検証の結果は全て無効になる。

保存先: $TMPDIR/cc-mp-verify-cloud-account/<service>-<sha256>.json
epoch:  $TMPDIR/cc-mp-verify-cloud-account/<service>.epoch
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


def _epoch_path(service_name: str) -> Path | None:
    base = _cache_dir()
    if base is None:
        return None
    return base / f"{_service_tag(service_name)}.epoch"


def _write_atomic(path: Path, text: str) -> None:
    """tmp に書いて `os.replace` で置き換える (並行する hook が途中状態を読まないように)。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


def current_epoch(service_name: str) -> int:
    """service の現在の epoch。一度も無効化されていない (epoch ファイルが無い / 読めない) なら 0。"""
    path = _epoch_path(service_name)
    if path is None or not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("epoch", 0))
    except (ValueError, TypeError, AttributeError, OSError):
        return 0


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
    entry の epoch が現在の epoch と違えば (書かれた後に切替が検出された) 無視する。
    """
    path = _cache_path(_cache_key(service_name, project_dir, expected, inline_env))
    if path is None or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("accounts_mtime") != accounts_mtime:
        return False
    ts = data.get("timestamp", 0)
    if time.time() - ts > _CACHE_TTL_SEC:
        return False
    if data.get("epoch", 0) != current_epoch(service_name):
        return False
    return bool(data.get("success"))


def set_success(
    service_name: str,
    project_dir: str,
    expected,
    accounts_mtime: float,
    inline_env=None,
    epoch: int | None = None,
) -> bool:
    """検証成功をキャッシュする。書けたら True。

    epoch は verify **開始**時点の `current_epoch()`。開始後に `invalidate()` で
    epoch が進んでいたら (切替 hook と並行した検証 = 旧状態を見ている可能性) 書かずに
    False を返す。None なら現在の epoch で書く (後方互換)。書き込み失敗は無視
    (キャッシュはベストエフォート)。
    """
    path = _cache_path(_cache_key(service_name, project_dir, expected, inline_env))
    if path is None:
        return False
    now_epoch = current_epoch(service_name)
    if epoch is None:
        epoch = now_epoch
    elif epoch != now_epoch:
        return False
    data = {
        "success": True,
        "accounts_mtime": accounts_mtime,
        "timestamp": time.time(),
        "epoch": epoch,
    }
    try:
        _write_atomic(path, json.dumps(data))
    except OSError:
        return False
    return True


def invalidate(service_name: str) -> int:
    """service の epoch を進め (tombstone)、成功 cache を project_dir / expected /
    inline env を問わず全て破棄する。

    epoch を先に進めてから削除する: 削除と並行して旧 epoch で書かれた / 残った entry も
    epoch 不一致で無視される。epoch は `max(現在 + 1, time_ns)` で、epoch ファイルが
    消されても単調増加を保つ (counter だけだと再出発した値が古い entry と一致しうる)。
    破棄した entry 数を返す (削除失敗・cache dir 不在は 0 扱いで例外にしない)。
    """
    base = _cache_dir()
    if base is None:
        return 0
    tag = _service_tag(service_name)
    new_epoch = max(current_epoch(service_name) + 1, time.time_ns())
    try:
        _write_atomic(
            base / f"{tag}.epoch",
            json.dumps({"epoch": new_epoch, "at": time.time()}),
        )
    except OSError:
        pass
    removed = 0
    try:
        entries = list(base.glob(f"{tag}-*.json"))
    except OSError:
        return 0
    for path in entries:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
