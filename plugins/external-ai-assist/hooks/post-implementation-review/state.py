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

0.12.0 から **PostToolUse(Bash) も commit を含む窓では cursor lock を取る**。それでも
循環待ちは起きない: `__main__._handle_bash` は state lock を**解放してから** cursor lock を
非ブロッキング (try-lock) で取るため、hold-and-wait が成立しない。
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

import selection

# in-flight の回収 TTL。レビュー backend の timeout を**必ず超える**必要がある。下回ると、
# 正常に走っている in-flight を別セッションの Stop が途中で横取りし、同じ diff で
# 外部 AI CLI が二重起動する。推奨値ではなく正しさの制約なので、backend 側から導出して
# 手で乖離できないようにしている。
#
# 導出元は既定値 (`TIMEOUT_SEC`) ではなく**上限** (`MAX_TIMEOUT_SEC`)。0.6.0 で
# `EXTERNAL_AI_POST_REVIEW_TIMEOUT` により timeout が可変になったため、既定値から
# 導くと「短く設定したセッションが、長く設定した別セッションの in-flight を
# TTL 超過とみなして奪う」経路ができる。TTL は全セッションで同じ値でなければならない。
#
# 0.12.0 で導出元を cursor 単体から **全 backend の上限の最大**
# (`selection.MAX_TOTAL_TIMEOUT_SEC`) に変えた。TTL は「どの backend を選んだセッション
# でも同じ値」でなければならないので、cursor だけを見ていると、上限の違う backend を
# 使うセッションが現れた時点で横取りが起きる (現状は cursor と codex が同じ 600 秒
# なので値は 900 秒のまま変わらない)。
IN_FLIGHT_TTL_SEC = selection.MAX_TOTAL_TIMEOUT_SEC + 300

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

# 内容指紋 (`fingerprints`) の保持数。pending と同じ枠にしてあるのは、指紋が意味を
# 持つのは pending ∪ in-flight にあるパスだけで、それ以外は `record_fingerprints` の
# 刈り取りで落ちるため (pending より多く持つ理由が無い)。
MAX_FINGERPRINT_ENTRIES = MAX_PENDING_PATHS

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


def _bash_reflog_path(session_id: str, tool_use_id: str) -> str:
    """commit 検出用の reflog スナップショット (`reflog.py` の窓の起点)。

    `git status` のスナップショットと**別ファイル**にしてあるのは、両者の失敗条件が
    独立しているため: 巨大な作業ツリー (`MAX_SNAPSHOT_ENTRIES` 超過) や `git status`
    の timeout で status 側が保存できなくても、commit 検出は成立させたい。
    同じ `bashsnap/` 配下なので GC の短い TTL (`BASH_SNAPSHOT_TTL_SEC`) がそのまま効く。
    """
    name = f"{_safe(session_id)}__{_safe(tool_use_id)}.reflog.json"
    return os.path.join(state_root(), "bashsnap", name)


def review_copy_path(session_id: str) -> str:
    return os.path.join(state_root(), "reviews", f"{_safe(session_id)[:16]}.txt")


def _empty_state() -> dict:
    return {
        "v": 1,
        "pending": {},
        "in_flight": {},
        "reviewed": {},
        "fingerprints": {},
        "last_review_at": 0.0,
        "last_backend": "",
    }


def _normalize(raw) -> dict:
    """壊れた/古い状態ファイルを黙って捨てて空状態に戻す (fail-open)。

    `_empty_state()` の**全キー**を引き継ぐこと。dict のキーだけをループしていた
    0.5.0 の形のままスカラーを足すと、読むたびに `last_review_at` が 0 に戻り
    cooldown が永久に効かない (`last_backend` も同じ壊れ方をする — 読むたびに空へ
    戻ると `alternate` 戦略が毎回「前回不明」になり、常に列挙順の先頭を選んでしまう)。
    """
    if not isinstance(raw, dict):
        return _empty_state()
    state = _empty_state()
    for key in ("pending", "in_flight", "reviewed", "fingerprints"):
        value = raw.get(key)
        if isinstance(value, dict):
            state[key] = value
    stamp = raw.get("last_review_at")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        state["last_review_at"] = float(stamp)
    backend = raw.get("last_backend")
    if isinstance(backend, str):
        state["last_backend"] = backend
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


def record_pending(session_id: str, paths: list[str]) -> int:
    """PostToolUse から呼ぶ。このセッションが変更した絶対パスを pending に積む。

    作業ツリー内かどうかの判定はここでは行わない。git を叩かずに済ませて
    PostToolUse を軽く保ち、除外は claim 時 (Stop) にまとめて行う。

    順序 (dict の挿入順) がレビュー順になる。上限超過は **末尾 (新しい方) から** 落とす:
    先頭は前回 Stop が繰り越した (pending に戻した) パスで、ここを落とすと予算超過で
    繰り越されたファイルが大量編集のターンで黙って消える。
    """
    if not paths:
        return 0
    try:
        with _locked_state(session_id) as state:
            pending = state["pending"]
            now = time.time()
            for p in paths:
                pending.setdefault(p, now)
            for newest in list(pending)[MAX_PENDING_PATHS:]:
                del pending[newest]
            return len(pending)
    except OSError:
        return 0


def claim_pending(session_id: str) -> tuple[str, list[str]] | None:
    """pending を in-flight へ原子的に遷移させ (claim_id, paths) を返す。

    同時に TTL 超過の in-flight を pending へ回収する (kill されたレビューの取りこぼし
    防止)。pending が空なら None を返す — 呼び出し側はレビューを走らせないこと。

    **state ファイルが一度も無いセッションは開かずに None を返す**。
    `_locked_state` は `flock.locked_file` を経由し `a+` で
    開くため、呼ぶだけで空の state ファイルが生成され、かつ末尾で必ず書き戻す
    (`_locked_state` の docstring参照)。編集 0 件のターンでも Stop は毎回
    `claim_pending()` を呼ぶため、これが $TMPDIR に「編集ゼロの空 state」を
    セッション数だけ溜め込んでいた実測 (19 セッション分) の主因だった。
    ファイルが既に存在するセッションは今までどおり開いて処理する
    (TTL 回収や claim が必要な可能性があるため)。
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
                    for p in entry.get("paths") or []:
                        state["pending"].setdefault(p, now)
                    del state["in_flight"][cid]

            if not state["pending"]:
                return None

            paths = list(state["pending"].keys())
            claim_id = uuid.uuid4().hex
            state["in_flight"][claim_id] = {"at": now, "paths": paths}
            state["pending"] = {}
            return claim_id, paths
    except OSError:
        return None


def complete_claim(session_id: str, claim_id: str, hashes: dict[str, str]) -> None:
    """レビュー結果を配信できた (block / REVIEW_CLEAN) 時に呼ぶ。

    in-flight を削除し、パスごとの diff hash を記録する。**cursor 失敗時に呼んでは
    いけない** — 記録すると未レビューの変更が「レビュー済み」扱いで永久に skip される。
    """
    try:
        with _locked_state(session_id) as state:
            entry = state["in_flight"].pop(claim_id, None)
            # レビューが終わったパスは pending ∪ in-flight から外れる = 指紋の意味が
            # 無くなる (commit レビューの P1 が見るのはこの集合だけ)。残すと state が
            # 膨らむだけなので、claim ごと落とす
            if isinstance(entry, dict):
                for path in entry.get("paths") or []:
                    if isinstance(path, str):
                        state["fingerprints"].pop(path, None)
            reviewed = state["reviewed"]
            for path, digest in hashes.items():
                reviewed.pop(path, None)  # 挿入順を更新して LRU として使う
                reviewed[path] = digest
            if len(reviewed) > MAX_REVIEWED_ENTRIES:
                for stale in list(reviewed)[: len(reviewed) - MAX_REVIEWED_ENTRIES]:
                    del reviewed[stale]
    except OSError:
        pass


def restore_claim(session_id: str, claim_id: str, paths: list[str]) -> None:
    """cursor.review() が失敗した時に呼ぶ。渡したパスだけを pending へ戻す。

    claim entry ごと削除するので、レビューに載せなかったパス (作業ツリー外・diff 空・
    前回と同一 hash) は復元されずそのまま消える。これが「REVIEW_CLEAN の後に同じ
    ファイルを再レビューしない」と「cursor 失敗の後に再レビューする」を両立させる。
    """
    try:
        with _locked_state(session_id) as state:
            state["in_flight"].pop(claim_id, None)
            now = time.time()
            for p in paths:
                state["pending"].setdefault(p, now)
    except OSError:
        pass


def recorded_paths(session_id: str) -> list[str]:
    """**このセッションが「今」未レビューの変更を抱えている**絶対パス (pending ∪ in-flight)。

    commit レビュー (`__main__._run_commit_review`) が I1' を満たすために使う集合。
    commit に入っていてもこの集合に無いパスは、**内容を一切送らずファイル名だけ通知**
    する — 「このセッションが編集した」と言えないパスの内容を外部へ出さないため
    (`git commit -a` は他セッション・人手の作業ツリー変更も巻き込む)。

    in-flight を含めるのは、Stop が claim 中 (レビュー実行中) に別の Bash が commit
    しても同じ結論になるようにするため。

    **`reviewed` は含めない (マージ前レビューの指摘)。** `reviewed` は「このセッションが
    過去に一度でも編集した」LRU 500 件のセッション全履歴で、Stop 経路では「hash が同じ
    なら再送しない」抑止にしか使われず**送信許可を与えていない**。ここに入れると
    「一度編集したパスは、以後セッション中ずっと誰が書いた内容でも送ってよい」に
    昇格してしまい、0.11.0 の Stop が送らない他者の内容を送る経路になる
    (再現: `tests/test_commit_flow.py::TestReviewedIsNotASendPermission`)。
    失うものは無い — Stop がレビューを終えて `reviewed` に移した内容は既に外部へ
    送って見てもらった後なので、同じ内容を commit 時にもう一度送る価値が無い。

    順序は pending → in-flight (重複は先勝ち)。state ファイルが一度も無ければ空
    (開かない — `claim_pending` と同じ理由)。
    """
    if not os.path.exists(_state_path(session_id)):
        return []
    try:
        with _locked_state(session_id) as state:
            ordered: dict[str, None] = {}
            for path in state["pending"]:
                ordered.setdefault(path, None)
            for entry in state["in_flight"].values():
                if not isinstance(entry, dict):
                    continue
                for path in entry.get("paths") or []:
                    if isinstance(path, str):
                        ordered.setdefault(path, None)
            return list(ordered)
    except OSError:
        return []


def drop_pending(session_id: str, paths: list[str]) -> None:
    """commit レビュー済みで、かつ未 commit の変更が残っていないパスを pending から外す。

    外さないと、Stop が同じパスを claim して `git diff HEAD` が空になっているのを見つけ、
    「差分が空で取得できませんでした」と**誤通知**する (実際には commit レビューで
    送信済み)。まだ未 commit の変更が残るパスは呼び出し側が渡さない = pending に残り、
    Stop が続きを見る。

    `reviewed` には書かない (**片側に倒した判断**): `reviewed` はパス → *HEAD 基準*
    diff の hash で、commit レビューが見たのは `old..new` の range diff なので値の
    意味が違う。空 diff の hash を入れても `_collect_diffs` は空 diff を hash 判定より
    手前で落とすため一度も参照されず、LRU (`MAX_REVIEWED_ENTRIES`) の枠を食って本物の
    エントリを追い出すだけになる。
    """
    if not paths:
        return
    try:
        with _locked_state(session_id) as state:
            for path in paths:
                state["pending"].pop(path, None)
                state["fingerprints"].pop(path, None)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 内容指紋 (commit レビューの P6, 0.12.0)
# --------------------------------------------------------------------------


def record_fingerprints(session_id: str, digests: dict[str, str | None]) -> None:
    """編集ツールが書いた直後のファイル内容の sha256 を、パスごとに記録する。

    値が `None` なら**消す** (1 MiB 超 / 読めない / 通常ファイルでない = 指紋なし)。
    「最後の編集が勝つ」ので同じパスへの再編集は上書きになる。指紋が無いパスは
    commit レビューで内容を送らない (`__main__` の P6) ため、消すことは常に
    送信範囲を狭める方向。

    記録のたびに **pending ∪ in-flight に無いキーを刈る**。指紋が意味を持つのは
    その集合のパスだけ (P1) で、残しても state が膨らむだけのため。さらに
    `MAX_FINGERPRINT_ENTRIES` を超えたら古い方から落とす — pending が末尾
    (新しい方) から落とすのと向きが逆なのは、指紋は「直前に書いた内容」の証拠で、
    古いものほど commit 待ちとして使われる可能性が低いため。
    """
    if not digests:
        return
    try:
        with _locked_state(session_id) as state:
            table = state["fingerprints"]
            for path, digest in digests.items():
                table.pop(path, None)  # 挿入順を更新 (上書きは最後尾へ)
                if digest:
                    table[path] = digest
            live = set(state["pending"])
            for entry in state["in_flight"].values():
                if isinstance(entry, dict):
                    live.update(p for p in (entry.get("paths") or []) if isinstance(p, str))
            for stale in [p for p in table if p not in live]:
                del table[stale]
            for oldest in list(table)[: max(0, len(table) - MAX_FINGERPRINT_ENTRIES)]:
                del table[oldest]
    except OSError:
        pass


def drop_fingerprints(session_id: str, paths: list[str]) -> None:
    """指定したパスの指紋を消す (Bash がそのファイルを書き換えたと分かったとき)。"""
    if not paths:
        return
    try:
        with _locked_state(session_id) as state:
            for path in paths:
                state["fingerprints"].pop(path, None)
    except OSError:
        pass


def fingerprints(session_id: str) -> dict[str, str]:
    """記録済みの内容指紋 (絶対パス -> sha256)。state ファイルが無ければ空。"""
    if not os.path.exists(_state_path(session_id)):
        return {}
    try:
        with _locked_state(session_id) as state:
            return {
                path: digest
                for path, digest in state["fingerprints"].items()
                if isinstance(path, str) and isinstance(digest, str) and digest
            }
    except OSError:
        return {}


def reviewed_hashes(session_id: str) -> dict[str, str]:
    try:
        with _locked_state(session_id) as state:
            return dict(state["reviewed"])
    except OSError:
        return {}


def pending_count(session_id: str) -> int:
    """claim せずに pending 件数だけ読む (cooldown 判定で「黙って skip」を避けるため)。

    state ファイルが一度も無ければ 0 (開かない — claim_pending
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

    state ファイルが一度も無ければ 0 (開かない)。
    """
    if not os.path.exists(_state_path(session_id)):
        return 0.0
    try:
        with _locked_state(session_id) as state:
            return float(state.get("last_review_at") or 0.0)
    except OSError:
        return 0.0


def last_backend(session_id: str) -> str:
    """このセッションで直近にレビュー結果を返した backend 名 (未記録なら空文字列)。

    `selection` の `alternate` 戦略が「前回と別」を選ぶための **1 値だけ**の記録。
    パス単位の記録も「レビュー済み hunk」の重複除去 state も持たない (ユーザー決定)。

    state ファイルが一度も無ければ空 (開かない — `claim_pending` と同じ理由)。
    """
    if not os.path.exists(_state_path(session_id)):
        return ""
    try:
        with _locked_state(session_id) as state:
            value = state.get("last_backend")
            return value if isinstance(value, str) else ""
    except OSError:
        return ""


def record_last_backend(session_id: str, name: str) -> None:
    """**レビュー結果を実際に返した** backend を記録する。

    失敗した backend は記録しない。記録してしまうと `alternate` が「失敗したほう」を
    避けるようになり、次のレビューで成功した backend と区別が付かなくなる —
    この値の意味は「前回どこにレビューさせたか」であって「前回どこが落ちたか」では
    ない (同じ行を 2 回見るときに別の目で見せる、が目的)。
    """
    if not name:
        return
    try:
        with _locked_state(session_id) as state:
            state["last_backend"] = name
    except OSError:
        pass


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
        flock.write_private(path, json.dumps(snapshot))
    except OSError:
        pass


def save_bash_reflog(session_id: str, tool_use_id: str, snapshot: dict) -> None:
    try:
        flock.write_private(_bash_reflog_path(session_id, tool_use_id), json.dumps(snapshot))
    except OSError:
        pass


def pop_bash_reflog(session_id: str, tool_use_id: str) -> dict | None:
    """保存した reflog スナップショットを取り出して消す (無ければ None = fail-closed)。"""
    return _pop_json(_bash_reflog_path(session_id, tool_use_id))


def pop_bash_snapshot(session_id: str, tool_use_id: str) -> dict | None:
    return _pop_json(_bash_snapshot_path(session_id, tool_use_id))


def _pop_json(path: str) -> dict | None:
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
