#!/usr/bin/env python3
"""PreToolUse(ExitPlanMode) hook: Cursor + Codex でプランをクロスレビューし、
セッション内で最大 MAX_REVIEWS 回ブロックする。

- Cursor: 既存コードベース整合観点 (primary)
- Codex: 要件・アーキ観点 (補完)

両方のレビュアーを並列実行し、critical な指摘があった場合のみ Claude に block を返す。
全レビュアーが REVIEW_CLEAN を返した場合、または全失敗の場合は fail-open (exit 0)。

マーカーの read→判定→write は flock で排他ロック (`_common.flock`)。
同一セッション並行起動時のカウント破綻を防ぐ。

## 0.6.0 で入れた「待たせ方」の制御

プラン 1 本あたりの承認前の待ちは最悪「各レビュアーの timeout」分 (並列なので最大側)。
0.5.0 は codex が 1500s だったのでこれだけで 25 分だった。次の 4 つで調整する:

| 環境変数 | 既定 | 効果 |
|---|---|---|
| `EXTERNAL_AI_PLAN_REVIEW` | `1` | この hook 自体の on/off |
| `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` | `600` | 両レビュアー共通の timeout (上限 1500) |
| `EXTERNAL_AI_PLAN_REVIEW_REVIEWERS` | 全件 | `cursor` / `codex` の選択 |
| `EXTERNAL_AI_PLAN_REVIEW_MODE` | `block` | `context` にすると差し戻さず所見だけ渡す |

`EXTERNAL_AI_REVIEW_MAX` は 0.2.0 からある名前なので温存し、`EXTERNAL_AI_PLAN_REVIEW`
と **AND** で効く (`=0` は従来どおり無効化スイッチ)。

**この上限が数えているのは「指摘ありで返ってきた回数」だけ**である点に注意。
指摘なし (`REVIEW_CLEAN`) とレビュアー失敗は `release_slot()` が枠を戻すので
カウントされない (`tests/test_settings_flow.py::TestWhatTheBudgetActuallyCaps`)。
つまりこれは**差し戻しの往復が無限に続くのを止める**ための上限であって、
セッション全体のレビュー回数や総待ち時間の上限ではない — プラン内容を変えて
`ExitPlanMode` を呼べば、上限に達していなくても新しいプランとして毎回レビューが走る。
総量を抑える手段は timeout / レビュアー選択 / hook 自体の無効化 (README 参照)。

`MODE=context` は公式の PreToolUse decision control フィールド
`hookSpecificOutput.additionalContext` ("String added to Claude's context alongside
the tool result." 逐語) を使う。ブロックしないぶん「差し戻し → プラン修正 →
再 ExitPlanMode で再度フルレビュー」の往復が消える (待ち時間が 1 ラウンド分になる)。

**`permissionDecision` は意図的に省く**。3 択のうち省略以外は採れない:

- `"allow"` は **ExitPlanMode の承認ゲートそのものを飛ばす**。この tool は「プランを
  利用者に見せて承認を取る」ための tool なので、hook が allow を返すと利用者が
  プランを見ないまま実装に入る。DX 改善のために承認を奪うのは本末転倒
  (docs が `"allow"` に `updatedInput` との組を要求しているのもこの経路)
- `"defer"` は docs に "Ignored when `permissionDecision` is `\"defer\"`" とあり、
  肝心の `additionalContext` が無視されるので目的を果たさない
- 省略すれば通常の承認フローが残ったまま所見だけが文脈に入る

ただし **「`permissionDecision` を省いて `additionalContext` だけ返す」形を直接
述べた記述は docs に無い** (2026-08 時点で明示的に探して不在を確認済み)。最も近いのは
"Other exit codes" の "Each field the event supports is honored, including
`permissionDecision`, `additionalContext`, `updatedInput`, and `systemMessage`" で、
これは根拠にはなるが保証ではない。仕様として断定しないこと。既定を `block` のままに
してあるのはこのため。

なお **PreToolUse の top-level `decision` / `reason` は docs 上 deprecated**
(`"block"` → `permissionDecision: "deny"` へのマッピングが明記されている)。既定の
block 経路は 0.2.0 からこの形で動いており、本 batch (DX 改善) の範囲を超えるため
移行していない — 別途起票する。PostToolUse / Stop の top-level `decision` / `reason`
は現行フォーマットのままなので、post-implementation-review 側は対象外。

exit 0 (JSON なし): ブロックしない (無効化 / レビュアー不在 / レビュー済み)
exit 0 + {"systemMessage": ...}: 所要時間と結果の要約のみ (ブロックしない)
exit 0 + {"decision": "block", "reason": ..., "systemMessage": ...}: 差し戻し
exit 0 + {"hookSpecificOutput": {..., "additionalContext": ...}}: MODE=context の所見注入
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# hooks/_common を解決するため、hook 内モジュールより先に hooks/ を sys.path に載せる
# (plugin root 内の相対配置なので ${CLAUDE_PLUGIN_ROOT} が cache コピーでも壊れない)。
_HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _common import flock, hooklog, notify, sentinel, settings  # noqa: E402

import codex  # noqa: E402
import cursor  # noqa: E402

REVIEWERS = [cursor, codex]

DEFAULT_MAX_REVIEWS = 2
_HEADERS = {
    "cursor": "## Cursor レビュー (既存コードベース整合観点)",
    "codex": "## Codex レビュー (要件・アーキ観点)",
}

ENV_ENABLED = "EXTERNAL_AI_PLAN_REVIEW"
ENV_MAX_REVIEWS = "EXTERNAL_AI_REVIEW_MAX"
ENV_REVIEWERS = "EXTERNAL_AI_PLAN_REVIEW_REVIEWERS"
ENV_MODE = "EXTERNAL_AI_PLAN_REVIEW_MODE"

MODE_BLOCK = "block"
MODE_CONTEXT = "context"

log = hooklog.make_logger("exitplan-review")

# フェンス / 装飾 / 「指摘なし」の前置き 1 文を許容する判定 (規則は _common/sentinel.py)
is_clean_review = sentinel.is_clean_review


def review_enabled() -> bool:
    """`EXTERNAL_AI_PLAN_REVIEW=0` で無効化 (0.6.0 で新設した正規のスイッチ)。

    `EXTERNAL_AI_REVIEW_MAX=0` との関係は **AND**。post-implementation-review 側の
    `EXTERNAL_AI_POST_REVIEW_MAX` は撤廃済みの死んだ別名なので「新しい変数が勝つ」で
    よいが、こちらの `EXTERNAL_AI_REVIEW_MAX` は**現役の回数予算**で `0` に
    「1 回も許さない」という固有の意味がある。新スイッチで上書きすると、
    README に載っている無効化手段が黙って効かなくなる。
    """
    return settings.flag(ENV_ENABLED, default=True)


def get_max_reviews() -> int:
    return settings.count(ENV_MAX_REVIEWS, DEFAULT_MAX_REVIEWS)


def get_mode() -> str:
    """`block` (既定・従来どおり差し戻す) か `context` (所見だけ渡して素通り)。"""
    raw = settings.raw(ENV_MODE).lower()
    return MODE_CONTEXT if raw == MODE_CONTEXT else MODE_BLOCK


def selected_reviewers() -> tuple[list, list[str]]:
    """実際に走らせるレビュアーと、`EXTERNAL_AI_PLAN_REVIEW_REVIEWERS` の未知名を返す。

    **事前チェックと `run_reviewers()` で別々に集合を計算してはいけない**。ずれると、
    1 つも走らないレビュアー集合のために `reserve_slot` が枠を消費し、以後の
    ExitPlanMode が「レビュー済み」扱いで素通りする。

    未知の名前 (`codx` のタイプミス等) しか指定されていない場合は空リストを返して
    no-op にする。既定の全件へ fallback すると、外したはずのレビュアーが黙って
    走ることになる。
    """
    wanted = settings.names(ENV_REVIEWERS)
    known = {r.NAME for r in REVIEWERS}
    unknown = [name for name in (wanted or []) if name not in known]
    chosen = [r for r in REVIEWERS if wanted is None or r.NAME in wanted]
    return [r for r in chosen if r.is_available()], unknown


def plan_hash(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# マーカーに記録するプラン (hash) の最大件数。長いセッションで多数の異なるプランを
# 出しても $TMPDIR のマーカーファイルが際限なく肥大化しないための安全弁
# (bd_092a232e-zh5.10)。超過分は古い順 (挿入順) に捨てる。
MAX_PLAN_ENTRIES = 50

# 予約 (reserve_slot が結果を知る前に +1 する仮の枠) が確定 (confirm_slot) も
# 解放 (release_slot) もされないまま残っている場合の回収 TTL。両レビュアーの
# timeout **上限** より必ず大きく取る必要がある — 下回ると、まだ走っている
# レビューの予約を別の ExitPlanMode 呼び出しが「kill された」と誤認して横取りする
# (post-implementation-review の IN_FLIGHT_TTL_SEC と同じ考え方)。
RESERVATION_TTL_SEC = max(cursor.MAX_TIMEOUT_SEC, codex.MAX_TIMEOUT_SEC) + 300

# plan-review-markers/ と plan-review-*.txt (0.6.0 以前を含む) の mtime GC TTL。
# post-implementation-review/state.py の STATE_TTL_SEC と同じ値・同じ方式だが、
# 実装はこの hook 内で完結させる (hook 固有の状態機械は共通化しない方針)。
MARKER_STATE_TTL_SEC = 48 * 3600


def _empty_marker() -> dict:
    return {"v": 1, "last": "", "plans": {}}


def _load_marker(f) -> dict:
    """マーカー本文 (JSON) を読む。壊れていれば (0.6.0 以前のテキスト形式を含め)
    空マーカー扱いにする (fail-open)。

    0.6.0 までは `"<hash>\\n<count>"` の 2 行テキストで、単一の (hash, count) しか
    持てなかった。JSON として解析できない旧形式は素通しで空マーカーに落ちる —
    1 セッション分の枠情報を失うだけで実害は $TMPDIR の 48 時間 TTL に収まる。
    """
    raw = flock.read_all(f)
    if not raw.strip():
        return _empty_marker()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _empty_marker()
    if not isinstance(data, dict):
        return _empty_marker()

    plans: dict[str, dict] = {}
    raw_plans = data.get("plans")
    if isinstance(raw_plans, dict):
        for hash_key, entry in raw_plans.items():
            if not isinstance(entry, dict):
                continue
            count = entry.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                continue
            reserved_at = entry.get("reserved_at")
            if not isinstance(reserved_at, (int, float)) or isinstance(reserved_at, bool):
                reserved_at = None
            plans[hash_key] = {"count": count, "reserved_at": reserved_at}

    last = data.get("last")
    return {"v": 1, "last": last if isinstance(last, str) else "", "plans": plans}


def _dump_marker(marker: dict) -> str:
    return json.dumps(marker, ensure_ascii=False)


def _reclaim_stale_reservations(marker: dict, now: float) -> None:
    """kill された hook が確定も解放もできなかった仮予約を TTL 経過後に回収する。

    reserve_slot は結果を知る前に count を +1 する「仮予約」を書き込み、
    `reserved_at` にその時刻を残す。レビューが REVIEW_CLEAN / 全失敗なら
    release_slot が、指摘ありで block/所見注入を確定したなら confirm_slot が
    `reserved_at` を None に戻す。ハーネスの hook timeout (ExitPlanMode: 1560s) で
    kill されるとどちらも呼ばれず、仮予約が残ったままになる。
    RESERVATION_TTL_SEC (両レビュアーの timeout 上限 + 猶予) を過ぎても
    `reserved_at` が残っているエントリは、レビューはとうに終わっているはずなのに
    確定も解放もされていない = kill されたとみなしてロールバックする
    (bd_092a232e-zh5.10)。
    """
    for hash_key, entry in list(marker["plans"].items()):
        reserved_at = entry.get("reserved_at")
        if reserved_at is None or now - reserved_at <= RESERVATION_TTL_SEC:
            continue
        entry["count"] -= 1
        entry["reserved_at"] = None
        if entry["count"] <= 0:
            del marker["plans"][hash_key]
            if marker["last"] == hash_key:
                marker["last"] = ""


def _evict_overflow_plans(marker: dict) -> None:
    """エントリ数が MAX_PLAN_ENTRIES を超えたら古い順 (挿入順) に捨てる。

    `marker["last"]` が指す hash がここで捨てられても `last` 自体は書き換えない
    (= dangling を許容する)。安全な理由: 唯一の参照元である reserve_slot の
    重複判定が `marker["last"] == current_hash and current_hash in marker["plans"]`
    と AND 条件になっており、`plans` に無い hash を `last` が指していても
    dedup 判定は素通り (False) するだけで実害が無いため。`_reclaim_stale_reservations`
    が `last` を明示的にクリアしているのとは非対称だが、そちらは「参照先が生きたまま
    無効化する」経路なので扱いが異なる。
    """
    plans = marker["plans"]
    if len(plans) <= MAX_PLAN_ENTRIES:
        return
    for stale in list(plans)[: len(plans) - MAX_PLAN_ENTRIES]:
        del plans[stale]


def reserve_slot(
    marker_file: str, current_hash: str, max_reviews: int
) -> tuple[bool, str | None]:
    """ロック下で原子的にスロットを確保し `(確保できたか, 利用者向け通知)` を返す。

    0.7.0 でマーカーを JSON 化し、上限を**プラン (hash) 単位**にした
    (bd_092a232e-zh5.10)。0.6.0 までは単一の (hash, count) しか持てず、count は
    block のたびに +1 されプランが変わっても戻らなかった。長いセッションで
    無関係な別プランの block が積み重なると、以後**すべての**プランが
    レビュー無しで通ってしまっていた。

    - `last` (旧 `saved_hash` の一般化): 直近に試みたプランの hash。**これと一致し
      かつ `plans` にまだ記録がある**場合だけ「同一内容の再確認」として黙って
      スキップする。1 つ前の試行としか比較しないので、途中で別プランを挟んで
      元のプランに戻ってきた場合は「同一内容」扱いにならず、通常の per-plan
      count 判定に入る (= 戻ってきたプラン自身の累積回数が問われる)
    - `plans[hash].count`: そのプラン (完全一致の hash) が block された回数。
      上限判定はこの値だけを見る — 別プランの block 回数は一切参照しない

    確保成功時は該当 hash の count を +1 し `reserved_at` を記録して `last` を
    更新する。レビュー結果が REVIEW_CLEAN / 全失敗なら release_slot()、
    指摘ありで block/所見注入を確定したら confirm_slot() を呼ぶこと
    (`reserved_at` を消して TTL 回収の対象から外すため)。

    **確保できない 2 つの理由は利用者から見て意味が違う**ので通知を分ける:

    - **上限到達**: 利用者はレビューを期待しているのに走らない → 通知する。
      黙って素通りすると「レビューが動かなくなった」と見え、この batch が潰そうと
      している「無言」そのものになる
    - **同一プランで再確認**: 直前と同じ内容なので結果は変わらない。毎回通知すると
      ノイズにしかならない → 黙る (stderr にだけ残す)
    """
    try:
        with flock.locked_file(marker_file) as f:
            marker = _load_marker(f)
            now = time.time()
            _reclaim_stale_reservations(marker, now)

            if marker["last"] == current_hash and current_hash in marker["plans"]:
                log("同一内容でレビュー済み")
                flock.rewrite(f, _dump_marker(marker))
                return False, None

            entry = marker["plans"].get(current_hash, {"count": 0, "reserved_at": None})
            if entry["count"] >= max_reviews:
                log(f"レビュー回数上限 ({max_reviews}) に達した")
                flock.rewrite(f, _dump_marker(marker))
                return False, (
                    f"{ENV_MAX_REVIEWS}={max_reviews} に達したためこのプランのレビューを"
                    "見送り (内容を変えれば新しいプランとして再度レビューします)"
                )

            entry["count"] += 1
            entry["reserved_at"] = now
            marker["plans"][current_hash] = entry
            marker["last"] = current_hash
            _evict_overflow_plans(marker)
            flock.rewrite(f, _dump_marker(marker))
            return True, None
    except OSError as e:
        log(f"マーカー read/write 失敗: {e}")
        return False, None


def release_slot(marker_file: str, reserved_hash: str) -> None:
    """reserve_slot() で確保した枠を戻す (REVIEW_CLEAN / reviewer 全失敗時)。

    該当 hash の count を -1 (0 未満にはしない。0 になればエントリごと削除) し、
    `reserved_at` を None に戻す。`last` が自分 (reserved_hash) ならクリアする
    (他プランが追い越して `last` を書き換えていればそちらは触らない)。

    枠を戻すのは意図的。上限は「差し戻しの往復で前に進まなくなる」のを止めるための
    ものなので、利用者を足止めしなかったレビュー (指摘なし / 失敗) に budget を
    払わせない。
    """
    try:
        with flock.locked_file(marker_file) as f:
            marker = _load_marker(f)
            entry = marker["plans"].get(reserved_hash)
            if entry is not None:
                entry["count"] -= 1
                entry["reserved_at"] = None
                if entry["count"] <= 0:
                    del marker["plans"][reserved_hash]
            if marker["last"] == reserved_hash:
                marker["last"] = ""
            flock.rewrite(f, _dump_marker(marker))
    except OSError as e:
        log(f"マーカー read/write 失敗: {e}")


def confirm_slot(marker_file: str, reserved_hash: str) -> None:
    """指摘ありで block / 所見注入 (MODE_CONTEXT) を確定した時に呼ぶ
    (bd_092a232e-zh5.10)。

    count は消費したまま (差し戻し済みとして永続) にしつつ `reserved_at` だけ
    None に戻し、TTL 回収の対象から外す。**これを呼ばないと、正当に確定した
    block も RESERVATION_TTL_SEC 経過後に「kill された仮予約」と誤認されて count
    が静かにロールバックされ、上限が実質緩んでしまう。**
    """
    try:
        with flock.locked_file(marker_file) as f:
            marker = _load_marker(f)
            entry = marker["plans"].get(reserved_hash)
            if entry is not None:
                entry["reserved_at"] = None
            flock.rewrite(f, _dump_marker(marker))
    except OSError as e:
        log(f"マーカー read/write 失敗: {e}")


def _gc_stale_markers(now: float | None = None) -> None:
    """plan-review-markers/*.marker と plan-review-*.txt を mtime TTL で掃除する
    (bd_092a232e-zh5.10)。post-implementation-review/stategc.py と同じ mtime TTL
    GC の考え方を踏襲するが、実装はこの hook 内で完結させる (hook 固有の状態機械は
    共通化しない方針)。
    """
    now = time.time() if now is None else now
    tmp_root = os.environ.get("TMPDIR", "/tmp")
    marker_dir = os.path.join(tmp_root, "plan-review-markers")
    candidates: list[str] = []
    if os.path.isdir(marker_dir):
        try:
            candidates.extend(
                os.path.join(marker_dir, name) for name in os.listdir(marker_dir)
            )
        except OSError:
            pass
    try:
        for name in os.listdir(tmp_root):
            if name.startswith("plan-review-") and name.endswith(".txt"):
                candidates.append(os.path.join(tmp_root, name))
    except OSError:
        pass

    for path in candidates:
        try:
            if now - os.path.getmtime(path) > MARKER_STATE_TTL_SEC:
                os.unlink(path)
        except OSError:
            continue


def run_reviewers(plan_text: str, active: list) -> tuple[dict[str, str], dict[str, str]]:
    """レビュアーを並列実行し `(指摘のある結果, レビュアーごとの状態)` を返す。

    状態は利用者向けの要約 (`systemMessage`) 用で、レビュー本文は入れない
    (`_common/notify.py` の方針)。

    全体 timeout は置かない。各レビュアーは自前の timeout で必ず返り、かつ
    `ThreadPoolExecutor` の with 終端は全 future の完了を待つため、`as_completed` に
    timeout を渡しても実質効かない (0.3.1 までの overall_timeout は dead logic だった)。
    """
    if not active:
        return {}, {}

    results: dict[str, str] = {}
    statuses: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        future_map = {pool.submit(r.review, plan_text): r for r in active}
        for future in as_completed(future_map):
            reviewer = future_map[future]
            try:
                result = future.result()
            except Exception as e:
                log(f"{reviewer.NAME} 失敗: {e}")
                statuses[reviewer.NAME] = "失敗"
                continue
            if not result:
                log(f"{reviewer.NAME}: 結果なし (timeout / 空応答)")
                statuses[reviewer.NAME] = "失敗"
                continue
            if is_clean_review(result):
                log(f"{reviewer.NAME}: REVIEW_CLEAN")
                statuses[reviewer.NAME] = "clean"
                continue
            statuses[reviewer.NAME] = "指摘あり"
            results[reviewer.NAME] = result
    return results, statuses


def summarize(statuses: dict[str, str], elapsed: float, outcome: str) -> str:
    """`クロスレビュー完了 (4分12秒): cursor=指摘あり, codex=clean → プランを差し戻し`"""
    detail = ", ".join(f"{name}={statuses[name]}" for name in sorted(statuses))
    return (
        f"クロスレビュー完了 ({notify.format_elapsed(elapsed)})"
        f"{': ' + detail if detail else ''} → {outcome}"
    )


def build_reason(results: dict[str, str]) -> str:
    sections = []
    for reviewer in REVIEWERS:
        name = reviewer.NAME
        if name in results:
            header = _HEADERS.get(name, f"## {name} レビュー")
            sections.append(f"{header}\n\n{results[name]}")

    body = "## クロスレビュー結果 (ExitPlanMode)\n\n" + "\n\n".join(sections) + "\n\n---\n\n"
    if get_mode() == MODE_CONTEXT:
        return (
            body
            + "**このレビューはプランを差し戻していません** "
            + f"({ENV_MODE}={MODE_CONTEXT})。妥当な指摘があれば実装に反映し、"
            "プランの前提が崩れる指摘なら利用者に確認してください。"
        )
    return (
        body + "レビュー指摘を踏まえてプランを見直し、再度 ExitPlanMode を呼んでください。"
        "既に対処済み・妥当でない指摘は無視して構いません。"
    )


def emit(output: dict, notices: list[str]) -> None:
    """stdout に hook 出力 JSON を **1 回だけ** 書く (空なら何も書かない)。

    stdout に JSON を 2 つ書くとハーネス側のパースが壊れるので、通知は溜めて
    最後にまとめる。`systemMessage` は公式 docs の universal field
    ("Every event accepts them") だが「some events discard them or deliver
    `systemMessage` somewhere other than the transcript」とも書かれており、
    対話 UI 以外での配信は本 plugin では未確認。同じ内容を `log()` にも出して、
    通知が出ない環境でも `--debug` で追えるようにする。
    """
    for message in notices:
        log(message)
    combined = notify.compose("exitplan-review", notices)
    if combined:
        output = {**output, "systemMessage": combined}
    if output:
        json.dump(output, sys.stdout, ensure_ascii=False)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"stdin JSON パース失敗: {e}")
        sys.exit(0)

    if payload.get("tool_name") != "ExitPlanMode":
        sys.exit(0)

    _gc_stale_markers()

    session_id = payload.get("session_id", "")
    if not session_id:
        log("session_id が空")
        sys.exit(0)

    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        log(f"tool_input が dict ではない: {type(tool_input).__name__}")
        sys.exit(0)

    plan_text = tool_input.get("plan", "")
    if not isinstance(plan_text, str) or not plan_text.strip():
        log("plan が空または非文字列")
        sys.exit(0)

    plan_stripped = plan_text.strip()

    if not review_enabled():
        log(f"{ENV_ENABLED}=0 によりレビュー無効化")
        sys.exit(0)

    # `EXTERNAL_AI_REVIEW_MAX` は現役の回数予算で、0 は「1 回も許さない」。新スイッチと AND。
    max_reviews = get_max_reviews()
    if max_reviews <= 0:
        log(f"{ENV_MAX_REVIEWS}=0 によりレビュー無効化")
        sys.exit(0)

    active, unknown = selected_reviewers()
    notices: list[str] = []
    if unknown:
        # タイプミスで黙って no-op になるのを防ぐ (枠は消費しない)
        notices.append(f"{ENV_REVIEWERS} の未知のレビュアー名を無視: {', '.join(unknown)}")
    if not active:
        log("実行するレビュアーなし (未インストール / 選択で全除外)")
        emit({}, notices)
        sys.exit(0)

    marker_dir = os.path.join(os.environ.get("TMPDIR", "/tmp"), "plan-review-markers")
    marker_file = os.path.join(marker_dir, f"{session_id}.exitplan.marker")
    current_hash = plan_hash(plan_stripped)

    reserved, slot_notice = reserve_slot(marker_file, current_hash, max_reviews)
    if not reserved:
        if slot_notice:
            notices.append(slot_notice)
        emit({}, notices)
        sys.exit(0)

    mode = get_mode()
    log(f"レビュー実行 (mode={mode}): {', '.join(r.NAME for r in active)}")
    started = time.monotonic()
    results, statuses = run_reviewers(plan_stripped, active)
    elapsed = time.monotonic() - started

    if not results:
        log("全レビュアーが REVIEW_CLEAN または結果なし (block しない、スロット戻す)")
        release_slot(marker_file, current_hash)
        notices.append(summarize(statuses, elapsed, "指摘なし (ブロックしません)"))
        emit({}, notices)
        sys.exit(0)

    reason = build_reason(results)
    # 指摘ありで確定 (block / 所見注入) するので、この予約は「kill された仮予約」
    # ではないと印を付ける (bd_092a232e-zh5.10)。呼び忘れると RESERVATION_TTL_SEC
    # 経過後に正当な block まで TTL 回収でロールバックされてしまう。
    confirm_slot(marker_file, current_hash)
    _save_review_copy(session_id, reason)

    if mode == MODE_CONTEXT:
        # 差し戻さず所見だけ渡す。`permissionDecision` を **意図的に省く**:
        # `"allow"` は ExitPlanMode の承認ゲートを飛ばして利用者がプランを見ないまま
        # 実装に入ってしまい、`"defer"` は additionalContext が無視される (docs 逐語)。
        # 省略形自体は docs に明示が無いので既定にはしない (詳細はモジュール docstring)。
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
        outcome = "所見のみ提示 (ブロックしません)"
    else:
        output = {"decision": "block", "reason": reason}
        outcome = "プランを差し戻し"

    notices.append(summarize(statuses, elapsed, outcome))
    emit(output, notices)


def _save_review_copy(session_id: str, reason: str) -> None:
    review_file = os.path.join(
        os.environ.get("TMPDIR", "/tmp"),
        f"plan-review-{session_id[:8]}.txt",
    )
    try:
        with open(review_file, "w") as f:
            f.write(reason)
        log(f"レビュー結果を保存 → {review_file}")
    except OSError:
        log("参照コピーの保存に失敗")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:
        log(f"fatal: {e}")
