"""Stop hook の session 単位 once-only 化 (0.19.0, bd_092a232e-snw.2)。

Stop hook は ``stop_hook_active`` しか見ず「報告済みかどうか」の状態を持たなかった
ため、tracked 機密ファイルを「意図的に管理対象とする」と承認しても、次ターン
以降も同じ file set で毎回 block し続けていた (Next.js 慣例の ``.env`` commit /
committed CA 証明書 / direnv の ``.envrc`` など「tracked が正」な repo で 0.14.0
離脱と同型の体験)。

ここでは **session 単位** で「報告済みの (status, path) 集合」を HOME 側に記録し、
現在の集合が報告済み集合の部分集合なら block を省略する。新しいファイルが増えた
(または untracked → tracked のように status が変わった) ときだけ再 block する。

- 置き場: ``~/.claude/sensitive-files-guardrail/stop-ack/<session_id>``。
  ``patterns.local.txt`` と同じ ``Path.home()`` 基準 (plugin cache の更新で
  消えず、テストは ``HOME`` 差し替えで隔離できる)。
- 内容: 1 行 1 エントリの sha256 hex digest (``"<cwd>\\t<status>\\t<path>"`` の
  digest)。path を平文で HOME 側に残さないため (ログ規則と同じ方針)。``cwd`` を
  含めるのは、同一 session 内で別 repo に ``cd`` したとき同じ相対 path
  (``tracked\\t.env``) を報告済みと誤認しないため (Stop のスキャン自体が
  ``git ls-files`` の cwd 相対なので、scope も cwd に揃える)。
- 失敗 (読取 / 書込 / mkdir 不能 / 壊れた内容) は全て「状態なし」扱い
  (= 従来通り block)。state 機構の不具合で block が **消える** 方向には倒さない。
- 古い session ファイルは書込み時に best-effort で GC する (TTL 7 日)。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Iterable

_STATE_SUBPATH = Path(".claude") / "sensitive-files-guardrail" / "stop-ack"

# session_id に許す文字種 (ファイル名にそのまま使うため)。Claude Code の
# session_id は UUID だが hook input は外部入力なので、path 区切り / ``..`` /
# dotfile を弾く (先頭は英数字のみ)。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

# 1 行 1 digest (sha256 hex)。これ以外の行は壊れた state として無視する。
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# 古い session の state を書込み時に掃除する TTL (秒)。session は日に何度も
# 作られるため、放置すると小ファイルが無限に溜まる。
_GC_TTL_SECONDS = 7 * 24 * 60 * 60


def resolve_state_dir() -> Path:
    """state ディレクトリ ``~/.claude/sensitive-files-guardrail/stop-ack`` を返す。

    ``_shared.patterns._resolve_local_patterns_path`` と同じ ``Path.home()`` 基準。
    """
    return Path.home() / _STATE_SUBPATH


def sanitize_session_id(raw: object) -> str | None:
    """hook input の ``session_id`` をファイル名として安全な形に検証する。

    str でない / 空 / 許可外文字 (path 区切り等) / 長すぎる / 先頭 ``.`` の
    いずれかなら None (= session 状態を使わず従来通り毎回 block)。
    """
    if not isinstance(raw, str):
        return None
    if not _SESSION_ID_RE.match(raw):
        return None
    return raw


def digest_entries(entries: Iterable[dict], scope: str = "") -> set[str]:
    """``find_sensitive_files`` の戻り値を
    ``{sha256("<scope>\\t<status>\\t<path>")}`` に畳む。

    path を平文で HOME 側に残さないため digest 化する。status を含めるのは
    untracked → tracked (``git rm --cached`` が必要になる) のような変化を
    「新しい事象」として再 block するため。``scope`` (呼出側は正規化した cwd)
    を含めるのは、同一 session 内で別 repo に ``cd`` したとき同じ相対 path を
    報告済みと誤認しないため。
    """
    digests: set[str] = set()
    for entry in entries:
        key = f"{scope}\t{entry.get('status', '')}\t{entry.get('path', '')}"
        digests.add(hashlib.sha256(key.encode("utf-8")).hexdigest())
    return digests


def _state_file(session_id: str) -> Path:
    return resolve_state_dir() / session_id


def load_acked(session_id: str) -> set[str]:
    """session の報告済み digest 集合を読む。

    不在 / 読取失敗 / decode 失敗は空集合。digest 形式でない行は無視する。
    """
    try:
        text = _state_file(session_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    acked: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if _DIGEST_RE.match(stripped):
            acked.add(stripped)
    return acked


def save_acked(session_id: str, digests: set[str]) -> None:
    """session の報告済み digest 集合を書く (best-effort、失敗は黙殺)。

    書込み失敗は「次回も block する」方向にしか倒れないので hook の結果には
    影響させない。一時ファイル経由の ``os.replace`` で途中状態を読ませない。
    書込み後に古い session ファイルを GC する。
    """
    state_dir = resolve_state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = state_dir / f".{session_id}.tmp"
        tmp.write_text(
            "".join(f"{d}\n" for d in sorted(digests)), encoding="utf-8"
        )
        os.replace(tmp, _state_file(session_id))
    except OSError:
        return
    _gc_stale(state_dir, keep=session_id)


def _gc_stale(state_dir: Path, keep: str, now: float | None = None) -> None:
    """TTL を過ぎた session state を削除する (best-effort、失敗は黙殺)。

    ``keep`` (今書いた session) は mtime に関わらず残す。``now`` はテスト用。
    """
    threshold = (now if now is not None else time.time()) - _GC_TTL_SECONDS
    try:
        entries = list(state_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == keep:
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < threshold:
                entry.unlink()
        except OSError:
            continue
