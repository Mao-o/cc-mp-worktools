"""Reason text builder。

各 handler は本モジュールの builder のみを呼び、``permissionDecisionReason`` に
入れる文字列を直接組み立てない。文言の語彙ルールと除外案内 (`!<basename>`) の
basename 展開を 1 箇所に集約し、後続の M4 等で文言を入れ替える際に触る箇所を
最小化する。

## 語彙ルール (H2)

| 関数族 | 末尾フレーズ | 用途 |
|---|---|---|
| ``make_deny`` 系 | 「block しました。」 | 機密確定一致 |
| ``ask_or_deny`` 系 | 「**確認のため一時停止します**」 | 判定不能だが機密の可能性。non-bypass=ask、bypass=deny |
| ``ask_or_allow`` 系 | 「**判定不能のため確認を挟みます (auto / bypass / plan では通過)**」 | Bash の静的解析不能。autonomous で日常コマンドを止めない |

LLM 向けの文章であることを意識し、各 builder は **「現状の説明」+「LLM への
next action」** の 2 文構造を取る。「続行しますか？」のような人間 UI 語、
「管理者に連絡してください」のような LLM が取れない action は使わない。

## basename 展開

メッセージ末尾の hint は ``!<basename>`` を **実 basename に展開** して、LLM が
そのままコピペで ``patterns.local.txt`` に追記できる形にする。glob operand
(例: ``*.env*``) はそのまま basename として埋める。
"""
from __future__ import annotations

import os
from typing import Literal

# 除外行を書き足す patterns.local.txt の preferred パス (CLAUDE.md 参照)。
_LOCAL_PATTERNS_PATH = "~/.claude/sensitive-files-guard/patterns.local.txt"


def _basename_of(operand: str) -> str:
    """operand から ``!<name>`` 用の basename を抽出する。

    通常 path は ``os.path.basename`` の結果。VCS pathspec (``HEAD:.env``) や
    URI (``file://.env``) はコロン / スラッシュ後尾の最終要素を素直に取る。
    末尾 ``/`` のディレクトリ系は basename が空になるので、その場合は operand
    全体を返す (例: ``foo/`` → ``foo/``)。
    """
    if not operand:
        return ""
    base = os.path.basename(operand)
    if base:
        return base
    # 末尾 / で basename が空のケース
    return operand


def _sanitize_for_inline(text: str) -> str:
    """reason 文中に埋め込む文字列のうち、Markdown backtick 衝突を防ぐ。

    `!<name>` を backtick で囲って表示するため、name 内の backtick を削る。
    削る方針は escape ではなく drop (LLM 向け表示で見やすさを優先)。
    """
    return text.replace("`", "")


def _exclude_hint(basename: str) -> str:
    """``patterns.local.txt`` への除外行追加案内を返す。

    basename が空なら一般化された hint。空でなければ ``!<basename>`` を埋め込む。
    """
    if not basename:
        return (
            f"許可したい場合は `{_LOCAL_PATTERNS_PATH}` に除外行 "
            "(`!<basename>`) を追加してください。"
        )
    safe = _sanitize_for_inline(basename)
    return (
        f"許可したい場合は `{_LOCAL_PATTERNS_PATH}` に "
        f"`!{safe}` を追加してください。"
    )


# Bash deny の検出種別タグ。reason の冒頭文を切り替える分類。
BashDenyKind = Literal[
    "literal",
    "glob",
    "input_redirect",
    "input_redirect_glob",
]


def bash_deny(
    first_token: str,
    operand: str,
    kind: BashDenyKind,
) -> str:
    """Bash 操作の deny reason を構築する。

    Args:
        first_token: 検出されたコマンドの第 1 トークン (例: ``cat``)。
            input redirect 系では空でもよい (caller 側の文脈による)。
        operand: 引っかかった operand。literal path / glob / redirect target。
        kind: 検出種別。reason の冒頭文と除外 hint の表現を切り替える。
    """
    basename = _basename_of(operand)

    if kind == "literal":
        head = (
            f"Bash コマンド ({first_token}) の operand ({operand}) が"
            "機密パターンに一致します。"
            "値が LLM コンテキストに露出する可能性があるため block しました。"
        )
    elif kind == "glob":
        head = (
            f"Bash コマンド ({first_token}) の operand glob ({operand}) が"
            "機密パターンと交差する候補を含みます。"
            "値が LLM コンテキストに露出する可能性があるため block しました。"
        )
    elif kind == "input_redirect":
        head = (
            f"Bash 入力リダイレクト先 ({operand}) が機密パターンに一致します。"
            "値が LLM コンテキストに露出する可能性があるため block しました。"
        )
    elif kind == "input_redirect_glob":
        head = (
            f"Bash 入力リダイレクト先 ({operand}) が"
            "機密パターンに一致する候補を含みます。"
            "値が LLM コンテキストに露出する可能性があるため block しました。"
        )
    else:  # pragma: no cover — kind は Literal で型保護されている
        head = (
            f"Bash コマンド ({first_token}) の operand ({operand}) が"
            "機密パターンに一致するため block しました。"
        )

    return f"{head} {_exclude_hint(basename)}"


def edit_deny(
    tool_label: str,
    basename: str,
    new_keys: list[str] | None = None,
    extra_note: str = "",
    *,
    max_suggested_keys: int = 30,
) -> str:
    """Edit / Write / MultiEdit の deny reason を構築する。

    既存 ``edit_handler._build_deny_reason`` の集約。dotenv 系で書き込み予定の
    キー名 ``new_keys`` が渡されたときは ``.env.example`` への代替案を埋め込む。
    ``extra_note`` は symlink / special 等の追加事情を末尾に挿入する。

    Args:
        tool_label: ``Edit`` / ``Write`` / ``MultiEdit`` のラベル。
        basename: 書き込み先ファイルの basename。除外 hint で ``!<basename>``
            に展開される。
        new_keys: dotenv parse で抽出された新規キー名リスト (順序維持)。
            非 dotenv では空リストか None を渡す。
        extra_note: 末尾に追加する補足 (symlink / special など)。
        max_suggested_keys: ``new_keys`` の上限 (3KB 制約のため切り詰める)。
    """
    header = (
        f"{tool_label}: 機密パターン一致のファイル ({basename}) への書き込みを "
        "block しました (値喪失や機密流出防止のため)。"
    )
    hint = _exclude_hint(basename)

    if new_keys:
        shown = new_keys[:max_suggested_keys]
        remaining = len(new_keys) - len(shown)
        lines: list[str] = [
            header,
            "",
            "代替案: 追加予定のキー名を `.env.example` に追記すると、"
            "差分把握がしやすくなります (値は後で個別設定):",
        ]
        for k in shown:
            lines.append(f"  {k}=")
        if remaining > 0:
            lines.append(f"  ... ({remaining} more)")
        lines.append("")
        lines.append(hint)
        if extra_note:
            lines.extend(["", extra_note])
        return "\n".join(lines)

    if extra_note:
        return f"{header} {hint}\n\n{extra_note}"
    return f"{header} {hint}"


# -- M3: patterns.txt 読込失敗 --------------------------------------------

PolicySeverity = Literal["deny", "pause"]


def policy_unavailable(severity: PolicySeverity, tool_label: str = "") -> str:
    """``patterns.txt`` が読めない時の reason を返す (M3)。

    severity:
      - ``"deny"``: Bash handler 用 (全 mode block)
      - ``"pause"``: Read / Edit / Write 用 (ask_or_deny で安全側)

    tool_label が空でなければ prefix として埋める (Edit/Write 等の文脈識別)。
    """
    if severity == "deny":
        return (
            "ガードポリシー (patterns.txt) が読み込めないため Bash コマンドを "
            "block しました。plugin パッケージング / 設定を確認してください。"
        )
    prefix = f"{tool_label}: " if tool_label else ""
    return (
        f"{prefix}ガードポリシー (patterns.txt) が読み込めません。"
        "plugin パッケージング / 設定を確認してから再試行してください。"
    )


# -- M2: Read handler 用 ask_or_deny --------------------------------------

ReadAskKind = Literal[
    "symlink",
    "special",
    "io_error",
    "normalize_failed",
    "redaction_failed",
    "open_failed",
]


def read_ask(kind: ReadAskKind) -> str:
    """Read tool で発生した判定不能ケースの reason 文 (ask_or_deny 用, M2)。

    末尾は「~してから再試行してください」で揃え、LLM が次にとれる action を
    明示する。
    """
    if kind == "symlink":
        return (
            "symlink 経由で機密パターンに一致するファイルを読もうとしています。"
            "symlink 先が意図した参照か確認してから再試行してください。"
        )
    if kind == "special":
        return (
            "非通常ファイル (FIFO / socket / device) が機密パターンに一致します。"
            "意図的な参照か確認してから再試行してください。"
        )
    if kind == "io_error":
        return (
            "ファイル状態の確認に失敗しました (権限 / IO エラー)。"
            "権限と存在を確認してから再試行してください。"
        )
    if kind == "normalize_failed":
        return (
            "file_path の正規化に失敗しました。"
            "パス文字列の異常 (NUL バイト等) を確認してから再試行してください。"
        )
    if kind == "redaction_failed":
        return (
            "redaction 処理に失敗しました。"
            "ファイル形式が想定外の可能性があります。手動で内容を確認してください。"
        )
    if kind == "open_failed":
        return (
            "安全な open に失敗しました (symlink race / 非通常ファイル疑い)。"
            "ファイル状態を確認してから再試行してください。"
        )
    # type-check ガードで到達しないが、念のためのフォールバック
    return "判定不能のため確認のため一時停止します。"  # pragma: no cover


# -- H2 + M2: Edit/Write 用 ask_or_deny -----------------------------------

EditPauseKind = Literal[
    "normalize_failed",
    "io_error",
    "parent_not_directory",
]


def edit_pause(kind: EditPauseKind, tool_label: str = "Edit/Write") -> str:
    """Edit / Write / MultiEdit で判定不能ケースの reason 文 (ask_or_deny 用)。"""
    if kind == "normalize_failed":
        return (
            f"{tool_label}: file_path の正規化に失敗しました。"
            "パス文字列を確認してから再試行してください。"
        )
    if kind == "io_error":
        return (
            f"{tool_label}: ファイル状態の確認に失敗しました (権限 / IO)。"
            "ファイル権限と存在を確認してから再試行してください。"
        )
    if kind == "parent_not_directory":
        return (
            f"{tool_label}: 親ディレクトリが通常ディレクトリではありません "
            "(symlink / 特殊 / 不在)。親ディレクトリの状態を確認してから "
            "再試行してください。"
        )
    return f"{tool_label}: 判定不能のため一時停止します。"  # pragma: no cover


# -- H2: Bash 用 ask_or_allow ---------------------------------------------

BashLenientKind = Literal[
    "hard_stop",
    "opaque_prefix",
    "residual_metachar",
    "shell_keyword",
    "tokenize_failed",
    "normalize_failed",
]

# autonomous モードに関する固定 suffix。permission_mode が auto / bypass / plan
# の場合は実際の判定で allow に倒すが、reason 文上では「LLM がどう振る舞うべきか」
# だけを伝える。
_BASH_LENIENT_SUFFIX = (
    "判定不能のため確認を挟みます (auto / bypass / plan では通過)。"
)


def bash_lenient(kind: BashLenientKind, detail: str = "") -> str:
    """Bash の静的解析不能ケースを ask_or_allow で扱う際の reason 文。

    Args:
        kind: 解析不能の種別
        detail: ``shell_keyword`` の場合のキーワード名など追加情報
    """
    if kind == "hard_stop":
        head = (
            "Bash コマンドに動的展開 / heredoc / process 置換 / グループ化 "
            "($, バッククォート, $(...), <<, <(...), (), {}) が含まれています。"
        )
    elif kind == "opaque_prefix":
        head = (
            "Bash コマンドが静的解析対象外の wrapper / インタプリタ / 任意 path "
            "実行で始まっています。"
        )
    elif kind == "residual_metachar":
        head = (
            "Bash セグメント内に解析対象外のリダイレクト / metachar が"
            "残っています。"
        )
    elif kind == "shell_keyword":
        kw = detail or "?"
        head = (
            f"シェル予約語 / 制御構文 ({kw}) で始まるセグメントは"
            "静的解析対象外です。"
        )
    elif kind == "tokenize_failed":
        head = "Bash コマンドの tokenize に失敗しました。"
    elif kind == "normalize_failed":
        head = "Bash コマンド内のパス正規化に失敗しました。"
    else:  # pragma: no cover — kind は Literal で型保護
        head = "Bash コマンドの静的解析に失敗しました。"
    return f"{head} {_BASH_LENIENT_SUFFIX}"


# -- __main__ wrapper 用 (起動 / 入力 / 内部例外) -------------------------


def hook_invocation_error() -> str:
    """argparse 失敗時の reason 文。LLM ではなく settings.json を直すべき類。"""
    return (
        "redact-hook の起動引数が不正です。"
        "settings.json の hooks 定義 (--tool 引数) を確認してください。"
    )


def stdin_parse_failed() -> str:
    """stdin の JSON 解析失敗時の reason 文。"""
    return (
        "hook 入力 JSON の解析に失敗しました。"
        "Claude Code 側 hook envelope 不整合の可能性があります。"
    )


def unsupported_platform() -> str:
    """SIGALRM 非対応 (Windows 等) の deny 文。"""
    return (
        "redact-hook は現状 UNIX (Linux / macOS) のみサポートしています。"
        "Windows 等では fail-closed で deny します (README の既知制限を参照)。"
    )


def handler_internal_error(tool: str, exc_type: str = "") -> str:
    """handler 内部例外 catch-all の reason 文 (ask_or_deny 用)。"""
    suffix = f" ({exc_type})" if exc_type else ""
    return (
        f"{tool} handler 内部エラー{suffix}で安全側に倒しました。"
        "操作を変えて再試行するか、~/.claude/logs/redact-hook.log を"
        "確認してください。"
    )
