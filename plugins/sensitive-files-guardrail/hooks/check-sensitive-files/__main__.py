#!/usr/bin/env python3
"""Stop hook: 機密ファイルパターンが残っていないか検出する。

tracked / untracked の両方を検査し、``.gitignore`` 済みでも **tracked は block**
(``git rm --cached`` が必要なため)。同一ターン内の 2 回目以降の Stop は
``stop_hook_active=true`` でスキップするため、**block が見えたら必ず対応する**
必要がある。

0.19.0 (内部バックログ): **session 単位の once-only 化**。報告済みの
(status, path) 集合を ``stop_ack`` で HOME 側に記録し、次の Stop で集合が
増えていなければ exit 0 にする。「意図的に管理対象とする」と承認された tracked
ファイル (Next.js 慣例の ``.env`` / committed CA 証明書 / direnv の ``.envrc``
等) で毎ターン同じ block が出続けるのを止めるため。新しい機密ファイルが増えた
とき (または untracked → tracked のように status が変わったとき) だけ再 block
する。``session_id`` が無い / 不正な hook input では従来通り毎回 block する。

block reason には ``patterns.local.txt`` への恒久除外レシピ
(``[project:$CLAUDE_PROJECT_DIR]`` + ``!<root 相対パス>``) を載せる (0.19.0、read 側
deny reason の hint と同じ ``_shared.patterns.exclude_recipe_lines``)。0.24.0 から
既定は **path 形** (承認した 1 ファイルだけを外す) で、basename 形 (同名すべて)
は明示的な選択として併記する。project root を解決できない (``resolve_project_root``
が None / cwd が root 配下でない) ときだけ basename 形のみ。絶対パスは
reason に出さない。

patterns.txt の読み取りに失敗した場合は stderr warning のみ出して exit 0
(fail-open)。read 側 hook と異なり、Stop は Claude の応答を止めるため
fail-closed にしない。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_pkg_dir = str(Path(__file__).resolve().parent)
_hooks_dir = str(Path(__file__).resolve().parent.parent)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

from _shared.streams import write_stdout  # noqa: E402
from _shared.patterns import (  # noqa: E402
    LOCAL_PATTERNS_DISPLAY_PATH,
    PROJECT_SECTION_PLACEHOLDER_NOTE,
    EXCLUDE_SCOPE_WARNING,
    exclude_recipe_lines,
    path_rule_for,
    resolve_project_root,
)
from checker import (  # noqa: E402
    find_sensitive_files,
    in_submodule,
    load_patterns,
    repo_context,
    root_offset,
    submodule_paths,
)
from stop_ack import (  # noqa: E402
    digest_entries,
    load_acked,
    sanitize_session_id,
    save_acked,
)


def _warn_stop_ack(detail: str) -> None:
    """stop_ack の state 読書き失敗を stderr に 1 行記録する (0.19.0)。

    判定は「状態なし」= 従来通り block に倒れるので hook の結果は変わらない。
    detail は ``load:<ExcName>`` / ``save:<ExcName>`` の固定形 (パスを含まない)。
    """
    sys.stderr.write(f"[check-sensitive-files] stop_ack_unavailable: {detail}\n")


def _serialize(reason: str) -> str:
    """stdout に出す JSON を組み立てる。

    予算計算 (``_json_chars`` / ``_WRAPPER_CHARS``) と実際の出力で **必ず同じ
    直列化** を使うための 1 箇所化 (``ensure_ascii`` を片方だけ変えると予算が
    静かに外れるため)。
    """
    return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)


def _json_chars(s: str) -> int:
    """``s`` を JSON 文字列として直列化したときの文字数 (両端の引用符を除く)。

    ``\\`` / ``"`` / 制御文字 (改行を含む) は 2 文字に、それ以外の制御文字は
    ``\\uXXXX`` の 6 文字にエスケープされる。``ensure_ascii=False`` なので
    非 ASCII は 1 文字のまま。手計算せず ``json.dumps`` に数えさせることで、
    エスケープ規則の取りこぼしを構造的に防ぐ。
    """
    return len(json.dumps(s, ensure_ascii=False)) - 2


# 空 reason での直列化長 = wrapper 自体の文字数。reason を囲む 2 つの引用符を
# 含むので、引用符を除いた ``_json_chars`` の値とちょうど足し合わさる。
_WRAPPER_CHARS = len(_serialize(""))

# stdout に出る JSON 全体の**文字数**予算 (内部バックログ + 外部レビュー R1)。
# 公式 hooks reference (Hook input and output / JSON output) は
# `additionalContext` / `systemMessage` / 素の stdout の 3 つを名指しして
# 「10,000 文字が上限、超過時はファイルに退避され preview + ファイルパスに
# 置換される」と明記するが、Stop hook の `reason` (`permissionDecisionReason`
# 相当) はこの列挙に含まれておらず、同じ上限が適用されるかどうかは docs から
# 確定できない。名指しされていない以上、保守的にこの値を安全マージンとして
# 採用した (適用されなくても外れるだけで、適用されるなら安全側に倒れる)。
#
# **単位は「直列化後の文字数」** (byte ではない)。0.27.0 当初は reason の生
# UTF-8 byte 数で予算化していたが、`json.dumps` のエスケープを無視していた:
# `\` / `"` / 制御文字は 1 文字が 2 文字に膨らむため、これらを多く含む POSIX
# 有効ファイル名では「reason 9,253 byte なのに stdout JSON は 13,088 文字」と
# いう逆転が起き 10,000 文字の枠を破っていた (実測)。逆に非 ASCII は
# `ensure_ascii=False` なので 1 文字のままだが 3 byte あり、byte 予算では
# 過大請求 (= 必要以上にファイル列挙を畳む) になっていた。どちらも直列化後の
# 文字数で計れば正しく揃う。
#
# 上限は 10,000 文字だが 9,216 (9 * 1024) を予算にして 784 文字の余裕を残す:
# 出力に付ける末尾改行など計上していない端数があるため。折り畳みマーカーは
# 0.27.0 の R2 修正で**予算の内側**に移した (以前は外側で必ず追記していたため
# マーカー分だけ予算を超えうる)。
#
# 裏が取れていない前提 (開示): docs の「10,000 characters」を文字数どおりに
# 解釈している。ハーネス側が実は byte で数えている可能性は docs からは否定
# できず、その場合 reason が非 ASCII ばかりだと UTF-8 で最大 3 倍 (約 27KB)
# になりうる。一次情報 (docs の文言) を採用し、実測できるまでこの解釈で運用する。
#
# core/output.py (read 側 hook) の MAX_REASON_BYTES + _truncate と同じ発想だが、
# あちらは末尾を単純 truncate するのに対し、こちらは**静的な案内文を必ず残し、
# path 由来の可変長部分 (ファイル列挙・恒久除外レシピの `!` 行・basename 形の
# 併記・submodule ディレクトリ一覧) だけを畳む** (静的案内が末尾にあり、素の
# truncate だと真っ先に失われるため)。
#
# 0.27.0 当初は「AskUserQuestion 案内 + 恒久除外レシピ」をまとめて固定 tail と
# 扱い予算の外に置いていたが、**レシピ行の中身は path 由来で可変長**だった
# (外部レビュー R2 P2-B): 3.5KB のネストした path 3 本で、列挙予算を 0 に
# 絞っても直列化後 11,580 文字に達し 10,000 文字の枠を破る (実測)。現在は
# 静的な案内文だけが予算の外側にあり、それらは定数から組み立てるので入力に
# 依らず固定サイズ。これにより「直列化後の出力は常に MAX_OUTPUT_CHARS 以下」
# が**入力に依らず**成立する (テスト: 件数 × path 長の格子で総当たり)。
MAX_OUTPUT_CHARS = 9 * 1024

# ファイル列挙が予算を超えて畳まれたときに追記する行のテンプレート。何件省略
# したかは必ず示す (黙って消すと「検査は実行されたが報告が消えた」ことに
# 気づけないため)。マーカー自体のコストも予算の内側で確保する (R2 P2-B)。
_OMITTED_FILES_TEMPLATE = "  ... ({n} more files; see git status)"

# 恒久除外レシピの ``!`` 行が予算に収まらないときの折り畳みマーカー。
# ``exclude_recipe_lines`` が limit=20 で付ける ``... (N more)`` と同じ形。
# 件数は**重複除去後の全除外対象数から表示できた数を引いた 1 本の値**にする
# (limit 由来のマーカーと予算由来のマーカーが別々の件数を主張すると、読み手が
# どちらの数も信用できなくなる)。
_OMITTED_RECIPE_TEMPLATE = "  ... ({n} more)"

# レシピ行 1 本だけで予算を超えるほど path が長いときの置換行。ファイル列挙側に
# 同種の行を置かないのは、あちらの ``... (N more files)`` が既に「何件出せな
# かったか」を正しく伝えており、長さの内訳まで足す価値が無いため。
_LONG_RECIPE_TEMPLATE = "  ... (先頭の除外行は path が長すぎるため省略: {n} 文字)"

# submodule 案内に載せるディレクトリ一覧の文字数上限。件数 (10 件) だけを
# 制限しても 1 本の path 長は無制限なので、この一覧を含む案内文が入力次第で
# 膨らみ「静的部分は固定サイズ」という前提が崩れる (R2 P2-B と同型)。文字数
# でも切ることで静的部分を実際に固定サイズに保つ。
_SUBMODULE_DIRS_MAX_CHARS = 400

# 「予算を気にせず素のまま組み立てたときの長さ」を測るための実質無限大。
# ``MAX_OUTPUT_CHARS`` より確実に大きければよい (畳み判定を通さないための番兵)。
_UNBOUNDED = 1 << 30


def _distinct(names: list[str]) -> list[str]:
    """``exclude_recipe_lines`` と**同じ規則**で重複除去した一覧を返す。

    出現順を保ち、空文字列を捨てる。折り畳みマーカーの件数を数えるために使う
    ので、あちらの dedupe 規則とずれると表示件数が食い違う。
    """
    return list(dict.fromkeys(n for n in names if n))


def _fit_lines(lines: list[str], budget: int) -> tuple[list[str], int]:
    """``lines`` を先頭から ``budget`` (文字) に収まる分だけ返す。

    戻り値は ``(収まった行, 消費した文字数)``。1 行あたりのコストは
    **JSON 直列化後の文字数 + 2** — ``"\\n".join`` の区切り文字 1 個が JSON では
    ``\\`` + ``n`` の 2 文字になるため。行本体のエスケープ量は ``_json_chars``
    が ``json.dumps`` に数えさせる (``\\`` や ``"`` を含むファイル名は生 byte 数
    の約 2 倍を消費する)。
    """
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = _json_chars(line) + 2
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return kept, used


def _enumerate_with_budget(
    paths: list[str], budget: int
) -> tuple[list[str], int, int]:
    """``  - path`` 形式の列挙行を ``budget`` (文字) の範囲で組み立てる。

    戻り値は ``(組み立てた行, 表示できなかった件数, 残り文字予算)``。残り予算は
    呼出側が次のリスト (untracked) と共有するために返す。

    省略が発生する場合は ``_OMITTED_FILES_TEMPLATE`` の行も**予算の内側**で
    確保してから詰め直す (R2 P2-B。以前はこの行を予算の外で追記していたため、
    直列化後の出力がマーカー分だけ予算を超えることがあった)。確保するのは
    最悪ケース (``n=len(paths)``) の桁数なので、実際のマーカーが枠を食み出す
    ことはない。
    """
    lines = [f"  - {p}" for p in paths]
    kept, used = _fit_lines(lines, budget)
    if len(kept) == len(lines):
        return kept, 0, budget - used
    reserve = _json_chars(_OMITTED_FILES_TEMPLATE.format(n=len(paths))) + 2
    kept, used = _fit_lines(lines, max(0, budget - reserve))
    return kept, len(paths) - len(kept), max(0, budget - used - reserve)


def _recipe_with_budget(
    rules: list[str], total: int, budget: int
) -> tuple[list[str], int]:
    """恒久除外レシピの ``!`` 行を ``budget`` (文字) に収める (R2 P2-B)。

    ``rules`` は表示候補 (インデント済み。``exclude_recipe_lines`` が limit=20 で
    既に絞ったもの)。``total`` は**重複除去後の全除外対象数**で、折り畳み
    マーカーの件数はこの値から数える。戻り値は ``(出力行, 消費した文字数)``。

    ``[project:...]`` ヘッダーと案内文はここでは扱わない (静的側で必ず残す) —
    ヘッダーを畳むと生き残った ``!`` 行の追記先セクションが分からなくなり、
    ユーザーが別セクションに貼って除外が効かない事故になるため。

    レシピ行が 1 本も入らない (= 先頭の path だけで予算を超える) ときは、
    その行を ``_LONG_RECIPE_TEMPLATE`` の 1 行に置換して長さを示す。
    """
    # 折り畳みが起きうるときは最悪ケース (n=total) のマーカー分を先に確保する。
    # 実際の件数の桁数はこれ以下なので、マーカーが枠を食み出さない。
    reserve = _json_chars(_OMITTED_RECIPE_TEMPLATE.format(n=total)) + 2
    kept, used = _fit_lines(rules, budget)
    if len(kept) == len(rules) and total == len(rules):
        return kept, used
    kept, used = _fit_lines(rules, max(0, budget - reserve))
    if not kept and rules:
        long_line = _LONG_RECIPE_TEMPLATE.format(n=_json_chars(rules[0]))
        long_cost = _json_chars(long_line) + 2
        if long_cost <= max(0, budget - reserve):
            return (
                [long_line, _OMITTED_RECIPE_TEMPLATE.format(n=total)],
                long_cost + reserve,
            )
        return [_OMITTED_RECIPE_TEMPLATE.format(n=total)], reserve
    marker = _OMITTED_RECIPE_TEMPLATE.format(n=total - len(kept))
    return [*kept, marker], used + reserve


def _fit_alts(rules: list[str], budget: int) -> list[str]:
    """basename 形の項目 (```!a```) を ``budget`` に収まる分だけ返す。

    区切りは ``" / "`` の 3 文字 (2 件目以降)。行ではなく 1 行の**中身**なので、
    ``_fit_lines`` のような改行分 (+2) は乗らない。
    """
    items: list[str] = []
    used = 0
    for rule in rules:
        item = f"`{rule}`"
        cost = _json_chars(item) + (3 if items else 0)
        if used + cost > budget:
            break
        items.append(item)
        used += cost
    return items


def _alts_with_budget(rules: list[str], total: int, budget: int) -> str:
    """basename 形の併記文字列 (```!a` / `!b```) を ``budget`` に収める。

    ``rules`` は ``exclude_recipe_lines(basenames)`` の ``!`` 行 (limit=20 で
    絞り済み)、``total`` は重複除去後の全 basename 数。前置き文
    (「同名ファイルをすべて外したい場合だけ〜」) は静的側にあるため、1 件も
    入らないときも空文字列は返さず ``... (N more)`` を返す (行が尻切れに
    なるのを防ぐ)。

    ``_enumerate_with_budget`` / ``_recipe_with_budget`` と同じ二段構え:
    まず素の予算で詰めてみて、**全件入るならマーカー分を確保しない**。
    無条件に確保すると、全件収まる入力でも枠が狭まって畳まれる (= 予算導入
    前より表示が減る) 退行になる。実測: 200 文字の basename 20 件で旧実装が
    全件出していた入力が 0 件に潰れた。
    """
    items = _fit_alts(rules, budget)
    if len(items) == total:
        return " / ".join(items)
    reserve = _json_chars(f"... ({total} more)") + 3  # マーカー + " / " 区切り
    items = _fit_alts(rules, max(0, budget - reserve))
    tail = f"... ({total - len(items)} more)"
    return " / ".join([*items, tail]) if items else tail


def _submodule_dirs_display(dirs: list[str]) -> str:
    """submodule ディレクトリ一覧の表示文字列 (件数 10 + 文字数の二重上限)。

    件数だけを絞っても 1 本の path 長は無制限なので、この一覧を含む案内文が
    静的部分として無制限に伸びうる (R2 P2-B と同型)。``_SUBMODULE_DIRS_MAX_CHARS``
    で直列化後の文字数も切り、静的部分を実際に固定サイズに保つ。
    """
    shown: list[str] = []
    used = 0
    for d in dirs[:10]:
        item = f"`{d}`"
        cost = _json_chars(item) + (2 if shown else 0)  # ", " 区切り
        if used + cost > _SUBMODULE_DIRS_MAX_CHARS:
            break
        shown.append(item)
        used += cost
    omitted = len(dirs) - len(shown)
    if not shown:
        return f"... ({omitted} more)"
    display = ", ".join(shown)
    if omitted:
        display += f" ... ({omitted} more)"
    return display


def _recipe_names(paths: list[str], root_offset_: str | None) -> list[str] | None:
    """レシピの path 形に使う root 相対 path 一覧を返す (0.24.0)。

    ``paths`` は ``git ls-files`` の cwd 相対 path。``root_offset_`` (cwd の root
    からの相対 prefix、``checker.root_offset``) を前置して root 相対にする。
    None (root 不明 / cwd が root 配下でない) なら path 形は組み立てられない
    ので None を返し、呼出側は basename 形だけを載せる。

    root 直下のファイル (``.env``) は ``/`` を含まないので ``path_rule_for`` が
    先頭 ``/`` を付ける (``!.env`` のままだと basename 形 = 同名すべてに化けて、
    「1 ファイルだけ」の案内と矛盾する、Codex R1 P1)。
    """
    if root_offset_ is None:
        return None
    if not root_offset_:
        return [path_rule_for(p) for p in paths]
    return [path_rule_for(f"{root_offset_}/{p}") for p in paths]


def _build_reason(
    tracked: list[str],
    untracked: list[str],
    session_scoped: bool,
    root_offset_: str | None = None,
    submodule_by_path: dict[str, str] | None = None,
) -> str:
    """block reason (LLM 向け plain text) を組み立てる。

    tracked / untracked を別セクションで列挙し、AskUserQuestion の選択肢と
    恒久除外レシピ (``[project:$CLAUDE_PROJECT_DIR]`` + ``!<root 相対パス>``) を
    添える。絶対パスは出さない (ヘッダーは環境変数名で示す)。``session_scoped``
    なら「このセッションでは同じ集合を再 block しない」注記を付ける。

    ``root_offset_`` (0.24.0): cwd の project root からの相対 prefix
    (``checker.root_offset``)。None 以外ならレシピを **path 形** (承認した
    1 ファイルだけ) で出し、basename 形 (同名すべて) を明示的な選択として
    併記する。None なら従来どおり basename 形だけを出す。

    ``submodule_by_path`` (内部バックログ): ``tracked`` の要素のうち submodule
    配下にあるものを ``{path: submodule 相対パス}`` で渡す。空でなければ
    tracked セクションの案内 (`git rm --cached`) が **親 repo からは効かない**
    submodule のディレクトリ一覧を追記する — 親 repo の index には submodule
    自体への gitlink エントリしかなく、配下の個別ファイルは submodule 自身の
    index が持つため。

    文字数予算 (内部バックログ + 外部レビュー R2 P2-B, ``MAX_OUTPUT_CHARS``):
    **path 由来の可変長部分をすべて予算の対象**にし、予算の外に置くのは
    定数から組み立てた**静的な案内文**だけにする。可変長なのは 4 箇所:

    1. ファイル列挙 (``  - path`` 行)
    2. 恒久除外レシピの ``!`` 行 (``exclude_recipe_lines``。件数は 20 で
       畳まれるが 1 本の path 長は無制限)
    3. basename 形の併記 (同じく path 由来)
    4. submodule 案内のディレクトリ一覧 (件数 10 で畳まれるが 1 本は無制限)

    0.27.0 当初は 2/3 を「固定 tail」として予算の外に置いていたため、3.5KB の
    path 3 本で列挙予算を 0 に絞っても直列化後 11,580 文字になり上限を破って
    いた (R2 P2-B、実測)。現在は 4 のみ固定上限
    (``_SUBMODULE_DIRS_MAX_CHARS``) で切って静的側に残し、1〜3 を予算配分する。

    配分の優先順位は **静的案内 (必ず残す) > レシピ行 > ファイル列挙**:
    まず静的部分 (``assemble`` に空の可変部を渡して測る) の直列化後文字数を
    求め、wrapper 分 (``_WRAPPER_CHARS``) と合わせて引いた残りを、レシピに
    半分・ファイル列挙に残り半分 (レシピが使い切らなかった分は列挙へ回る)
    で分ける。半分で頭打ちにするのは、レシピが長大な path で予算を独占して
    ファイル列挙が 0 件になるのを防ぐため (tracked / untracked の分け方と
    同じ発想)。``[project:...]`` ヘッダーと案内文はレシピ側でも静的扱いで
    必ず残す — ヘッダーが消えると生き残った ``!`` 行の追記先が分からなくなる。

    溢れた分は必ず件数マーカー (``_OMITTED_FILES_TEMPLATE`` /
    ``_OMITTED_RECIPE_TEMPLATE``) で明示する (黙って消さない)。マーカー自体の
    コストも予算の内側で確保する。この結果、**直列化後の出力が
    ``MAX_OUTPUT_CHARS`` 以下**であることが入力に依らず成立する
    (テスト: 件数 × path 長 × basename 長 × root_offset の格子で総当たり)。
    """
    head = ["【セキュリティ確認】", ""]

    tracked_header = (
        "【tracked】以下のファイルは git で追跡中で、機密パターンに一致します:"
        if tracked
        else None
    )
    tracked_guidance: list[str] = []
    if tracked:
        tracked_guidance.append(
            "対応: `.gitignore` に追加した上で `git rm --cached <path>` を実行して"
            "ください (index から外すだけで実ファイルは残ります)。"
        )
        submodule_dirs = sorted(
            {
                submodule_by_path[p]
                for p in tracked
                if submodule_by_path and p in submodule_by_path
            }
        )
        if submodule_dirs:
            # distinct dir 数に比例して伸びると、この行は静的部分 (= budget
            # 計算の外側) にあるため無制限に肥大しファイル列挙を圧迫する
            # (P2-2, 300 dir で reason が 9,241 byte に達しファイル列挙が潰れる
            # 実測あり)。件数 (10) に加え文字数でも畳んで固定サイズに保つ
            # (件数だけでは 1 本の path 長が無制限のまま — R2 P2-B と同型)。
            dirs_display = _submodule_dirs_display(submodule_dirs)
            tracked_guidance.append(
                "ただし上記のうち submodule 配下のファイルには、このコマンドが"
                "**親 repo からは効きません** (submodule は別の git index を"
                "持つため)。該当 submodule のディレクトリに `cd` してから、その"
                "ディレクトリ内での相対パスで `git rm --cached` の実行と "
                "`.gitignore` への追加を行ってください: " + dirs_display
            )

    untracked_header = (
        "【untracked】以下のファイルは機密パターンに一致し、まだ `.gitignore` 未登録です:"
        if untracked
        else None
    )
    untracked_guidance: list[str] = []
    if untracked:
        untracked_guidance.append(
            "対応: `.gitignore` に追加するか、意図的に管理対象とするか確認してください。"
        )

    tail_head: list[str] = [
        "AskUserQuestion ツールで各ファイルについてユーザーに確認してください:",
        "  選択肢1: 「.gitignore に追加」 (Recommended)",
        "  選択肢2: 「意図的に管理対象とする」",
        "",
    ]
    paths = [*tracked, *untracked]
    basenames = [os.path.basename(p) for p in paths]
    path_names = _recipe_names(paths, root_offset_)
    if path_names is not None:
        intro = "追記内容 (path 形 — 承認した 1 ファイルだけを外す):"
        recipe_names = path_names
    else:
        intro = (
            "追記内容 (basename 形 — プロジェクト root を解決できないため"
            " path 形は組み立てられない):"
        )
        recipe_names = basenames
    recipe_intro = (
        "【恒久除外】「意図的に管理対象とする」が選ばれた場合は、ユーザーの承認を"
        f"得た上で `{LOCAL_PATTERNS_DISPLAY_PATH}` に次を追記します"
        f" ({PROJECT_SECTION_PLACEHOLDER_NOTE})。"
        + EXCLUDE_SCOPE_WARNING.format(scope="同じ名前のファイル")
        + intro
    )
    # ``exclude_recipe_lines`` は [project:] ヘッダー + ``!`` 行 (limit=20) +
    # limit 由来の ``... (N more)`` を返す。ヘッダーは静的側で必ず残し、
    # ``!`` 行だけを予算対象にする。limit 由来のマーカーは捨て、表示できな
    # かった件数は **重複除去後の全件数** から数え直して 1 本にまとめる
    # (limit 由来と予算由来で別々の件数を出すと読み手が数を信用できない)。
    recipe_all = exclude_recipe_lines(recipe_names)
    recipe_header = f"  {recipe_all[0]}"
    recipe_rules = [f"  {ln}" for ln in recipe_all[1:] if ln.startswith("!")]
    recipe_total = len(_distinct(recipe_names))
    alts_prefix = "同名ファイルをすべて外したい場合だけ basename 形にする: "
    if path_names is not None:
        # basename 形は「同名すべて」を承知の上での明示的な選択として併記する
        alts_rules = [ln for ln in exclude_recipe_lines(basenames)[1:] if ln.startswith("!")]
        alts_total = len(_distinct(basenames))
    else:
        alts_rules, alts_total = [], 0
    session_note = (
        "このセッションでは同じファイル集合について再度 block しません"
        " (新たな機密ファイルが増えたときのみ再通知)。"
    )

    def assemble(
        t_lines: list[str],
        t_omitted: int,
        u_lines: list[str],
        u_omitted: int,
        r_lines: list[str],
        alts: str,
    ) -> str:
        """可変長パーツを受け取って reason 全体を組み立てる。

        予算計測 (空の可変部を渡す) と実出力の**両方**がこの 1 関数を通る
        ため、「計った骨格」と「出した骨格」がずれない (0.27.0 で
        ``_serialize`` を 1 箇所化したのと同じ理由)。
        """
        out: list[str] = [*head]
        if tracked_header:
            out.append(tracked_header)
            out.extend(t_lines)
            if t_omitted:
                out.append(_OMITTED_FILES_TEMPLATE.format(n=t_omitted))
            out.extend(tracked_guidance)
            out.append("")
        if untracked_header:
            out.append(untracked_header)
            out.extend(u_lines)
            if u_omitted:
                out.append(_OMITTED_FILES_TEMPLATE.format(n=u_omitted))
            out.extend(untracked_guidance)
            out.append("")
        out.extend(tail_head)
        out.append(recipe_intro)
        out.append(recipe_header)
        out.extend(r_lines)
        if path_names is not None:
            out.append(alts_prefix + alts)
        if session_scoped:
            out.append("")
            out.append(session_note)
        return "\n".join(out)

    # 静的部分 (= 可変部を空にした骨格) の直列化長 + wrapper を差し引いた残りを
    # path 由来の可変部に配分する。予算は「実際に stdout に出る JSON の文字数」
    # で計る (外部レビュー R1)。
    static_chars = _json_chars(assemble([], 0, [], 0, [], ""))
    remaining = max(0, MAX_OUTPUT_CHARS - _WRAPPER_CHARS - static_chars)

    # レシピ (`!` 行 + basename 併記) を先に配分する。半分で頭打ちにするのは、
    # 長大な path のレシピが予算を独占してファイル列挙が 0 件になるのを防ぐ
    # ため。使い切らなかった分はファイル列挙へ回る。
    #
    # ただし**素の出力が丸ごと予算に収まるなら頭打ちを外す**: 半分で切ると、
    # 予算導入前なら全件出ていた入力 (長い basename が 20 件 + 短い path など)
    # で併記が畳まれ、表示が減る退行になる (実測)。必要量が全部収まると
    # 分かっているときは必要分だけ渡し、残りをファイル列挙に回す。
    recipe_need = sum(_json_chars(ln) + 2 for ln in recipe_rules)
    if recipe_total > len(recipe_rules):
        recipe_need += (
            _json_chars(
                _OMITTED_RECIPE_TEMPLATE.format(n=recipe_total - len(recipe_rules))
            )
            + 2
        )
    alts_need = (
        _json_chars(_alts_with_budget(alts_rules, alts_total, _UNBOUNDED))
        if path_names is not None
        else 0
    )
    enum_need = sum(_json_chars(f"  - {p}") + 2 for p in paths)
    if recipe_need + alts_need + enum_need <= remaining:
        recipe_budget = recipe_need + alts_need
    else:
        recipe_budget = remaining // 2
    recipe_lines, recipe_used = _recipe_with_budget(
        recipe_rules, recipe_total, recipe_budget
    )
    alts = (
        _alts_with_budget(alts_rules, alts_total, recipe_budget - recipe_used)
        if path_names is not None
        else ""
    )
    enum_budget = max(0, remaining - recipe_used - _json_chars(alts))

    # tracked と untracked の両方に該当があるときは、先着順 (tracked が先に
    # 全予算を使い切る) にすると untracked が 0 件表示になりうる —
    # untracked は「.gitignore に追加するだけ」で対処できる分、tracked
    # (`git rm --cached` が要る) と同じくらい実例を見せる価値がある。
    # 予算を半分ずつに分け、tracked が使い切らなかった分だけ untracked に
    # 回す (どちらか一方しか無ければ従来どおり全予算を使う)。
    if tracked and untracked:
        tracked_budget = enum_budget // 2
    else:
        tracked_budget = enum_budget
    tracked_lines, tracked_omitted, tracked_leftover = _enumerate_with_budget(
        tracked, tracked_budget
    )
    untracked_lines, untracked_omitted, _ = _enumerate_with_budget(
        untracked, enum_budget - tracked_budget + tracked_leftover
    )

    return assemble(
        tracked_lines,
        tracked_omitted,
        untracked_lines,
        untracked_omitted,
        recipe_lines,
        alts,
    )


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        return 0
    if not isinstance(hook_input, dict):
        return 0

    # 同一ターン内の 2 回目以降はブロックしない (ループ防止)
    if hook_input.get("stop_hook_active", False):
        return 0

    cwd = hook_input.get("cwd", "")
    if not cwd:
        return 0
    # repo root と cwd prefix を 1 回の rev-parse で得る (作業ツリー外なら None)
    ctx = repo_context(cwd)
    if ctx is None:
        return 0
    toplevel, prefix = ctx

    patterns_file = Path(__file__).resolve().parent / "patterns.txt"
    try:
        rules = load_patterns(patterns_file, cwd=cwd)
    except OSError as e:
        sys.stderr.write(
            f"[check-sensitive-files] patterns_unavailable: {type(e).__name__}\n"
        )
        return 0

    if not rules:
        return 0

    # path 形 rule の基準 root (= [project:] セクションの key、0.24.0)。git の
    # toplevel ではなく Read / Edit / Bash と同じ解決を使う (レシピの互換性)
    root = resolve_project_root(cwd)
    sensitive = find_sensitive_files(cwd, rules, root=root)
    if not sensitive:
        return 0

    # 0.19.0: session 単位の once-only。報告済み集合に新規が無ければ黙る。
    # session_id が無い / 不正なら state を使わず従来通り毎回 block する。
    session_id = sanitize_session_id(hook_input.get("session_id"))
    # digest は repo root + root 相対 path (表示は cwd 相対のまま、Codex R2 P2-2)
    digests = digest_entries(sensitive, scope=toplevel, prefix=prefix)
    acked: set[str] = set()
    if session_id is not None:
        acked = load_acked(session_id, warn=_warn_stop_ack)
        if digests <= acked:
            return 0

    tracked = [f["path"] for f in sensitive if f["status"] == "tracked"]
    untracked = [f["path"] for f in sensitive if f["status"] == "untracked"]

    # submodule 内 tracked の案内分岐 (内部バックログ)。tracked が空なら
    # 追加の git 呼出をしない (repo_context 系と同じ「不要な呼出を増やさない」
    # 方針)。
    submodule_by_path: dict[str, str] = {}
    if tracked:
        submods = submodule_paths(cwd)
        if submods:
            for path in tracked:
                sm = in_submodule(path, submods)
                if sm is not None:
                    submodule_by_path[path] = sm

    reason = _build_reason(
        tracked,
        untracked,
        session_scoped=session_id is not None,
        root_offset_=root_offset(cwd, root),
        submodule_by_path=submodule_by_path,
    )

    if session_id is not None:
        save_acked(session_id, acked | digests, warn=_warn_stop_ack)

    # ``print`` ではなく ``write_stdout`` (UTF-8 bytes をバイナリ層へ書く) を
    # 通す。reason はほぼ全文が日本語なので、``PYTHONIOENCODING=ascii`` 等の
    # 非 UTF-8 stdout では ``print`` が ``UnicodeEncodeError`` を送出し、hook が
    # exit 1 で落ちて block が 1 byte も出ない (機密ファイルが報告されない、
    # 外部レビュー R2 P2-A)。末尾改行も同じ経路で書く。
    write_stdout(_serialize(reason) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
