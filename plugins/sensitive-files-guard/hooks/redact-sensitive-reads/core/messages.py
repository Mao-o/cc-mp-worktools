"""Reason text builder。

各 handler は本モジュールの builder のみを呼び、``permissionDecisionReason`` に
入れる文字列を直接組み立てない。文言の語彙ルール、除外案内 (`!<basename>`) の
basename 展開を 1 箇所に集約する。

## 語彙ルール (H2)

| 関数族 | 末尾フレーズ | 用途 |
|---|---|---|
| ``make_deny`` 系 | 「block しました。」 | 機密確定一致 |
| ``ask_or_deny`` 系 | 「**確認のため一時停止します**」 | 判定不能だが機密の可能性。non-bypass=ask、bypass=deny |
| ``ask_or_allow`` 系 | 「**判定不能のため確認を挟みます (auto / bypass では通過)**」 | Bash の静的解析不能。autonomous で日常コマンドを止めない |

LLM 向けの文章であることを意識し、各 builder は **「現状の説明」+「LLM への
next action」** の 2 文構造を取る。「続行しますか？」のような人間 UI 語、
「管理者に連絡してください」のような LLM が取れない action は使わない。

## basename 展開

メッセージ末尾の hint は ``!<basename>`` を **実 basename に展開** して、LLM が
そのままコピペで ``patterns.local.txt`` に追記できる形にする。glob operand
(例: ``*.env*``) はそのまま basename として埋める。

## 出力形式 (0.7.0 で plain text 化、0.10.0 で category 別 dispatch)

deny 系 reason は plain text の複数行で出す。0.4.2〜0.6.x では
``<SFG_DENY tool reason guard>`` 構造化包装で「後段 hook が機械パースできる」
schema を提供していたが、worktools にそうした後段 hook が存在せず
overengineering だったため 0.7.0 で撤廃。``note:`` / ``matched_operand:`` /
``first_token:`` / ``basename:`` / ``suggested_keys:`` / ``extra_note:`` /
``suggestion:`` の各行を改行区切りで連結した plain text を返す。

0.10.0 で ``bash_deny`` を **first_token カテゴリ別 dispatch** に再編 (思想 2 =
block 時は意図を汲んだメッセージを返す)。9 カテゴリ (``read_full`` /
``read_partial`` / ``search`` / ``mutate`` / ``load`` / ``move`` / ``history`` /
``transfer`` / ``archive``) ごとに「想定意図 → 提供する情報・代替案」を切替え、
該当 dotenv ファイルの minimal info (鍵名・型・status・length・placeholder) を
``<DATA untrusted>`` 包装で reason 内に埋め込む。grep 系では operand から
env-var 名を抽出 (E4) し、dotenv parse 結果と照合した ``matched_pattern_keys``
を出す。failure (file 不在 / parse 失敗 / open 失敗) 時は generic reason に
降りる。

Read 側の ``<DATA untrusted="true">`` 包装と ``escape_data_tag`` は維持
(鍵名が LLM コンテキストに残るため最低限の包装防御として意味あり)。
"""
from __future__ import annotations

import os
import re
from typing import Callable, Literal

# 除外行を書き足す patterns.local.txt の preferred パス (CLAUDE.md 参照)。
_LOCAL_PATTERNS_PATH = "~/.claude/sensitive-files-guard/patterns.local.txt"


def _basename_of(operand: str) -> str:
    """operand から ``!<name>`` 用の basename を抽出する。

    通常 path は ``os.path.basename`` の結果。VCS pathspec (``HEAD:.env``) や
    リモート pathspec (``user@host:path/.env``) はコロン後尾の最終要素を抽出
    (``os.path.basename`` は ``:`` を区切り文字として扱わないため自前で行う)。
    URI (``file://.env``) は ``os.path.basename`` の ``/`` 区切りで処理される。
    末尾 ``/`` のディレクトリ系は basename が空になるので、その場合は operand
    全体を返す (例: ``foo/`` → ``foo/``)。
    """
    if not operand:
        return ""
    base = os.path.basename(operand)
    if not base:
        # 末尾 / で basename が空のケース
        return operand
    # VCS / リモート pathspec の ``:`` 後尾を取り出す
    if ":" in base:
        tail = base.rsplit(":", 1)[1]
        if tail:
            return tail
    return base


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
            f"恒久的に許可したい場合は、ユーザーの承認を得た上で "
            f"`{_LOCAL_PATTERNS_PATH}` に除外行 "
            "(`!<basename>`) を追加してください。承認なしに自分で追加しないこと。"
        )
    safe = _sanitize_for_inline(basename)
    return (
        f"恒久的に許可したい場合は、ユーザーの承認を得た上で "
        f"`{_LOCAL_PATTERNS_PATH}` に "
        f"`!{safe}` を追加してください。承認なしに自分で追加しないこと。"
    )


# -- Bash deny dispatcher (0.10.0, E3 + E4) ------------------------------

# first_token → category マップ。E3 で導入したカテゴリ別 dispatcher の中核。
# 未知 first_token は ``"generic"`` カテゴリにフォールバック。
_BASH_DENY_CATEGORY: dict[str, str] = {
    # read_full: ファイル全体を閲覧。Read 同等 minimal info を返す。
    "cat": "read_full",
    "less": "read_full",
    "more": "read_full",
    "bat": "read_full",
    "xxd": "read_full",
    "od": "read_full",
    "hexdump": "read_full",
    "base64": "read_full",
    # read_partial: 先頭/末尾。-n N の値で鍵 list を絞る。
    "head": "read_partial",
    "tail": "read_partial",
    # search: grep family。E4 で pattern 抽出 + matched_pattern_keys。
    "grep": "search",
    "rg": "search",
    "ag": "search",
    "ack": "search",
    "egrep": "search",
    "fgrep": "search",
    # mutate: 加工。実行不可だが minimal info を返す。
    "awk": "mutate",
    "sed": "mutate",
    # load: shell load。direnv / dotenv-cli を推奨。
    "source": "load",
    ".": "load",
    # move: コピー / 移動。secrets manager 推奨。
    "cp": "move",
    "mv": "move",
    # history: git の commit / 差分閲覧。``git rm --cached`` + rotate を推奨。
    "git": "history",
    # transfer: ネット越し転送。強く非推奨。
    "curl": "transfer",
    "wget": "transfer",
    "scp": "transfer",
    "rsync": "transfer",
    # archive: アーカイブ。--exclude で除外を推奨。
    "tar": "archive",
    "zip": "archive",
    "gzip": "archive",
}


def _category_for_first_token(first_token: str) -> str:
    """first_token を 9 カテゴリ + ``generic`` に解決する。"""
    return _BASH_DENY_CATEGORY.get(first_token, "generic")


def _common_meta_lines(first_token: str, operand: str) -> list[str]:
    """``matched_operand:`` / ``first_token:`` の共通 meta 行を返す。"""
    lines: list[str] = []
    if operand:
        lines.append(f"matched_operand: {operand}")
    if first_token:
        lines.append(f"first_token: {first_token}")
    return lines


def _append_minimal_info(lines: list[str], file_render: str) -> None:
    """``minimal info (Read 同等):`` ラベルと file_render の中身を追加する。

    ``file_render`` は ``redaction.file_render.render_for_bash`` が返す
    ``<DATA untrusted>`` 包装込みの文字列。空ならラベルごと省略。
    """
    if file_render:
        lines.append("minimal info (Read 同等):")
        lines.append(file_render)


# head / tail の ``-n N`` / ``-N`` / ``--lines=N`` を抽出するための regex 一覧
# (上から順に match を試す)。``-N`` (BSD-style) は単独の ``-`` 後に数値が続く形。
_HEAD_TAIL_N_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|\s)-n\s+(\d+)\b"),
    re.compile(r"--lines=(\d+)\b"),
    re.compile(r"(?:^|\s)-(\d+)\b"),
)

# default 行数 (``head`` / ``tail`` 共通の慣習)。``-n`` 抽出失敗時の fallback。
_HEAD_TAIL_N_DEFAULT = 10


def _extract_head_tail_n(command: str, default: int = _HEAD_TAIL_N_DEFAULT) -> int:
    """``head`` / ``tail`` の行数指定を command 文字列から読む。

    抽出失敗または値が想定外 (1〜10000) なら ``default``。
    """
    if not command:
        return default
    for pattern in _HEAD_TAIL_N_PATTERNS:
        m = pattern.search(command)
        if m:
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if 1 <= n <= 10000:
                return n
    return default


def _format_dotenv_key_line(k: dict) -> str:
    """``redact_dotenv`` の 1 key dict を 1 行表示用に整形する。

    ``redaction.dotenv.format_dotenv`` の表示と整合させるが、こちらは Bash
    deny の result セクションで個別キーを展開する用途。
    """
    name = k["name"]
    type_part = f"<type={k['type']}"
    if k.get("prefix"):
        type_part += f' prefix="{k["prefix"]}"'
    type_part += ">"
    status_part = "  ".join(k.get("status", []))
    line = f"  {name}  {type_part}  {status_part}"
    if "<empty>" not in k.get("status", []):
        line += f"  length={k.get('length', 0)}"
    if k.get("placeholder"):
        line += f'  matched="{k["placeholder"]}"'
    return line


def _suggestion_other_keys(dotenv_info: dict | None) -> str | None:
    """dotenv_info から ``<placeholder>`` / ``<empty>`` を持つキー数を集計し、
    「他の鍵も見直してください」の suggestion 文を返す。該当なしなら None。
    """
    if not dotenv_info:
        return None
    info_keys = dotenv_info.get("keys", [])
    ph = sum(1 for k in info_keys if "<placeholder>" in k.get("status", []))
    em = sum(1 for k in info_keys if "<empty>" in k.get("status", []))
    if not (ph or em):
        return None
    parts: list[str] = []
    if ph:
        parts.append(f"{ph} 件の <placeholder>")
    if em:
        parts.append(f"{em} 件の <empty>")
    return (
        "API 失敗の調査なら、上記以外に "
        + " / ".join(parts)
        + " のキーも見直してください。"
    )


# -- Bash deny category builders (0.10.0) --------------------------------


def _bash_deny_read_full(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``cat`` / ``less`` / ``more`` / ``bat`` / ``xxd`` / ``od`` / ``hexdump``
    / ``base64`` 等、ファイル全体を閲覧する意図の deny reason。"""
    basename = _basename_of(operand)
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) の"
        "全体を閲覧しようとしたため block しました。"
        "値の LLM コンテキスト混入を防ぐため block 固定です。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    _append_minimal_info(lines, file_render)
    lines.append(
        "suggestion: 値そのものではなく構造のみを把握したいなら、"
        "Read tool を使ってください (本 hook が同様の minimal info を返します)。"
    )
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_read_partial(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``head`` / ``tail`` の deny reason。``-n N`` の値で鍵 list を絞る。"""
    basename = _basename_of(operand)
    is_tail = first_token == "tail"
    label = "末尾" if is_tail else "先頭"
    n = _extract_head_tail_n(command)
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) の"
        f"{label} {n} 行を確認しようとしたため block しました。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    if dotenv_info is not None:
        info_keys = dotenv_info.get("keys", [])
        total = len(info_keys)
        if is_tail:
            shown = info_keys[-n:] if n < total else list(info_keys)
        else:
            shown = info_keys[:n]
        lines.append(f"keys ({label} {n}, 全 {total} 件):")
        for k in shown:
            lines.append(_format_dotenv_key_line(k))
        lines.append(
            "note: real values are not in context. only key names, type, prefix,"
            " length, status tags, and placeholder hints are returned."
        )
    else:
        _append_minimal_info(lines, file_render)
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_search(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``grep`` / ``rg`` / ``ag`` / ``ack`` / ``egrep`` / ``fgrep`` の deny reason。

    E4 で導入: ``grep_keys`` (extract_grep_keys の結果) と ``dotenv_info["keys"]``
    の照合で ``matched_pattern_keys`` を出す。pattern 抽出も照合も成立しない
    ときは全鍵 list の minimal info に降りる。
    """
    basename = _basename_of(operand)
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) を"
        "検索しようとしたため block しました。"
        "検索結果に値の一部が含まれて LLM コンテキストに露出するリスクがあります。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))

    used_pattern_keys = False
    if grep_keys:
        if dotenv_info is not None:
            keys_by_name = {k["name"]: k for k in dotenv_info.get("keys", [])}
            matched = [name for name in grep_keys if name in keys_by_name]
            nomatched = [name for name in grep_keys if name not in keys_by_name]
            if matched:
                used_pattern_keys = True
                lines.append(f"matched_pattern_keys: [{', '.join(matched)}]")
                lines.append("result:")
                for name in matched:
                    lines.append(_format_dotenv_key_line(keys_by_name[name]))
            if nomatched:
                used_pattern_keys = True
                lines.append(f"nomatch_pattern_keys: [{', '.join(nomatched)}]")
        else:
            used_pattern_keys = True
            lines.append(f"pattern_keys: [{', '.join(grep_keys)}]")

    if not used_pattern_keys:
        _append_minimal_info(lines, file_render)

    other = _suggestion_other_keys(dotenv_info)
    if other:
        lines.append(f"suggestion: {other}")
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_mutate(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``awk`` / ``sed`` の deny reason。加工は実行できないが minimal info は返す。"""
    basename = _basename_of(operand)
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) を"
        "加工 (テキスト処理) しようとしたため block しました。"
        "加工結果の出力に値が含まれて LLM コンテキストに露出するリスクがあります。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    _append_minimal_info(lines, file_render)
    lines.append(
        "suggestion: 加工は実行できません。鍵名・型・状態は上記 minimal info を"
        "確認してください。"
        " 値の置換が目的なら、対象ファイルを直接編集する代わりに別ファイルへの"
        " patch / diff 適用を検討してください。"
    )
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_load(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``source`` / ``.`` の deny reason。direnv / dotenv-cli を推奨。"""
    basename = _basename_of(operand)
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) を"
        "shell に load しようとしたため block しました。"
        "load された値は env として LLM が観察可能な範囲に露出するリスクがあります。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    _append_minimal_info(lines, file_render)
    lines.append(
        "suggestion: 環境変数として読み込みたいなら direnv (`.envrc`) や"
        " dotenv-cli の利用を推奨します。"
        " 1Password CLI / pass / git-secret 経由の secret 読込でも代替できます。"
    )
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_move(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``cp`` / ``mv`` の deny reason。secrets manager / .env.example 派生を推奨。"""
    basename = _basename_of(operand)
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) を"
        "コピー / 移動しようとしたため block しました。"
        "別パスへの複製は gitignore 範囲外への漏洩リスクがあります。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    lines.append(
        "suggestion: バックアップが目的なら 1Password CLI / pass / git-secret"
        " 等の secrets manager を推奨します。"
        " `.env.example` 派生で運用するなら `cp .env.example .env.local` の"
        "方向で代替できます。"
    )
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_history(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``git`` の deny reason (``git show HEAD:.env`` / ``git diff .env`` /
    ``git log -p .env`` 等)。tracked なら漏洩済みの可能性を提示。"""
    basename = _basename_of(operand)
    safe_basename = _sanitize_for_inline(basename) or basename
    note = (
        f"git 経由で機密ファイル ({operand}) の commit / 差分を"
        "閲覧しようとしたため block しました。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    lines.append(
        f"suggestion: この {safe_basename} が tracked になっているなら、"
        "過去 commit に値が残っており既に漏洩済みの可能性があります。"
        f" `git rm --cached {safe_basename}` で untrack 後に値を rotate してください。"
        " untracked なら別パスから誤って参照していないか確認してください。"
    )
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_transfer(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``curl`` / ``wget`` / ``scp`` / ``rsync`` の deny reason。"""
    basename = _basename_of(operand)
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) を"
        "転送しようとしたため block しました。"
        "ネット越し / リモートへの転送は漏洩リスクが大きく強く非推奨です。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    lines.append(
        "suggestion: 機密値はリモート転送せず、必要があれば受信側で"
        " secrets manager (1Password CLI / Vault / SOPS 等) に置く構成に"
        "してください。"
    )
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_archive(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """``tar`` / ``zip`` / ``gzip`` の deny reason。--exclude を推奨。"""
    basename = _basename_of(operand)
    safe_basename = _sanitize_for_inline(basename) or basename
    note = (
        f"Bash コマンド ({first_token}) で機密ファイル ({operand}) を"
        "アーカイブに含めようとしたため block しました。"
        "アーカイブ経由で値がそのまま転送 / 配布されるリスクがあります。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    lines.append(
        "suggestion: アーカイブから機密ファイルを除外してください。"
        f" tar なら `--exclude={safe_basename}`、zip なら `-x {safe_basename}`、"
        " gzip は単一ファイル圧縮なので別ファイルを対象にしてください。"
    )
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


def _bash_deny_generic(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
) -> str:
    """既知 category 外の deny reason (0.7.0〜0.9.0 の generic 相当に minimal info を追加)。"""
    basename = _basename_of(operand)
    note = (
        f"Bash コマンド ({first_token}) の operand ({operand}) が"
        "機密パターンに一致するため block しました。"
        "値が LLM コンテキストに露出する可能性があります。"
    )
    lines: list[str] = [f"note: {note}"]
    lines.extend(_common_meta_lines(first_token, operand))
    _append_minimal_info(lines, file_render)
    lines.append(f"suggestion: {_exclude_hint(basename)}")
    return "\n".join(lines)


# 9 builder + generic を category キーで dispatch する table。
_BashDenyBuilder = Callable[..., str]
_BASH_DENY_BUILDERS: dict[str, _BashDenyBuilder] = {
    "read_full": _bash_deny_read_full,
    "read_partial": _bash_deny_read_partial,
    "search": _bash_deny_search,
    "mutate": _bash_deny_mutate,
    "load": _bash_deny_load,
    "move": _bash_deny_move,
    "history": _bash_deny_history,
    "transfer": _bash_deny_transfer,
    "archive": _bash_deny_archive,
    "generic": _bash_deny_generic,
}


def bash_deny(
    first_token: str,
    operand: str,
    *,
    command: str = "",
    file_render: str = "",
    dotenv_info: dict | None = None,
    grep_keys: list[str] | None = None,
) -> str:
    """Bash 操作の deny reason を plain text で構築する (0.10.0 で category dispatch)。

    first_token のカテゴリで builder を切替え、コマンド意図に合った文言と
    Read 同等 minimal info / matched_pattern_keys を埋め込む。新規 keyword
    引数 ``command`` / ``file_render`` / ``dotenv_info`` / ``grep_keys`` を
    渡さない呼び出しでも動作する (旧 0.7.0〜0.9.0 互換、generic 相当の出力)。

    Args:
        first_token: 検出されたコマンドの第 1 トークン (例: ``cat``)。
        operand: 引っかかった operand。literal path か glob 含む path。
        command: ``envelope["tool_input"]["command"]`` の全体文字列。
            head / tail の ``-n N`` 抽出に使う。
        file_render: ``redaction.file_render.render_for_bash`` で生成した
            ``<DATA>`` 包装込みの minimal info 文字列。空なら埋め込まない。
        dotenv_info: ``redact_dotenv`` の戻り値 dict。dotenv 以外 / 失敗時 None。
            search builder で matched_pattern_keys 照合、read_partial で
            head/tail 切り出しに使用。
        grep_keys: ``extract_grep_keys`` で抽出した env-var 名候補リスト。
            grep 系以外では None。
    """
    category = _category_for_first_token(first_token)
    builder = _BASH_DENY_BUILDERS[category]
    return builder(
        first_token=first_token,
        operand=operand,
        command=command,
        file_render=file_render,
        dotenv_info=dotenv_info,
        grep_keys=grep_keys,
    )


def edit_deny(
    tool_label: str,
    basename: str,
    new_keys: list[str] | None = None,
    extra_note: str = "",
    *,
    max_suggested_keys: int = 30,
) -> str:
    """Edit / Write の deny reason を plain text で構築する。

    dotenv 系で書き込み予定のキー名 ``new_keys`` が渡されたときは
    ``.env.example`` への代替案を埋め込む。``extra_note`` は symlink / special
    等の追加事情を ``extra_note:`` 行として挿入する (0.7.0 で kind 引数を
    廃止し、文脈は extra_note のみで表現する形に縮約)。

    Args:
        tool_label: ``Edit`` / ``Write`` のラベル。
        basename: 書き込み先ファイルの basename。``basename:`` 行と除外 hint で
            ``!<basename>`` に展開される。
        new_keys: dotenv parse で抽出された新規キー名リスト (順序維持)。
            非 dotenv では空リストか None を渡す。
        extra_note: ``extra_note:`` 行に入れる補足 (symlink / special など)。
        max_suggested_keys: ``new_keys`` の上限 (3KB 制約のため切り詰める)。
    """
    note = (
        f"{tool_label}: 機密パターン一致のファイル ({basename}) への書き込みを "
        "block しました (値喪失や機密流出防止のため)。"
    )

    lines: list[str] = [f"note: {note}", f"basename: {basename}"]

    if new_keys:
        shown = new_keys[:max_suggested_keys]
        remaining = len(new_keys) - len(shown)
        lines.append("suggested_keys:")
        for k in shown:
            lines.append(f"  {k}=")
        if remaining > 0:
            lines.append(f"  ... ({remaining} more)")
        lines.append(
            "suggestion_alt: 追加予定のキー名を `.env.example` に追記すると、"
            "差分把握がしやすくなります (値は後で個別設定)。"
        )

    if extra_note:
        lines.append(f"extra_note: {extra_note}")

    lines.append(f"suggestion: {_exclude_hint(basename)}")

    return "\n".join(lines)


# -- M3: patterns.txt 読込失敗 --------------------------------------------

PolicySeverity = Literal["deny", "pause"]


def policy_unavailable(severity: PolicySeverity, tool_label: str = "") -> str:
    """``patterns.txt`` が読めない時の reason を返す (M3, 0.7.0 で plain text 化)。

    severity:
      - ``"deny"``: Bash handler 用 (全 mode block)。
      - ``"pause"``: Read / Edit / Write 用 (ask_or_deny で安全側)。

    tool_label が空でなければ pause 文の prefix として埋める。deny 系では
    無視 (Hook 自体の問題のため)。
    """
    if severity == "deny":
        return (
            "ガードポリシー (patterns.txt) が読み込めないため "
            "Bash コマンドを block しました。"
            "plugin パッケージング / 設定を確認してください。"
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
    """Edit / Write で判定不能ケースの reason 文 (ask_or_deny 用)。"""
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

# autonomous モードに関する固定 suffix。permission_mode が auto / bypass の
# 場合は実際の判定で allow に倒すが、reason 文上では「LLM がどう振る舞うべきか」
# だけを伝える。
_BASH_LENIENT_SUFFIX = (
    "判定不能のため確認を挟みます (auto / bypass では通過)。"
)


def bash_lenient(kind: BashLenientKind, detail: str = "") -> str:
    """Bash の静的解析不能ケースを ask_or_allow で扱う際の reason 文。

    Args:
        kind: 解析不能の種別
        detail: ``shell_keyword`` の場合のキーワード名など追加情報
    """
    if kind == "hard_stop":
        head = (
            "Bash コマンドに動的展開 / heredoc / process 置換 / 入力リダイレクト "
            "/ グループ化 ($, バッククォート, $(...), <<, <(...), <, (), {}) が"
            "含まれています。"
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
