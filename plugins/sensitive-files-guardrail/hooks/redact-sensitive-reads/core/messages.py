"""Reason text builder。

各 handler は本モジュールの builder のみを呼び、``permissionDecisionReason`` に
入れる文字列を直接組み立てない。文言の語彙ルール、除外案内 (`!<root 相対パス>` /
`!<basename>`) の展開を 1 箇所に集約する。

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

0.24.0 から、project root 相対 path を確定できる経路 (Edit / Write の
``file_path``、Bash の literal operand) では **path 形** ``!<root 相対パス>``
(承認した 1 ファイルだけを外す) を既定として案内し、basename 形 (同名すべて)
は明示的な選択として併記する。glob operand / VCS pathspec (``HEAD:.env``) /
root 配下でない path / root 不明のときは basename 形だけを案内する
(``_exclude_hint`` の ``relpath`` 引数)。

0.19.0 から hint は ``[project:$CLAUDE_PROJECT_DIR]``
セクション配下への追記を **既定** として案内する (``_shared.patterns`` の
レシピ定数と共通、Stop hook の block reason も同じ)。ヘッダー無し行
(全プロジェクト共通) は明示的な選択にする。reason に絶対パスを出さない方針の
ため、ヘッダーは環境変数名のまま示し書き込む側が実パスに置き換える。

## 出力形式 (0.7.0 で plain text 化、0.10.0 で category 別 dispatch)

deny 系 reason は plain text の複数行で出す。0.4.2〜0.6.x では
``<GUARDRAIL_DENY tool reason guard>`` 構造化包装で「後段 hook が機械パースできる」
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

0.20.0 で ``edit_deny`` を **kind 別 dispatch** に拡張 (E6)。書き込み先の状態
(``new`` / ``overwrite`` / ``symlink`` / ``special``) で note と代替案を切替え、
``overwrite`` では上書き対象の既存ファイルの minimal info を
``<DATA untrusted>`` 包装で埋め込む。**判定 (deny/ask/allow) は変えず、reason
文字列の情報量だけが変わる**のは 0.10.0 の bash_deny 拡張と同じ方針。

## reason の byte 予算

``core.output.MAX_REASON_BYTES`` (3KB) を超えた分は ``core.output._truncate``
が末尾から切る。切られて困る行 (除外案内) が末尾にあるため、**サイズが入力
依存で伸びるセクションを埋め込む builder は自分で予算内に収める**こと。
``edit_deny`` の minimal info がその実装例 (``_fit_data_block``)。
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Callable, Literal

from _shared.patterns import (
    EXCLUDE_SCOPE_WARNING,
    LOCAL_PATTERNS_DISPLAY_PATH,
    escape_glob,
    path_rule_for,
    PROJECT_SECTION_HEADER_HINT,
    PROJECT_SECTION_PLACEHOLDER_NOTE,
)
# reason の byte 予算。``core.output`` は typing しか import しないため循環しない。
# builder 側で予算を見るのは E6 (0.20.0) の minimal info 埋め込みだけで、
# ``_truncate`` による最終防御は従来どおり ``core.output`` 側に残る。
from core.output import MAX_REASON_BYTES

# 除外行を書き足す patterns.local.txt の preferred パス (表示用。実体の解決は
# ``_shared.patterns._resolve_local_patterns_path``、0.19.0 で _shared と共有)。
_LOCAL_PATTERNS_PATH = LOCAL_PATTERNS_DISPLAY_PATH


def _join_with_exclude_hint(
    lines: list[str],
    basename: str,
    literal_name: bool = False,
    relpath: str = "",
) -> str:
    """本文 + 除外案内を組み立てる。**案内は必ず全文残す** (0.23.0)。

    除外案内には「path 形は 1 ファイル / basename 形は同名すべて」
    「Read/Bash/Edit/Write の保護そのものが外れる」という影響範囲の開示が
    含まれる。``core.output._truncate`` は末尾から無条件に切るため、minimal
    info が大きいと **レシピ (``!.env``) だけ見えて警告が切れる**状態になっていた
    (30 キーの ``.env`` で実測: reason 3,071 byte、`保護そのものが外れます` が消失)。
    実行可能な除外コマンドを見せながら影響範囲を隠すのは informed consent の
    逆なので、可変長側 (minimal info) を先に削って案内の場所を確保する。

    ``relpath`` (0.24.0) は project root 相対 path。空でなければ path 形の
    レシピを既定として案内する (``_exclude_hint`` 参照)。

    ``_truncate`` 自体は最終防御としてそのまま残す (ここを通らない経路もあるため)。

    0.26.0: 予算超過時、``lines`` に埋め込まれた ``<DATA>``...``</DATA>``
    1 要素 (``_append_minimal_info`` が ``file_render`` をまるごと 1 行として
    追加したもの) を検出できれば ``_fit_data_block`` でその部分だけを折り畳む
    (``_fold_data_block``)。それ以外の固定行 (``note:`` / ``matched_operand:``
    等) には手を付けない。従来は ``lines`` 全体を末尾から盲目 byte cut して
    おり、``</DATA>`` 閉じタグや per-format の末尾 note (「実値は無い」の
    免責事項) を key 行の途中でちぎっていた (90 key の ``.env`` で実測)。
    埋め込みが無い、または折り畳んでも収まらない場合は従来どおりの盲目 cut に
    フォールバックする (安全側の最終防御は維持)。
    """
    hint = f"suggestion: {_exclude_hint(basename, literal_name, relpath)}"
    tail = "\n" + hint
    budget = MAX_REASON_BYTES - len(tail.encode("utf-8"))
    body = "\n".join(lines)

    if budget <= 0:
        # 案内だけで予算を超える異常ケース。本文を捨ててでも案内を優先する
        # (本文は情報提供、案内は安全側の判断材料)。
        return hint

    encoded = body.encode("utf-8")
    if len(encoded) > budget:
        folded = _fold_data_block(lines, budget)
        if folded is not None:
            body = folded
        else:
            marker = "\n...[truncated]"
            keep = budget - len(marker.encode("utf-8"))
            if keep <= 0:
                return hint
            cut = encoded[:keep]
            # UTF-8 の途中で切らない
            while cut and (cut[-1] & 0xC0) == 0x80:
                cut = cut[:-1]
            body = cut.decode("utf-8", errors="ignore") + marker

    return body + tail


def _fold_data_block(lines: list[str], budget: int) -> str | None:
    """埋め込み ``<DATA>``...``</DATA>`` 要素を検出できれば折り畳む (0.26.0)。

    ``lines`` の中に (``_append_minimal_info`` が足した) ``file_render`` 丸ごと
    1 要素として ``<DATA ...>`` で始まり ``</DATA>`` で終わる文字列があれば、
    その要素だけを ``_fit_data_block`` で ``budget`` に収める。それ以外の行
    (固定長の note: / matched_operand: 等) の byte 数を先に引いてから
    ``_fit_data_block`` へ渡すことで、閉じタグと末尾 note を保護したまま
    key 行だけを畳める。

    見つからない、または折り畳んでも ``budget`` に収まらない場合は ``None``
    (呼出側が旧来の盲目 byte cut にフォールバックする)。
    """
    data_idx = None
    for i, line in enumerate(lines):
        if line.startswith("<DATA ") and line.endswith(_DATA_CLOSING_TAG):
            data_idx = i
            break
    if data_idx is None:
        return None

    before = lines[:data_idx]
    after = lines[data_idx + 1:]
    fixed_cost = sum(_line_cost(x) for x in before + after)
    fitted = _fit_data_block(
        lines[data_idx], budget - fixed_cost, next_action=_OMIT_ACTION_USE_READ,
    )
    if not fitted:
        return None
    candidate = "\n".join(before + ["\n".join(fitted)] + after)
    if len(candidate.encode("utf-8")) > budget:
        # 概算 (行ごとの ``_line_cost``) の誤差が悪い方に転んだ極端なケース。
        # 折り畳みを諦めて呼出側の盲目 cut に委ねる (安全側)。
        return None
    return candidate


def fit_read_reason(reason: str, budget: int = MAX_REASON_BYTES) -> str:
    """Read handler 専用: ``<DATA>`` 包装済み reason を budget 以内に折り畳む
    (0.26.0)。

    Read の reason は ``engine.redact`` / ``engine.redact_large_file`` の
    出力そのもの (``<DATA>...</DATA>`` の 1 ブロックが reason 全体) で、
    Bash/Edit と違って周囲に他の固定行を持たない。したがって ``_fit_data_block``
    に budget をそのまま渡せばよい。

    予算超過時、以前は ``core.output._truncate`` の盲目 byte cut だけに
    頼っており、90 key の ``.env`` を Read すると ``</DATA>`` 閉じタグと
    「実値は無い」の末尾 note が key 行の途中で失われていた (Bash 側 0.23.0
    の除外案内保護と同型の欠落が、Read には全く存在しなかった)。

    ``next_action`` を付けない (空文字既定): Bash/Edit の呼出元には「Read
    tool でこの絶対パスを読めば続きが見える」が真だが、**この関数自体が
    Read の reason を組んでいる**ため、Read の中で Read を勧めるのは
    自己参照で無意味 (かつ事実として何も増えない)。

    折り畳んでも中身ゼロ (header 3 行のみ) になるほど極端なケース
    (basename が極端に長い等) では折り畳みを諦め、**入力をそのまま返す**。
    最終的に ``core.output.make_deny`` の ``_truncate`` が最終防御として
    働くので、verdict や「何かしらの reason が返る」保証は崩れない —
    変わるのは折り畳みで得られたはずの追加の情報量だけ。
    """
    if len(reason.encode("utf-8")) <= budget:
        return reason
    fitted = _fit_data_block(reason, budget)
    if not fitted:
        return reason
    return "\n".join(fitted)


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


def _exclude_hint(
    basename: str, literal_name: bool = False, relpath: str = ""
) -> str:
    """``patterns.local.txt`` への除外行追加案内を返す。

    basename が空なら一般化された hint。空でなければ ``!<basename>`` を埋め込む。

    ``relpath`` (project root 相対 path、0.24.0) が空でなければ **path 形**
    ``!<relpath>`` (承認した 1 ファイルだけを外す) を既定として案内し、
    basename 形は「同名ファイルをすべて外したい場合だけ」の明示的な選択として
    併記する。relpath が無い (root 不明 / root 配下でない / glob operand /
    VCS pathspec) ときは従来どおり basename 形だけを案内し、path 形を案内
    できない旨を添える (影響範囲の開示 ``EXCLUDE_SCOPE_WARNING`` は両形を
    説明しているので、読み手が basename 形の広さを知らずに書くことはない)。

    0.19.0: ``[project:$CLAUDE_PROJECT_DIR]`` セクション配下
    への追記を既定として案内する。0.18.0 まではヘッダー無し行 (= 全プロジェクト
    共通) だけを案内していたため、あるプロジェクトで承認した除外が他プロジェクト
    にも無条件で効く側 (0.15.0 が ``[project:]`` セクションで防ごうとした事故) に
    既定で誘導していた。絶対パスは reason に出さない方針のため、ヘッダーは
    ``$CLAUDE_PROJECT_DIR`` の変数名で示し、書き込む側が実パスに置き換える。
    """
    if basename:
        # ``literal_name`` のときだけ fnmatch のメタ文字を escape する。
        #
        # Read / Edit / Stop の basename は実ファイル名なので、``key[1].pem`` の
        # ような名前をそのまま出すとレシピが別物になる (自身にマッチせず
        # ``key1.pem`` を巻き込む)。一方 Bash の operand は
        # ``cat .env*`` のように**ユーザーが書いた glob** でありうるので、
        # そこを escape すると意図した glob 除外が壊れる。
        rule = escape_glob(basename) if literal_name else basename
        entry = f"`!{_sanitize_for_inline(rule)}`"
        # スコープ修飾は EXCLUDE_SCOPE_WARNING 側が説明するので、ここでは
        # 名前だけを示す ([project:] は rule の読込先を決めるだけで、
        # 読み込まれた後は絶対パス全部に効くため「このプロジェクト内」は誤り)。
        scope = f"`{_sanitize_for_inline(basename)}` という名前のファイル"
    else:
        entry = "除外行 (`!<basename>`)"
        scope = "同名のファイル"

    if relpath:
        # path 形は実ファイルの root 相対 path (glob ではない) なので常に
        # literal 化する。``/`` は ``escape_glob`` の対象外なのでそのまま残る。
        # root 直下のファイル (``.env``) は ``path_rule_for`` が先頭 ``/`` を
        # 付けて path 形を保つ (``!.env`` だと basename 形に化ける、Codex R1 P1)。
        path_entry = (
            f"`!{_sanitize_for_inline(escape_glob(path_rule_for(relpath)))}`"
        )
        target = f"{path_entry} (この 1 ファイルだけ)"
        alt = f"同名ファイルをすべて外したい場合だけ {entry} にします。"
    else:
        target = entry
        # 短く保つ (byte 予算。理由の詳細は docs/PATTERNS.md に任せる)
        alt = "(root を解決できないため path 形は案内できません。)" if basename else ""
    return (
        "恒久的に許可したい場合は、ユーザーの承認を得た上で "
        f"`{_LOCAL_PATTERNS_PATH}` の `{PROJECT_SECTION_HEADER_HINT}` "
        f"セクション配下に {target} を追加してください "
        f"({PROJECT_SECTION_PLACEHOLDER_NOTE})。{alt}"
        f"{EXCLUDE_SCOPE_WARNING.format(scope=scope)}"
        "承認なしに自分で追加しないこと。"
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
    # history: git 経由の参照 / 操作。0.19.0 で subcommand 別に文面を分ける
    # (show / diff / log = 閲覧、add / mv / rm / restore = 操作)。
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


# ``render_for_bash`` が minimal info を作れなかった理由 (kind) →
# (reason 行に載せるラベル, LLM が次に取れる action)。
#
# 0.15.0 以前は ``file_render`` が空のとき minimal info セクションを **黙って
# 省略** していた。情報も next action も無い deny reason を受け取ったモデルは、
# Read への切替ではなく **別コマンドでの迂回** に倒れる (2026-08-17 実観測)。
# 不在は必ず明示し、kind ごとの next action を添える。
_MINIMAL_INFO_UNAVAILABLE: dict[str, tuple[str, str]] = {
    "unresolved": (
        "operand path が hook の cwd から解決できなかった",
        "Read tool に **絶対パス** を渡してください (同じ minimal info が"
        "返ります)。同一コマンド内の先行 `cd` は hook 側の cwd に反映されない"
        "ため、相対パスのままでは解決できません。",
    ),
    "not_regular": (
        "通常ファイルではない (symlink / 特殊ファイル)",
        "symlink 先が意図した参照かを確認したうえで、Read tool に実体の"
        "絶対パスを渡してください。",
    ),
    "stat_failed": (
        "ファイル状態の確認に失敗した (権限 / IO)",
        "ファイルの存在と権限を確認してください。",
    ),
    "open_failed": (
        "安全な open に失敗した (権限 / symlink 検知)",
        "ファイルの存在と権限を確認してください。",
    ),
    "redact_failed": (
        "内容の解析に失敗した (想定外の形式)",
        "鍵名一覧は取得できません。ファイル形式をユーザーに確認してください。",
    ),
    "normalize_failed": (
        "operand path の正規化に失敗した",
        "パス文字列の異常 (NUL バイト等) を確認してください。",
    ),
    "no_operand": (
        "operand path を特定できなかった",
        "Read tool に **絶対パス** を渡してください。",
    ),
}

# 未知 kind / kind 未指定のときの汎用フォールバック。
_MINIMAL_INFO_UNAVAILABLE_DEFAULT = (
    "理由不明",
    "Read tool に **絶対パス** を渡すと同じ minimal info が得られます。",
)


# project root 基準で再解決したときのラベルと注記 (0.16.0)。
# ``cwd`` 基準では見つからなかったので、**別ディレクトリの同名ファイル**を
# 読んでいる可能性がある。候補である旨と確定手段を必ず添える。
_PROJECT_ROOT_LABEL = (
    "minimal info (Read 同等 / cwd では解決できず project root 基準で"
    "解決した候補):"
)
_PROJECT_ROOT_CAVEAT = (
    "suggestion: 上記は $CLAUDE_PROJECT_DIR (project root) 基準で解決した"
    "ファイルの情報です。同一コマンド内の先行 `cd` 等で実際の対象が別"
    "ディレクトリの同名ファイルである可能性があります。確実に対象を特定したい"
    "場合は Read tool に **絶対パス** を渡してください。"
)


def _resolved_base_line(resolved_base: str) -> str | None:
    """``resolved_base:`` 行を返す (空なら None)。

    「候補です」と書くだけでは、読み手は **実際に取り違えたのか** を確認でき
    ない。解決基準にした project root の basename を 1 要素だけ出して、自分が
    意図したディレクトリ名と突き合わせられるようにする。絶対 path は出さない
    (``matched_operand`` が相対 path を出す現行方針の範囲に収める)。
    """
    if not resolved_base:
        return None
    safe = _sanitize_for_inline(resolved_base)
    return f"resolved_base: {safe}/ ($CLAUDE_PROJECT_DIR の basename)"


def _append_minimal_info(
    lines: list[str],
    file_render: str,
    render_status: str = "",
    resolved_base: str = "",
) -> None:
    """``minimal info (Read 同等):`` ラベルと file_render の中身を追加する。

    ``file_render`` は ``redaction.file_render.render_for_bash`` が返す
    ``<DATA untrusted>`` 包装込みの文字列。``render_status`` は同関数の 3 番目の
    戻り値をそのまま渡す。

    - 成功 (``render_status == ""``): 従来どおりのラベル + 中身
    - project root 再解決 (``"project_root"``): 候補である旨のラベル + 注記
    - 失敗 (それ以外): **黙って省略せず** ``minimal info: unavailable (<理由>)``
      と kind 別 next action を出す (silent degradation 対策)
    """
    if file_render:
        if render_status == "project_root":
            lines.append(_PROJECT_ROOT_LABEL)
            base_line = _resolved_base_line(resolved_base)
            if base_line:
                lines.append(base_line)
            lines.append(file_render)
            lines.append(_PROJECT_ROOT_CAVEAT)
        else:
            lines.append("minimal info (Read 同等):")
            lines.append(file_render)
        return
    label, action = _MINIMAL_INFO_UNAVAILABLE.get(
        render_status, _MINIMAL_INFO_UNAVAILABLE_DEFAULT
    )
    lines.append(f"minimal info: unavailable ({label})")
    lines.append(f"suggestion: {action}")


def _append_project_root_caveat(
    lines: list[str],
    render_status: str,
    resolved_base: str = "",
) -> None:
    """``dotenv_info`` を直接展開する経路 (read_partial / search) 用の注記。

    それらは ``_append_minimal_info`` を通らずに鍵行を出すため、project root
    再解決だった場合の判別行 + 候補注記をここで別途付ける。
    """
    if render_status != "project_root":
        return
    base_line = _resolved_base_line(resolved_base)
    if base_line:
        lines.append(base_line)
    lines.append(_PROJECT_ROOT_CAVEAT)


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
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
    # 0.16.0: 「Read tool を使ってください」は **minimal info を出せなかった
    # ときだけ** 出す (`_append_minimal_info` の unavailable 分岐が担当)。
    # info を出せているのに Read を勧めるのは、同じ情報を取り直させるだけの
    # 往復の無駄になるため。
    _append_minimal_info(lines, file_render, render_status, resolved_base)
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_read_partial(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
        _append_project_root_caveat(lines, render_status, resolved_base)
    else:
        _append_minimal_info(lines, file_render, render_status, resolved_base)
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_search(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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

    # 0.16.0: ``pattern_keys:`` のエコー (dotenv parse なしで grep pattern を
    # 返しただけ) は実情報ゼロなので、``used_pattern_keys`` が True でも
    # file_render が空なら unavailable を明示する。
    if not file_render:
        _append_minimal_info(lines, "", render_status, resolved_base)
    elif not used_pattern_keys:
        _append_minimal_info(lines, file_render, render_status, resolved_base)
    else:
        # matched_pattern_keys 経路は minimal info を出さないので注記だけ付ける。
        _append_project_root_caveat(lines, render_status, resolved_base)

    other = _suggestion_other_keys(dotenv_info)
    if other:
        lines.append(f"suggestion: {other}")
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_mutate(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
    _append_minimal_info(lines, file_render, render_status, resolved_base)
    # 0.16.0: 「上記 minimal info を確認してください」は info を出せたときのみ。
    # 空のときに書くと存在しない情報を指す dangling reference になる。
    refer_info = (
        "鍵名・型・状態は上記 minimal info を確認してください。"
        if file_render
        else ""
    )
    lines.append(
        f"suggestion: 加工は実行できません。{refer_info}"
        " 値の置換が目的なら、対象ファイルを直接編集する代わりに別ファイルへの"
        " patch / diff 適用を検討してください。"
    )
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_load(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
    _append_minimal_info(lines, file_render, render_status, resolved_base)
    lines.append(
        "suggestion: 環境変数として読み込みたいなら direnv (`.envrc`) や"
        " dotenv-cli の利用を推奨します。"
        " 1Password CLI / pass / git-secret 経由の secret 読込でも代替できます。"
    )
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_move(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


# git の global option のうち値を **別トークン** で取るもの (subcommand 抽出時に
# 値を読み飛ばす)。``--git-dir=<path>`` のような ``=`` 結合形は ``-`` 始まりとして
# 単独で skip される。
_GIT_GLOBAL_VALUE_OPTS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
})

# git subcommand のうち「閲覧」ではなく index / 作業ツリーへの **操作** (0.19.0)。これ以外 (show / diff / log / cat-file / blame / grep 等)
# は従来通り「commit / 差分の閲覧」文面。0.18.0 までは全 subcommand が閲覧文面
# だったため ``git rm .env`` / ``git add .env`` が「閲覧しようとした」と誤った
# 意図を返していた。
_GIT_OPERATE_SUBCOMMANDS = frozenset({
    "add", "rm", "mv", "restore", "checkout", "reset", "stash", "clean",
    "update-index", "apply", "commit",
})


def _git_subcommand_of(command: str, operand: str = "") -> str:
    """command 文字列から deny 対象の ``git`` subcommand を推定する (0.19.0)。

    複合コマンド (``cd app && git add .env``) でも動くよう、``git`` トークン
    ごとに後続 window (次の ``git`` トークンまで) を見て operand を含むものを
    優先し、無ければ最初の ``git`` を採る。global option (``-C dir`` /
    ``-c k=v`` / ``--git-dir=...``) は読み飛ばす。``bash_deny`` の signature を
    変えずに済ませるため ``command`` 全体から推定する (``read_partial`` の
    ``-n N`` 抽出と同じトレードオフ)。解決できなければ空文字列。
    """
    if not command:
        return ""
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        tokens = command.split()
    first_sub = ""
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i] != "git":
            i += 1
            continue
        j = i + 1
        sub = ""
        while j < n:
            tok = tokens[j]
            if tok in _GIT_GLOBAL_VALUE_OPTS:
                j += 2
                continue
            if tok.startswith("-"):
                j += 1
                continue
            sub = tok
            break
        k = j + 1
        while k < n and tokens[k] != "git":
            k += 1
        if operand and any(
            tok == operand or tok.endswith("=" + operand) for tok in tokens[j:k]
        ):
            # ``--pathspec-from-file=.env`` のような値結合形の operand も window
            # 一致に含める (deny 判定は別経路で確定済み、ここは文面のみ)。
            return sub
        if not first_sub:
            first_sub = sub
        i = max(k, i + 1)
    return first_sub


def _git_operate_suggestion(sub: str, safe_basename: str) -> str:
    """操作系 subcommand ごとの代替案 (``suggestion:`` 行の本文)。"""
    if sub == "rm":
        return (
            f"`git rm --cached {safe_basename}` (subcommand 直書き・"
            "`--pathspec-from-file` 無し) は index からの除去のみで allow されます。"
            " 作業ツリーからも削除する `--cached` 無しの形は値を失う破壊操作、"
            " `--pathspec-from-file` は operand の中身を pathspec として読み echo"
            " するため block します。untrack 後は値を rotate してください。"
        )
    if sub in ("add", "commit", "update-index", "mv"):
        prefix = (
            "`git mv` で別名にしても履歴と index に値が残ります。"
            if sub == "mv"
            else ""
        )
        return (
            f"{prefix}機密ファイルを commit 対象に含めないでください。"
            f" `.gitignore` に {safe_basename} を追加し、既に tracked なら"
            f" `git rm --cached {safe_basename}` で untrack した上で値を"
            " rotate してください。"
        )
    return (
        f"作業ツリーの {safe_basename} を上書き / 削除する可能性がある操作です。"
        " 現在の値が失われてよいかユーザーに確認してください"
        " (バックアップが必要なら secrets manager 側へ)。tracked なら"
        f" `git rm --cached {safe_basename}` で untrack してから扱ってください。"
    )


def _bash_deny_history(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
) -> str:
    """``git`` の deny reason。subcommand で文面を分ける (0.19.0)。

    - 閲覧 (``git show HEAD:.env`` / ``git diff .env`` / ``git log -p .env`` 等):
      tracked なら漏洩済みの可能性を提示し ``git rm --cached`` + rotate を推奨。
    - 操作 (``git add`` / ``git rm`` / ``git mv`` / ``git restore`` 等、
      ``_GIT_OPERATE_SUBCOMMANDS``): 「閲覧」ではなく index / 作業ツリーへの
      操作として説明し、allow される ``git rm --cached`` 形を案内する。
    """
    basename = _basename_of(operand)
    safe_basename = _sanitize_for_inline(basename) or basename
    sub = _git_subcommand_of(command, operand)
    lines: list[str] = []
    if sub in _GIT_OPERATE_SUBCOMMANDS:
        note = (
            f"git {sub} で機密ファイル ({operand}) を index / 作業ツリーに"
            "対して操作しようとしたため block しました。"
        )
        lines.append(f"note: {note}")
        lines.extend(_common_meta_lines(first_token, operand))
        lines.append(
            f"suggestion: {_git_operate_suggestion(sub, safe_basename)}"
        )
    else:
        note = (
            f"git 経由で機密ファイル ({operand}) の commit / 差分を"
            "閲覧しようとしたため block しました。"
        )
        lines.append(f"note: {note}")
        lines.extend(_common_meta_lines(first_token, operand))
        lines.append(
            f"suggestion: この {safe_basename} が tracked になっているなら、"
            "過去 commit に値が残っており既に漏洩済みの可能性があります。"
            f" `git rm --cached {safe_basename}` で untrack 後に値を rotate"
            " してください (この形は allow されます)。"
            " untracked なら別パスから誤って参照していないか確認してください。"
        )
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_transfer(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_archive(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


def _bash_deny_generic(
    *,
    first_token: str,
    operand: str,
    command: str,
    file_render: str,
    dotenv_info: dict | None,
    grep_keys: list[str] | None,
    render_status: str,
    resolved_base: str,
    relpath: str = "",
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
    _append_minimal_info(lines, file_render, render_status, resolved_base)
    return _join_with_exclude_hint(lines, basename, relpath=relpath)


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
    render_status: str = "",
    resolved_base: str = "",
    relpath: str = "",
) -> str:
    """Bash 操作の deny reason を plain text で構築する (0.10.0 で category dispatch)。

    first_token のカテゴリで builder を切替え、コマンド意図に合った文言と
    Read 同等 minimal info / matched_pattern_keys を埋め込む。新規 keyword
    引数 ``command`` / ``file_render`` / ``dotenv_info`` / ``grep_keys`` /
    ``render_status`` を渡さない呼び出しでも動作する (旧 0.7.0〜0.9.0 互換、
    generic 相当の出力)。

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
        render_status: ``render_for_bash`` の 3 番目の戻り値 (失敗 kind)。
            ``file_render`` が空のとき ``minimal info: unavailable (<理由>)``
            の理由ラベルと next action の選択に使う。成功時は空文字。
        relpath: operand の project root 相対 path (0.24.0)。literal operand が
            root 配下に解決できたときだけ handler が渡し、除外案内を path 形
            (``!<relpath>``、1 ファイルだけ) にする。glob / VCS pathspec /
            root 外は空文字 (basename 形のみ案内)。
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
        render_status=render_status,
        resolved_base=resolved_base,
        relpath=relpath,
    )


# -- E6: Edit / Write の状況別 deny 文面 (0.20.0) --------------------------
#
# ``kind`` は書き込み先の最終要素の状態 (``core.safepath.classify`` の結果) を
# **文面の分岐**に使うためのもの。0.4.2 の ``edit_deny(kind=...)`` は
# ``<GUARDRAIL_DENY reason="...">`` 属性に載せる **機械可読 schema** の一部で、
# 後段 hook が存在せず overengineering だったため 0.7.0 で撤去した。本引数は
# その再導入ではなく、思想 2 (block 時は意図を汲んだメッセージを返す) 側の
# 拡張で、値は reason 文字列の分岐にのみ使われる。
EditDenyKind = Literal["new", "overwrite", "symlink", "special"]

# kind → ``note:`` 行の本文テンプレート (``tool_label`` / ``basename`` を埋める)。
# 全 kind が語彙ルールどおり「block しました」で終わること
# (``tests/test_messages.py::TestVocabularyConsistency`` が全 kind を fixate)。
_EDIT_DENY_NOTE: dict[str, str] = {
    "new": (
        "{tool_label}: 機密パターン一致のファイル ({basename}) を新規作成"
        "しようとしたため block しました (実値が LLM コンテキストと作業ツリーに"
        "残るのを防ぐため)。"
    ),
    # 「上書き」と書かないのは Edit と Write で書き換え方が違うため
    # (Edit = 対象を絞った置換 / Write = ファイル全体の置換)。両方に当てはまる
    # 「書き換え」にし、tool 差は ``_EDIT_OVERWRITE_TOOL_CLAUSE`` 側で言う。
    "overwrite": (
        "{tool_label}: 既存の機密ファイル ({basename}) を書き換えようとしたため "
        "block しました (意図しない値の破壊と機密流出を防ぐため)。"
    ),
    "symlink": (
        "{tool_label}: symlink 経由で機密パターン一致のファイル ({basename}) に"
        "書き込もうとしたため block しました (実体側のファイルが書き換わるため)。"
    ),
    "special": (
        "{tool_label}: 非通常ファイル (FIFO / socket / device) である "
        "{basename} への書き込みを block しました。"
    ),
}

# kind → ``suggestion:`` 行 (除外 hint の手前に置く状況別の代替案)。
# ``overwrite`` は dotenv かどうかで文面が変わるため別扱い
# (``_edit_kind_suggestion``)。``new`` は代替案が「同じキーで .env.example を
# 作る」= dotenv 前提なので ``suggestion_alt`` (new_keys 依存) 側に置き、
# ここには行を持たない。
_EDIT_DENY_SUGGESTION: dict[str, str] = {
    "symlink": (
        "symlink 先が意図した参照か確認してください。実体が複数プロジェクトで"
        "共有される場所にあるなら、コピーを作らず symlink を維持する運用を"
        "推奨します。"
    ),
    "special": (
        "FIFO / socket / device への書き込みは意図しない副作用を招きます。"
        "通常ファイルを対象にするか、パス指定の誤りが無いか確認してください。"
    ),
}

# ``overwrite`` の代替案は **tool 軸 × format 軸の 2 つ**を連結して作る。
# 手書きの文面を組み合わせ数だけ用意すると、片方の軸を足したときに漏れる。
#
# tool 軸: Edit と Write は書き換え方が違う。0.20.0 の初版は両方に
# 「ファイル全体の上書きは現在の値を失います」「全体の上書きではなく差分適用を」
# と書いていたが、**Edit は既に対象を絞った置換**なので事実と違い、かつ
# 「patch にしろ」という助言も的外れだった (PR #47 Codex P2)。
_EDIT_OVERWRITE_TOOL_CLAUSE: dict[str, str] = {
    "Write": (
        "Write はファイル全体を置き換えるため、現在の値はすべて失われます。"
    ),
    "Edit": (
        "Edit は対象を絞った置換なのでファイル全体は失われませんが、"
        "機密ファイルへの書き込みは block 固定です。"
    ),
}
# ``handle()`` の既定 ``tool_label="Edit/Write"`` のような tool 未確定の呼び出し用。
_EDIT_OVERWRITE_TOOL_CLAUSE_DEFAULT = (
    "既存の機密ファイルへの書き込みは block 固定です。"
)

# format 軸: 「既存値を保ったまま反映する方法」。tool には依存しない。
_EDIT_OVERWRITE_FORMAT_CLAUSE_DOTENV = (
    "既存値を保ったまま項目を足すなら、dotenv-cli の merge 機能で "
    "`.env.example` の差分だけを追記できます。"
)
_EDIT_OVERWRITE_FORMAT_CLAUSE_OTHER = (
    "既存値を保ったまま項目を足すなら、差分適用 (patch) での反映を"
    "検討してください。"
)

# ``suggestion_alt:`` (new_keys があるときだけ出る) の kind 別文面。
_EDIT_SUGGESTION_ALT_NEW = (
    "同じキー名で `.env.example` を作成し、値は空にしてください。"
    "実値は手動入力か 1Password CLI 等のシークレット管理ツール経由で設定します。"
)
_EDIT_SUGGESTION_ALT_DEFAULT = (
    "追加予定のキー名を `.env.example` に追記すると、"
    "差分把握がしやすくなります (値は後で個別設定)。"
)
# overwrite かつ new_keys の全件が既存キー集合に含まれる (= 純粋な値の更新で
# 新規追加が無い) ときの suggestion_alt (0.26.0)。
#
# _EDIT_SUGGESTION_ALT_DEFAULT をそのまま使うと、上の ``suggested_keys:`` に
# 既存キー (例: 既存 TOKEN の値を差し替えただけ) が並んでいるのに
# 「追加予定のキー名」と説明する自己矛盾になる (同じ reason の minimal info
# セクションで「既存キーとして表示」しているのと矛盾)。suggested_keys の
# 2 セクション化はしない (変更範囲を最小にする判断、NOTES 参照) 代わりに、
# alt 側で **上の一覧が更新であって追加ではない**ことを明示的に言い直す。
#
# **長さは既定文言と同程度に保つ** (0.26.0 隔離内レビュー P2-1)。``edit_deny``
# は tail (この文言を含む) を先に組んでから残りを minimal info の予算に回す
# ため、ここが長いと minimal info セクションが丸ごと消える
# (``minimal info: unavailable`` すら出ない)。初版は 383 byte あり、corpus
# 1,623 件中 30 件で実際に消えていた。「値の更新はユーザーに直接依頼するか
# 1Password CLI 経由で」の一文は ``_EDIT_OVERWRITE_*_CLAUSE`` 側の
# suggestion と重複するので落とす。上限は
# ``tests/test_messages.py::TestEditSuggestionAltUpdateBudget`` が固定する。
_EDIT_SUGGESTION_ALT_UPDATE = (
    "上記の suggested_keys は新規追加ではなく **既存キーの値の更新**です"
    "（`.env.example` への追記は不要）。"
)

# 上書き対象の既存ファイルを Read 同等 minimal info として載せるときのラベル。
_EDIT_EXISTING_LABEL = "minimal info (Read 同等 / 上書き対象の既存ファイル):"

# minimal info を出せなかったときの理由ラベル。ラベル文言は Bash 側の
# ``_MINIMAL_INFO_UNAVAILABLE`` と意味が同じ status を共有する (実体は
# ``render_for_bash`` を再利用しているため)。next action だけは Edit 専用に
# する: Bash 側の「Read tool に **絶対パス** を渡す」は先行 cd で cwd が
# ずれる Bash 固有の事情が前提で、file_path が最初から絶対パスで確定している
# Edit/Write では的外れ。
#
# 0.26.0 より前は理由を問わず単一ラベル + 単一 next action
# (``_EDIT_EXISTING_NEXT_ACTION``) を返していたが、これは render 失敗時には
# 二重に事実と違った: (1) Read も ``ask_or_deny`` (非 bypass では ask) に
# 倒れるので verdict は "block" ではない、(2) render 失敗時の Read は同じ
# 理由で **1 行も** minimal info を返さない。budget 分岐
# (``_EDIT_EXISTING_UNAVAILABLE_BUDGET``、情報自体は取得できている) だけは
# Read 誘導が正しいので、次アクションを 2 系統に分ける。
_EDIT_EXISTING_UNAVAILABLE_RENDER = "既存ファイルの読み取り / 解析に失敗"
_EDIT_EXISTING_UNAVAILABLE_BUDGET = "reason の byte 予算に収まらないため省略"

# status 別の理由ラベル (0.26.0)。keys は ``redaction.file_render.render_for_bash``
# / ``core.messages._MINIMAL_INFO_UNAVAILABLE`` と同じ slug。未知 / 空文字は
# ``_EDIT_EXISTING_UNAVAILABLE_RENDER`` (旧来の汎用ラベル) にフォールバックする。
_EDIT_EXISTING_UNAVAILABLE_LABELS: dict[str, str] = {
    "unresolved": "既存ファイルが見つからない (削除 / 移動された可能性)",
    "not_regular": "通常ファイルではない (symlink / 特殊ファイル)",
    "stat_failed": "ファイル状態の確認に失敗した (権限 / IO)",
    "open_failed": "安全な open に失敗した (権限 / symlink 検知)",
    "redact_failed": "内容の解析に失敗した (想定外の形式)",
    "normalize_failed": "パスの正規化に失敗した",
}

_EDIT_EXISTING_NEXT_ACTION = (
    "既存のキー構成を確認したい場合は、同じ **絶対パス** を Read tool に"
    "渡してください (Read も block されますが同じ minimal info が返ります)。"
)
# render 失敗 (権限 / IO / 形式不明等) 時の next action。budget 分岐と違い、
# Read への切替を勧めない — 同じファイルを同じ理由で読むので情報は増えない。
_EDIT_EXISTING_RENDER_FAILED_ACTION = (
    "同じ絶対パスを Read tool で読んでも同じ理由で失敗し、情報は返りません。"
    "ファイルの権限・実体を確認してください。"
)

# ``redaction.engine.build_reason`` が組む ``<DATA>`` 包装の閉じタグと
# header 行数 (``<DATA ...>`` / ``NOTE: ...`` / ``file: ...``)。
_DATA_CLOSING_TAG = "</DATA>"
_DATA_HEADER_LINES = 3


def _line_cost(line: str) -> int:
    """``"\\n".join`` に 1 行足したときの増分 byte 数 (改行 1 byte 込み)。"""
    return len(line.encode("utf-8")) + 1


# omit marker に付ける next action の英語文言 (0.26.0)。DATA block の
# 中身は英語で統一されている (``build_reason`` / ``format_dotenv`` 等) ため
# ここも英語にする。Read tool は「同じ絶対パスなら Bash/Edit より広い予算
# (exclude hint 等の固定行を持たない) で折り畳まれる」ため、Bash/Edit の
# 呼出元から見ると実際に情報が増える提案になる。Read 自身の呼出元
# (``fit_read_reason``) だけは渡さない — 自己参照 (Read の中で Read を
# 勧める) になり事実として無意味なため。
_OMIT_ACTION_USE_READ = "use Read tool with the absolute path for the rest"

# 省略マーカーの単位ラベル (0.26.0)。既定は「行」。keys-only scan のブロックは
# 1 行 = 1 鍵なので「鍵」と言い換える — ``... (2 more lines)`` では「鍵名を
# 何個落としたか」が読み手に伝わらないため。
_OMIT_UNIT_LINES = "lines"
_OMIT_UNIT_KEYS = "keys"

# ``redaction.keyonly_scan.KEYONLY_SCAN_MARKER`` と同じ文字列。``core`` から
# ``redaction`` を import すると依存が逆流する (``redaction.file_render`` が
# ``core.safepath`` を使っている) ため、実体は両側に置き
# ``tests/test_messages.py`` の assumption test で突合する。
_KEYONLY_SCAN_MARKER = "keys-only scan"


def _omit_marker(
    n: int, next_action: str = "", unit: str = _OMIT_UNIT_LINES
) -> str:
    suffix = f"; {next_action}" if next_action else ""
    return f"  ... ({n} more {unit}{suffix})"


def _omit_unit_for(block: str) -> str:
    """``<DATA>`` ブロックの ``format:`` 行から省略単位ラベルを決める。

    ``build_reason`` の header 3 行の直後が本文の 1 行目 (``format:``) である
    ことに依存する (``_DATA_HEADER_LINES`` と同じ前提。
    ``TestDataBlockAssumptions`` が生成側と突合している)。
    """
    for line in block.split("\n", _DATA_HEADER_LINES + 1)[
        : _DATA_HEADER_LINES + 1
    ]:
        if line.startswith("format:") and _KEYONLY_SCAN_MARKER in line:
            return _OMIT_UNIT_KEYS
    return _OMIT_UNIT_LINES


def _fit_data_block(block: str, budget: int, next_action: str = "") -> list[str]:
    """``<DATA>`` 包装済み minimal info を ``budget`` byte 以内の行列に収める。

    先頭から行を採用し、入り切らない分は ``  ... (N more lines)`` に畳む。
    閉じタグ ``</DATA>`` は必ず残す (包装が壊れると ``escape_data_tag`` の
    外殻破壊防御が意味を失うため)。

    0.26.0 から、閉じタグの直前が per-format の ``note:`` 行 (「実値は
    無い」等の免責事項。``format_dotenv`` / ``format_jsonlike`` /
    ``format_opaque`` は全て末尾をこの規約で終える) なら、それも閉じタグと
    同格の固定 tail として保護を試みる。ただし note を保護すると header 3 行
    しか入らなくなる (= それまで見えていた key 行が 0 行に後退する) 場合は、
    **note を諦めて閉じタグだけを保護する従来動作にフォールバック**する。
    情報を今までより減らしてしまっては本末転倒なため、2 段構えにしている。

    Args:
        block: ``redaction.file_render.render_for_bash`` の 1 番目の戻り値。
        budget: この block に使ってよい byte 数 (各行の改行 1 byte を含む)。
        next_action: 省略マーカーに付ける追加の action (``_omit_marker`` 参照)。
            空文字なら付けない。

    Returns:
        採用した行のリスト。1 行も採用できなければ空リスト。
    """
    unit = _omit_unit_for(block)
    fitted = _fit_data_block_core(
        block, budget, protect_note=True, next_action=next_action, unit=unit,
    )
    if fitted:
        return fitted
    return _fit_data_block_core(
        block, budget, protect_note=False, next_action=next_action, unit=unit,
    )


def _fit_data_block_core(
    block: str,
    budget: int,
    *,
    protect_note: bool,
    next_action: str = "",
    unit: str = _OMIT_UNIT_LINES,
) -> list[str]:
    """``_fit_data_block`` の実装本体 (note 保護の有無を切替可能にしたもの)。"""
    lines = block.split("\n")
    closing: list[str] = []
    body = lines
    if lines and lines[-1] == _DATA_CLOSING_TAG:
        closing = [lines[-1]]
        body = lines[:-1]
        if protect_note and body and body[-1].startswith("note:"):
            closing = [body[-1]] + closing
            body = body[:-1]
    closing_cost = sum(_line_cost(x) for x in closing)
    # 省略マーカーの上限幅 (件数が最大のとき = 最長) で予約する。実際に出る
    # マーカーはこれ以下なので、予約しておけば予算超過は起きない。
    marker_reserve = _line_cost(_omit_marker(len(body), next_action, unit))

    kept: list[str] = []
    used = 0
    for i, line in enumerate(body):
        cost = _line_cost(line)
        has_rest = i < len(body) - 1
        need = cost + closing_cost + (marker_reserve if has_rest else 0)
        if used + need > budget:
            break
        used += cost
        kept.append(line)

    if len(kept) <= _DATA_HEADER_LINES:
        return []
    omitted = len(body) - len(kept)
    if omitted:
        kept.append(_omit_marker(omitted, next_action, unit))
    return kept + closing


def _edit_kind_suggestion(kind: str, is_dotenv: bool, tool_label: str) -> str:
    """kind 別 ``suggestion:`` 行の本文を返す (無ければ空文字)。

    ``overwrite`` だけ **tool 軸 × format 軸**の連結。それ以外の kind は tool に
    依存しない (新規作成 / symlink / 特殊ファイルの事情は Edit と Write で同じ)。
    """
    if kind != "overwrite":
        return _EDIT_DENY_SUGGESTION.get(kind, "")
    tool_clause = _EDIT_OVERWRITE_TOOL_CLAUSE.get(
        tool_label, _EDIT_OVERWRITE_TOOL_CLAUSE_DEFAULT
    )
    format_clause = (
        _EDIT_OVERWRITE_FORMAT_CLAUSE_DOTENV
        if is_dotenv
        else _EDIT_OVERWRITE_FORMAT_CLAUSE_OTHER
    )
    return f"{tool_clause}{format_clause}"


def _edit_existing_info_lines(
    existing_render: str,
    budget: int,
    status: str = "",
) -> list[str]:
    """``overwrite`` の minimal info セクションを予算内で組む。

    予算に収まらないときは **黙って省略せず** ``minimal info: unavailable``
    + next action の 2 行に降りる (0.16.0 の silent degradation 対策と同じ方針)。
    その 2 行すら入らない場合だけセクション全体を落とす — 末尾の除外案内
    (``suggestion: ... patterns.local.txt ...``) を残す方を優先するため。

    ``status`` (0.26.0): ``existing_render`` が空 (= render 失敗) の
    ときだけ効く。``handlers.edit_handler._render_existing`` が転送する
    ``render_for_bash`` の失敗 kind で、理由ラベルを status 別にし、
    next action を「render 失敗」と「budget 超過 (情報自体は取得できている)」の
    2 系統に分ける。旧実装は理由を問わず単一ラベル + Read 誘導の next action
    だった (render 失敗時は Read も同じ理由で必ず失敗するため誤誘導だった)。
    """
    if existing_render:
        fitted = _fit_data_block(
            existing_render, budget - _line_cost(_EDIT_EXISTING_LABEL),
            next_action=_OMIT_ACTION_USE_READ,
        )
        if fitted:
            return [_EDIT_EXISTING_LABEL] + fitted
        fallback = [
            f"minimal info: unavailable ({_EDIT_EXISTING_UNAVAILABLE_BUDGET})",
            f"suggestion: {_EDIT_EXISTING_NEXT_ACTION}",
        ]
    else:
        label = _EDIT_EXISTING_UNAVAILABLE_LABELS.get(
            status, _EDIT_EXISTING_UNAVAILABLE_RENDER
        )
        fallback = [
            f"minimal info: unavailable ({label})",
            f"suggestion: {_EDIT_EXISTING_RENDER_FAILED_ACTION}",
        ]

    if sum(_line_cost(x) for x in fallback) > budget:
        return []
    return fallback


def edit_deny(
    tool_label: str,
    basename: str,
    new_keys: list[str] | None = None,
    extra_note: str = "",
    *,
    kind: EditDenyKind,
    is_dotenv: bool = False,
    existing_render: str = "",
    existing_render_status: str = "",
    existing_keys: frozenset[str] = frozenset(),
    max_suggested_keys: int = 30,
    relpath: str = "",
) -> str:
    """Edit / Write の deny reason を plain text で構築する (0.20.0 で kind 分岐)。

    行の並びは Bash 側 builder と揃える —
    ``note:`` → ``basename:`` → minimal info → ``suggested_keys:`` →
    ``extra_note:`` → 状況別 ``suggestion:`` → 除外案内 ``suggestion:``。

    ### 予算 (``core.output.MAX_REASON_BYTES``) の扱い

    minimal info セクション以外を先に組んでから残り byte を計算し、その範囲に
    収まる行数だけ minimal info を載せる。したがって **この関数が minimal info
    を足したせいで末尾の除外案内が truncate されることはない**
    (``suggested_keys`` だけで予算を使い切る極端な入力では、E6 以前と同様に
    ``core.output._truncate`` が最終防御として働く)。

    ### 文面を決める軸は 2 つ (kind と tool)

    ``kind`` (書き込み先の状態) と ``tool_label`` (Edit か Write か) は **直交**
    する。``overwrite`` の代替案だけが両方に依存し、tool 軸の clause と format
    軸の clause を連結して作る (``_edit_kind_suggestion``)。Edit は対象を絞った
    置換、Write はファイル全体の置換なので、同じ ``overwrite`` でも
    「現在の値がすべて失われる」かどうかが変わるため。

    Args:
        tool_label: ``Edit`` / ``Write`` のラベル。``overwrite`` の代替案の
            tool 軸 clause の選択にも使う (未知の値は tool 中立の clause)。
        basename: 書き込み先ファイルの basename。``basename:`` 行と除外 hint で
            ``!<basename>`` に展開される。
        new_keys: dotenv parse で抽出された新規キー名リスト (順序維持)。
            非 dotenv では空リストか None を渡す。``overwrite`` かつ全件が
            ``existing_keys`` に含まれる場合は「更新」文面に切り替わる
            (0.26.0)。
        extra_note: ``extra_note:`` 行に入れる補足。kind で表現しきれない文脈を
            呼出側が足すための拡張点 (0.20.0 時点で handler は使わない)。
        kind: 書き込み先の状態 (``new`` / ``overwrite`` / ``symlink`` /
            ``special``)。文面の分岐にのみ使い、判定 (deny/ask/allow) には
            影響しない。**必須キーワード** — 既定値を置くと呼び忘れたときに
            「新規作成です」と誤った説明を返してしまうため。
        is_dotenv: 対象 basename が dotenv 系か。``overwrite`` の代替案を
            dotenv-cli merge にするかの判定に使う。
        existing_render: ``kind == "overwrite"`` のとき、上書き対象の既存
            ファイルを ``redaction.file_render.render_for_bash`` に通した
            ``<DATA>`` 包装文字列。空なら unavailable 行に降りる。
        existing_render_status: ``existing_render`` が空のときの失敗理由
            (``render_for_bash`` の status slug、0.26.0)。unavailable
            行の理由ラベルと next action を status 別に分岐するために使う。
            ``existing_render`` が非空なら無視される。
        existing_keys: ``kind == "overwrite"`` のとき、上書き対象ファイルの
            既存キー名集合 (dotenv のみ、0.26.0)。``new_keys`` の
            **全件**がこの集合に含まれる場合、``suggested_keys`` は新規追加
            ではなく値の更新だと分かるので ``suggestion_alt`` の文面を
            切り替える。取得できない (非 dotenv / render 失敗) ときは空集合を
            渡す — 判定できないので従来の「追加予定」文面のままにする
            (誤って「更新」と言い切らない安全側)。
        max_suggested_keys: ``new_keys`` の上限 (3KB 制約のため切り詰める)。
        relpath: 書き込み先の project root 相対 path (0.24.0)。root 配下に
            解決できたときだけ handler が渡し、除外案内を path 形
            (``!<relpath>``、1 ファイルだけ) にする。空なら basename 形のみ。
    """
    note = _EDIT_DENY_NOTE[kind].format(
        tool_label=tool_label, basename=basename
    )

    head: list[str] = [f"note: {note}", f"basename: {basename}"]
    tail: list[str] = []

    if new_keys:
        shown = new_keys[:max_suggested_keys]
        remaining = len(new_keys) - len(shown)
        tail.append("suggested_keys:")
        for k in shown:
            tail.append(f"  {k}=")
        if remaining > 0:
            tail.append(f"  ... ({remaining} more)")
        if kind == "new":
            alt = _EDIT_SUGGESTION_ALT_NEW
        elif (
            kind == "overwrite"
            and existing_keys
            and set(new_keys) <= existing_keys
        ):
            # 全件が既存キー = 純粋な値の更新で新規追加が無い。
            # 部分一致 (新規キーと既存キーが混在) は「追加予定」側の説明が
            # 少なくとも新規分には正しいままなので、従来文面を維持する。
            alt = _EDIT_SUGGESTION_ALT_UPDATE
        else:
            alt = _EDIT_SUGGESTION_ALT_DEFAULT
        tail.append(f"suggestion_alt: {alt}")

    if extra_note:
        tail.append(f"extra_note: {extra_note}")

    kind_suggestion = _edit_kind_suggestion(kind, is_dotenv, tool_label)
    if kind_suggestion:
        tail.append(f"suggestion: {kind_suggestion}")

    # 除外案内は ``_join_with_exclude_hint`` が最後に付ける (Bash 経路と同じ)。
    #
    # 0.23.0 の初版は tail に直接 append していたが、``suggested_keys`` が
    # 予算計算に入っていなかったため、書き込む content にキーが多いと
    # **レシピ (`!.env`) だけ見えて警告と「承認なしに追加しない」が切れる**
    # 状態になっていた (実測: 57 文字級のキー 30 本で発生)。
    # 可変長側 (suggested_keys / existing info) を削って案内の場所を確保する。
    hint_len = len(
        f"\nsuggestion: {_exclude_hint(basename, True, relpath)}".encode("utf-8")
    )

    info: list[str] = []
    if kind == "overwrite":
        used = len("\n".join(head + tail).encode("utf-8")) + hint_len
        info = _edit_existing_info_lines(
            existing_render, MAX_REASON_BYTES - used, existing_render_status,
        )

    return _join_with_exclude_hint(
        head + info + tail, basename, literal_name=True, relpath=relpath
    )


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
    "segment_too_large",
    "normalize_failed",
    "program_dynamic",
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
    elif kind == "segment_too_large":
        head = (
            "Bash セグメントが静的解析の長さ上限を超えています "
            "(巨大な引数やペイロードを 1 コマンドに埋め込んだ形)。"
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
    elif kind == "program_dynamic":
        what = detail or "?"
        head = (
            f"プログラム / 設定文字列にコマンド実行 / ファイル入出力の構文 "
            f"({what}) が含まれています (awk・sed のプログラム、git の shell alias "
            "等)。シングルクォートは Bash の展開を止めるだけで、呼び出される"
            "プログラムには解釈されます。"
        )
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
