"""Hook 出力 JSON builder。

Phase 0 実測で確定した唯一信頼できる情報注入経路である
`permissionDecisionReason` を使い、deny/ask を返す。
systemMessage トップレベルは届かないため使用しない。

## 三態判定の使い分け

- ``make_deny``: 機密パターン確定一致、policy 入力欠如など、ユーザーの permission
  mode に関わらず必ず block したいケース。
- ``ask_or_deny``: 判定不能だが、機密の可能性があり bypass モードではデフォルト
  allow に倒したくないケース。Read/Edit handler の symlink/special/parent-dir
  fail、非 bash tool の catch-all 例外などで使う。non-bypass = ask、bypass = deny。
- ``ask_or_allow``: 判定不能だが、autonomous 実行 (auto /
  bypassPermissions) では日常コマンドを止めない方を優先するケース。Bash
  handler の opaque wrapper (``bash -c``, ``eval``, ``python3 -c``, ``sudo``,
  ``xargs``, ``env`` 等、``handlers.bash.constants._OPAQUE_WRAPPERS``) や
  hard-stop metachar、shell keyword、abs/rel path exec、residual metachar、
  shlex/normalize 失敗で使う。``awk`` / ``sed`` は 0.17.0 で opaque wrapper
  から「mutate」カテゴリ (実行不可、``make_deny`` 固定) に移動済み — 本節の
  旧い列挙が古いバージョンのまま残っていたので更新した (内部バックログ)。
  default = ask、
  auto/bypass = allow。acceptEdits / dontAsk は明示的に非 lenient
  (ask 維持)。機密確定は使わず ``make_deny`` 固定。

## allow の判定 (L4, 0.4.3)

``make_allow()`` は既定で ``{}`` を返す現行仕様だが、将来 Phase 0 spec が
``permissionDecision: "allow"`` 明示出力に変わっても破綻しないよう、テストは
``is_allow(r)`` 述語で判定すること。

## lenient allow の開示 (``additionalContext``、0.33.0)

公式 hooks reference の逐語 (2026-09-19 再確認): ``permissionDecisionReason`` は
「For ``"allow"`` and ``"ask"``, shown to the user but not Claude」。つまり
``ask_or_allow`` が autonomous mode で allow に倒したとき、**なぜ通ったのかは
Claude に一切伝わらない**。Claude に渡せる唯一の PreToolUse チャネルは
``hookSpecificOutput.additionalContext`` (「String added to Claude's context
alongside the tool result」) なので、lenient allow に限りここへ 1 文の事実記述
(``LENIENT_ALLOW_CONTEXT``) を載せる。

- **判定は不変**。``permissionDecision`` は出さない (``{}`` と同じく通常の
  permission flow に委ねる)。明示 ``"allow"`` を出すと「ハーネスの確認を
  スキップさせる」意味になり allow が強くなる = 判定境界の変更なので出さない
- 文面は**固定文字列**。command / path / 値は載せない (reason 側の
  minimal-info 原則と同じ。reason 文字列を流用すると operand が混ざる)
- 公式の書き方指針の逐語: 「Write the text as factual statements rather than
  imperative system instructions」。指示文ではなく事実記述にしてある
- **載せる対象は ``ask_or_allow`` が lenient に倒した全件ではない**。
  ``handlers.bash_handler`` 側の ``_gate_lenient_note`` が「command に機密
  パターンらしい token が含まれる」ときだけ残し、それ以外は素の allow に戻す。
  lenient allow は実測で全 Bash 呼出の 4 割強なので、全件に載せると note 自体が
  ``permissionDecisionReason`` のノイズ回避方針 (``core/patterns.py``) と同じ
  問題をコンテキスト側で起こす。1 文固定で操作対象を含まないことは**混入量の
  上限**を決めるだけで、頻度は絞りが決める
- したがって ``ask_or_allow`` は「note を作る」までを担い、「載せるか」は
  handler が決める。この関数を別 tool から呼ぶときは同じ絞りを通すか、
  頻度が問題にならないことを確認すること
"""
from __future__ import annotations

from typing import Literal, TypedDict

# reason のハード上限 (プランの目標: 1-2KB)。
# Step 4 で 4KB → 3KB に縮小 (Phase 0 実測で 1KB/8KB/32KB のいずれも完全配信される
# ことは確認済みだが、他 hook と合算した全体コンテキスト圧迫を抑えるため余裕を持たせる)。
MAX_REASON_BYTES = 3 * 1024

# reason 末尾に付ける truncation マーカー
TRUNCATE_MARKER = "\n...[truncated]"

# autonomous 実行モード: ``ask_or_allow`` がここに含まれる
# permission_mode で allow に倒す。
#   - "auto": CLI 2.1.83+ の前段 classifier モード
#   - "bypassPermissions": 全確認スキップモード
#   - "plan": Plan mode (副作用は plan 承認まで保留、Bash も dry-run 相当)
# それ以外 ("default" / "acceptEdits" / "dontAsk") は ask に倒す。
# "acceptEdits" は Edit/Write 専用モードで Bash lenient の意図なし、"dontAsk" は
# 明示的な非 lenient 判断として既存方針を維持する。
#
# 0.3.3 で前方互換のため "plan" を含めていたが、Phase 0 実測 (2026-04-22) で
# 当時の CLI (2.1.101 系) では plan mode で PreToolUse hook が発火しないことが
# 確認され、0.6.0 で dead entry として撤去していた。0.13.0 (2026-05-18) で
# ユーザー実機で plan mode 中の Bash hook 発火を確認し、再度 LENIENT_MODES に
# 追加。plan mode は副作用が plan 承認まで保留される dry-run 的な状態のため、
# Bash 静的解析不能ケース (opaque wrapper 等) の ``ask_or_allow`` では allow に
# 倒して操作性を優先する。機密 path 確定 match (``make_deny``) と Read/Edit の
# ``ask_or_deny`` は plan mode でも安全側 (deny / ask) を維持する。
LENIENT_MODES = frozenset({"auto", "bypassPermissions", "plan"})

# lenient allow のときだけ ``additionalContext`` に載せる 1 文 (0.33.0)。
# 固定文字列であることが前提 (module docstring の「lenient allow の開示」)。
# 事実記述にしてあるのは公式指針 (imperative な system instruction 形は
# prompt-injection 防御に当たって Claude ではなくユーザーに晒される) に従うため。
LENIENT_ALLOW_CONTEXT = (
    "sensitive-files-guardrail: 静的解析では機密パスの有無を判定できない"
    "コマンドでしたが、permission mode が autonomous なため確認なしで通しました。"
)


def _truncate(reason: str, limit: int = MAX_REASON_BYTES) -> str:
    """reason が limit byte を超えたら UTF-8 境界で安全に切る。"""
    encoded = reason.encode("utf-8")
    if len(encoded) <= limit:
        return reason
    keep = limit - len(TRUNCATE_MARKER.encode("utf-8"))
    if keep <= 0:
        return TRUNCATE_MARKER.strip()
    truncated = encoded[:keep]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="ignore") + TRUNCATE_MARKER


def _is_lenient_mode(envelope: dict) -> bool:
    """envelope.permission_mode が autonomous 実行モード (auto /
    bypassPermissions) か。"""
    return envelope.get("permission_mode") in LENIENT_MODES


# -- L3: hookSpecificOutput の shape を TypedDict で固定 ------------------


class HookSpecificOutput(TypedDict, total=False):
    """Phase 0 で確定した hookSpecificOutput shape (PreToolUse)。

    全フィールドが ``total=False`` なのは ``make_allow()`` が ``{}`` を返す
    現行仕様 (= 全 key 不在) を許容するため。allow の判定は ``is_allow(r)``
    で行う。
    """

    hookEventName: Literal["PreToolUse"]
    permissionDecision: Literal["deny", "ask"]
    permissionDecisionReason: str
    # allow でも Claude に届く唯一のチャネル (0.33.0)。``permissionDecision``
    # とは独立に設定でき、載せても判定は動かない。
    additionalContext: str


class HookResponse(TypedDict, total=False):
    """hook が stdout に書く JSON 全体の shape。"""

    hookSpecificOutput: HookSpecificOutput


def make_deny(reason: str) -> HookResponse:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _truncate(reason),
        }
    }


def make_ask(reason: str) -> HookResponse:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": _truncate(reason),
        }
    }


def make_allow(additional_context: str = "") -> HookResponse:
    """no-op allow (明示的な allow は出さず、空オブジェクトで通す)。

    判定するときは ``is_allow(r)`` を使うこと。``r == {}`` で書くと将来 spec
    変更で壊れる。

    ``additional_context`` を渡すと ``hookSpecificOutput.additionalContext``
    だけを載せた allow を返す (0.33.0)。``permissionDecision`` は**出さない**
    ので判定は素の allow と同一 (``is_allow`` / ``decision_of`` の結果も同じ) で、
    増えるのは Claude 向けの情報だけ。module docstring の「lenient allow の開示」
    を参照。
    """
    if not additional_context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": additional_context,
        }
    }


def additional_context_of(response: HookResponse | dict) -> str:
    """response に載っている ``additionalContext`` を返す (無ければ空文字)。

    ``handlers.bash_handler`` が segment ループを跨いで「lenient allow に倒した」
    事実を持ち運ぶために使う (lenient mode の ``ask_or_allow`` は allow を返すので
    ``decision_of`` では区別が付かない)。
    """
    if not isinstance(response, dict):
        return ""
    hook = response.get("hookSpecificOutput")
    if not isinstance(hook, dict):
        return ""
    value = hook.get("additionalContext")
    return value if isinstance(value, str) else ""


def is_allow(response: HookResponse | dict) -> bool:
    """response が allow を意味するか判定する (L4, 0.4.3)。

    allow のシグナル:
    1. ``hookSpecificOutput`` フィールドが不在 (現行 ``make_allow`` の挙動)。
    2. ``hookSpecificOutput`` が空 dict / dict 以外。
    3. ``permissionDecision`` が ``"deny"`` でも ``"ask"`` でもない
       (将来 spec 拡張で ``"allow"`` 明示出力に対応する場合に True を返す)。

    response 自体が dict でないときは ``False`` (allow と誤判定しないため)。
    """
    if not isinstance(response, dict):
        return False
    hook = response.get("hookSpecificOutput")
    if not isinstance(hook, dict):
        return True
    decision = hook.get("permissionDecision")
    return decision not in ("deny", "ask")


def decision_of(response: HookResponse | dict) -> str | None:
    """response の ``permissionDecision`` を返す (allow は ``None``、0.32.0)。

    ``is_allow`` と同じ判定規約 (``"deny"`` / ``"ask"`` 以外は allow) を、
    「どちらだったか」を知りたい呼出向けに 1 箇所で持つ。遅延ログの
    leveling (``core.logging.flush_deferred``) と ``handlers.bash_handler``
    の segment 集約が使う。
    """
    if not isinstance(response, dict):
        # dict でない = 想定外の戻り。``None`` (= allow) を返すと allow と
        # 誤認されるので ``"deny"`` を返す (``is_allow`` が False を返すのと
        # 同じ向き。遅延ログ側では「診断を残す」に倒れる)。
        return "deny"
    hook = response.get("hookSpecificOutput")
    if not isinstance(hook, dict):
        return None
    decision = hook.get("permissionDecision")
    return decision if decision in ("deny", "ask") else None


def ask_or_deny(reason: str, envelope: dict) -> HookResponse:
    """bypass モードでは ask が自動 allow されるため deny にフォールバック。

    Phase 0 実測で確定: bypassPermissions モード下では ask + reason は
    そのままツール実行に通ってしまう。この hook が機密ファイル検出で ask を
    返したい文脈では、bypass 判定を見て deny に倒す。

    Read/Edit handler の symlink/special/parent-dir fail、非 bash tool の catch-all
    例外など、「判定不能だが機密の可能性があり bypass で allow してはいけない」
    用途で使う。
    """
    if envelope.get("permission_mode") == "bypassPermissions":
        return make_deny(reason)
    return make_ask(reason)


def ask_or_allow(reason: str, envelope: dict) -> HookResponse:
    """autonomous / plan モードでは allow に倒す。

    Bash handler の静的解析不能ケース (opaque wrapper、hard-stop metachar、shell
    keyword、abs/rel path exec、residual metachar、shlex/normalize 失敗) 用。

    autonomous 実行 (``auto`` / ``bypassPermissions``) を選んでいるユーザーは
    「日常コマンドが片っ端から止まる」のを避けたい意図がある。plan mode は
    副作用が plan 承認まで保留される dry-run 的な状態で、ここで止めても plan
    記述自体が遮られて操作性だけが落ちるため、同じく allow に倒す (0.13.0)。
    いずれのケースも「機密かもしれない」止まりで「機密と確定した」わけでは
    ないため、確定 match (``make_deny``) より弱い保護に倒す。

    ``acceptEdits`` / ``dontAsk`` は意図的に lenient 扱いしない (明示的に ask 維持)。

    機密パターン確定 (literal or glob 候補列挙で True) のときはこの関数を使わず
    ``make_deny`` を直接呼ぶこと。

    lenient に倒したときは ``additionalContext`` に ``LENIENT_ALLOW_CONTEXT`` を
    載せて返す (0.33.0)。``reason`` は ``permissionDecisionReason`` 用で allow では
    Claude に届かないため、「静的判定できないまま通した」事実が Claude 側に
    伝わらなかった。判定は allow のままで、載るのは固定 1 文だけ。

    この note を**最終応答に残すかは呼出側が決める**: Bash handler は
    ``_gate_lenient_note`` で「command に機密パターンらしい token を含む」もの
    だけに絞り、残りは素の allow に戻す (module docstring 参照)。
    """
    if _is_lenient_mode(envelope):
        return make_allow(LENIENT_ALLOW_CONTEXT)
    return make_ask(reason)
