"""アカウント不一致の deny を hook 自身の切替で置き換える opt-in (auto-switch)。

**なぜ必要か**: gh のアカウントを頻繁に切り替える運用では、不一致のたびに
「deny → Claude が案内された切替コマンドを単独で実行 → 元のコマンドを打ち直す」の
往復が入り、そのたびに作業が止まる (permission mode によっては切替コマンド自体の
承認待ちも入る)。期待値は accounts.local.json に宣言済みで、切替先のアカウントにも
ログイン済みなら、hook がその場で切り替えれば止まらずに済む (内部バックログ)。

**既定は無効 (opt-in)**。切替は CLI の設定 (gh なら hosts.yml) を書き換えるので、
同じマシンの**他のターミナル・セッションにも効く**。この副作用を受け入れる人だけが
有効にする。

**切り替えるのは「止める場面」だけ** — enforce で deny になる target (WRITE tier と、
`"$readonly": "deny"` のときの QUERY) に限る。判定するのは dispatcher
(`core/dispatcher.py` の `_dispatch_impl`):

- QUERY の不一致 (通して警告する) では切り替えない。切替はマシン全体に効く副作用
  なので、読むだけのコマンドのために並行する作業の CLI を動かさない
- `warn` / `off` モードでは切り替えない (止めない設定なので、置き換える deny が無い)
- 対話が必要なログインは行わない。切替先は**既にログイン済みのアカウント**だけ

**解決順** (先に決まったものが勝つ。`core/mode.py` と同じ並び):

1. 環境変数 `VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH` (settings.json の `env` / 起動時 env)。
   全プロジェクトで有効にする経路はこれだけ (グローバル既定の accounts.local.json は
   自前の設定を持つプロジェクトでは読まれない)
2. accounts.local.json の予約キー `"$auto_switch"` (プロジェクト単位)
3. 無効

値は **service 名の並び** (env はカンマ区切りの `github`、ファイルは `["github"]`)。
`true` のような一括指定を受け付けないのは、対応 service を増やしたときに既存の設定が
黙って広がらないようにするため。env の `off` はファイルの指定も含めて無効にする。
不正な値・自動切替に対応していない service 名は**無効として扱い** (= 従来どおり
deny)、note を返す。

**並行セッションのガード**: 直前 (`GUARD_SEC` 以内) に同じ切替対象 (gh なら host) を
**別の値へ**自動切替した記録があれば、切り替えずに従来どおり deny する。別アカウントを
期待する 2 つのセッションが交互に切り替えると、片方の hook が通したコマンドが
実行される前にもう片方が切り替え、**通したコマンドが別アカウントで動きうる**
(この plugin が防ぐ事故そのもの)。記録するのは自動切替だけで、手動の切替は見えない。
記録はプロジェクトではなく**切替先の値**で照合する — 同じ repo の worktree 同士は
同じ期待値を持つので、そこで見送る理由が無い。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from core import budget, cache

ENV_VAR = "VERIFY_CLOUD_ACCOUNT_AUTO_SWITCH"
# accounts.local.json の予約キー。`$mode` / `$readonly` (builder は書かない手編集キー)
# と違い、builder の `auto-switch` サブコマンドが書く (builder の docstring の D15)。
FILE_KEY = "$auto_switch"
OFF = "off"

# 別の値への自動切替を見送る窓 (秒)。hook が通したコマンドは通常 1 秒以内に動き出すが、
# permission の承認待ちなどで遅れる余地を見て `cache.IN_FLIGHT_SEC` と同じ長さにする。
# 承認待ちがこれより長い場合の穴は README の既知の制限に書いてある。
GUARD_SEC = 60

# 切替記録のファイル名 (`<service>.autoswitch.json`)。成功 cache の glob
# (`<service>-*.json`) に掛からない名前にする。
_GUARD_SUFFIX = ".autoswitch.json"

# 切替をしなかった / できなかったときに deny 文面へ添える注記の前置き。
# **切替コマンドの実形を書かないこと** — deny 文面の remediation 案内は verify() が
# 書いたものだけに保つ (`core/dispatcher._guides_remediation` と
# tests の TestRemediationGuidanceContract が文面からコマンドを拾う)。
_NOTE_HEAD = "※ 自動切替 (auto-switch) は行いませんでした: "
_FAILED_HEAD = "※ 自動切替 (auto-switch) を試みましたが、"

NOTE_SWITCHING_HERE = (
    _NOTE_HEAD + "このコマンド自身がアカウントの状態を変える操作を含むため、"
    "hook からは切り替えません。"
)
NOTE_BUDGET = (
    _NOTE_HEAD + "1 コマンド分の検証時間の予算を使い切っていたため、切替を"
    "始めませんでした。"
)


def supports(service) -> bool:
    """service が自動切替の契約 (`plan_switch` / `apply_switch`) を宣言しているか。"""
    return callable(getattr(service, "plan_switch", None)) and callable(
        getattr(service, "apply_switch", None)
    )


def _supported_keys(services) -> list[str]:
    return sorted(svc.ACCOUNT_KEY for svc in services if supports(svc))


def _parse_names(raw_names, services, source: str) -> tuple[frozenset[str], str | None]:
    """service 名の並びを解釈する。対応していない名前は落として note に残す。"""
    supported = set(_supported_keys(services))
    enabled: list[str] = []
    rejected: list[str] = []
    for raw in raw_names:
        name = raw.strip().lower()
        if not name:
            continue
        target = enabled if name in supported else rejected
        if name not in target:
            target.append(name)
    note = None
    if rejected:
        note = (
            f"{source} の {', '.join(rejected)} は自動切替に対応していません "
            f"(対応: {', '.join(sorted(supported)) or 'なし'})。"
            "その指定は無効として扱いました。"
        )
    return frozenset(enabled), note


def from_env(services, env=None) -> tuple[frozenset[str] | None, str | None]:
    """環境変数から有効な service を読む。`(services, note)`。

    未設定 (または空) は `None` = ファイルの指定に委ねる。`off` は空集合 = ファイルに
    書いてあっても無効。**それ以外の値もファイルには落とさない** — 綴り間違いで
    ファイル側の指定が生きると「env で止めたつもりが切り替わる」になるため、env が
    書かれていればその解釈 (不正な名前を落とした残り) だけで決める。
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_VAR)
    if raw is None or not raw.strip():
        return None, None
    if raw.strip().lower() == OFF:
        return frozenset(), None
    return _parse_names(raw.split(","), services, f"{ENV_VAR}={raw!r}")


def from_accounts(accounts, services) -> tuple[frozenset[str] | None, str | None]:
    """accounts.local.json の `"$auto_switch"` から有効な service を読む。

    配列 (`["github"]`) が正規の形。1 つだけなら文字列 (`"github"`) も受け付ける。
    `"off"` は明示的な無効。それ以外の型 (`true` / 数値 / `null` 等) は無効 + note。
    """
    if not isinstance(accounts, dict) or FILE_KEY not in accounts:
        return None, None
    raw = accounts[FILE_KEY]
    if isinstance(raw, str):
        if raw.strip().lower() == OFF:
            return frozenset(), None
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        return frozenset(), (
            f'accounts.local.json の "{FILE_KEY}" は service 名の配列で指定して'
            'ください (例: ["github"])。自動切替は無効として扱いました。'
        )
    return _parse_names(raw, services, f'accounts.local.json の "{FILE_KEY}"')


def effective(
    env_value: frozenset[str] | None, file_value: frozenset[str] | None
) -> frozenset[str]:
    """env → `"$auto_switch"` → 無効 の順で有効な service の集合を決める。"""
    if env_value is not None:
        return env_value
    if file_value is not None:
        return file_value
    return frozenset()


def resolve(accounts, services, env=None) -> tuple[frozenset[str], list[str]]:
    """有効な service の集合と、利用者に伝える note の一覧を返す。"""
    env_value, env_note = from_env(services, env)
    file_value, file_note = from_accounts(accounts, services)
    notes = [n for n in (env_note, file_note) if n]
    return effective(env_value, file_value), notes


# --- 並行セッションのガード ---------------------------------------------------


def _guard_path(service_name: str):
    return cache.service_state_path(service_name, _GUARD_SUFFIX)


def _read_records(service_name: str) -> dict:
    """切替の記録。無い・読めない記録 (stat できない・壊れた・入れ子が深い・オブジェクトで
    ない。`cache.read_state`) と、cache の dir を使えないときは、記録が無いのと同じ。

    ガードはベストエフォート (`record_switch` も書けなくても判定を変えない)。旧実装は存在
    確認に `Path.is_file()` を使い、Python 3.13 までは stat できない記録で例外にしていた
    (dispatcher の `_auto_switch` が握って「内部エラーのため自動切替を行わない」になり、
    3.14 からは記録が無いのと同じで切り替えていた)。
    """
    records = cache.read_state(_guard_path(service_name))
    return records if records is not None else {}


def conflicting_switch(
    service_name: str, steps, now_ns: int | None = None
) -> str | None:
    """`steps` のどれかが「直前の別の値への自動切替」とぶつかるなら注記を返す。

    `steps` は `(対象, 現在値, 切替先)` の並び (service の `plan_switch` の戻り値)。
    同じ値への切替 (同じ repo の worktree 同士など) はぶつからない。
    """
    now = time.time_ns() if now_ns is None else now_ns
    records = _read_records(service_name)
    for target, _current, want in steps:
        rec = records.get(target)
        if not isinstance(rec, dict):
            continue
        value = rec.get("value")
        at_ns = rec.get("at_ns")
        if not isinstance(value, str) or not isinstance(at_ns, int):
            continue
        elapsed = now - at_ns
        if value == want or not 0 <= elapsed < GUARD_SEC * 1_000_000_000:
            continue
        project = rec.get("project")
        where = f" ({project})" if isinstance(project, str) and project else ""
        return (
            _NOTE_HEAD
            + f"{elapsed // 1_000_000_000} 秒前に別の作業{where}が {target} を "
            f"{value} に自動で切り替えています。並行するセッションが {value} で"
            "作業中の可能性があるため見送りました。切り替える前にユーザーに確認して"
            "ください (切り替えると、そのセッションのコマンドが別アカウントで動きます)。"
        )
    return None


def record_switch(
    service_name: str, steps, project_dir: str, now_ns: int | None = None
) -> None:
    """自動切替した対象を記録する (ベストエフォート。書けなくても判定は変えない)。"""
    path = _guard_path(service_name)
    if path is None:
        return
    now = time.time_ns() if now_ns is None else now_ns
    records = _read_records(service_name)
    for target, _current, want in steps:
        records[target] = {"value": want, "project": project_dir, "at_ns": now}
    try:
        cache.write_state(path, json.dumps(records, ensure_ascii=False))
    except OSError:
        pass


# --- 切替の実行 -----------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """`attempt()` の結果。

    - `resolved`: 切替後 (または切替不要と判った後) の再検証が一致した
    - `switched`: 実際に切り替えた `(対象, 切替前, 切替後)` の並び
    - `error`: 切り替えた後の再検証が返したエラー (状態が変わったので、切替前の
      verify() の文面より正確)。切り替えていなければ None (呼び出し側は元の文面を使う)
    - `note`: 切り替えなかった / 失敗した理由 (deny 文面に添える)
    """

    resolved: bool = False
    switched: tuple = ()
    error: str | None = None
    note: str | None = None


def _service_name(service) -> str:
    return service.__name__.rsplit(".", 1)[-1]


def attempt(
    service,
    entry,
    project_dir: str,
    env=None,
    context=None,
    *,
    switching_here: bool = False,
) -> Outcome:
    """期待値へ切り替えて再検証する。呼ぶのは「deny になる不一致」のときだけ。

    手順と、各段で止める理由:

    1. コマンド自身が状態を変える (`switching_here`) → 切り替えない。明示的に
       アカウントを操作しているところへ hook が別の切替を重ねない
    2. **CLI を起動する前ごとに予算を確かめる** (`core/budget.py`)。予算の超過見積りは
       「最後の確認の後に起動する呼び出し数」を前提にしているため
    3. `plan_switch` で全対象の切替先がログイン済みか確かめる。1 つでも切り替えられ
       なければ**何も切り替えない** (一部だけ切り替えて deny、を作らない)
    4. 並行セッションのガード
    5. 成功 cache を破棄してから切り替える (他プロジェクトの成功 cache が、切替後の
       状態で TTL 分通ってしまうのを防ぐ。`cache.invalidate` の in-flight 窓で、
       この後の検証成功も cache されない)
    6. 切り替えた対象を記録し、再検証する
    """
    if switching_here:
        return Outcome(note=NOTE_SWITCHING_HERE)
    if budget.expired():
        return Outcome(note=NOTE_BUDGET)

    steps, reason = service.plan_switch(entry, project_dir, env=env)
    if reason:
        return Outcome(note=_NOTE_HEAD + reason)

    svc_name = _service_name(service)
    if steps:
        conflict = conflicting_switch(svc_name, steps)
        if conflict:
            return Outcome(note=conflict)
        if budget.expired():
            return Outcome(note=NOTE_BUDGET)
        cache.invalidate(svc_name)
        done, apply_error = service.apply_switch(steps, env=env)
        if done:
            record_switch(svc_name, done, project_dir)
        if apply_error and not done:
            # 状態は変わっていないので、切替前の verify() の文面がそのまま正しい。
            return Outcome(note=_FAILED_HEAD + apply_error)
    else:
        done, apply_error = (), None

    # 切り替えた後 (または plan の時点で既に一致していた場合) の再検証。
    if budget.expired():
        return Outcome(
            switched=tuple(done),
            error=(
                "自動切替の後、検証時間の予算を使い切ったため一致を確認できません"
                "でした。もう一度実行すると切替後の状態で検証されます。"
            ),
        )
    after = service.verify(entry, project_dir, env=env, context=context)
    if after is None:
        return Outcome(resolved=True, switched=tuple(done))
    note = _FAILED_HEAD + (
        apply_error if apply_error else "切り替えた後も期待値と一致しませんでした。"
    )
    return Outcome(switched=tuple(done), error=after, note=note)


def notice(service, switched) -> str:
    """自動切替したことを伝える文面 (allow 時は additionalContext、deny 時は注記)。"""
    describe = getattr(service, "describe_switch", None)
    body = describe(switched) if callable(describe) else ", ".join(
        f"{target}: {before} → {after}" for target, before, after in switched
    )
    return f"[verify-cloud-account] 自動切替 (auto-switch): {body}"
