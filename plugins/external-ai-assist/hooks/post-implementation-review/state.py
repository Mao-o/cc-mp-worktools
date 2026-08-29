"""post-implementation-review の状態管理 (pending / in-flight / reviewed) と排他制御。

## 状態機械

このセッションが変更したパスは PostToolUse で `pending` に積まれ、Stop で
`in-flight` へ**原子的に予約**され、レビュー結果を配信できた時点で消える。

    PostToolUse            Stop (claim)              Stop (complete)
    ------------           ------------              ---------------
    pending += path   ->   in-flight[cid] = paths -> in-flight から削除
                           pending = {}              reviewed[path] = hash

    cursor 失敗時: restore_claim() で pending へ戻す (未レビューのため)
    kill された時: in-flight が残る -> TTL 超過を後続 Stop が pending へ回収

**削除ではなく in-flight 予約にする理由**: 割り込みでユーザーが次メッセージを送ると
Stop hook はプロセスごと落ちる。単純な drain (読み出し + 削除) だとその瞬間にパスが
消えて永久に未レビューになる。in-flight のまま残し、claim 時刻が TTL を超えたものを
後続の Stop が回収すれば kill されても取りこぼさない。

**UserPromptSubmit でリセットしない理由**: バックグラウンドで走り続ける Stop hook と
競合する。公式 claude-plugins-official/security-guidance も同じ race を踏んで
UPS リセットを TTL ベースに置き換えている (hooks/diffstate.py の
"Replaces the UPS-reset that raced against background Stop.")。drain-at-Stop なら
ターン境界が「前回 Stop が消費した時点」で定義されるので競合しない。

## ロックは 2 種類あり、決してネストしたまま cursor を回さない

- **state lock**: 状態ファイルの read-modify-write のみ (`_common.flock.locked_file`)。
  常に短時間で解放する
- **cursor lock**: cwd をキーに `cursor agent` を直列化する。review() 実行中ずっと保持
  (非ブロッキング + fail-open 分岐が固有なので `_common` に寄せていない)

Stop の取得順は cursor lock -> state lock -> (state 解放) -> review。state lock を
握ったまま review すると、全セッションの PostToolUse が cursor の timeout 上限 (600 秒) まで
ブロックされる。
PostToolUse は state lock しか取らないため、この順序で循環待ちは発生しない。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from contextlib import contextmanager

from _common import flock

import cursor

# in-flight の回収 TTL。cursor の timeout を**必ず超える**必要がある。下回ると、
# 正常に走っている in-flight を別セッションの Stop が途中で横取りし、同じ diff で
# cursor が二重起動する。推奨値ではなく正しさの制約なので、cursor 側から導出して
# 手で乖離できないようにしている。
#
# 導出元は既定値 (`TIMEOUT_SEC`) ではなく**上限** (`MAX_TIMEOUT_SEC`)。0.6.0 で
# `EXTERNAL_AI_POST_REVIEW_TIMEOUT` により timeout が可変になったため、既定値から
# 導くと「短く設定したセッションが、長く設定した別セッションの in-flight を
# TTL 超過とみなして奪う」経路ができる。TTL は全セッションで同じ値でなければならない。
IN_FLIGHT_TTL_SEC = cursor.MAX_TIMEOUT_SEC + 300

# 状態ファイル自体の GC TTL (mtime 基準)。書き込みのたびに mtime が更新されるため、
# 稼働中セッションのファイルが消えることはない。
STATE_TTL_SEC = 48 * 3600

# Bash スナップショットの GC TTL。state より大幅に短くする。
# スナップショットは「対応する PostToolUse が pop するまで」しか意味を持たないが、
# Bash が実行されなかった場合 (permission 拒否 / 別 hook の block / 中断) は
# PostToolUse が来ず孤児になる。長い TTL で抱えても得が無いうえ、大きな repo では
# 1 件あたり最大 MAX_SNAPSHOT_ENTRIES 件のエントリを持つため嵩む。
BASH_SNAPSHOT_TTL_SEC = 3600

MAX_PENDING_PATHS = 200
MAX_REVIEWED_ENTRIES = 500

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]")


def _tmp_root() -> str:
    return os.environ.get("TMPDIR") or "/tmp"


def state_root() -> str:
    return os.path.join(_tmp_root(), "post-implementation-review")


def _safe(key: str) -> str:
    return _SAFE_KEY.sub("_", key)[:80] or "unknown"


def _state_path(session_id: str) -> str:
    return os.path.join(state_root(), "state", f"{_safe(session_id)}.json")


def _bash_snapshot_path(session_id: str, tool_use_id: str) -> str:
    name = f"{_safe(session_id)}__{_safe(tool_use_id)}.json"
    return os.path.join(state_root(), "bashsnap", name)


def review_copy_path(session_id: str) -> str:
    return os.path.join(state_root(), "reviews", f"{_safe(session_id)[:16]}.txt")


def _empty_state() -> dict:
    return {"v": 1, "pending": {}, "in_flight": {}, "reviewed": {}, "last_review_at": 0.0}


def _normalize(raw) -> dict:
    """壊れた/古い状態ファイルを黙って捨てて空状態に戻す (fail-open)。

    `_empty_state()` の**全キー**を引き継ぐこと。dict のキーだけをループしていた
    0.5.0 の形のままスカラーを足すと、読むたびに `last_review_at` が 0 に戻り
    cooldown が永久に効かない。
    """
    if not isinstance(raw, dict):
        return _empty_state()
    state = _empty_state()
    for key in ("pending", "in_flight", "reviewed"):
        value = raw.get(key)
        if isinstance(value, dict):
            state[key] = value
    stamp = raw.get("last_review_at")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        state["last_review_at"] = float(stamp)
    return state


@contextmanager
def _locked_state(session_id: str):
    """状態ファイルを flock 下で開き、mutate した結果を書き戻す。

    yield された dict をそのまま書き戻すため、呼び出し側は dict を直接編集してよい。
    """
    with flock.locked_file(_state_path(session_id)) as f:
        content = flock.read_all(f)
        try:
            state = _normalize(json.loads(content) if content.strip() else None)
        except (json.JSONDecodeError, ValueError):
            state = _empty_state()
        yield state
        flock.rewrite(f, json.dumps(state, ensure_ascii=False))


def _pending_head(value) -> str | None:
    """pending エントリ (新形式 dict {"at":.., "head":..} または旧形式の bare
    timestamp) から記録済み HEAD SHA を取り出す。旧形式には無いので None
    (Stop 側は現行 HEAD にフォールバックする。bd_092a232e-zh5.16)。
    """
    if isinstance(value, dict):
        head = value.get("head")
        return head if isinstance(head, str) and head else None
    return None


def record_pending(
    session_id: str, paths: list[str], heads: dict[str, str | None] | None = None
) -> int:
    """PostToolUse から呼ぶ。このセッションが変更した絶対パスを pending に積む。

    作業ツリー内かどうかの判定はここでは行わない。git を叩かずに済ませて
    PostToolUse を軽く保ち、除外は claim 時 (Stop) にまとめて行う。

    順序 (dict の挿入順) がレビュー順になる。上限超過は **末尾 (新しい方) から** 落とす:
    先頭は前回 Stop が繰り越した (pending に戻した) パスで、ここを落とすと予算超過で
    繰り越されたファイルが大量編集のターンで黙って消える。

    `heads` は各パスの「pending に入った時点の HEAD SHA」(bd_092a232e-zh5.16。
    絶対パス→SHA。指定の無いパスは None = Stop 側で現行 HEAD にフォールバック)。
    既に pending に居るパスの記録は上書きしない (`setdefault`) — 最初にこの
    ウィンドウへ入った時点の HEAD を保つ。carry-over (`__main__._run_review` の
    繰り越し) は元 claim が持っていた heads をそのまま渡すこと。ここで新しい
    HEAD へ差し替えると、繰り越すたびに基点が現行 HEAD に近づいていき
    「commit 済みで diff が消える」バグを再導入してしまう。
    """
    if not paths:
        return 0
    heads = heads or {}
    try:
        with _locked_state(session_id) as state:
            pending = state["pending"]
            now = time.time()
            for p in paths:
                pending.setdefault(p, {"at": now, "head": heads.get(p)})
            for newest in list(pending)[MAX_PENDING_PATHS:]:
                del pending[newest]
            return len(pending)
    except OSError:
        return 0


def claim_pending(session_id: str) -> tuple[str, list[str]] | None:
    """pending を in-flight へ原子的に遷移させ (claim_id, paths) を返す。

    同時に TTL 超過の in-flight を pending へ回収する (kill されたレビューの取りこぼし
    防止)。pending が空なら None を返す — 呼び出し側はレビューを走らせないこと。

    **state ファイルが一度も無いセッションは開かずに None を返す**
    (bd_092a232e-zh5.11)。`_locked_state` は `flock.locked_file` を経由し `a+` で
    開くため、呼ぶだけで空の state ファイルが生成され、かつ末尾で必ず書き戻す
    (`_locked_state` の docstring参照)。編集 0 件のターンでも Stop は毎回
    `claim_pending()` を呼ぶため、これが $TMPDIR に「編集ゼロの空 state」を
    セッション数だけ溜め込んでいた実測 (19 セッション分) の主因だった。
    ファイルが既に存在するセッションは今までどおり開いて処理する
    (TTL 回収や claim が必要な可能性があるため)。

    **返り値の tuple 形状は変えていない** (呼び出し側の 2-tuple unpack を壊さない
    ため)。pending 記録時点の HEAD (bd_092a232e-zh5.16) は in-flight エントリに
    `heads` として同居させ、`claim_heads()` で別途取得する。
    """
    if not os.path.exists(_state_path(session_id)):
        return None
    try:
        with _locked_state(session_id) as state:
            now = time.time()
            for cid, entry in list(state["in_flight"].items()):
                if not isinstance(entry, dict):
                    del state["in_flight"][cid]
                    continue
                if now - float(entry.get("at") or 0) > IN_FLIGHT_TTL_SEC:
                    reclaimed_heads = entry.get("heads") or {}
                    for p in entry.get("paths") or []:
                        state["pending"].setdefault(
                            p, {"at": now, "head": reclaimed_heads.get(p)}
                        )
                    del state["in_flight"][cid]

            if not state["pending"]:
                return None

            pending = state["pending"]
            paths = list(pending.keys())
            heads = {p: _pending_head(pending[p]) for p in paths}
            claim_id = uuid.uuid4().hex
            state["in_flight"][claim_id] = {"at": now, "paths": paths, "heads": heads}
            state["pending"] = {}
            return claim_id, paths
    except OSError:
        return None


def claim_heads(session_id: str, claim_id: str) -> dict[str, str | None]:
    """`claim_pending()` が確保した claim の pending-記録時 HEAD SHA を取得する。

    2-tuple の返り値を変えずに済ませるため、heads は in-flight エントリに
    同居させて別途読み出す (bd_092a232e-zh5.16)。claim_id が見つからない
    (既に complete/restore 済み等) 場合は空 dict。
    """
    try:
        with _locked_state(session_id) as state:
            entry = state["in_flight"].get(claim_id)
            if not isinstance(entry, dict):
                return {}
            heads = entry.get("heads")
            return dict(heads) if isinstance(heads, dict) else {}
    except OSError:
        return {}


def complete_claim(session_id: str, claim_id: str, hashes: dict[str, str]) -> None:
    """レビュー結果を配信できた (block / REVIEW_CLEAN) 時に呼ぶ。

    in-flight を削除し、パスごとの diff hash を記録する。**cursor 失敗時に呼んでは
    いけない** — 記録すると未レビューの変更が「レビュー済み」扱いで永久に skip される。
    """
    try:
        with _locked_state(session_id) as state:
            state["in_flight"].pop(claim_id, None)
            reviewed = state["reviewed"]
            for path, digest in hashes.items():
                reviewed.pop(path, None)  # 挿入順を更新して LRU として使う
                reviewed[path] = digest
            if len(reviewed) > MAX_REVIEWED_ENTRIES:
                for stale in list(reviewed)[: len(reviewed) - MAX_REVIEWED_ENTRIES]:
                    del reviewed[stale]
    except OSError:
        pass


def restore_claim(
    session_id: str,
    claim_id: str,
    paths: list[str],
    heads: dict[str, str | None] | None = None,
) -> None:
    """cursor.review() が失敗した時に呼ぶ。渡したパスだけを pending へ戻す。

    claim entry ごと削除するので、レビューに載せなかったパス (作業ツリー外・diff 空・
    前回と同一 hash) は復元されずそのまま消える。これが「REVIEW_CLEAN の後に同じ
    ファイルを再レビューしない」と「cursor 失敗の後に再レビューする」を両立させる。

    `heads` は claim 時点の記録 (`claim_heads()` で取得したもの。
    bd_092a232e-zh5.16)。省略時は全パス None (SHA なし = 現行 HEAD にフォールバック)
    として戻す。
    """
    heads = heads or {}
    try:
        with _locked_state(session_id) as state:
            state["in_flight"].pop(claim_id, None)
            now = time.time()
            for p in paths:
                state["pending"].setdefault(p, {"at": now, "head": heads.get(p)})
    except OSError:
        pass


def reviewed_hashes(session_id: str) -> dict[str, str]:
    try:
        with _locked_state(session_id) as state:
            return dict(state["reviewed"])
    except OSError:
        return {}


def pending_count(session_id: str) -> int:
    """claim せずに pending 件数だけ読む (cooldown 判定で「黙って skip」を避けるため)。

    state ファイルが一度も無ければ 0 (開かない。bd_092a232e-zh5.11 — claim_pending
    と同じ「呼ぶだけでファイルが生成される」問題への対処)。
    """
    if not os.path.exists(_state_path(session_id)):
        return 0
    try:
        with _locked_state(session_id) as state:
            return len(state["pending"])
    except OSError:
        return 0


def last_review_at(session_id: str) -> float:
    """直近で cursor を実際に走らせ終えた時刻 (epoch 秒)。未実施なら 0。

    state ファイルが一度も無ければ 0 (開かない。bd_092a232e-zh5.11)。
    """
    if not os.path.exists(_state_path(session_id)):
        return 0.0
    try:
        with _locked_state(session_id) as state:
            return float(state.get("last_review_at") or 0.0)
    except OSError:
        return 0.0


def mark_review_done(session_id: str) -> None:
    """cursor の実行が終わった時点で呼ぶ (成功・失敗を問わない)。

    cooldown は「レビューとレビューの間隔」なので**完了時刻**を基準にする。開始時刻に
    すると、10 分かかったレビューの直後に次のレビューが走ってしまう。セッション単位で
    持つ (状態ファイルがセッション単位。cursor lock だけが作業ツリー単位)。
    """
    try:
        with _locked_state(session_id) as state:
            state["last_review_at"] = time.time()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Bash 経由の変更を拾うための git status スナップショット
# --------------------------------------------------------------------------


def save_bash_snapshot(session_id: str, tool_use_id: str, snapshot: dict) -> None:
    path = _bash_snapshot_path(session_id, tool_use_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(snapshot, f)
    except OSError:
        pass


def pop_bash_snapshot(session_id: str, tool_use_id: str) -> dict | None:
    path = _bash_snapshot_path(session_id, tool_use_id)
    try:
        with open(path) as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return snapshot if isinstance(snapshot, dict) else None


# --------------------------------------------------------------------------
# cursor agent の直列化ロック
# --------------------------------------------------------------------------


@contextmanager
def cursor_lock(cwd: str):
    """cwd をキーに cursor agent を直列化する。取得できなければ False を yield。

    **stale claim TTL を置いていないのは意図的**。flock はプロセス終了時に
    カーネルが解放するため、hook が kill されてもロックは残らない。TTL を足すと
    「まだ走っている cursor のロックを TTL 超過とみなして奪う」経路を自分で作ることに
    なる。残る穴は 1 つだけ: hook が SIGKILL されると cursor の子プロセスが孤児として
    生き残りうる。その場合ロックだけ先に解放される (実害は cursor の一時的な二重起動)。
    """
    key = _safe_cwd_key(cwd)
    path = os.path.join(state_root(), "locks", f"cursor-{key}.lock")

    # yield は「ロックファイルを開けなかった経路」と「通常経路」で排他的に 1 回ずつ。
    # with 本体が OSError を投げた時 (stdout が閉じた BrokenPipeError 等) に
    # 2 回目の yield へ落ちると RuntimeError にすり替わるため、両者を try でまとめない。
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+")
    except OSError:
        # ロックファイルすら作れない環境では直列化を諦めて先へ進む (fail-open)
        yield True
        return

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except OSError:
        acquired = False

    if acquired:
        # GC がロック保持中のファイルを消して inode が分岐するのを防ぐため mtime を更新
        try:
            os.utime(path, None)
        except OSError:
            pass

    try:
        yield acquired
    finally:
        try:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


def _safe_cwd_key(cwd: str) -> str:
    import hashlib

    real = os.path.realpath(cwd or ".")
    return hashlib.sha256(real.encode()).hexdigest()[:16]
