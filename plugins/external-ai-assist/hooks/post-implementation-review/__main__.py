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

## 未対応 CLI での自動 fail-closed (同じ 0.8.0 batch への追補、マージ前レビューの指摘)

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
import stat
import subprocess
import time

# hooks/_common を解決するため、hook 内モジュールより先に hooks/ を sys.path に載せる
# (plugin root 内の相対配置なので ${CLAUDE_PLUGIN_ROOT} が cache コピーでも壊れない)。
_HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

from _common import backends, flock, hooklog, notify, sentinel, settings  # noqa: E402

import cursor  # noqa: E402
import exclusion  # noqa: E402
import gitscan  # noqa: E402
import reflog  # noqa: E402
import selection  # noqa: E402
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

# commit レビュー (PostToolUse(Bash)) の diff 収集に使う時間予算。Stop の
# COLLECT_BUDGET_SEC より短いのは、同じ hook 呼び出しの中で窓全体の `--name-only`
# (`range_paths` 1 回) と `changed_vs_head` 2 回 (送信前の P4 判定 + レビュー後の
# 整理) が乗るため (突合は tests/test_review_set.py::TestTimeoutBudgets)。
COMMIT_COLLECT_BUDGET_SEC = 20

# systemMessage / stderr に列挙するファイル名の上限 (それ以上は件数だけ)
MAX_LISTED_NAMES = 10

# 内容指紋 (commit レビューの P6) を取る 1 ファイルの上限。これを超えるファイルは
# 「指紋なし」= commit レビューで内容を送らない。PostToolUse(Write/Edit) は 10 秒の
# hook timeout を git 無しで回している経路なので、読み取りコストをここで頭打ちにする
# (1 MiB の sha256 は macOS / Python 3.14 で 1 ms 未満。計測は CLAUDE.md の表)。
FINGERPRINT_MAX_BYTES = 1024 * 1024

# commit された blob を読んで指紋と突き合わせるときの合計バイト上限。窓の中で巨大な
# ファイルに差し替えて commit されたケースで、hook のメモリに数百 MB を載せないため。
# 超えた分は「取得できなかった」= 送らない側に落ちる。
FINGERPRINT_TOTAL_MAX_BYTES = 8 * 1024 * 1024

_EDIT_TOOLS = ("Write", "Edit", "NotebookEdit")

ENV_ENABLED = "EXTERNAL_AI_POST_REVIEW"
ENV_LEGACY_MAX = "EXTERNAL_AI_POST_REVIEW_MAX"
ENV_BASH_TRACKING = "EXTERNAL_AI_POST_REVIEW_BASH_TRACKING"
ENV_COMMIT = "EXTERNAL_AI_POST_REVIEW_COMMIT"
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

# pre-tool / post-tool で cursor CLI の検出 (`cursor.is_available`) に許す秒数。
#
# 0.11.0 の検出はキャッシュが使えない環境で hook 1 回ごとに probe する
# (`_common/cursorcli.py`)。この 2 フェーズは Write / Edit / NotebookEdit / Bash の
# たびに走り、同じ 10 秒の hook timeout の中で git (rev-parse 2s + status 5s = 最悪 7s) と
# 同居するため、検出には残りより短い枠を渡す (`cursorcli.PROBE_BUDGET_SEC` = 3 秒を
# そのまま使うと合計 10 秒で hook timeout と同着になり、ハーネスの kill が自前の
# fail-open より先に来る)。予算切れは保留 (`PROBE_UNKNOWN`) に落ちるだけで、機能は
# 止まらない。Stop (690 秒) には渡さない。
# 突合は `tests/test_review_set.py::TestTimeoutBudgets`。
PER_TOOL_PROBE_BUDGET_SEC = 2.0

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
    戻るだけ — マージ前レビューの指摘への対応。モジュール docstring
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


def commit_review_enabled() -> bool:
    """`EXTERNAL_AI_POST_REVIEW_COMMIT=0` で commit 単位レビュー (0.12.0) だけを切る。

    `EXTERNAL_AI_POST_REVIEW=0` (hook 全体) / `..._BASH_TRACKING=0` (Bash 追跡ごと) でも
    止まる。BASH_TRACKING を切ると pre-tool が窓の起点を保存しないため、commit 検出は
    自然に fail-closed になる (`reflog.py` の fail-closed 1)。
    """
    return settings.flag(ENV_COMMIT, default=True)


def diff_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


#: レビュー本文のヘッダに出す backend の表示名 (未知の名前はそのまま使う)。
_BACKEND_LABELS = {"cursor": "Cursor", "codex": "Codex"}

#: `systemMessage` に出す試行結果の表示名 (レビュー本文は入れない)。
_STATUS_LABELS = {
    backends.STATUS_OK: "完了",
    backends.STATUS_FAILED: "失敗",
    backends.STATUS_LIMIT: "利用上限",
}


#: `build_reason` のヘッダに出すレビュー単位。Stop は未 commit の差分、
#: PostToolUse(Bash) は「その Bash の中で作られた commit」を見る (0.12.0)。
SCOPE_DIFF = "差分レビュー"
SCOPE_COMMIT = "commit レビュー"


def build_reason(
    review_output: str, backend_name: str = cursor.NAME, scope: str = SCOPE_DIFF
) -> str:
    """レビュー本文を Claude へ返す形に整える。

    ヘッダに backend 名を出すのは、同じセッションで Stop と後続のレビューが別の
    backend に振られうる (`selection` の `alternate`) ため — どの目で見た指摘かが
    分からないと、Claude も利用者も 2 回目の指摘を 1 回目の焼き直しと区別できない。
    `scope` は同じ理由で「どの単位を見た指摘か」(未 commit の差分 / commit) を示す。
    """
    label = _BACKEND_LABELS.get(backend_name, backend_name)
    return (
        f"## 実装直後レビュー結果 ({label}, {scope})\n\n"
        + review_output
        + "\n\n---\n\n"
        "critical な指摘があれば対応し、軽微・妥当でないと判断した指摘は"
        "理由を添えてスキップした上で作業を完了してください。"
    )


# --------------------------------------------------------------------------
# PreToolUse(Bash) / PostToolUse
# --------------------------------------------------------------------------


def _private_root_ok() -> bool:
    """状態ディレクトリ (`state.state_root()`) が安全に使えるかを確認する。

    共有 `$TMPDIR` では他ユーザーが先回りして所有者違い/誰でも書けるディレクトリを
    作れる (マージ前レビューの指摘)。安全でなければ False を返し、呼び出し側は state の
    読み書きを一切行わない — この plugin は差分を外部 AI CLI に送るので、状態を
    信用できない環境では動かないほうが安全 (`_common/flock.py` の
    `ensure_private_root` docstring 参照)。

    ここでは log のみで `systemMessage` は出さない: `pre-tool` / `post-tool` は
    ツール呼び出しのたびに発火するため、ここで通知すると「毎回エラーに見える」
    形になってしまう。利用者への 1 行通知は `handle_stop` 側の同じ検査に
    一本化する (ターンに 1 回だけ発火する)。
    """
    try:
        flock.ensure_private_root(state.state_root())
        return True
    except flock.UnsafeStateDirError:
        log("状態ディレクトリを安全に使えないため、この呼び出しでは state を書き込まない")
        return False


def _per_tool_deadline() -> float:
    """pre-tool / post-tool で cursor CLI の検出に許す締切 (time.monotonic 基準)。"""
    return time.monotonic() + PER_TOOL_PROBE_BUDGET_SEC


def handle_pre_tool(payload: dict) -> None:
    """無効化 / cursor 不在なら git も state も一切触らない。

    以前は `bash_tracking_enabled()` しか見ておらず、`EXTERNAL_AI_POST_REVIEW=0`
    や cursor 未インストールの環境でも Bash のたびに `git status` が走っていた。
    Stop 側の `review_enabled()` / `cursor.is_available()` と同じ条件をここでも
    先頭で評価し、無効時は git 呼び出しも state 書込も発生させない。

    検出には `PER_TOOL_PROBE_BUDGET_SEC` の締切を渡す (マージ前レビューの指摘): 0.11.0 の
    `is_available()` はキャッシュが使えない環境で probe を伴うため、git の予算と合わせて
    10 秒の hook timeout と同着になりうる。

    0.12.0 で判定を `selection.any_available()` (設定された backend のうち 1 つでも
    起動できるか) に広げた。`EXTERNAL_AI_POST_REVIEW_BACKENDS` 未設定なら候補は cursor
    だけなので、0.11.0 と同じ 1 回の検出しか走らない。
    """
    if not review_enabled() or not selection.any_available(_per_tool_deadline()):
        return
    if payload.get("tool_name") != "Bash" or not bash_tracking_enabled():
        return
    # `bash_tracking_enabled()` の**後**に置く (マージ前レビューの指摘): 前に置くと、
    # Bash 追跡だけを個別に無効化した利用者の Bash 呼び出しでも毎回
    # `os.mkdir`/`chmod` を試みてしまう (この判定より後段の git 呼び出しと
    # 同じく、opt-out した経路には触れない)。
    if not _private_root_ok():
        return
    session_id = payload.get("session_id") or ""
    tool_use_id = payload.get("tool_use_id") or ""
    if not session_id or not tool_use_id:
        return
    root = gitscan.worktree_root(payload.get("cwd") or os.getcwd())
    if not root:
        return
    if commit_review_enabled():
        # commit 検出の窓の起点 (rev-parse 1 回。詳細は reflog.py)。**status とは別
        # ファイルに保存する**ので、巨大な作業ツリーで `git status` が諦めても
        # commit レビューは成立する
        head_log = gitscan.head_log_snapshot(root)
        if head_log is not None:
            state.save_bash_reflog(session_id, tool_use_id, head_log)
    snapshot = gitscan.status_snapshot(root)
    if snapshot is None:
        # git status 失敗 (timeout / 非 0 終了) / MAX_SNAPSHOT_ENTRIES 超過で不完全。
        # null を書いて後で pop 側に判定させるより、そもそも書かない方が
        # $TMPDIR に残骸を増やさない (pop_bash_snapshot はファイル無しでも None を
        # 返すので、後続の属性付け断念という効果は変わらない)
        log("Bash 前: git status 失敗のため snapshot を保存しない (属性付けを断念)")
        return
    state.save_bash_snapshot(session_id, tool_use_id, snapshot)


def handle_post_tool(payload: dict) -> dict:
    """無効化 / backend 不在なら git も state も一切触らない
    (handle_pre_tool と同じ理由。検出の締切も同じ)。

    返り値は stdout に出す JSON (無ければ `{}`)。0.12.0 の commit レビューだけが
    非空を返す — Bash の窓の中で作られた commit の指摘を、**その Bash のツール結果の
    隣に** `hookSpecificOutput.additionalContext` で入れるため。公式 Hooks reference
    逐語: "Claude Code wraps the string in a system reminder and inserts it into the
    conversation at the point where the hook fired" / 挿入位置は PostToolUse では
    "next to the tool result"。サブエージェントが commit した場合もその結果の隣に入る
    (Stop では親セッションにしか返せない — `CLAUDE.md` の「却下した設計案」参照)。
    """
    if not review_enabled() or not selection.any_available(_per_tool_deadline()):
        return {}
    if not _private_root_ok():
        return {}
    session_id = payload.get("session_id") or ""
    if not session_id:
        return {}
    tool_name = payload.get("tool_name") or ""
    cwd = payload.get("cwd") or os.getcwd()

    if tool_name in _EDIT_TOOLS:
        paths = _edited_paths(payload.get("tool_input") or {}, cwd)
        if paths:
            state.record_pending(session_id, paths)
            if commit_review_enabled():
                # P6: ツールが書いた直後の内容の指紋。**git は呼ばない** (この経路は
                # hook timeout 10 秒を git 無しで回している)。commit レビューは、
                # commit された blob のバイトがこれと一致したパスだけ内容を送る。
                # commit レビューを切っている環境では誰も読まないので記録しない
                state.record_fingerprints(
                    session_id, {path: _file_fingerprint(path) for path in paths}
                )
        return {}

    if tool_name == "Bash":
        return _handle_bash(payload, session_id, cwd)
    return {}


def _handle_bash(payload: dict, session_id: str, cwd: str) -> dict:
    """Bash 1 回分の後処理: 変更パスを pending に積み、窓の中の commit をレビューする。

    **I1' の要: commit レビューが使う「編集を記録したパス」は、この Bash の
    status 差分を積む *前* に読む。** `changed_between` は「status から消えたパス」も
    変化として返す (commit / checkout で HEAD と一致したケースを拾うため) ので、
    `git commit -a` を走らせた Bash では**他人が作業ツリーに残していた変更**まで
    「このセッションが変えた」として pending に入る。その後で commit レビューを
    動かすと、他人の内容が積集合を通ってしまう。読む順を入れ替えるだけで
    塞げる (status の取得自体は Bash 直後のまま = 別セッションの編集を巻き込む窓を
    広げない。レビューを先に回して 10 分待ってから status を撮ると、その間の他
    セッションの編集を全部拾ってしまう)。

    **さらに、その窓に commit があった (または窓が信用できなかった) 場合は、
    「status から消えただけで記録に無いパス」を pending に積まない** (マージ前
    レビューの指摘)。読む順の入れ替えは同一窓の混入しか塞がず、積まれた他者パスは
    **次の窓では正規の送信許可**になっていた。通知 (「編集記録が無いので送らない」) と
    state (pending に積む) が矛盾していたのを揃える。

    帰結: 1 回の Bash の中で編集も commit も済ませたパス (`sed -i` + `git commit`)
    は記録に無いので**内容を送らずファイル名だけ通知する**。`git commit -a` が
    他人の作業ツリー変更も巻き込む以上「このセッションが編集した」と言い切れないので、
    これが正しい失敗方向。

    スナップショットは 2 つとも、**他のどの早期 return よりも先に pop する**
    (`BASH_TRACKING` を窓の途中で切られても $TMPDIR に孤児を残さない)。
    """
    tool_use_id = payload.get("tool_use_id") or ""
    if not tool_use_id:
        return {}
    pre_reflog = state.pop_bash_reflog(session_id, tool_use_id)
    pre_status = state.pop_bash_snapshot(session_id, tool_use_id)
    if not bash_tracking_enabled():
        return {}
    root = gitscan.worktree_root(cwd)
    commit_review = commit_review_enabled() and bool(root)
    recorded = state.recorded_paths(session_id) if commit_review else []
    commits, why, why_notice = (
        reflog.appended(pre_reflog) if commit_review else ([], "", None)
    )
    # 窓に commit があった / 窓が信用できなかった (commits is None) ときだけ
    # 「消えただけのパス」を止める。commit の無い普通の Bash は 0.11.0 と同じ挙動
    guard = recorded if (commits is None or commits) else None
    _record_bash_changes(session_id, root, pre_status, guard, commit_review)
    if not commit_review:
        return {}
    # 指紋は `_record_bash_changes` の**後**に読む: 同じ Bash がファイルを書き換えて
    # いれば、そこで落とした指紋をこの窓の判定にも効かせるため (順が逆だと失効が
    # 次の窓からしか効かない)。P1 の集合 (`recorded`) は逆に**前**に読む必要がある
    # ので、2 つの読み取りは意図的に前後へ分かれている。
    # **commit の無い窓では読まない**: `state` の読み出しは flock + read-modify-write
    # なので、commit を含まない Bash のたびに余計な書き込みが 1 回増えてしまう
    fingerprints = state.fingerprints(session_id) if commits else {}
    return _commit_review(
        session_id,
        root,
        pre_reflog,
        pre_status,
        commits,
        why,
        why_notice,
        recorded,
        fingerprints,
    )


def _file_fingerprint(path: str) -> str | None:
    """**この時点でディスク上にある**生バイトの sha256 (commit レビューの P6)。

    取れなければ None = 指紋なし。ツールが書いたバイト列そのものではなく、その直後に
    この hook が読めたバイト列である点に注意 (間に別の書き手が入れば、その内容を
    承認してしまう)。hook の入力にツールが書いた内容は含まれないのでここが上限。

    None になるのは **通常ファイルでない / `FINGERPRINT_MAX_BYTES` 超 / 読めない**
    のいずれか。`state.record_fingerprints` が None を「消す」と解釈するので、
    大きくなったファイルや消えたファイルの古い指紋が残り続けることはない
    (= そのパスは以後 commit レビューで内容を送らない)。

    **symlink は追わない** (`os.lstat` で判定する): 追うと、リンク先の内容の指紋を
    リンク自身の指紋として記録してしまう。git が持つのはリンク先の文字列なので
    どのみち一致せず、追う理由が無い。
    """
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > FINGERPRINT_MAX_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read(FINGERPRINT_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > FINGERPRINT_MAX_BYTES:
        return None
    return hashlib.sha256(data).hexdigest()


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


def _record_bash_changes(
    session_id: str,
    root: str | None,
    pre: dict | None,
    recorded_guard: list[str] | None = None,
    commit_review: bool = False,
) -> None:
    """Bash 実行前後の status スナップショット差分を pending に積む。

    pre / post どちらかの `git status` が失敗 (timeout / 非 0
    終了 / MAX_SNAPSHOT_ENTRIES 超過で不完全) なら、比較そのものを諦める。
    以前は失敗時に `status_snapshot` が `{}` を返しており、`changed_between({}, post)`
    が post 側の全エントリ (他セッション・他人の変更を含む) を「変化あり」として
    pending に積んでいた (逆に post 失敗時は pre 側の全件が対象になる)。

    pre スナップショットの pop と root の解決は呼び出し側 (`_handle_bash`) が行う —
    commit レビューと同じ Bash 呼び出しで共有するため (`git rev-parse` を 2 回
    走らせない)。

    `recorded_guard` が渡された窓 (= commit があった / 窓が信用できなかった) では、
    **post の status から消えただけのパス**のうち guard (このセッションの pending ∪
    in-flight) に無いものを積まない。`git commit -a` が巻き込んだ他人の作業ツリー
    変更がここに出るので、積むと**次の窓の送信許可**になってしまう
    (`_handle_bash` の docstring 参照)。post にまだ残っている (= 未 commit の変更が
    現にある) パスは従来どおり積む — こちらは Bash が実際に作業ツリーを変えた証拠で、
    0.11.0 の Stop も同じものを見る。
    """
    if pre is None:
        log("Bash 後: pre スナップショットが無い (git 外/取得失敗/未実行) ため属性付けを断念")
        return
    if not root:
        return
    post = gitscan.status_snapshot(root)
    if post is None:
        log("Bash 後: post の git status が失敗したため属性付けを断念")
        return
    changed = gitscan.changed_between(pre, post)
    if recorded_guard is not None:
        # 照合は作業ツリー相対で行う (recorded 側は symlink 別名のまま入りうる)
        known = {gitscan.to_relative(root, p) for p in recorded_guard}
        changed = [rel for rel in changed if rel in post or rel in known]
    if commit_review:
        _expire_fingerprints(session_id, root, pre, changed)
    if changed:
        state.record_pending(session_id, [os.path.join(root, rel) for rel in changed])


def _expire_fingerprints(
    session_id: str, root: str, pre: dict, changed: list[str]
) -> None:
    """この Bash が**作業ツリーのファイルを書き換えた**パスの内容指紋を捨てる (P6)。

    判定は P3' と同じ突合 (`[size, mtime_ns, ctime_ns]` が窓の開始時点と現在で
    一致するか)。`sed -i` / フォーマッタ / `git checkout <ref> -- <path>` のように
    Bash がファイルを書き換えたパスは、以後「このセッションのツールが最後に書いた
    内容」を名乗れないので指紋を消す。

    **`changed` 全部を消してはいけない**: `gitscan.changed_between` は「status から
    消えたパス」(= commit されて HEAD と一致した) も変化として返すので、全部消すと
    「Write → 次の Bash で commit」という主要フローで指紋が commit レビューの直前に
    消えてしまう。`git add` のように status の code だけが動くケースも同じ理由で
    残す (実測: `git status` / `git add` / `git commit` は作業ツリーのファイルの
    `[size, mtime_ns, ctime_ns]` を変えない)。

    消し忘れても P6 の突合そのもの (commit された blob のバイト vs 指紋) が残る —
    ここは早期失効による多層防御であって、唯一の担保ではない。

    **変化が 1 件も無ければ `state` を開かない**: 読み出しも flock +
    read-modify-write なので、何も変えていない Bash のたびに書き込みが増える。
    """
    if not changed:
        return
    known = state.fingerprints(session_id)
    if not known:
        return
    by_rel: dict[str, list[str]] = {}
    for path in known:
        rel = gitscan.to_relative(root, path)
        if rel:
            by_rel.setdefault(rel, []).append(path)
    stale: list[str] = []
    for rel in changed:
        paths = by_rel.get(rel)
        if not paths:
            continue
        before = pre.get(rel)
        if not isinstance(before, list) or len(before) < 4:
            stale.extend(paths)  # 窓の開始時点で dirty でなかった / 旧形式
            continue
        if list(before[1:4]) != gitscan.stat_entry(root, rel):
            stale.extend(paths)
    if stale:
        state.drop_fingerprints(session_id, stale)


# --------------------------------------------------------------------------
# commit 単位レビュー (PostToolUse(Bash), 0.12.0)
# --------------------------------------------------------------------------
#
# Stop は「未 commit の差分」専任のまま。commit してしまうと `git diff HEAD` が空に
# なり、Stop は「取得できませんでした」としか言えない (復元してはいけない理由は
# CLAUDE.md の「HEAD 基準が空になったパスは復元せず通知する」節)。そこを埋めるのが
# この経路。
#
# ## 不変条件 I1' (等価性で縛る)
#
#   commit レビューがあるパスについて外部へ送る差分は、**その Bash の直前に Stop が
#   来ていたら Stop 経路が送っていた差分 (`git diff HEAD -- <path>`) と同一**で
#   なければならない。同一だと示せないパスは内容を送らず、ファイル名だけ通知する。
#
# 「どの git 操作なら安全か」を列挙する方針は 3 回失敗した (HEAD SHA 基点 / 内容指紋 /
# reflog の行 allow-list)。git には履歴を動かさずに内容を運ぶ経路が多数あり
# (`merge --squash` / `cherry-pick -n` / `checkout <ref> -- <path>` /
# `restore --source` / `stash pop` / `apply` は **`logs/HEAD` に 1 行も書かない** —
# 2026-09-20 実測)、列挙は終わらない。**操作の種類から内容の来歴を推論するのをやめ、
# Stop 経路の露出を上限として、それを 1 バイトも超えないことを示す**のが I1'。
#
# ## 送信条件 (全て満たすパスだけ送る)
#
# 窓の条件 (満たさなければ窓ごと何も送らない):
#   W1. `reflog.appended` の fail-closed (pre 欠落 / 読めない / 短縮 / parse 不能 /
#       追記バイト上限 / commit 数上限)
#   W2. 追記が `commit:` / `commit (amend):` / `commit (initial):` の行だけで
#       構成される (`old == new` の行は無視)
#   W3. 行が連鎖している (先頭の old == pre の HEAD、以降 old_i == new_{i-1})
#   W4. pre-tool の `git status` スナップショットがある
#
# パスの条件:
#   P1. pending ∪ in-flight にある (`state.recorded_paths`。**reviewed は含めない**)
#   P2. pre の status スナップショットに載っている (= 窓を開いた時点で dirty)
#   P3'. `(size, mtime_ns, ctime_ns)` が pre と現在で一致する (窓の間に書き換わって
#        いない)。**代理変数**であって「書き換わっていない」ことの証明ではない
#   P4'. index のタグが `H` (通常) である (`git ls-files -v`)。`assume-unchanged` /
#        `skip-worktree` / unmerged では次の P4 が作業ツリーを見ないため
#   P4. commit 後に `git diff HEAD -- <path>` が空 (部分 commit ではない)
#   P6. **commit された blob の生バイトの sha256 が、このセッションの編集ツールが
#       最後に書いた内容の指紋と一致する** (`state.fingerprints`)
#   P5. 既存の除外規則と予算を通る (`_resolve_paths` / `_collect_commit_diffs`)
#   + Stop がレビュー済みの内容 (`reviewed` の hash と同値の diff) は送らない
#
# ## なぜこれで等価になるか (前提を明示する)
#
# 窓を開いた時点の作業ツリーの内容を W0、HEAD を H とする。P3' より窓の終わりの内容も
# W0 — ただしこれは **stat の一致を「内容が同じ」の代理にしている**。W2 / W3 より窓の
# 終わりの HEAD は最後の `<new>` = N。P4 より N の内容 = W0 — ただしこれは
# **`git diff HEAD` が作業ツリーを忠実に見ることを前提**にしており、index のタグが
# `H` 以外だと成り立たない (だから P4' が要る)。よって送る `git diff H N -- <path>` =
# diff(H の内容, W0) = 窓を開いた時点の `git diff HEAD -- <path>`。
#
# **この 2 つの代理判定はどちらも独立に偽になりうる** (マージ前レビューの P1-1 /
# P1-2 で実演済み: 同一サイズの書き換え + mtime 復元、`assume-unchanged` で index の
# 他者版を commit)。そこで **P6 を最後の砦に置く**: 送る差分の「新しい側」の内容が、
# このセッションの編集ツールが書いたバイト列と**一致することを直接確かめる**。P6 は
# P3' / P4' / P4 のいずれにも依存しない (commit された blob を直接読む) ので、代理
# 判定が破れても他者の内容は送られない。
#
# **P6 の前提も 1 つだけ明示しておく**: 指紋は「編集ツールの直後に `PostToolUse` が
# ディスク上で見たバイト列」であって、ツールが書いたバイト列そのものではない。
# ツールの書き込みと hook の読み取りの**間に**別の書き手が同じパスを上書きすると、
# 指紋はその内容を承認する。これは 0.11.0 の Stop も同じものを送る範囲
# (pending のパスの現在の内容) なので送信範囲は広がらないが、**P6 は「編集ツールが
# 書いた」ことの証明ではない**。閉じるには編集ツール自身の書き込み内容を
# ハーネス側から受け取る必要があり、hook の入力には含まれていない。
#
# **前回撤去した「内容指紋で復元する」案との違い**: あれは指紋の一致を根拠に
# **基点を過去の commit へ探しに行った** (同じ内容の Write をすると、無関係な過去の
# commit が基点になり、その commit が消した行まで送られた)。今回は基点を reflog の窓
# (W1〜W4) で固定したままで、指紋は「新しい側の内容の同定」にしか使わない。
# 床テスト: `TestSendScope::test_noop_edit_does_not_send_unrelated_history_on_commit`。
#
# **床テストはこの等価性そのものを assert する**
# (`tests/test_commit_flow.py::TestWindowEquivalence`)。
#
# ## 受け入れる帰結 (README の「既知の限界」と対)
#
# 同じ Bash の中で編集して commit した場合 (`sed -i ... && git commit -am`)、pre-commit
# フックがファイルを書き換える場合、`git add -p` の部分 commit、`reset --soft` での
# squash、rebase / merge を含む Bash は commit レビューの対象外 (ファイル名の通知のみ)。
# Bash 経由でしか触っていないパス (指紋が無い)、削除の commit (blob が無い)、
# clean/smudge フィルタや `core.autocrlf` で blob と作業ツリーのバイトが食い違う repo も
# 同じく P6 を通らないので、内容は送らずファイル名だけ通知する。


#: 送らなかったパスの区分。通知文は**固定文言 + ファイル名のみ**で、内容は出さない。
SKIP_UNRECORDED = "unrecorded"
SKIP_MISMATCH = "mismatch"
SKIP_PARTIAL = "partial"
SKIP_FINGERPRINT = "fingerprint"

_SKIP_NOTICES = {
    # 「編集記録が無い」だと、Stop でレビュー済みになって `reviewed` へ移ったパス
    # (P1 は pending ∪ in-flight のみ) にも出るため、利用者が「自分が編集したのに
    # 記録が無いと言われる」と混乱する (マージ前レビューの指摘)。
    SKIP_UNRECORDED: (
        "{n} ファイルはこのセッションの未レビューの変更として記録が無いため"
        "内容を送信していません (ファイル名のみ): "
    ),
    SKIP_MISMATCH: (
        "{n} ファイルは Bash の窓を開いた時点の未 commit の変更と一致しないため"
        "内容を送信していません (ファイル名のみ): "
    ),
    SKIP_PARTIAL: (
        "{n} ファイルは commit 後も未 commit の変更が残る (部分 commit) ため"
        "内容を送信していません (ファイル名のみ): "
    ),
    SKIP_FINGERPRINT: (
        "{n} ファイルは commit された内容がこのセッションのツールが最後に書いた内容と"
        "一致しないため内容を送信していません (ファイル名のみ): "
    ),
}

#: W4 に当たったときの利用者向け 1 行 (`reflog.NOTICE_*` と同じ扱い)。
NOTICE_NO_STATUS = (
    "Bash 実行前の git status スナップショットが無いため、commit レビューを"
    "行いませんでした"
)


#: backend lock を他が握っていて窓を見送ったときの 1 行 (無言にしない)。
NOTICE_LOCK_HELD = (
    "同一作業ツリーで別のレビューが実行中のため、この Bash の commit レビューを"
    "見送りました"
)

#: `_deliver_commit_review` が壊れたときの最終手段 (指摘は失うが無言にはしない)。
NOTICE_DELIVERY_FAILED = "レビュー結果の整形に失敗しました"


class CommitSend:
    """`_send_commit_review` の結果。dataclass にしない理由は `ReviewBatch` と同じ。"""

    def __init__(
        self, outcome, elapsed: float, sent_rels: list[str], deduplicated: list[str] | None = None
    ) -> None:
        self.outcome = outcome
        self.elapsed = elapsed
        self.sent_rels = sent_rels
        self.deduplicated = list(deduplicated or [])  # 送らなかったが commit 済みが確定した rel


def _commit_review(
    session_id: str,
    root: str,
    pre_reflog: dict | None,
    pre_status: dict | None,
    commits: list | None,
    why: str,
    why_notice: str | None,
    recorded: list[str],
    fingerprints: dict[str, str],
) -> dict:
    """Bash の窓の中で作られた commit をレビューする。何も送らないときは `{}`。

    `commits` / `why` / `why_notice` は `_handle_bash` が **status を積む前に**
    `reflog.appended()` で解いたもの (積む側の guard と同じ判定を共有するため)。
    `recorded` も同じ理由で status を積む前に読んだ集合 (P1)。

    ロック順は Stop と同じ (backend lock → state lock → 解放 → review)。claim は
    取らない — commit の差分は作業ツリーの状態に依存しない不変の範囲なので、
    途中で死んでも「次の Stop に持ち越す」対象が無い (失われるのは 1 回のレビュー機会
    だけで、未レビューの変更が消えるわけではない)。

    **try の範囲は「送信前」までに限る** (マージ前レビューの指摘)。backend から
    指摘を受け取った後の state 整理で例外が出ても、指摘は必ず配信する。
    """
    if commits is None:
        log(f"commit レビューを見送り (fail-closed): {why}")
        return _with_notices({}, [why_notice]) if why_notice else {}
    if not commits:
        return {}
    if pre_status is None:
        # W4: 窓を開いた時点の dirty 集合が分からないと P2 / P3 を判定できない。
        # 巨大な作業ツリーで `git status` を諦めた環境では commit レビューが効かない —
        # 受け入れる (送信範囲が広がる側には倒さない)
        log("commit レビューを見送り (fail-closed): Bash 前の status スナップショットが無い")
        return _with_notices({}, [NOTICE_NO_STATUS])

    cooled = _commit_cooldown_notice(session_id, len(commits))
    if cooled:
        log(cooled)
        return _with_notices({}, [cooled])

    by_rel = _recorded_by_rel(root, recorded)
    with state.cursor_lock(root) as acquired:
        if not acquired:
            # 窓は 1 回きりで次に回らないので、**無言で失わない** (cooldown /
            # commit 数上限と同じ扱い。マージ前レビューの指摘)
            log(NOTICE_LOCK_HELD)
            return _with_notices({}, [NOTICE_LOCK_HELD])
        try:
            prepared = _prepare_commit_review(
                session_id,
                root,
                pre_reflog or {},
                pre_status,
                commits,
                by_rel,
                fingerprints,
                state.reviewed_hashes(session_id),
            )
        except Exception as e:  # noqa: BLE001 — 送信前なので「送らない」に倒す
            log(f"commit レビューの準備中に例外: {type(e).__name__}: {e}")
            return {}
        if prepared is None:
            return {}
        notices, batch = prepared
        if batch is None:
            return _with_notices({}, notices)
        try:
            sent = _send_commit_review(session_id, root, commits, batch, notices)
        except Exception as e:  # noqa: BLE001 — 送信中の失敗も「送らない」に倒す
            log(f"commit レビューの送信中に例外: {type(e).__name__}: {e}")
            return {}
    try:
        return _deliver_commit_review(session_id, root, len(commits), notices, sent, by_rel)
    except Exception as e:  # noqa: BLE001 — 配信の整形で落ちても無言にはしない
        # `_quiet` は state 整理しか覆っていないので、その外側 (`build_reason` /
        # `_resolve_mode` / `notify.compose`) の例外は main の fail-open に握られて
        # stdout が空になる = 送った後に指摘ごと捨てる形になる (マージ前レビューの指摘)
        log(f"commit レビュー結果の配信中に例外: {type(e).__name__}: {e}")
        return {"systemMessage": NOTICE_DELIVERY_FAILED}


def _commit_cooldown_notice(session_id: str, count: int) -> str | None:
    """cooldown 中なら利用者向けの一文を返す (Stop と同じ枠を共有する)。

    Stop 側 (`_cooldown_notice`) と違い「pending が空なら黙る」という絞りは要らない —
    この経路は commit したときにしか走らないので、通知が毎ターン出ることはない。
    見送った commit は**次の機会に回らない** (窓は 1 回きり) が、その変更は Stop の
    pending には残っているので「レビューされないまま消える」ことは無い。
    """
    cooldown = settings.count(ENV_COOLDOWN, 0)
    if cooldown <= 0:
        return None
    remaining = cooldown - (time.time() - state.last_review_at(session_id))
    if remaining <= 0:
        return None
    return (
        f"前回レビューから {cooldown} 秒 ({ENV_COOLDOWN}) 未満のため、"
        f"{count} 件の commit のレビューを見送りました"
    )


def _recorded_by_rel(root: str, recorded: list[str]) -> dict[str, list[str]]:
    """`state.recorded_paths()` を「作業ツリー相対 → 元の絶対パス」に畳む。

    値を元の絶対パスのまま持つのは、`_resolve_paths` が **claim されたときの名前**
    から lexical 名・symlink 別名を作って除外判定に当てるため。実際には
    `exclusion.expand_aliases` が symlink map から別名を独立に復元できるので
    (マージ前レビューの指摘)、これは除外の*冗長な*担保であって唯一の担保ではない。
    """
    by_rel: dict[str, list[str]] = {}
    for path in recorded:
        rel = gitscan.to_relative(root, path)
        if rel:
            by_rel.setdefault(rel, []).append(path)
    return by_rel


def _window_base(root: str, pre_reflog: dict) -> str | None:
    """窓の diff 基点 = 窓を開いた時点の HEAD。HEAD が無かった repo は空ツリー。

    空ツリーの object id を直書きしないのは SHA-256 の repo で値が違うため
    (`gitscan.empty_tree`)。引けなければ None = 窓ごと諦める。
    """
    head = pre_reflog.get("head") or ""
    if head:
        return head
    return gitscan.empty_tree(root)


def _pre_stat(pre_status: dict, rel: str) -> list[int] | None:
    """窓を開いた時点のそのパスの `[size, mtime_ns, ctime_ns]`。**無ければ None**。

    None = 「窓を開いた時点で未 commit の変更が無かった」= P2 不成立。`git merge --squash`
    / `cherry-pick -n` / `stash pop` のように窓の中で初めて内容が入ったパスがここで落ちる。

    **長さ検査は現行の形式 (4 要素) を要求する**。`ctime_ns` を足す前の 3 要素の
    スナップショットが $TMPDIR に残っていても、判定不能 = 送らない側へ自動的に落ちる
    (fail-closed のまま移行できる)。
    """
    entry = pre_status.get(rel)
    if not isinstance(entry, list) or len(entry) < 4:
        return None
    return list(entry[1:4])


def _classify_commit_paths(
    root: str, names: list[str], by_rel: dict[str, list[str]], pre_status: dict
) -> tuple[list[str], dict[str, list[str]]] | None:
    """窓の変更パスを P1〜P4' で振り分け、`(送ってよい rel, 区分 → rel)` を返す。

    P4' (`git ls-files -v`) / P4 (`git diff HEAD`) の判定に失敗したら None =
    窓ごと諦める。「一部だけ取れた」状態で先へ進むと、送ってはいけないパスが
    素通りする。P6 (内容指紋) は除外判定の後なので `_prepare_commit_review` 側。
    """
    skipped: dict[str, list[str]] = {
        SKIP_UNRECORDED: [],
        SKIP_MISMATCH: [],
        SKIP_PARTIAL: [],
        SKIP_FINGERPRINT: [],
    }
    candidates: list[str] = []
    for rel in names:
        if rel not in by_rel:
            skipped[SKIP_UNRECORDED].append(rel)  # P1
            continue
        pre_entry = _pre_stat(pre_status, rel)
        if pre_entry is None:
            skipped[SKIP_MISMATCH].append(rel)  # P2: 窓を開いた時点で dirty でない
            continue
        if pre_entry != gitscan.stat_entry(root, rel):
            skipped[SKIP_MISMATCH].append(rel)  # P3': 窓の間に書き換わった
            continue
        candidates.append(rel)

    if not candidates:
        return candidates, skipped

    # P4': index のタグが `H` 以外 (assume-unchanged / skip-worktree / unmerged) の
    # パスでは `git diff HEAD` が作業ツリーを見ないので、次の P4 が意味を失う
    flagged = gitscan.flagged_index_entries(root, candidates)
    if flagged is None:
        log("index エントリのタグを確認できないため commit レビューを見送り")
        return None
    if flagged:
        skipped[SKIP_MISMATCH].extend(rel for rel in candidates if rel in flagged)
        candidates = [rel for rel in candidates if rel not in flagged]
        if not candidates:
            return candidates, skipped

    remaining = gitscan.changed_vs_head(root, candidates)  # P4 (git 1 回)
    if remaining is None:
        log("commit 後の HEAD 差分を確認できないため commit レビューを見送り")
        return None
    kept: list[str] = []
    for rel in candidates:
        if rel in remaining:
            skipped[SKIP_PARTIAL].append(rel)
        else:
            kept.append(rel)
    return kept, skipped


def _verify_fingerprints(
    root: str,
    last: str,
    rels: list[str],
    by_rel: dict[str, list[str]],
    fingerprints: dict[str, str],
) -> tuple[list[str], list[str]]:
    """P6: commit された内容が「このセッションのツールが最後に書いた内容」か確かめる。

    `(送ってよい rel, 一致しなかった rel)` を返す。**指紋が無いパスは git を呼ぶ前に
    落とす** (Bash 経由でしか触っていないパス・1 MiB 超・読めなかったファイル)。

    同じ実体に複数の別名 (symlink 経由の claim) があるときは、**全ての別名が同じ
    指紋を持つこと**を要求する (片方しか記録が無い / 食い違う状態は判定不能 =
    送らない)。

    ここが「送る差分の新しい側 = このセッションのツールが書いたバイト列」を直接
    示す唯一の条件で、P3' / P4' / P4 のどれが代理判定として破れても独立に残る。
    """
    expected: dict[str, str] = {}
    for rel in rels:
        digests = {fingerprints.get(path) for path in by_rel.get(rel, [])}
        digest = digests.pop() if len(digests) == 1 else None
        if digest:
            expected[rel] = digest

    actual = (
        gitscan.blob_digests(
            root,
            last,
            list(expected),
            FINGERPRINT_MAX_BYTES,
            FINGERPRINT_TOTAL_MAX_BYTES,
        )
        if expected
        else {}
    )
    kept: list[str] = []
    mismatched: list[str] = []
    for rel in rels:
        if rel in expected and actual.get(rel) == expected[rel]:
            kept.append(rel)
        else:
            mismatched.append(rel)
    return kept, mismatched


def _prepare_commit_review(
    session_id: str,
    root: str,
    pre_reflog: dict,
    pre_status: dict,
    commits: list,
    by_rel: dict[str, list[str]],
    fingerprints: dict[str, str],
    reviewed: dict[str, str],
) -> tuple[list[str], ReviewBatch | None] | None:
    """送る diff を組み立てる。窓ごと諦めるときは None。

    返り値の `batch` が None なら「送るものは無いが通知はある」。
    """
    base = _window_base(root, pre_reflog)
    if base is None:
        log("空ツリーの object id を引けないため commit レビューを見送り")
        return None
    last = commits[-1].new
    # 窓全体を 1 本の範囲として見る (commit ごとに分けない)。W2 / W3 が成り立つ
    # ときだけここに来るので、この範囲 = 窓の中で作られた commit の総和
    names = gitscan.range_paths(root, base, last)
    if names is None:
        log("commit の変更パスを取得できないため commit レビューを見送り")
        return None

    classified = _classify_commit_paths(root, names, by_rel, pre_status)
    if classified is None:
        return None
    candidates, skipped = classified

    matched: list[str] = []
    seen: set[str] = set()
    for rel in candidates:
        for abs_path in by_rel[rel]:
            if abs_path not in seen:
                seen.add(abs_path)
                matched.append(abs_path)

    rels, overflow, excluded = _resolve_paths(root, matched, exclusion.load_policy())

    # P6 は除外判定の**後**に置く: 除外されたパスの blob をそもそも読まないため
    # (「除外」と「指紋が一致しない」の二重通知も避ける)
    rels, unverified = _verify_fingerprints(root, last, rels, by_rel, fingerprints)
    skipped[SKIP_FINGERPRINT].extend(unverified)

    notices: list[str] = []
    for reason in (SKIP_UNRECORDED, SKIP_MISMATCH, SKIP_PARTIAL, SKIP_FINGERPRINT):
        rels_ = skipped[reason]
        if rels_:
            notices.append(_SKIP_NOTICES[reason].format(n=len(rels_)) + _list_names(rels_))

    if excluded:
        notices.append(
            f"{len(excluded)} ファイルを外部 AI レビューから除外 (内容は送信していません): "
            + _list_names(f"{name} ({reason})" for name, reason in excluded)
        )
    if overflow:
        notices.append(
            f"{len(overflow)} ファイルは 1 回あたり {MAX_REVIEW_PATHS} 件の上限により"
            "この commit レビューでは送信していません: " + _list_names(_rel_names(root, overflow))
        )

    batch = _collect_commit_diffs(root, base, last, rels, by_rel, reviewed)
    if batch.deferred_time:
        notices.append(
            f"{len(batch.deferred_time)} ファイルは git diff の時間予算超過により"
            "送信していません: " + _list_names(_rel_names(root, batch.deferred_time))
        )
    if batch.deferred_size:
        notices.append(
            f"{len(batch.deferred_size)} ファイルは diff 合計 {MAX_DIFF_BYTES // 1000} KB の"
            "予算に収まらないため送信していません: "
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
        log("commit レビュー対象の差分が無い (送信条件を満たすパスが無い) ため skip")
        # 重複抑止で落としたパスは commit 済み = pending に残すと次の Stop が
        # 「差分が空で取得できませんでした」と誤通知する (マージ前レビューの指摘)。
        # 送信は起きていないが「レビュー済みの内容が commit された」ことは確定して
        # いるので、送ったパスと同じ整理を通す
        _quiet(
            lambda: _settle_commit_review(session_id, root, batch.deduplicated, by_rel),
            "重複抑止パスの整理",
        )
        return notices, None

    min_lines = settings.count(ENV_MIN_LINES, 0)
    changed_lines = _count_changed_lines(batch.sections)
    if min_lines > 0 and changed_lines < min_lines:
        notices.insert(
            0,
            f"変更 {changed_lines} 行が {ENV_MIN_LINES}={min_lines} に満たないため"
            "commit レビューを見送り",
        )
        log(notices[0])
        return notices, None

    return notices, batch


def _send_commit_review(
    session_id: str, root: str, commits: list, batch: ReviewBatch, notices: list[str]
) -> CommitSend:
    """選んだ backend に diff を渡す。ここより後ろの例外は配信を止めない。"""
    diff_text = "\n".join(batch.sections)
    chosen, unknown_backends = selection.configured()
    if unknown_backends:
        notices.append(_unknown_backends_notice(unknown_backends))
    strategy_name = selection.strategy(len(chosen))
    sent_rels = list(dict.fromkeys(_rel_names(root, batch.submitted)))
    log(
        f"commit レビューを実行 ({len(commits)} commit, {len(sent_rels)} ファイル, "
        f"{len(diff_text)} chars, strategy={strategy_name})"
    )
    started = time.monotonic()
    outcome = selection.run_review(
        diff_text,
        cwd=root,
        chosen=chosen,
        strategy_name=strategy_name,
        last=state.last_backend(session_id),
        log=log,
        manifest=_sent_manifest(root, batch),
    )
    return CommitSend(outcome, time.monotonic() - started, sent_rels, batch.deduplicated)


def _quiet(action, what: str) -> None:
    """レビュー結果を受け取った**後**の副作用を、例外で配信を止めずに実行する。

    マージ前レビューの指摘: 以前は `_run_commit_review` 全体を 1 つの `try` が
    覆っており、`_settle_commit_review` の例外が backend から受け取った指摘ごと
    握り潰していた (`mark_review_done` / `record_last_backend` は実行済みなので
    cooldown だけ消費される最悪の形)。送信**前**の例外は従来どおり「送らない」に
    倒すが、送信**後**は必ず配信する。
    """
    try:
        action()
    except Exception as e:  # noqa: BLE001
        log(f"commit レビュー後の{what}に失敗 (指摘は配信する): {type(e).__name__}: {e}")


def _deliver_commit_review(
    session_id: str,
    root: str,
    commit_count: int,
    notices: list[str],
    sent: CommitSend,
    by_rel: dict[str, list[str]],
) -> dict:
    """受け取った結果を `additionalContext` (または `decision: block`) にして返す。"""
    outcome = sent.outcome
    # cooldown は「レビューとレビューの間隔」で成否を問わないので、失敗時も更新する
    _quiet(lambda: state.mark_review_done(session_id), "レビュー時刻の記録")
    summary = (
        f"commit レビュー完了 ({notify.format_elapsed(sent.elapsed)}, "
        f"{commit_count} commit / {len(sent.sent_rels)} ファイル{_attempt_detail(outcome)})"
    )
    if outcome.skipped:
        notices.append(
            f"{len(outcome.skipped)} 個の backend は残り時間が足りないため試していません: "
            + _list_names(outcome.skipped)
        )

    result = outcome.text
    if not result:
        # **pending は触らない** (設計 B3)。残ったパスは Stop が見て
        # 「差分が空で取得できませんでした」と報告する = 事実のまま
        log("全 backend が commit レビュー結果を返さなかった (state は触らない)")
        notices.insert(0, f"{summary} → 結果を取得できず (timeout / 失敗)")
        return _with_notices({}, notices)

    _quiet(lambda: state.record_last_backend(session_id, outcome.backend), "backend 名の記録")
    _quiet(
        lambda: _settle_commit_review(
            session_id, root, list(dict.fromkeys(sent.sent_rels + sent.deduplicated)), by_rel
        ),
        "pending の整理",
    )

    if is_clean_review(result):
        log(f"{outcome.backend}: REVIEW_CLEAN (commit レビュー、Claude への出力なし)")
        notices.insert(0, f"{summary} → 指摘なし")
        return _with_notices({}, notices)

    reason = build_reason(result, outcome.backend, SCOPE_COMMIT)
    _save_review_copy(session_id, reason)
    mode, version_fallback_notice = _resolve_mode()
    if mode == MODE_BLOCK:
        # PostToolUse の `decision: "block"` はツールを止めない (既に実行済み)。公式
        # Hooks reference 逐語: `"block"` adds the `reason` next to the tool result.
        # つまり additionalContext と同じ位置に届く。`auto` の版数 fail-closed は
        # Stop 向けの下限 (2.1.163) をそのまま流用している (README の該当節を参照)
        message = f"{summary} → 指摘あり (Claude に対応を依頼しました)"
        if version_fallback_notice:
            message += f" {version_fallback_notice}"
        notices.insert(0, message)
        return _with_notices({"decision": "block", "reason": reason}, notices)
    notices.insert(0, f"{summary} → 指摘あり (レビュー結果を Claude の文脈に渡しました)")
    return _with_notices(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": reason,
            }
        },
        notices,
    )


def _settle_commit_review(
    session_id: str, root: str, sent_rels: list[str], by_rel: dict[str, list[str]]
) -> None:
    """レビューが成立したあとの state 整理 (設計 B3)。

    **送ったパスのうち、もう `git diff HEAD` に出ないもの**だけを pending から外す。
    外さないと Stop がそのパスを claim して「差分が空で取得できませんでした」と
    誤通知する (実際にはこの commit レビューで送信済み)。

    送信前の P4 で「HEAD 差分が空」は確認済みだが、レビューの待ち時間 (最悪 10 分)
    の間に別の編集が来ている可能性があるので**ここでもう一度確認する** — その場合は
    pending に残し、次の Stop に続きを見せる。取得に失敗したら何も外さない (二重通知に
    なるだけで、送信範囲は広がらない側の失敗)。

    **pending から外すのは実体名と `by_rel` が持つ「claim されたときの名前」の両方**
    (マージ前レビューの指摘)。pending のキーは symlink 別名 (`<root>/link/a.py`) の
    まま入りうるので実体名だけでは外れず、直後の Stop が「差分が空で取得できません
    でした」と**嘘の通知**を出す。逆に同じ窓の `_record_bash_changes` は実体名で
    積むので、別名だけでも足りない。
    """
    if not sent_rels:
        return
    remaining = gitscan.changed_vs_head(root, sent_rels)
    if remaining is None:
        log("commit 後の HEAD 差分を確認できないため pending を整理しない")
        return
    settled: dict[str, None] = {}
    for rel in sent_rels:
        if rel in remaining:
            continue
        settled.setdefault(os.path.join(root, rel), None)
        for alias in by_rel.get(rel) or []:
            settled.setdefault(alias, None)
    if settled:
        state.drop_pending(session_id, list(settled))


def _collect_commit_diffs(
    root: str,
    base: str,
    last: str,
    rels: list[str],
    by_rel: dict[str, list[str]],
    reviewed: dict[str, str],
) -> ReviewBatch:
    """窓全体の範囲 (`base..last`) × パスの diff を、Stop と同じ予算表で積む。

    **Stop が既にレビューした内容は載せない** (マージ前レビューの指摘): I1' が
    成り立っているなら、ここで作る range diff のバイト列は「窓を開いた時点の
    `git diff HEAD -- <path>`」と 1 バイトも違わないので、`reviewed` に入っている
    *HEAD 基準* diff の hash と**同じ体系で比較できる** (2026-09-22 実測: tracked の
    変更 / untracked の新規 / mode 変更のいずれでも両者は byte-identical)。
    内容を変えない再編集 (同じ内容の Write / revert して戻した編集) で pending に
    戻ったパスを commit しても、Stop なら送らないものを commit レビューが送る、
    ということが起きなくなる。

    commit ごとに分けず窓全体を 1 本にするのは I1' の等価性のため — 送るのは
    「窓を開いた時点の `git diff HEAD`」と同じものでなければならず、途中の commit で
    区切るとその等式が成り立たない。1 パスが 1 section になるので、`_sent_manifest`
    の `zip(submitted, sections)` も 1:1 のままになる。
    """
    batch = ReviewBatch()
    used = 0
    deadline = time.monotonic() + COMMIT_COLLECT_BUDGET_SEC
    for index, rel in enumerate(rels):
        abs_path = os.path.join(root, rel)
        if time.monotonic() > deadline:
            batch.deferred_time = [os.path.join(root, r) for r in rels[index:]]
            break
        text = gitscan.range_diff(root, base, last, rel)
        if not text.strip():
            continue
        if _already_reviewed(by_rel.get(rel, [abs_path]), diff_hash(text), reviewed):
            log(f"commit レビュー: Stop がレビュー済みの内容のため送らない: {rel}")
            batch.deduplicated.append(rel)
            continue
        full_size = len(text.encode())
        if full_size > MAX_FILE_DIFF_BYTES:
            text = _truncate_section(text, MAX_FILE_DIFF_BYTES)
        size = len(text.encode())
        separator = 1 if batch.sections else 0
        if used + separator + size > MAX_DIFF_BYTES:
            batch.deferred_size.append(abs_path)
            continue
        batch.sections.append(text)
        batch.submitted.append(abs_path)
        used += separator + size
        if full_size > MAX_FILE_DIFF_BYTES:
            batch.truncated.append((rel, full_size))
    return batch


def _already_reviewed(abs_paths: list[str], digest: str, reviewed: dict[str, str]) -> bool:
    """この diff を Stop が既にレビュー済みか (`reviewed` の hash と同値か)。

    claim されたときの名前 (symlink 別名を含む) のどれかで記録されていれば該当と
    みなす — `reviewed` のキーは claim 時の絶対パスで、実体名とは限らないため。
    """
    return any(reviewed.get(path) == digest for path in abs_paths)


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

    if not review_enabled():
        log("EXTERNAL_AI_POST_REVIEW=0 によりレビュー無効化")
        return

    # 送信先の候補 (既定は cursor のみ)。1 つも起動できず、かつ設定に未知の名前も
    # 無ければ「外部 AI CLI が入っていない」なので 0.11.0 と同じく完全に無言で抜ける。
    chosen, unknown_backends = selection.configured()
    active = selection.available(chosen)
    if not active and not unknown_backends:
        log("利用可能な外部 AI backend が無い (未インストール)")
        return

    # `stategc.gc_stale()` (直後) は state_root() 配下を列挙・削除・chmod するので、
    # その**前**に安全性を確認する。ここで検出できなければ GC も以降のレビューも
    # 一切行わない (攻撃者所有のディレクトリを信用してしまう経路)。
    # **無効化 / cursor 未インストールの判定より後に置く**: 前に置くと、この機能を
    # 使っていない利用者にまで「レビューをスキップしました」通知が毎ターン出て
    # しまう (マージ前レビューの指摘。handle_pre_tool / handle_post_tool と同じ理由で、
    # 機能 off のときは state に一切触れないのが既存の設計方針)。pre-tool /
    # post-tool 側でも同じ検査をしている (`_private_root_ok`) が、そちらは無出力で
    # state を書かないだけなので、利用者への 1 行通知はここに一本化する
    # (ターンに 1 回だけ発火する)。
    try:
        flock.ensure_private_root(state.state_root())
    except flock.UnsafeStateDirError:
        msg = (
            "状態ディレクトリ (共有一時領域) の所有者/権限が信頼できないため、"
            "このターンはレビューをスキップしました"
        )
        log(msg)
        json.dump(_with_notices({}, [msg]), sys.stdout, ensure_ascii=False)
        return

    stategc.gc_stale()

    if not active:
        # 未知の名前しか書かれていない (`BACKENDS=cursr` のタイプミス等)。既定へ
        # fallback すると外したはずの backend が黙って走るので候補は空のままにし、
        # 代わりに 1 行通知する — 黙って止まると「レビューが動かなくなった」としか
        # 見えず、この plugin が潰そうとしている「無言」そのものになる。
        #
        # **編集の有無で絞り込めない**点に注意: この設定では PostToolUse 側の門番
        # (`any_available`) も通らないので pending は常に空になり、「編集のあるターン
        # だけ」という他の通知と同じ絞り方ができない。設定を直せば止まる通知
        # (利用者の設定ミス) なので、毎ターン出るのを承知で出し続ける側に倒す。
        msg = _unknown_backends_notice(unknown_backends) + " (レビューできる backend がありません)"
        log(msg)
        json.dump(_with_notices({}, [msg]), sys.stdout, ensure_ascii=False)
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

    batch = _collect_diffs(root, rels, state.reviewed_hashes(session_id))
    # 繰り越しは捨てずに pending へ戻す (次の Stop でレビューされる)。claim 順を保って 1 回で
    # 積む: 予算超過 (rels の途中) → 時間切れ (rels の末尾) → 上限超過 (rels の外) の順
    carried = batch.deferred + overflow
    if carried:
        state.record_pending(session_id, carried)
    if batch.unretrievable:
        # HEAD 基準の diff が空だったパス。復元は試みない (`_collect_diffs` の
        # docstring「HEAD 基準の diff が空のパスは復元を試みない」参照)。pending
        # には戻さない (状況が変わらない限り毎ターン同じ結果になるだけなので、
        # 繰り返し報告しない)。
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
    chosen, unknown_backends = selection.configured()
    if unknown_backends:
        notices.append(_unknown_backends_notice(unknown_backends))
    strategy_name = selection.strategy(len(chosen))
    log(
        f"差分レビューを実行 ({len(batch.submitted)} ファイル, {len(diff_text)} chars, "
        f"strategy={strategy_name}, 候補={', '.join(b.NAME for b in chosen)})"
    )
    started = time.monotonic()
    outcome = selection.run_review(
        diff_text,
        cwd=root,
        chosen=chosen,
        strategy_name=strategy_name,
        last=state.last_backend(session_id),
        log=log,
        manifest=_sent_manifest(root, batch),
    )
    elapsed = time.monotonic() - started
    state.mark_review_done(session_id)
    summary = (
        f"差分レビュー完了 ({notify.format_elapsed(elapsed)}, "
        f"{len(batch.submitted)} ファイル{_attempt_detail(outcome)})"
    )
    if outcome.skipped:
        notices.append(
            f"{len(outcome.skipped)} 個の backend は残り時間が足りないため試していません: "
            + _list_names(outcome.skipped)
        )

    result = outcome.text
    if not result:
        log("全 backend がレビュー結果を返さなかった (fail-open、pending に戻す)")
        state.restore_claim(session_id, claim_id, batch.submitted)
        notices.insert(0, f"{summary} → 結果を取得できず (timeout / 失敗)。次ターンに持ち越し")
        return _with_notices({}, notices)

    # 結果を返した backend だけを記録する (`alternate` が次回「別の目」を選ぶため)
    state.record_last_backend(session_id, outcome.backend)

    if is_clean_review(result):
        log(f"{outcome.backend}: REVIEW_CLEAN (block しない、レビュー済みとして確定)")
        state.complete_claim(session_id, claim_id, batch.hashes)
        notices.insert(0, f"{summary} → 指摘なし")
        return _with_notices({}, notices)

    reason = build_reason(result, outcome.backend)
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


def _unknown_backends_notice(unknown: list[str]) -> str:
    return f"{selection.ENV_BACKENDS} の未知の backend 名を無視: {', '.join(unknown)}"


def _attempt_detail(outcome: selection.Outcome) -> str:
    """`systemMessage` に出す「どの backend がどうだったか」(本文は入れない)。

    フォールバックが起きたターンは `cursor=失敗, codex=完了` のように並ぶので、
    利用者は「どこへ送られたか」を通知だけで追える (**送信先は通知に出す**)。
    """
    if not outcome.attempts:
        return ""
    detail = ", ".join(
        f"{name}={_STATUS_LABELS.get(status, status)}" for name, status in outcome.attempts
    )
    return f": {detail}"


def _sent_manifest(root: str, batch: ReviewBatch) -> str:
    """hooklog に残す「何を送ったか」(パス名とバイト数のみ。内容は書かない)。

    どの backend へ何を送ったかを後から追えることは、この plugin の外部送信に対する
    説明責任そのものなので、`MAX_LISTED_NAMES` では省略せず全件を並べる
    (`systemMessage` ではなく stderr の debug log なのでノイズにならない)。
    `selection.run_review()` が起動のたびに backend 名と対で記録する。
    """
    return ", ".join(
        f"{rel} ({len(section.encode())} bytes)"
        for rel, section in zip(_rel_names(root, batch.submitted), batch.sections)
    )


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
    nested: dict[str, bool] = {}
    candidates: dict[str, list[str]] = {}
    for path in claimed:
        rel = gitscan.to_relative(root, path)
        if not rel or os.path.isdir(os.path.join(root, rel)):
            continue
        if _in_nested_git(root, rel, nested):
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


def _in_nested_git(root: str, rel: str, memo: dict[str, bool]) -> bool:
    """`rel` が root 配下の**入れ子の git リポジトリ / 作業ツリー**の中にあるか。

    `git worktree add <root>/.claude/worktrees/<name>` や、サブディレクトリでの
    `git init` で作ったものが該当する (前者の `.git` は**ファイル**、後者は
    ディレクトリなので `os.path.exists` で両方を見る)。

    中身は**別のリポジトリの変更**なので、root 基準の `git diff HEAD -- <rel>` は
    常に空になる。放置すると「差分が空で取得できませんでした (commit 済みの可能性)」を
    毎ターン出し続ける (`git status -uall` の `dir/` エントリは snapshot 側で捨てて
    いるが、Write / Edit は絶対パスで直接積まれるのでそこを通らない)。作業ツリー外の
    絶対パスと**同じ扱い**にして、pending に戻さず・hash も記録せず・通知もせず落とす。

    **入れ子側の diff を取りに行く経路は作らない。** その作業ツリーを cwd にした
    セッション (隔離サブエージェント含む) が自分の hook で見るのが正しい担当分け。

    git は呼ばない (`os.path.exists` だけ)。同じ親ディレクトリを何度も stat しないよう
    呼び出し側の `memo` に結果を残す。**root 自身の `.git` は見ない** (rel の親だけを
    root 側から順に辿る)。
    """
    parent = os.path.dirname(rel)
    if not parent:
        return False
    cached = memo.get(parent)
    if cached is not None:
        return cached
    found = False
    prefix = ""
    for part in parent.split(os.sep):
        prefix = os.path.join(prefix, part) if prefix else part
        if memo.get(prefix):
            found = True
            break
        if os.path.exists(os.path.join(root, prefix, ".git")):
            memo[prefix] = True
            found = True
            break
        memo[prefix] = False
    memo[parent] = found
    return found


def _lexical_relative(root_real: str, path: str) -> str | None:
    """claim されたパスを、root 配下の symlink を解決せずに (lexical に) root 相対へ変換する。

    `to_relative` (全体を realpath) だと `credentials/` → `ordinary/` のような symlink
    ディレクトリ経由の claim が `ordinary/data.json` になり、除外判定から `credentials` が
    消える (マージ前レビューの指摘)。親ディレクトリだけ realpath する方式も同じ穴があった。
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
        self.unretrievable: list[str] = []  # HEAD 基準の diff が空だった絶対パス (復元は試みない)
        self.deduplicated: list[str] = []  # Stop がレビュー済みの内容なので送らなかった rel (commit 経路)

    @property
    def deferred(self) -> list[str]:
        """pending へ戻す絶対パス (未レビュー)。claim 順 = バイト予算 (途中) → 時間切れ (末尾)。"""
        return self.deferred_size + self.deferred_time


def _collect_diffs(
    root: str,
    rels: list[str],
    reviewed: dict[str, str],
) -> ReviewBatch:
    """パスごとに diff を取り、予算に収まるものだけを ReviewBatch に積む。

    前回レビュー時と同一 hash のパスは載せない。差分が空のパスも載せない。
    どちらも submitted に入らないので、cursor 失敗時にも復元されずそのまま消える。

    **HEAD 基準の diff が空のパスは復元を試みない。** tracked かつ HEAD が存在する
    パスの HEAD 基準 diff が空なのは、(a) 単に何も変わっていない (revert 済み等)、
    または (b) このセッションが同一ターン内で commit し、ファイルが既に HEAD と
    一致している、のどちらかで、この時点では区別できない。過去に「前回 Stop の
    HEAD」を基点まで遡って (b) を復元する経路を試したが、`<基点>..HEAD` が
    「どのリモートにも存在しない」ことしか検証できず「このセッションが書いた」
    ことまでは検証できないため、同一 worktree を共有する別のローカルの書き手
    (別セッション・人間の手動 commit) が push せずに同じパスへ commit した内容も
    復元して送ってしまうことがマージ前レビューで実演された (pull 由来の混入は
    別途遮断できていたが、これは別ベクトルだった)。安全な復元には編集時点の
    内容退避が要り、「PostToolUse を軽く保つ」という設計と衝突するため、
    ここでは復元せず**常に黙って捨てず通知する**方針にしている (送信範囲が
    広がる方向には倒さない。設計の変遷は CHANGELOG.md / CLAUDE.md を参照):

    - 差分が空で、かつそのパスが tracked (untracked ではない)・HEAD が存在する、
      の両方を満たすなら `batch.unretrievable` に積む — 黙って消費せず、利用者に
      レビューされなかったことを可視化するため (`_run_review` が通知にする)。
      **ディスク上の存在は問わない** (マージ前レビューの指摘): 追跡ファイルの削除が
      同一ターン内で commit されると、HEAD・ディスクの両方からパスが消え、
      `git diff HEAD -- rel` は「両側に無い」ため空になる。以前はここで
      `os.path.exists` も条件にしており、この削除のケースだけ通知対象から
      漏れて黙って消費されていた
    - それ以外の空 diff (untracked で中身が空、HEAD が無い等の元から復元しようが
      ないケース) は黙って捨てる

    **「実体の無い pending エントリ」との区別は諦めている**: 一度も commit
    されていないファイル (このセッションが作成後、同一ターン内で削除して
    一度も commit しなかった一時ファイル等) も、tracked かつ HEAD 存在なら
    ここに積まれうる。しかし commit 済みの delete と未 commit の phantom は
    どちらも「HEAD 上に存在しない」状態になった時点で `git cat-file -e
    HEAD:rel` が両方とも失敗し、cheap な git 状態だけでは区別できない
    (履歴全体を辿れば区別できるが、この hook の git 予算 [`gitscan.py` 参照]
    には収まらない)。正当な削除の見落としの方が実害が大きいため、雑音低減より
    「全部通知する」側を優先する。

    **予算はファイル単位で当てる**:

    - 1 ファイルが MAX_FILE_DIFF_BYTES を超える → 先頭だけを `(truncated)` 付きで送り、
      hash は全文で記録する (変わらない限り再掲しない。変われば切り詰めた形で再掲)
    - 積み上げ合計が MAX_DIFF_BYTES を超えるファイル → 送らず deferred_size へ
      (hash を記録しないので次ターンにそのまま再掲される)。後続の小さいファイルは
      予算が残っていれば送る (first-fit)

    COLLECT_BUDGET_SEC を超えた時点で打ち切り、未処理パスを deferred_time として返す。
    Stop 全体の hook timeout (690s) のうち cursor が上限 600s + kill 猶予 15s を使うため、
    git に使える時間は限られる (`gitscan.py` モジュール docstring の予算表を参照)。
    経過時間で頭を押さえる。deferred は捨てずに pending へ戻す。
    """
    untracked = gitscan.untracked_among(root, rels)
    has_head = gitscan.head_exists(root)

    batch = ReviewBatch()
    used = 0
    deadline = time.monotonic() + COLLECT_BUDGET_SEC

    for index, rel in enumerate(rels):
        if time.monotonic() > deadline:
            batch.deferred_time = [os.path.join(root, r) for r in rels[index:]]
            break

        is_untracked = rel in untracked
        text = gitscan.path_diff(root, rel, is_untracked, has_head)
        # HEAD 基準で空 = 「本当に無変更」「同一ターン内 commit で HEAD と一致
        # した」「追跡ファイルの削除が同一ターン内で commit された (HEAD にも
        # ディスクにもパスが無い)」のいずれかで、この時点では区別できない
        # (docstring 参照)。
        empty_at_head = not is_untracked and has_head and not text.strip()

        if not text.strip():
            if empty_at_head:
                # ディスク上の存在は問わない (マージ前レビューの指摘)。以前は
                # `os.path.exists` も条件にしていたため、削除+同一ターン内
                # commit のケース (ディスクからも消える) だけ通知対象から漏れて
                # 黙って消費されていた。実体の無い pending エントリとの区別は
                # cheap な git 状態だけでは付かない (docstring 参照) ので、
                # 雑音低減より正当な削除を落とさないことを優先する。
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
        output = handle_post_tool(payload)
        if output:
            json.dump(output, sys.stdout, ensure_ascii=False)
    else:
        handle_stop(payload)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:  # hook が例外で Claude Code を止めないよう fail-open
        log(f"fatal: {e}")
