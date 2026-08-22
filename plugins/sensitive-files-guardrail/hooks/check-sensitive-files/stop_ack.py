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
- 内容: 1 行 1 エントリの sha256 hex digest
  (``"<repo root>\\t<status>\\t<root 相対 path>"`` の digest)。path を平文で HOME
  側に残さないため (ログ規則と同じ方針)。repo root を scope に含めるのは、同一
  session 内で別 repo に ``cd`` したとき同じ相対 path (``tracked\\t.env``) を
  報告済みと誤認しないため。path は ``git rev-parse --show-prefix`` (cwd の root
  からの相対) を前置して root 相対に正規化する — ``git ls-files`` は cwd 相対で
  出力するため、root とサブディレクトリで同じ物理ファイルが別 digest になり
  ``cd`` 後に once-only が効かなくなるのを防ぐ (Codex R2 P2-2)。
- 失敗 (読取 / 書込 / mkdir 不能 / 壊れた内容) は全て「状態なし」扱い
  (= 従来通り block)。state 機構の不具合で block が **消える** 方向には倒さない。
- 古い session ファイルは書込み時に best-effort で GC する (最後の block から
  7 日。内容が digest 行だけの「自分が書いた形」のファイルのみ削除)。
- 読取 / 書込失敗は ``warn`` callback で stderr に可視化する (判定は block 側)。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable

_STATE_SUBPATH = Path(".claude") / "sensitive-files-guardrail" / "stop-ack"

# session_id に許す文字種 (ファイル名にそのまま使うため)。Claude Code の
# session_id は UUID だが hook input は外部入力なので、path 区切り / ``..`` /
# dotfile を弾く (先頭は英数字のみ)。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

# 1 行 1 digest (sha256 hex)。これ以外の行は壊れた state として無視する。
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# GC で削除してよい「自分が書いた state」と見なす上限サイズ。digest 64 文字 ×
# 数百行でも数十 KB に収まる。これを超えるファイルは他人のものとして触らない。
_STATE_FILE_MAX_BYTES = 64 * 1024

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


def digest_entries(
    entries: Iterable[dict], scope: str = "", prefix: str = ""
) -> set[str]:
    """``find_sensitive_files`` の戻り値を
    ``{sha256("<scope>\\t<status>\\t<prefix><path>")}`` に畳む。

    path を平文で HOME 側に残さないため digest 化する。status を含めるのは
    untracked → tracked (``git rm --cached`` が必要になる) のような変化を
    「新しい事象」として再 block するため。``scope`` (呼出側は repo root) を
    含めるのは、同一 session 内で別 repo に ``cd`` したとき同じ相対 path を
    報告済みと誤認しないため。``prefix`` (cwd の repo root からの相対パス、
    ``git rev-parse --show-prefix``) を path に前置するのは、``git ls-files`` が
    cwd 相対で出力するため root とサブディレクトリで同じ物理ファイルが別 digest
    にならないようにするため (Codex R2 P2-2)。
    """
    digests: set[str] = set()
    for entry in entries:
        key = (
            f"{scope}\t{entry.get('status', '')}\t"
            f"{prefix}{entry.get('path', '')}"
        )
        digests.add(hashlib.sha256(key.encode("utf-8")).hexdigest())
    return digests


def _state_file(session_id: str) -> Path:
    return resolve_state_dir() / session_id


def load_acked(
    session_id: str, warn: Callable[[str], None] | None = None
) -> set[str]:
    """session の報告済み digest 集合を読む。

    不在は空集合 (通常の初回)。読取失敗 / decode 失敗も空集合だが ``warn`` に
    ``load:<ExcName>`` を渡して可視化する (判定は「状態なし」= block 側)。
    digest 形式でない行は無視する。
    """
    try:
        text = _state_file(session_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeDecodeError) as e:
        if warn is not None:
            warn(f"load:{type(e).__name__}")
        return set()
    acked: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if _DIGEST_RE.match(stripped):
            acked.add(stripped)
    return acked


def save_acked(
    session_id: str,
    digests: set[str],
    warn: Callable[[str], None] | None = None,
) -> None:
    """session の報告済み digest 集合を書く (best-effort)。

    書込み失敗は「次回も block する」方向にしか倒れないので hook の結果には
    影響させないが、``warn`` に ``save:<ExcName>`` を渡して可視化する。一時
    ファイル経由の ``os.replace`` で途中状態を読ませない。書込み後に古い
    session ファイルを GC する。
    """
    state_dir = resolve_state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = state_dir / f".{session_id}.tmp"
        tmp.write_text(
            "".join(f"{d}\n" for d in sorted(digests)), encoding="utf-8"
        )
        os.replace(tmp, _state_file(session_id))
    except OSError as e:
        if warn is not None:
            warn(f"save:{type(e).__name__}")
        return
    _gc_stale(state_dir, keep=session_id)


def _looks_like_state_file(path: Path) -> bool:
    """``save_acked`` が書いた形 (小さく、全行が digest) か。

    GC は「自分が書いたもの」だけを消す。名前は session_id 形式なら何でも通る
    (英数字 + ``._-`` だけの名前は珍しくない) ので、内容で判定する。空ファイル
    (書込み途中の ``.tmp`` 等) は state 扱い。
    """
    try:
        if path.stat().st_size > _STATE_FILE_MAX_BYTES:
            return False
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return all(
        _DIGEST_RE.match(line.strip())
        for line in text.splitlines()
        if line.strip()
    )


def _gc_stale(state_dir: Path, keep: str, now: float | None = None) -> None:
    """TTL を過ぎた session state を削除する (best-effort、失敗は黙殺)。

    ``keep`` (今書いた session) は mtime に関わらず残す。削除対象は
    ``_looks_like_state_file`` が True の regular file のみ (同じ dir に誰かが
    置いた別ファイルは触らない)。``now`` はテスト用。
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
            if (
                entry.is_file()
                and entry.stat().st_mtime < threshold
                and _looks_like_state_file(entry)
            ):
                entry.unlink()
        except OSError:
            continue
