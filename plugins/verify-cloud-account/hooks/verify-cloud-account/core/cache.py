"""PreToolUse hook の verify() 成功結果を短期キャッシュする。

同一プロジェクトで `gh pr list && gh pr view && gh pr comment ...` のように
短時間に連打されるケースで `gh auth status` / `aws sts` / `gcloud config ...`
などを毎回呼び直すコストを削減する。検証成功のみキャッシュし、失敗 (deny 発生)
は常に再検証する。

キー: service / project_dir / 期待値 / 行頭の inline env / context option に加え、
hook プロセスの env のうちアカウントを決める変数 (`identity_env()`。v0.17.0)。
プロジェクトごとの固定 (settings の `env` の `AWS_PROFILE` 等) は保存した時点で
起動中のセッションに反映されるため、値が変わったら別 entry として再検証する。

キャッシュ無効化:
- TTL (既定 30 秒) を過ぎた
- accounts.local.json の mtime が変わった
- キャッシュファイルが存在しない or JSON 破損
- アカウント状態を変えうるコマンド (`gh auth switch` / `gcloud config set` /
  `firebase use <x>` / `kubectl config use-context` / `aws sso login` 等) を
  dispatcher が検出した時点 (PreToolUse) で `invalidate(service_name)` が service の
  **epoch を進め**、切替検出時刻 (tombstone) を記録し、既存 entry を全て削除する。
  CLI のアカウント状態はマシン全体で共有される (hosts.yml / gcloud 設定 /
  configstore / kubeconfig / SSO token cache) ため、project_dir や inline env で
  絞らず service 単位で破棄する

epoch / in-flight 窓: 削除だけだと、切替 hook と並行して走った別 hook が旧状態を
検証して新しい entry を書き、切替後に TTL 残り分だけ通ってしまう。そのため
- entry には verify 開始時点の epoch を記録し、読む側は entry の epoch が現在と
  違えば無視、書く側は開始時と現在の epoch が違えば書かない (無効化**前**に開始した
  検証の結果を公開しない)
- tombstone から IN_FLIGHT_SEC (60 秒) は「切替の実行中」とみなして成功 cache を
  書かない (無効化**後**・切替完了**前**に開始した検証の結果を公開しない)。hook は
  実行前にしか走らず切替コマンドの完了時刻が分からないため時間で区切る。残る穴は
  60 秒を超える対話 login 中の並行検証のみ (README 既知の制限)

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
# 切替コマンド検出 (invalidate) からこの秒数は切替の実行中とみなし、成功 cache を書かない。
# 対話 login (ブラウザ認証) でもおおむね収まる長さ。代償は切替後 60 秒の毎回再検証。
IN_FLIGHT_SEC = 60

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


def service_state_path(service_name: str, suffix: str) -> Path | None:
    """cache dir に置く service 別の補助ファイル (`<service><suffix>`) のパス。

    成功 cache と同じ場所 (hook プロセスを跨いで共有される `$TMPDIR` 配下) に置きたい
    状態 (`core/auto_switch.py` の切替記録) のための入口。dir を作れなければ None。
    `suffix` は `-` で始めないこと — `invalidate()` の glob (`<service>-*.json`) に
    掛かって成功 cache と一緒に消える。
    """
    if suffix.startswith("-"):
        raise ValueError(f"suffix must not start with '-': {suffix!r}")
    base = _cache_dir()
    if base is None:
        return None
    return base / f"{_service_tag(service_name)}{suffix}"


def write_state(path: Path, text: str) -> None:
    """`service_state_path()` のファイルを原子的に書く (失敗は OSError のまま返す)。"""
    _write_atomic(path, text)


def identity_env(service, env) -> dict[str, str]:
    """service の CLI が「どのアカウント / project で動くか」を決める変数を env から抜く。

    service は `IDENTITY_ENV_VARS` (名前) と `IDENTITY_ENV_PREFIXES` (prefix) を宣言する
    (services/__init__.py の契約。全 service 必須)。dispatcher は検証に渡すのと同じ env
    (hook プロセスの env に行頭の inline env を重ねたもの) を渡し、結果を成功 cache の
    キーに含める。値はキーの hash の材料にだけ使い、cache ファイルには書かない。
    """
    names = getattr(service, "IDENTITY_ENV_VARS", frozenset())
    prefixes = tuple(getattr(service, "IDENTITY_ENV_PREFIXES", ()))
    return {
        name: value
        for name, value in env.items()
        if name in names or (prefixes and name.startswith(prefixes))
    }


def _cache_key(
    service_name: str,
    project_dir: str,
    expected,
    inline_env=None,
    context=None,
    identity_env=None,
) -> str:
    material = json.dumps(
        {
            "svc": service_name,
            "pd": project_dir,
            "exp": expected,
            "env": inline_env or {},
            # コンテキスト option (`--profile` / `--project` / `--context` 等) も
            # 検証結果を左右するのでキーに含める。含めないと
            # `aws --profile other s3 rm` が既定 profile の成功 entry を hit して
            # 未検証のまま allow される。
            "ctx": context or {},
            # hook プロセスの env のうちアカウントを決める変数 (`identity_env()`)。
            # プロジェクトごとの固定 (settings の `env` の `AWS_PROFILE` 等) は
            # 保存した時点で起動中のセッションに反映されるので、含めないと値を
            # 変えた直後の TTL の間は前の値での成功 entry を hit する。
            "ident": identity_env or {},
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_service_tag(service_name)}-{digest}"


def _read_epoch(service_name: str) -> tuple[int, int]:
    """(epoch, tombstone_ns) を返す。epoch ファイルが無い / 読めないなら (0, 0)。"""
    path = _epoch_path(service_name)
    # `os.path.isfile` を使う: pathlib の `Path.is_file()` は Python 3.13 まで、ENOENT など以外の
    # OSError (長すぎる名前を指す symlink の ENAMETOOLONG・EACCES) をそのまま投げる (try の外に
    # あるので、下の except と同じく dispatch() の外まで抜けて検証をスキップしていた)。
    if path is None or not os.path.isfile(path):
        return 0, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("epoch", 0)), int(data.get("at_ns", 0))
    # 入れ子の深いファイルの RecursionError も「読めない」と同じ (捕まえないと dispatch() の
    # 外まで抜け、__main__ の最終防波堤が検証をスキップする)。UTF-8 でないファイルの
    # UnicodeDecodeError は ValueError に含まれる。
    except (ValueError, TypeError, AttributeError, OSError, RecursionError):
        return 0, 0


def current_epoch(service_name: str) -> int:
    """service の現在の epoch。一度も無効化されていない (epoch ファイルが無い / 読めない) なら 0。"""
    return _read_epoch(service_name)[0]


def _in_flight(tombstone_ns: int) -> bool:
    """切替検出 (tombstone) から IN_FLIGHT_SEC 以内なら True (成功 cache を書かない)。"""
    if not tombstone_ns:
        return False
    return time.time_ns() - tombstone_ns < IN_FLIGHT_SEC * 1_000_000_000


def get_success(
    service_name: str,
    project_dir: str,
    expected,
    accounts_mtime: float,
    inline_env=None,
    context=None,
    identity_env=None,
) -> bool:
    """検証成功が短期キャッシュにあれば True を返す。

    inline_env (コマンド行頭の `AWS_PROFILE=...` 等)、context (コマンドの
    `--profile` / `--project` / `--context` 等)、identity_env (hook プロセスの env の
    うちアカウントを決める変数) が異なれば検証結果も変わりうるため、すべてキーに
    含める。差で別エントリになり、profile A の成功が profile B で誤って allow される
    ことを防ぐ。
    entry の epoch が現在の epoch と違えば (書かれた後に切替が検出された) 無視する。
    """
    path = _cache_path(
        _cache_key(
            service_name, project_dir, expected, inline_env, context, identity_env
        )
    )
    # stat できない entry も cache miss (`Path.is_file()` ではなく `os.path.isfile`。`_read_epoch`)。
    if path is None or not os.path.isfile(path):
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # 読めない entry は cache miss (検証し直す)。JSON の破損に加えて、UTF-8 でない
    # (UnicodeDecodeError。ValueError に含まれる) / 入れ子が深い (RecursionError) ファイルも
    # 同じ。捕まえないと dispatch() の外まで抜け、__main__ の最終防波堤が検証をスキップする。
    except (ValueError, OSError, RecursionError):
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
    context=None,
    epoch: int | None = None,
    identity_env=None,
) -> bool:
    """検証成功をキャッシュする。書けたら True。

    epoch は verify **開始**時点の `current_epoch()`。開始後に `invalidate()` で
    epoch が進んでいたら (切替 hook と並行した検証 = 旧状態を見ている可能性) 書かずに
    False を返す。None なら現在の epoch で書く (後方互換)。
    切替検出 (tombstone) から IN_FLIGHT_SEC 以内も書かない (切替の実行中とみなす)。
    identity_env はキーにだけ使い、entry には書かない (`get_success` と同じ値を渡す)。
    書き込み失敗は無視 (キャッシュはベストエフォート)。
    """
    path = _cache_path(
        _cache_key(
            service_name, project_dir, expected, inline_env, context, identity_env
        )
    )
    if path is None:
        return False
    epoch_now, tombstone_ns = _read_epoch(service_name)
    if epoch is None:
        epoch = epoch_now
    elif epoch != epoch_now:
        return False
    if _in_flight(tombstone_ns):
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
    """service の epoch を進め、切替検出時刻 (tombstone) を記録し、成功 cache を
    project_dir / expected / inline env を問わず全て破棄する。

    epoch / tombstone を先に書いてから削除する: 削除と並行して旧 epoch で書かれた /
    残った entry も epoch 不一致で無視される。epoch は `max(現在 + 1, time_ns)` で、
    epoch ファイルが消されても単調増加を保つ (counter だけだと再出発した値が古い
    entry と一致しうる)。破棄した entry 数を返す (削除失敗・cache dir 不在は 0 扱いで
    例外にしない)。
    """
    base = _cache_dir()
    if base is None:
        return 0
    tag = _service_tag(service_name)
    now_ns = time.time_ns()
    new_epoch = max(current_epoch(service_name) + 1, now_ns)
    try:
        _write_atomic(
            base / f"{tag}.epoch",
            json.dumps({"epoch": new_epoch, "at_ns": now_ns}),
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
