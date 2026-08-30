#!/usr/bin/env python3
"""実装直後の差分を Cursor でレビューし、指摘があれば Claude に差し戻す hook 群。

**レビュー対象は「前回 Stop がレビュー対象として消費した時点以降に、このセッションが
変更したファイル」だけ**。作業ツリー全体の `git diff HEAD` は使わない。同一ディレクトリで
複数セッションが動くと、一行も編集していないセッションが隣のセッションの編集を
5〜10 分かけてレビューしてしまうため。

3 つの phase を 1 エントリポイントで捌く (hooks.json から `--phase` で振り分け):

| phase | hook | 役割 |
|---|---|---|
| `pre-tool` | PreToolUse(Bash) | Bash 実行前の `git status` スナップショットを保存 |
| `post-tool` | PostToolUse(Write/Edit/NotebookEdit, Bash) | 変更パスを pending に積む |
| `stop` | Stop | pending を claim してレビュー、結果を配信 |

Bash にも張るのは、`sed -i` / フォーマッタ / スクリプト生成による変更を
Write/Edit だけ見ていると取りこぼすため。実行前後の `git status` を突き合わせれば
Bash 経由の変更も「どのセッションがやったか」付きで拾える。

ターン境界を UserPromptSubmit ではなく「前回 Stop の消費時点」で定義する理由、
in-flight 予約と TTL 回収の設計は state.py の docstring を参照。
外部に送らないファイルの判定 (既定除外 glob / 追加 glob / CODE_ONLY) は exclusion.py。

## 0.6.0 で入れた「頻度と待ち時間」の制御

Stop は編集のあった全ターンで発火し、最大 `cursor.timeout_sec()` 秒ブロックする。
0.5.0 は利用者向けの出力が一切無く (stderr は debug log 止まり)、最大 11 分の無言に
なっていた。次で調整・可視化する:

| 環境変数 | 既定 | 効果 |
|---|---|---|
| `EXTERNAL_AI_POST_REVIEW` | `1` | この hook 自体の on/off |
| `EXTERNAL_AI_POST_REVIEW_TIMEOUT` | `300` | cursor の timeout (上限 600) |
| `EXTERNAL_AI_POST_REVIEW_MIN_LINES` | `0` | 変更行数がこれ未満のターンは見送り |
| `EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC` | `0` | 前回レビュー完了から N 秒は見送り |

見送り (`MIN_LINES` / `COOLDOWN_SEC`) では **pending を消費しない**ので、貯まった
変更は次に走るレビューへまとめて載る。所要時間と結果は `systemMessage` に出す。

## 0.8.0 で入れた「hook error に見せない」変更

指摘ありのとき、0.7.0 までは常に `decision: "block"` + `reason` を返していた。この形式は
Claude Code のトランスクリプト上で**エラー扱い**として表示される。正常に完了したレビューが
毎ターンエラー通知に見えるのは、離脱率の観点で好ましくない。

公式 Hooks reference (`Stop decision control` 節) 逐語:

> `hookSpecificOutput.additionalContext`: Non-error feedback for Claude. The conversation
> continues so Claude can act on it, but unlike `decision: "block"` it is shown in the
> transcript as hook feedback rather than a hook error.

同節はさらに、`additionalContext` でも継続の仕組みは `decision: "block"` と**同じ**だと
明記している:

> It keeps the conversation going through the same loop protections as `decision: "block"`,
> namely the `stop_hook_active` input and the 8-consecutive-continuation cap, but the
> transcript labels it "Stop hook feedback" and no hook error notification is shown.

つまり `stop_hook_active` の扱い (`handle_stop` 冒頭の再帰防止) も 8 回連続の上限も
ハーネス側の同一機構であり、この変更で Claude の動作 (継続すること・reason を読むこと) は
変わらない。変わるのは**表示だけ**。

- **既定を `hookSpecificOutput.additionalContext` (hookEventName: `Stop`) に変更**。
  実機確認 (nested `claude -p`, CLI 2.1.251, 2026-08-30): Stop の 1 回目が
  `additionalContext` を返すと、2 回目の Stop payload は `stop_hook_active: true` で
  再度発火する (継続が実際に起きている)。**確認は 2 段階で行った**: 1 段目は
  injection 文言 ("reply with the single word BANANA") で試し、Claude が「フック出力
  経由の注入」と見なして拒否した (継続の仕組みは確認できたが、これだけでは指摘への
  関与を確認したことにならない)。2 段目で `build_reason()` と同じ形の現実的な指摘文
  (実在するファイルへの妥当な指摘 2 件) に差し替えたところ、Claude は指摘を 1 件ずつ
  評価し、critical でないと判断した理由を添えて明示的にスキップする応答をした —
  `decision: "block"` の `reason` と同じように指摘へ関与することを確認済み。**公式
  changelog 記載の対応下限は CLI 2.1.163
  (2026-06-04, "Hooks: Stop and SubagentStop hooks can now return
  `hookSpecificOutput.additionalContext` to give Claude feedback and keep the turn going
  without being labeled a hook error")** — この行以前の CLI では `additionalContext` が
  無視され、レビュー指摘が Claude に届かないまま Stop してしまう可能性がある
- **`EXTERNAL_AI_POST_REVIEW_MODE=block` で 0.7.0 までの `decision: "block"` に戻せる**
  (opt-in)。2.1.163 未満の CLI を使っている場合や、外部ツールが hook のエラー扱いを
  シグナルとして監視している場合の避難路

## 未対応 CLI での自動 fail-closed (同じ 0.8.0 batch への追補、Codex R1 P1 対応)

上の opt-in だけでは、2.1.163 未満の CLI で plugin を更新した既存ユーザーが
`EXTERNAL_AI_POST_REVIEW_MODE=block` の存在を知らない限り、`additionalContext` が
黙って無視されレビュー指摘が届かないまま Stop してしまう。しかも `_run_review` は
指摘を組み立てた時点で既に `state.complete_claim(...)` を呼んでいるため、この指摘は
再試行されず永久に失われる。

`get_mode()` (実体は `_resolve_mode()`) を 3 値に拡張して対処する:

| `EXTERNAL_AI_POST_REVIEW_MODE` | 挙動 |
|---|---|
| `block` | 明示。版数判定を飛ばし常に `decision: "block"` |
| `context` | 明示。版数判定を飛ばし常に `additionalContext` (2.1.163 未満での既知の問題を承知の上という前提。利用者の責任) |
| 未設定 / `auto` / 未知の値 | 版数を検出し、2.1.163 以上なら `context`、**未満または不明なら `block`** に fail-closed する (指摘を届かないまま失う方向には倒さない。コストは legacy 表示に戻るだけ) |

版数検出は 3 段 (`_claude_code_version()` / `_detect_claude_code_version()`):

1. 環境変数 `CLAUDE_CODE_VERSION` — **公式 Hooks reference の環境変数一覧には無い**。
   同名の変数自体は公式 Claude Code settings reference (`policyHelper` 節) に存在するが
   Enterprise の managed settings 解決ヘルパー専用で、hook プロセスへの注入は明記されて
   いない (`llms-docs:researching-claude-docs` で 2026-08-30 に逐語確認、以下 Q1〜Q3)。
   将来 hooks 側にも公開された場合に備えて一応最初に見るが、現状は素通りして次段に進む
   想定
2. 環境変数 `CLAUDE_CODE_EXECPATH` のパス要素のうち版数だけの文字列
   (`^\\d+\\.\\d+\\.\\d+$`) に一致するもの。ローカルインストール
   (`~/.local/share/claude/versions/<version>/...`) で実機観測された形式だが、
   **公式ドキュメントのどこにも記載が無い未文書化の内部実装詳細**
   (`hooks` / `hooks-guide` / `env-vars` / `settings-reference` / `plugins-reference`
   を含む公式コーパス全体でゼロヒットを確認済み)。npm 配布はこの形式のパスにならない
   ため、その場合は次段に進む
3. `claude --version` を subprocess で実行 (timeout `_VERSION_SUBPROCESS_TIMEOUT_SEC` =
   3 秒)。stdout 先頭の `\\d+\\.\\d+\\.\\d+` を parse。PATH に無い / timeout / parse 失敗は
   すべて None。長時間 CLI (cursor/codex) 用の `_common/subproc.py` (孫プロセスの
   process group 管理付き) ではなく `subprocess.run` を直接使う — 即座に終了する単純な
   probe であり、`subproc.py` 内部の `ps` 呼び出しと同じ扱いで十分なため

2 と 3 は公式契約の外側にあるベストエフォートの手段なので、**3 (subprocess) を
最終的な信頼できるフォールバックとして必ず残す** (2 の形式が将来変わっても、
「版数が分かる」経路自体は失われない)。判定結果はプロセス内で 1 回だけ計算して
キャッシュする (Claude Code の版数はプロセスの生存中に変わらないため、state
ファイルへの永続化は不要)。

exit 0 (JSON なし): Stop を妨げない
exit 0 + {"systemMessage": ...}: 完了要約 / 除外・繰り越し・見送りの通知 (Stop を妨げない)
exit 0 + {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": ...},
          "systemMessage": ...}: レビュー結果を返す (`EXTERNAL_AI_POST_REVIEW_MODE=context`
    明示、または auto 解決で対応版数と判定した場合)
exit 0 + {"decision": "block", "reason": ..., "systemMessage": ...}:
    レビュー結果を返す (`EXTERNAL_AI_POST_REVIEW_MODE=block` 明示、または auto 解決で
    非対応・不明な版数と判定した場合)
"""
from __future__ import annotations

import os
import sys

# Windows 非対応 (`_common.flock` / `state.py` が `fcntl` に依存)。他モジュールの import
# (`_common` 系・`state`) で ImportError が起きる前に判定して抜ける。ここより後ろで
# import すると、Windows では毎ツール呼出で hook error 通知が出てしまう
# (対応状況は README の「前提」節を参照)。
if os.name != "posix":
    sys.exit(0)

import hashlib
import json
import re
import subprocess
import time

# hooks/_common を解決するため、hook 内モジュールより先に hooks/ を sys.path に載せる
# (plugin root 内の相対配置なので ${CLAUDE_PLUGIN_ROOT} が cache コピーでも壊れない)。
_HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _common import flock, hooklog, notify, sentinel, settings  # noqa: E402

import cursor  # noqa: E402
import exclusion  # noqa: E402
import gitscan  # noqa: E402
import state  # noqa: E402
import stategc  # noqa: E402

# 1 回のレビューで cursor に渡す diff 合計の上限。ファイル (セクション) 単位で積み上げ、
# 収まらないファイルは **送らずに pending へ戻す** (hash も記録しない)。結合後に末尾を切る
# 方式だと、切り落とされたファイルが「レビュー済み」扱いになり以後再掲されなかった (0.4.1 まで)。
MAX_DIFF_BYTES = 40000

# 1 ファイルの diff 上限。これを超えるファイルは先頭だけを `(truncated)` 付きで送り、
# **この場合だけ** hash を記録する (先頭は見ているので、変わらない限り再掲しない)。
# MAX_DIFF_BYTES 以下でなければならない (先頭のファイルが必ず収まる = 永久繰り越しが無い)。
# 合計の 80% にしているのは、切り詰めは「その diff の末尾を二度と見ない」恒久的な損失で、
# 繰り越しは「次ターンまで待つ」だけの遅延なので、1 ファイルはなるべく丸ごと送るため。
MAX_FILE_DIFF_BYTES = 32000

# 1 回のレビューで diff を取るパス数の上限。溢れた分は捨てずに pending へ戻し、
# 次の Stop でレビューする (silent truncation にしない)。
MAX_REVIEW_PATHS = 60

# パス単位 diff 収集の時間予算。Stop の hook timeout 690s のうち cursor が上限 600s
# (`cursor.MAX_TIMEOUT_SEC`。既定は 300s だが env で伸ばせるので上限で見る) +
# kill 猶予 15s (3 × KILL_GRACE_SEC) を使うため、git に回せるのは約 75s。他の git 呼び出し
# (rev-parse 2 回 (各 2 秒) + ls-files 10 × 2 [symlink_map と untracked_among] + 予算判定後に走る
# 最後の 1 パスの path_diff 5) を引いた残りに収まるよう決めている
# (式は tests/test_review_set.py::TestTimeoutBudgets で固定。合計 59s)。
COLLECT_BUDGET_SEC = 30

# systemMessage / stderr に列挙するファイル名の上限 (それ以上は件数だけ)
MAX_LISTED_NAMES = 10

_EDIT_TOOLS = ("Write", "Edit", "NotebookEdit")

ENV_ENABLED = "EXTERNAL_AI_POST_REVIEW"
ENV_LEGACY_MAX = "EXTERNAL_AI_POST_REVIEW_MAX"
ENV_BASH_TRACKING = "EXTERNAL_AI_POST_REVIEW_BASH_TRACKING"
ENV_MIN_LINES = "EXTERNAL_AI_POST_REVIEW_MIN_LINES"
ENV_COOLDOWN = "EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC"
ENV_MODE = "EXTERNAL_AI_POST_REVIEW_MODE"

MODE_CONTEXT = "context"
MODE_BLOCK = "block"
#: `block` / `context` どちらでもない値の正規の綴り。実際の分岐では未設定・未知の値と
#: 区別しない (どちらも版数に応じた自動選択に落ちる) が、README / エラーメッセージで
#: 「意図して auto を選んだ」ことを書けるように定数化しておく。
MODE_AUTO = "auto"

# Stop の `additionalContext` が効く最低版数 (根拠はモジュール docstring
# 「0.8.0 で入れた『hook error に見せない』変更」節に逐語引用した公式 changelog、
# 2026-06-04 付・CLI 2.1.163)。
_MIN_VERSION_FOR_ADDITIONAL_CONTEXT = (2, 1, 163)

# `_claude_code_version()` の検出順で見る環境変数 (根拠と注意点はモジュール docstring
# 「未対応 CLI での自動 fail-closed」節)。
ENV_CC_VERSION = "CLAUDE_CODE_VERSION"
ENV_CC_EXECPATH = "CLAUDE_CODE_EXECPATH"

# `claude --version` probe の timeout (秒)。cursor/codex のような長時間 CLI ではなく
# 即終了する単純な呼び出しなので、`_common/subproc.py` の process group 管理は使わず
# 短い固定値で十分 (`_version_from_subprocess` 参照)。
_VERSION_SUBPROCESS_TIMEOUT_SEC = 3

# バージョン文字列の parse に使う 2 種類の正規表現。EXECPATH のパス要素は「その要素が
# 版数だけであること」を要求する完全一致、env var / `claude --version` の出力は
# 末尾に他の文字列 (`(Claude Code)` 等) が付きうるので先頭一致にする。
_VERSION_FULL_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_VERSION_PREFIX_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

# `_claude_code_version()` の結果 (このモジュール読み込み単位で 1 回だけ計算)。
# None も有効な計算結果 (「検出できなかった」) なので、「未計算」との区別に
# 専用のキーを使う (dict にキーが無ければ未計算)。
_VERSION_CACHE: dict[str, tuple[int, int, int] | None] = {}
_VERSION_CACHE_KEY = "detected"


log = hooklog.make_logger("post-implementation-review")

# フェンス / 装飾 / 「指摘なし」の前置き 1 文を許容する判定 (規則は _common/sentinel.py)
is_clean_review = sentinel.is_clean_review


def _match_version(pattern: re.Pattern, text: str) -> tuple[int, int, int] | None:
    match = pattern.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _version_from_subprocess() -> tuple[int, int, int] | None:
    """`claude --version` を実行して版数を取り出す (`_claude_code_version()` 最終段)。

    PATH に無い (`FileNotFoundError` — `OSError` のサブクラス) / timeout / stdout の
    parse 失敗はすべて None (fail-open: この関数自体は例外を外に投げない)。
    `check=True` は使わない — 非 0 終了でも stdout があれば parse を試み、無ければ
    自然に None へ落ちる (`CalledProcessError` を個別に捕捉する必要が無い)。
    stdin は hook 自身の stdin (payload の pipe) を子に継承させないため明示的に
    `DEVNULL` にする (`_common/subproc.py::run_captured` と同じ配慮)。
    """
    try:
        result = subprocess.run(
            ["claude", "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_VERSION_SUBPROCESS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _match_version(_VERSION_PREFIX_RE, result.stdout or "")


def _detect_claude_code_version() -> tuple[int, int, int] | None:
    """`_claude_code_version()` の実処理 (キャッシュ無し)。検出順は 3 段。

    a. 環境変数 `CLAUDE_CODE_VERSION`
    b. 環境変数 `CLAUDE_CODE_EXECPATH` のパス要素のうち版数だけの文字列に一致するもの
    c. `claude --version` の subprocess 実行

    各段の根拠・公式ドキュメントとの整合性 (a/b は非公式) はモジュール docstring
    「未対応 CLI での自動 fail-closed」節を参照。
    """
    version = _match_version(_VERSION_PREFIX_RE, os.environ.get(ENV_CC_VERSION, ""))
    if version is not None:
        return version

    execpath = os.environ.get(ENV_CC_EXECPATH, "")
    for part in execpath.split(os.sep):
        version = _match_version(_VERSION_FULL_RE, part)
        if version is not None:
            return version

    return _version_from_subprocess()


def _claude_code_version() -> tuple[int, int, int] | None:
    """このプロセスから見える Claude Code の版数 (`(major, minor, patch)`)。

    検出できなければ None。プロセス内 (このモジュール読み込み単位) で 1 回だけ計算し
    キャッシュする — Claude Code の版数は hook プロセスの生存中に変わらないため、
    state ファイルへの永続化は不要 (hook は 1 回の呼び出しごとに使い捨てのプロセス)。
    """
    if _VERSION_CACHE_KEY not in _VERSION_CACHE:
        _VERSION_CACHE[_VERSION_CACHE_KEY] = _detect_claude_code_version()
    return _VERSION_CACHE[_VERSION_CACHE_KEY]


def _stop_supports_additional_context(version: tuple[int, int, int] | None) -> bool:
    """Stop の `hookSpecificOutput.additionalContext` が効く版数 (2.1.163 以上) か。"""
    return version is not None and version >= _MIN_VERSION_FOR_ADDITIONAL_CONTEXT


def _format_version(version: tuple[int, int, int] | None) -> str:
    return ".".join(str(part) for part in version) if version is not None else "不明"


def _resolve_mode() -> tuple[str, str | None]:
    """`(mode, version_fallback_notice)` を返す (`get_mode()` と `_run_review()` の共通実体)。

    `block` / `context` の明示指定は版数判定を飛ばし、そのまま使う (`context` を明示した
    利用者は 2.1.163 未満での既知の問題を承知の上という前提 — 利用者の責任)。

    それ以外 (未設定 / `MODE_AUTO` / 未知の値) はすべて自動選択: `_claude_code_version()`
    で検出した版数が `_stop_supports_additional_context()` を満たせば `context`、満たさない
    (未満 または 検出できない) なら **`block` に倒す** (fail-closed: 指摘を Claude に
    届かないまま失う方向には倒さない。コストは legacy の `decision: "block"` 表示に
    戻るだけ — Codex R1 P1 指摘への対応。モジュール docstring
    「未対応 CLI での自動 fail-closed」節を参照)。

    `version_fallback_notice` は「auto 解決で版数非対応と判定して block に倒した」ときだけ
    利用者向けの付記文を返す (それ以外は None)。明示指定 (`block`/`context`) では常に
    None — 利用者が意図して選んだモードに版数起因の言い訳を混ぜると、「なぜ block なのか」
    の説明が矛盾して見える。
    """
    raw = settings.raw(ENV_MODE).lower()
    if raw == MODE_BLOCK:
        return MODE_BLOCK, None
    if raw == MODE_CONTEXT:
        return MODE_CONTEXT, None

    version = _claude_code_version()
    if _stop_supports_additional_context(version):
        return MODE_CONTEXT, None
    notice = (
        f"(Claude Code {_format_version(version)} は Stop の additionalContext "
        "非対応のため block で差し戻し)"
    )
    return MODE_BLOCK, notice


def get_mode() -> str:
    """`context` (既定・0.8.0 から): `hookSpecificOutput.additionalContext` で所見を渡す
    (hook error に見せない。モジュール docstring の「0.8.0 で入れた変更」参照)。
    `block` にすると 0.7.0 までの `decision: "block"` に戻せる。

    未設定・`auto`・未知の値は、実行中の Claude Code が対応版数 (2.1.163 以上) かを
    自動検出して選ぶ (`_resolve_mode()`)。版数が未満・不明なら **`block` に fail-closed
    する** (0.7.0 までと同じ表示に戻るだけで、指摘そのものは失わない)。版数を理由に
    block へ倒したことを利用者に伝える付記文が要る場合は `_resolve_mode()` を直接使う
    (`_run_review` 参照)。
    """
    return _resolve_mode()[0]


def review_enabled() -> bool:
    """`EXTERNAL_AI_POST_REVIEW=0` で無効化。

    v0.2.0 の `EXTERNAL_AI_POST_REVIEW_MAX` はレビュー回数の予算だったが、ターン
    スコープ化で意味を失ったため撤廃した。ただし `=0` を「hook の無効化スイッチ」
    として使っている既存環境があるので、その用法だけは互換のため生かしている
    (0 以外の数値は無視 = 回数制限は掛からない)。**撤廃済みの死んだ別名**なので、
    新しい変数が設定されていればそちらが勝つ。exitplan-review の
    `EXTERNAL_AI_REVIEW_MAX` は現役の回数予算なので AND で効き、扱いが違う。
    """
    if settings.raw(ENV_ENABLED):
        return settings.flag(ENV_ENABLED, default=True)
    return settings.raw(ENV_LEGACY_MAX) != "0"


def bash_tracking_enabled() -> bool:
    return settings.flag(ENV_BASH_TRACKING, default=True)


def diff_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_reason(cursor_output: str) -> str:
    return (
        "## 実装直後レビュー結果 (Cursor, 差分レビュー)\n\n"
        + cursor_output
        + "\n\n---\n\n"
        "critical な指摘があれば対応し、軽微・妥当でないと判断した指摘は"
        "理由を添えてスキップした上で作業を完了してください。"
    )


# --------------------------------------------------------------------------
# PreToolUse(Bash) / PostToolUse
# --------------------------------------------------------------------------


def handle_pre_tool(payload: dict) -> None:
    """無効化 / cursor 不在なら git も state も一切触らない。

    以前は `bash_tracking_enabled()` しか見ておらず、`EXTERNAL_AI_POST_REVIEW=0`
    や cursor 未インストールの環境でも Bash のたびに `git status` が走っていた。
    Stop 側の `review_enabled()` / `cursor.is_available()` と同じ条件をここでも
    先頭で評価し、無効時は git 呼び出しも state 書込も発生させない。
    """
    if not review_enabled() or not cursor.is_available():
        return
    if payload.get("tool_name") != "Bash" or not bash_tracking_enabled():
        return
    session_id = payload.get("session_id") or ""
    tool_use_id = payload.get("tool_use_id") or ""
    if not session_id or not tool_use_id:
        return
    root = gitscan.worktree_root(payload.get("cwd") or os.getcwd())
    if not root:
        return
    snapshot = gitscan.status_snapshot(root)
    if snapshot is None:
        # git status 失敗 (timeout / 非 0 終了) / MAX_SNAPSHOT_ENTRIES 超過で不完全。
        # null を書いて後で pop 側に判定させるより、そもそも書かない方が
        # $TMPDIR に残骸を増やさない (pop_bash_snapshot はファイル無しでも None を
        # 返すので、後続の属性付け断念という効果は変わらない)
        log("Bash 前: git status 失敗のため snapshot を保存しない (属性付けを断念)")
        return
    state.save_bash_snapshot(session_id, tool_use_id, snapshot)


def handle_post_tool(payload: dict) -> None:
    """無効化 / cursor 不在なら git も state も一切触らない
    (handle_pre_tool と同じ理由)。"""
    if not review_enabled() or not cursor.is_available():
        return
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    tool_name = payload.get("tool_name") or ""
    cwd = payload.get("cwd") or os.getcwd()

    if tool_name in _EDIT_TOOLS:
        paths = _edited_paths(payload.get("tool_input") or {}, cwd)
        if paths:
            state.record_pending(session_id, paths)
        return

    if tool_name == "Bash":
        _record_bash_changes(payload, session_id, cwd)


def _edited_paths(tool_input: dict, cwd: str) -> list[str]:
    """編集系ツールの入力から対象パスを取り出す。

    Write / Edit は `file_path` に絶対パスが安定して入る (CLI 2.1.233 実測)。
    NotebookEdit は現環境に非搭載だが、搭載環境で `notebook_path` を使う可能性が
    あるため両方見る。MultiEdit は現環境に存在しないので matcher からも外している。
    """
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return []
    return [raw if os.path.isabs(raw) else os.path.join(cwd, raw)]


def _record_bash_changes(payload: dict, session_id: str, cwd: str) -> None:
    """Bash 実行前後の status スナップショット差分を pending に積む。

    pre / post どちらかの `git status` が失敗 (timeout / 非 0
    終了 / MAX_SNAPSHOT_ENTRIES 超過で不完全) なら、比較そのものを諦める。
    以前は失敗時に `status_snapshot` が `{}` を返しており、`changed_between({}, post)`
    が post 側の全エントリ (他セッション・他人の変更を含む) を「変化あり」として
    pending に積んでいた (逆に post 失敗時は pre 側の全件が対象になる)。
    """
    if not bash_tracking_enabled():
        return
    tool_use_id = payload.get("tool_use_id") or ""
    if not tool_use_id:
        return
    pre = state.pop_bash_snapshot(session_id, tool_use_id)
    if pre is None:
        log("Bash 後: pre スナップショットが無い (git 外/取得失敗/未実行) ため属性付けを断念")
        return
    root = gitscan.worktree_root(cwd)
    if not root:
        return
    post = gitscan.status_snapshot(root)
    if post is None:
        log("Bash 後: post の git status が失敗したため属性付けを断念")
        return
    changed = gitscan.changed_between(pre, post)
    if changed:
        state.record_pending(session_id, [os.path.join(root, rel) for rel in changed])


# --------------------------------------------------------------------------
# Stop
# --------------------------------------------------------------------------


def handle_stop(payload: dict) -> None:
    if payload.get("stop_hook_active"):
        log("stop_hook_active=True によりスキップ (再帰防止)")
        return

    session_id = payload.get("session_id") or ""
    if not session_id:
        log("session_id が空")
        return

    stategc.gc_stale()

    if not review_enabled():
        log("EXTERNAL_AI_POST_REVIEW=0 によりレビュー無効化")
        return
    if not cursor.is_available():
        log("cursor 未インストール")
        return

    cwd = payload.get("cwd") or os.getcwd()
    root = gitscan.worktree_root(cwd)
    if not root:
        log("git worktree 外のため skip")
        return

    # cooldown は claim の**前**に見る (claim すると pending を消費してしまう)。
    # cursor lock も取らない — ロックを取らずに済むなら他セッションを待たせない。
    cooled = _cooldown_notice(session_id)
    if cooled:
        log(cooled)
        json.dump(_with_notices({}, [cooled]), sys.stdout, ensure_ascii=False)
        return

    # cursor lock を先に取る。取れなければ claim もしないので pending は温存される。
    # state lock は claim_pending() の内側で完結し、cursor 実行中は保持しない。
    with state.cursor_lock(root) as acquired:
        if not acquired:
            log("同一作業ツリーで別セッションがレビュー中のため skip (pending は温存)")
            return
        output = _review_claim(payload, session_id, root)

    if output:
        json.dump(output, sys.stdout, ensure_ascii=False)


def _cooldown_notice(session_id: str) -> str | None:
    """cooldown 中なら利用者向けの一文を返す (そうでなければ None)。

    `EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC` は「前回レビュー完了から N 秒未満なら
    今回は走らせない」。**pending は消費しない**ので、貯まった変更は cooldown 明けの
    Stop でまとめて 1 回のレビューに載る。

    pending が空のターンでは黙る。編集していないターンまで毎回通知すると、
    通知そのものがノイズになって読まれなくなる。

    **副作用として、pending が空で in-flight だけが TTL 超過している場合は cooldown を
    素通りして `claim_pending()` の回収経路に入る**。cooldown (`> IN_FLIGHT_TTL_SEC` の
    設定時のみ起きる) より「kill されたレビューを取りこぼさない」ほうを優先する。
    """
    cooldown = settings.count(ENV_COOLDOWN, 0)
    if cooldown <= 0:
        return None
    remaining = cooldown - (time.time() - state.last_review_at(session_id))
    if remaining <= 0:
        return None
    waiting = state.pending_count(session_id)
    if not waiting:
        return None
    return (
        f"前回レビューから {cooldown} 秒 ({ENV_COOLDOWN}) 未満のためレビューを見送り: "
        f"{waiting} ファイルは残り約 {int(remaining)} 秒後のターンでまとめてレビューします"
    )


def _review_claim(payload: dict, session_id: str, root: str) -> dict:
    """claim を取り、**何が起きても握りっぱなしにしない**ことを保証する薄い外枠。

    claim を取った後で例外が出ると、in-flight にエントリが残ったまま hook が死ぬ。
    復元されるのは TTL (`IN_FLIGHT_TTL_SEC` = 900s) 超過後の Stop なので、その間
    このセッションの変更は pending へ戻らず**レビューが 15 分沈黙する**。呼び出し元の
    fail-open (`__main__` の `except Exception`) はプロセスを守るだけで状態は戻さない。

    実際の経路: `EXTERNAL_AI_POST_REVIEW_TIMEOUT` に非有限値が入ると
    `Popen.communicate(timeout=nan)` が `ValueError` を投げる (この穴は
    `_common/settings.py` 側でも塞いだが、**例外の出どころを 1 つ塞ぐより
    「claim は必ず戻る」を構造で保証するほうが強い**)。

    復元は `claimed` 全件に対して行う (レビュー済みのものが混ざっても、hash 一致で
    次回の `_collect_diffs` が落とすので二重レビューにはならない)。除外済みパスが
    戻っても、除外は claim のたびに再適用されるので外部に送られることはない。
    """
    claim = state.claim_pending(session_id)
    if claim is None:
        log("このセッションが変更したファイルが無いため skip")
        return {}
    claim_id, claimed = claim
    try:
        return _run_review(session_id, root, claim_id, claimed)
    except Exception as e:
        log(f"レビュー中に例外 (claim を pending へ戻す): {e}")
        state.restore_claim(session_id, claim_id, claimed)
        return _with_notices(
            {},
            [
                f"レビューを完了できませんでした ({type(e).__name__})。"
                f"{len(claimed)} ファイルは次のレビューに持ち越します"
            ],
        )


def _run_review(session_id: str, root: str, claim_id: str, claimed: list[str]) -> dict:
    """除外 → diff 収集 → cursor → 状態確定。stdout に出す JSON (無ければ {}) を返す。

    利用者向けの通知 (除外・繰り越し・切り詰め) は `systemMessage` にまとめる (block 時は
    `decision` / `reason` と同居させる。公式 docs の共通フィールドで Stop でも有効)。
    systemMessage が表示されない環境でも stderr に同じ内容を残し、除外そのものは通知の
    配信に依存しない。

    ここから送出された例外は `_review_claim` が拾って claim を復元する。
    """
    notices: list[str] = []

    # 基点フォールバックの「前回の基点」を読んでから、今回の HEAD で
    # 上書きする (次の Stop の基点になる)。順序が重要 — 先に上書きすると、今回
    # 自身が作った HEAD を「前回の基点」として誤って使ってしまう。current_head
    # が None (HEAD 不在 = 初回 commit 前) の場合は上書きしない (前回の値を温存)。
    old_base = state.get_base_sha(session_id)
    new_head = gitscan.current_head(root)
    if new_head:
        state.set_base_sha(session_id, new_head)

    rels, overflow, excluded = _resolve_paths(root, claimed, exclusion.load_policy())
    if excluded:
        # 除外は恒久: pending にも reviewed にも残さない。ファイル名は出すが内容は出さない
        notices.append(
            f"{len(excluded)} ファイルを外部 AI レビューから除外 (内容は送信していません): "
            + _list_names(f"{name} ({reason})" for name, reason in excluded)
        )
    if overflow:
        notices.append(
            f"{len(overflow)} ファイルは 1 回あたり {MAX_REVIEW_PATHS} 件の上限により"
            "次ターンに繰り越し: " + _list_names(_rel_names(root, overflow))
        )

    batch = _collect_diffs(root, rels, state.reviewed_hashes(session_id), old_base)
    # 繰り越しは捨てずに pending へ戻す (次の Stop でレビューされる)。claim 順を保って 1 回で
    # 積む: 予算超過 (rels の途中) → 時間切れ (rels の末尾) → 上限超過 (rels の外) の順
    carried = batch.deferred + overflow
    if carried:
        state.record_pending(session_id, carried)
    if batch.unretrievable:
        # HEAD 基準の diff が空で、基点フォールバックでも証明できず
        # 救えなかったパス。pending には戻さない (証明できない状況が変わらない
        # 限り毎ターン同じ結果になるだけなので、繰り返し報告しない)。
        notices.append(
            f"{len(batch.unretrievable)} ファイルは差分が空で取得できませんでした "
            "(commit 済みの可能性。内容は送信していません): "
            + _list_names(_rel_names(root, batch.unretrievable))
        )
    if batch.deferred_time:
        notices.append(
            f"{len(batch.deferred_time)} ファイルは git diff の時間予算超過により"
            "次ターンに繰り越し: " + _list_names(_rel_names(root, batch.deferred_time))
        )
    if batch.deferred_size:
        notices.append(
            f"{len(batch.deferred_size)} ファイルは diff 合計 {MAX_DIFF_BYTES // 1000} KB の"
            "予算に収まらないため次ターンに繰り越し (レビュー済みにはしません): "
            + _list_names(_rel_names(root, batch.deferred_size))
        )
    if batch.truncated:
        notices.append(
            f"{len(batch.truncated)} ファイルは diff が {MAX_FILE_DIFF_BYTES // 1000} KB を"
            "超えるため先頭のみ送信 (truncated): "
            + _list_names(f"{rel} ({size} bytes)" for rel, size in batch.truncated)
        )
    for notice in notices:
        log(notice)

    if not batch.sections:
        log("レビュー対象の差分が無い (空 diff / 前回と同一 / 除外のみ) ため skip")
        state.complete_claim(session_id, claim_id, {})
        return _with_notices({}, notices)

    # しきい値は「実際に送る diff」で測る (除外・繰り越し後の量が課金に対応するため)
    min_lines = settings.count(ENV_MIN_LINES, 0)
    changed_lines = _count_changed_lines(batch.sections)
    if min_lines > 0 and changed_lines < min_lines:
        # 消費せず pending に戻す (cursor 失敗時と同じ経路。hash も記録しない)
        state.restore_claim(session_id, claim_id, batch.submitted)
        notices.insert(
            0,
            f"変更 {changed_lines} 行が {ENV_MIN_LINES}={min_lines} に満たないため"
            f"レビューを見送り: {len(batch.submitted)} ファイルは次のレビューにまとめます",
        )
        log(notices[0])
        return _with_notices({}, notices)

    diff_text = "\n".join(batch.sections)
    log(f"Cursor によるレビューを実行 ({len(batch.submitted)} ファイル, {len(diff_text)} chars)")
    started = time.monotonic()
    result = cursor.review(diff_text, cwd=root)
    elapsed = time.monotonic() - started
    state.mark_review_done(session_id)
    summary = f"差分レビュー完了 ({notify.format_elapsed(elapsed)}, {len(batch.submitted)} ファイル)"

    if not result:
        log("Cursor レビュー失敗 (fail-open、pending に戻す)")
        state.restore_claim(session_id, claim_id, batch.submitted)
        notices.insert(0, f"{summary} → 結果を取得できず (timeout / 失敗)。次ターンに持ち越し")
        return _with_notices({}, notices)

    if is_clean_review(result):
        log("Cursor: REVIEW_CLEAN (block しない、レビュー済みとして確定)")
        state.complete_claim(session_id, claim_id, batch.hashes)
        notices.insert(0, f"{summary} → 指摘なし")
        return _with_notices({}, notices)

    reason = build_reason(result)
    state.complete_claim(session_id, claim_id, batch.hashes)
    _save_review_copy(session_id, reason)
    # 通知文はモードで分岐させる (hook が「したこと」だけを述べる)。block (明示、または
    # auto 解決で版数非対応と判定した場合) はハーネスが継続を保証するので「依頼しました」
    # と言い切ってよい。既定の auto 解決は、版数が additionalContext に対応していれば
    # context を選ぶのでここでも「依頼しました」は真になる。版数非対応で自動的に block
    # へ倒れたときだけ、`_resolve_mode()` の付記文でどの版数だったか (不明なら「不明」)
    # を添える — 明示 `MODE=block` では付記文は None なので何も足されない。
    mode, version_fallback_notice = _resolve_mode()
    if mode == MODE_BLOCK:
        message = f"{summary} → 指摘あり (Claude に対応を依頼しました)"
        if version_fallback_notice:
            message += f" {version_fallback_notice}"
        notices.insert(0, message)
        return _with_notices({"decision": "block", "reason": reason}, notices)
    notices.insert(0, f"{summary} → 指摘あり (レビュー結果を Claude の文脈に渡しました)")
    return _with_notices(
        {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": reason,
            }
        },
        notices,
    )


def _count_changed_lines(sections: list[str]) -> int:
    """diff の追加・削除行数を数える。

    しきい値の単位を「ファイル数」ではなく行数にしているのは、typo 1 行の修正と
    1 ファイル 300 行の書き換えを区別したいのが本設定の主旨だから。

    **接頭辞でファイルヘッダを判別してはいけない**。`sections` の 1 要素は
    `gitscan.path_diff` が返す 1 ファイル分の diff なので、`--- a/path` /
    `+++ b/path` は必ず最初の `@@` より前に来る。逆に `@@` より後ろでは:

    - `-- コメント` (SQL / Lua / Haskell) を削除した行が `--- コメント`
    - `++ 何か` を追加した行が `+++ 何か`

    になり、`"--- "` / `"+++ "` で弾くと中身の行まで落ちる。「SQL のコメント行
    だけ消したターン」が 0 行と数えられ、`MIN_LINES` に引っかかって実質的な変更が
    黙って skip される (最初の実装は `"---"` / `"+++"`、次が `"--- "` / `"+++ "`。
    どちらも中身の行と区別できていなかった — 接頭辞では原理的に無理)。

    **最初の `@@` 以降だけを数える**のが唯一の正確な方法。hunk ヘッダ自体は `@`
    始まりなので数に入らず、中身がどんな文字列でも誤判定しない。`@@` を含まない
    section (binary 差分など) は 0 行。
    """
    total = 0
    for section in sections:
        in_hunk = False
        for line in section.splitlines():
            if not in_hunk:
                # ヘッダ領域 (`diff --git` / `index` / `---` / `+++`) を読み飛ばす
                in_hunk = line.startswith("@@")
                continue
            if line.startswith(("+", "-")):
                total += 1
    return total


def _with_notices(output: dict, notices: list[str]) -> dict:
    """利用者向け通知を `systemMessage` に載せる (組み立ては両 review hook 共通)。

    書いてよい内容の線引き (要約と件数のみ。レビュー本文・diff は出さない) は
    `_common/notify.py` の docstring を正典とする。
    """
    message = notify.compose("post-implementation-review", notices)
    if message:
        output["systemMessage"] = message
    return output


def _list_names(names) -> str:
    items = list(names)
    shown = ", ".join(items[:MAX_LISTED_NAMES])
    if len(items) > MAX_LISTED_NAMES:
        shown += f", 他 {len(items) - MAX_LISTED_NAMES} 件"
    return shown


def _rel_names(root: str, abs_paths: list[str]) -> list[str]:
    return [gitscan.to_relative(root, p) or p for p in abs_paths]


def _resolve_paths(
    root: str, claimed: list[str], policy: exclusion.Policy
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """claim したパスを作業ツリー相対に正規化し、(rels, overflow_abs, excluded) を返す。

    作業ツリー外の絶対パスはここで落ちる (復元もしない — 残すと毎 Stop 走査され続ける)。
    ディレクトリも落とす: 入れ子の git リポジトリは `git status -uall` でも `dir/` の
    まま出てくるため、v0.3.0 が書いた state に残っている可能性がある。

    除外 (exclusion.Policy) もここで当てる。除外されたパスは作業ツリー外と同じく
    **復元しない・hash も記録しない** (恒久除外)。上限 (MAX_REVIEW_PATHS) の手前で除外する
    ので、除外ファイルが枠を食ったり overflow として pending に戻ったりしない。
    判定には実体 (realpath 相対。git に渡すのもこれ)、**lexical なパス** (root だけ
    realpath で同定し、配下の symlink 構成要素名はそのまま残したもの)、**別名** (repo 内の
    symlink を列挙し、実体がその target 配下なら `link + 残り` を生成) のすべてを渡す —
    `credentials/` → `ordinary/` のような symlink ディレクトリ経由の claim や機密名の
    リンクを、どの名前が機密に見えても外部に送らない (安全側に倒す)。別名が要るのは
    Bash 経由の変更: `git status` は実体名しか返さないので lexical 名が claim に現れない。

    順序は claim 順 (= pending に積まれた順) を保つ。前回 Stop が繰り越した (pending に
    戻した) パスは次ターンの先頭に来るので、予算超過で繰り越されたファイルが新しい編集に
    毎回追い越されて永久に残ることがない。
    """
    root_real = os.path.realpath(root).rstrip(os.sep) or os.sep
    symlinks = gitscan.symlink_map(root)
    # 実体 (realpath 相対) ごとに判定候補を集める。同じ実体が別名 (symlink) で複数回 claim
    # されていても、どの名前が機密に見えるかを全部見てから判定する
    candidates: dict[str, list[str]] = {}
    for path in claimed:
        rel = gitscan.to_relative(root, path)
        if not rel or os.path.isdir(os.path.join(root, rel)):
            continue
        names = candidates.setdefault(rel, [rel])
        lexical = _lexical_relative(root_real, path)
        for name in [lexical, *exclusion.expand_aliases(rel, symlinks)]:
            if name and name not in names:
                names.append(name)

    rels: list[str] = []
    excluded: list[tuple[str, str]] = []
    for rel, names in candidates.items():
        hit = policy.explain(names)
        if hit is not None:
            excluded.append(hit)  # (当たった名前, 理由)
            continue
        rels.append(rel)
    overflow = [os.path.join(root, r) for r in rels[MAX_REVIEW_PATHS:]]
    return rels[:MAX_REVIEW_PATHS], overflow, excluded


def _lexical_relative(root_real: str, path: str) -> str | None:
    """claim されたパスを、root 配下の symlink を解決せずに (lexical に) root 相対へ変換する。

    `to_relative` (全体を realpath) だと `credentials/` → `ordinary/` のような symlink
    ディレクトリ経由の claim が `ordinary/data.json` になり、除外判定から `credentials` が
    消える (Codex PR レビュー P1)。親ディレクトリだけ realpath する方式も同じ穴があった。
    ここでは **root の別名** (`/tmp` → `/private/tmp`、symlink された親ディレクトリ) だけを
    realpath で同定し、その下の構成要素は名前のまま残す。祖先を浅い方から試して最初に root と
    一致したところで切るので、root 配下に root 自身へ戻る symlink があっても途中の名前は残る。
    root の別名が見つからなければ None (作業ツリー外)。
    """
    parts = os.path.normpath(path).split(os.sep)
    for cut in range(1, len(parts)):
        prefix = os.sep.join(parts[:cut]) or os.sep
        if os.path.realpath(prefix) == root_real:
            return os.sep.join(parts[cut:]) or None
    return None


class ReviewBatch:
    """`_collect_diffs` の結果。submitted / hashes は cursor に渡すファイルだけ。

    dataclass にしていないのは、テストが `__main__.py` を `sys.modules` 未登録のまま
    `exec_module` で読むため (`from __future__ import annotations` 下の dataclass は
    モジュール名前空間の解決で落ちる)。
    """

    def __init__(self) -> None:
        self.sections: list[str] = []
        self.submitted: list[str] = []  # 絶対パス
        self.hashes: dict[str, str] = {}
        self.deferred_time: list[str] = []  # 時間予算で未処理 (絶対パス)
        self.deferred_size: list[str] = []  # 合計バイト予算で未送信 (絶対パス)
        self.truncated: list[tuple[str, int]] = []  # (rel, 切り詰め前の bytes)
        self.unretrievable: list[str] = []  # HEAD 基準が空で基点でも救えなかった絶対パス

    @property
    def deferred(self) -> list[str]:
        """pending へ戻す絶対パス (未レビュー)。claim 順 = バイト予算 (途中) → 時間切れ (末尾)。"""
        return self.deferred_size + self.deferred_time


def _collect_diffs(
    root: str,
    rels: list[str],
    reviewed: dict[str, str],
    base_sha: str | None = None,
) -> ReviewBatch:
    """パスごとに diff を取り、予算に収まるものだけを ReviewBatch に積む。

    前回レビュー時と同一 hash のパスは載せない。差分が空のパス (commit 済み・revert
    済み) も (基点フォールバックで救えない限り) 載せない。どちらも submitted に
    入らないので、cursor 失敗時にも復元されずそのまま消える。

    **HEAD 基準の diff が空のパスへの基点フォールバック**: tracked かつ
    HEAD が存在するパスの HEAD 基準 diff が空なのは、(a) 単に何も変わっていない
    (revert 済み等)、または (b) このセッションが同一ターン内で commit し、
    ファイルが既に HEAD と一致している (元のバグ) のどちらかで、この時点では
    区別できない。`base_sha` (前回 Stop が記録した HEAD) がある場合だけ:

    1. `gitscan.is_local_only_range(root, base_sha)` で `base_sha..HEAD` の
       全 commit が手元由来 (どのリモートにも存在しない) と証明できたときだけ
       `gitscan.diff_since(root, rel, base_sha)` を試す。証明できなければ
       (pull 等で他人の commit が混ざっている) 試さない — 送信範囲が広がる
       方向には倒さない
    2. それでも空なら「本当に何も変わっていない」と確定するので黙って捨てる
       (base_sha が無い場合と同じ経路)
    3. 基点フォールバックでも救えず、かつそのパスが実際に存在する (phantom な
       pending エントリではない) なら `batch.unretrievable` に積む —
       黙って消費せず、利用者にレビューされなかったことを可視化するため
       (`_run_review` が通知にする)。`base_sha` 自体が無い (このセッションの
       初回 Stop) 場合も「証明できない」の一種としてここに含める

    **予算はファイル単位で当てる**:

    - 1 ファイルが MAX_FILE_DIFF_BYTES を超える → 先頭だけを `(truncated)` 付きで送り、
      hash は全文で記録する (変わらない限り再掲しない。変われば切り詰めた形で再掲)
    - 積み上げ合計が MAX_DIFF_BYTES を超えるファイル → 送らず deferred_size へ
      (hash を記録しないので次ターンにそのまま再掲される)。後続の小さいファイルは
      予算が残っていれば送る (first-fit)

    COLLECT_BUDGET_SEC を超えた時点で打ち切り、未処理パスを deferred_time として返す。
    Stop 全体の hook timeout (700s) のうち cursor が上限 600s + kill 猶予 15s を使うため、
    git に使える時間は限られる (`gitscan.py` モジュール docstring の予算表を参照)。
    経過時間で頭を押さえる。deferred は捨てずに pending へ戻す。
    """
    untracked = gitscan.untracked_among(root, rels)
    has_head = gitscan.head_exists(root)

    batch = ReviewBatch()
    used = 0
    deadline = time.monotonic() + COLLECT_BUDGET_SEC

    # is_local_only_range は base_sha..HEAD 全体に対する判定 (パスごとに変わらない)
    # なので、必要になった最初の 1 回だけ計算してこの Stop 内で使い回す。
    local_only_cache: dict[str, bool] = {}

    def local_only() -> bool:
        if base_sha is None:
            return False
        if base_sha not in local_only_cache:
            local_only_cache[base_sha] = gitscan.is_local_only_range(root, base_sha)
        return local_only_cache[base_sha]

    for index, rel in enumerate(rels):
        if time.monotonic() > deadline:
            batch.deferred_time = [os.path.join(root, r) for r in rels[index:]]
            break

        is_untracked = rel in untracked
        text = gitscan.path_diff(root, rel, is_untracked, has_head)
        # HEAD 基準で空 = 「本当に無変更」と「同一ターン内 commit で HEAD と
        # 一致した」のどちらかで、この時点では区別できない (docstring 参照)。
        empty_at_head = not is_untracked and has_head and not text.strip()
        if empty_at_head and base_sha is not None and local_only():
            text = gitscan.diff_since(root, rel, base_sha)

        if not text.strip():
            if (
                empty_at_head
                and os.path.exists(os.path.join(root, rel))
                and (base_sha is None or not local_only())
            ):
                # 証明できない (基点が無い、または他人の commit が混ざっている)
                # ため取得を諦める。パスが実在する (phantom な pending エントリ
                # ではない) ときだけ「取得できなかった」と可視化する
                batch.unretrievable.append(os.path.join(root, rel))
            continue
        abs_path = os.path.join(root, rel)
        digest = diff_hash(text)  # hash は切り詰め前の全文で取る
        if reviewed.get(abs_path) == digest:
            continue

        full_size = len(text.encode())
        if full_size > MAX_FILE_DIFF_BYTES:
            text = _truncate_section(text, MAX_FILE_DIFF_BYTES)
        size = len(text.encode())
        separator = 1 if batch.sections else 0  # "\n".join の区切り分
        if used + separator + size > MAX_DIFF_BYTES:
            batch.deferred_size.append(abs_path)
            continue

        batch.sections.append(text)
        batch.submitted.append(abs_path)
        batch.hashes[abs_path] = digest
        used += separator + size
        if full_size > MAX_FILE_DIFF_BYTES:
            batch.truncated.append((rel, full_size))
    return batch


_TRUNCATED_MARKER = (
    "\n... (truncated for review: only the first part of this file's diff is shown; "
    "{full} bytes in total)\n"
)


def _truncate_section(text: str, limit: int) -> str:
    """1 ファイル分の diff を marker 込みで limit バイト以下に切り詰める。

    行の途中で切ると diff の hunk が壊れて読みにくいので、可能なら最後の改行で切る
    (ただし半分未満まで戻るほど長い行なら byte 境界で切る)。UTF-8 の途中で切れた
    バイトは捨てる。
    """
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    marker = _TRUNCATED_MARKER.format(full=len(encoded))
    budget = max(limit - len(marker.encode()), 0)
    cut = encoded[:budget]
    newline = cut.rfind(b"\n")
    if newline >= budget // 2:
        cut = cut[:newline]
    return cut.decode("utf-8", errors="ignore") + marker


def _save_review_copy(session_id: str, reason: str) -> None:
    path = state.review_copy_path(session_id)
    try:
        flock.write_private(path, reason)
        log(f"レビュー完了 → {path}")
    except OSError:
        log("参照コピーの保存に失敗")


# --------------------------------------------------------------------------


def parse_phase(argv: list[str]) -> str:
    if "--phase" in argv:
        idx = argv.index("--phase")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return "stop"


def main(argv: list[str] | None = None) -> None:
    phase = parse_phase(argv if argv is not None else sys.argv[1:])
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"stdin JSON パース失敗: {e}")
        return
    if not isinstance(payload, dict):
        return

    if phase == "pre-tool":
        handle_pre_tool(payload)
    elif phase == "post-tool":
        handle_post_tool(payload)
    else:
        handle_stop(payload)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:  # hook が例外で Claude Code を止めないよう fail-open
        log(f"fatal: {e}")
