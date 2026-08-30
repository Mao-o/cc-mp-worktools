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


# block reason の byte 予算 (内部バックログ)。公式 hooks reference が明記する
# 「hook の stdout 文字列は 10,000 文字が上限」に対する安全マージン。byte 数を
# 上限にすると (multibyte 文字は 1 文字が複数 byte を消費するため) この
# reason 文字列自体の文字数は必ず 9,216 未満に収まる。ただし実際に stdout に
# 出るのは `json.dumps({"decision": ..., "reason": reason})` で、JSON
# エンコードで改行 (``\n``) が 2 文字 (``\`` + ``n``) に膨らむ分と wrapper
# 自体の文字数が上乗せされる。9,216 という値はこの上乗せ分 (数百文字程度)
# を吸収するための余裕であり、「reason の文字数」と「実際の stdout の文字数」
# を厳密には区別していない — 実測 (250 ファイルのネストしたパス、改行
# 約 260 個) では reason 8,292 文字 + JSON オーバーヘッド 300 文字弱で
# 10,000 文字の枠に十分収まることを確認済みだが、改行が非常に多く 1 行が
# 短い病的な入力では余裕が目減りする。
# core/output.py (read 側 hook) の MAX_REASON_BYTES + _truncate と同じ発想だが、
# あちらは末尾を単純 truncate するのに対し、こちらは**固定 tail
# (AskUserQuestion 案内 + 恒久除外レシピ) を必ず残し、ファイル列挙だけを畳む**
# (固定 tail が末尾にあり、素の truncate だと真っ先に失われるため)。
MAX_REASON_BYTES = 9 * 1024

# ファイル列挙が予算を超えて畳まれたときに追記する行のテンプレート。この行
# 自体は budget 計算の対象外 (常に表示する — 何件省略したかを黙って伝えずに
# 消すと「検査は実行されたが報告が消えた」ことに気づけないため)。
_OMITTED_FILES_TEMPLATE = "  ... ({n} more files; see git status)"


def _enumerate_with_budget(
    paths: list[str], budget: int
) -> tuple[list[str], int, int]:
    """``  - path`` 形式の列挙行を ``budget`` (byte) の範囲で組み立てる。

    先頭から順に収まる分だけ行にし、溢れた時点で打ち切る。戻り値は
    ``(組み立てた行, 表示できなかった件数, 残り byte 予算)``。残り予算は
    呼出側が次のリスト (untracked) と共有するために返す。

    1 行あたりのコストは ``UTF-8 byte 数 + 1`` (``"\\n".join`` で他の行と
    結合するときの区切り文字分)。
    """
    lines: list[str] = []
    for i, path in enumerate(paths):
        line = f"  - {path}"
        cost = len(line.encode("utf-8")) + 1
        if cost > budget:
            return lines, len(paths) - i, budget
        lines.append(line)
        budget -= cost
    return lines, 0, budget


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

    byte 予算 (内部バックログ, ``MAX_REASON_BYTES``): tracked / untracked の
    ファイル列挙 (``  - path`` 行) 以外は全て**固定サイズ** (件数に依存しない
    — 恒久除外レシピは ``exclude_recipe_lines`` が既に 20 件で畳んでおり、
    submodule 案内の ``dirs_display`` も同じ発想で 10 件で畳んでいる。
    どちらも欠くと distinct 件数に比例して skeleton 自体が伸び、この不変条件が
    崩れる — P2-2 で submodule 案内が無制限だった実例あり) なので、まず固定
    部分 (このメソッド内では ``head`` / ``*_header`` /
    ``*_guidance`` / ``tail``) を組み立ててその byte 数を求め、残り予算を
    ファイル列挙に充てる。tracked / untracked の両方に該当があるときは
    残り予算を半分ずつに分け (tracked が使い切らなかった分だけ untracked に
    回す) — 先着順にすると片方 (実装上は先に処理する tracked) が予算を
    独占し、もう片方が実例 0 件のまま "... more files" だけになりうるため。
    列挙が溢れたら ``_OMITTED_FILES_TEMPLATE`` で件数を明示する (黙って
    消さない)。tail 自体を切り詰めることは**しない** — 固定部分だけで予算を
    超える病的なケース (極端に長いファイル名が多数など) では reason が
    ``MAX_REASON_BYTES`` を超えることを許容する (AskUserQuestion の案内や
    恒久除外レシピを失う方が実害が大きいため)。
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
            # distinct dir 数に比例して伸びると、この行は skeleton (= 固定部分
            # 扱いで budget 計算の外側) にあるため無制限に肥大しファイル列挙を
            # 圧迫する (P2-2, 300 dir で reason が 9,241 byte に達しファイル
            # 列挙が潰れる実測あり)。exclude_recipe_lines と同じ発想で先頭 10
            # 件 + 省略件数に畳み、skeleton を実質固定サイズに保つ。
            shown_dirs = submodule_dirs[:10]
            dirs_display = ", ".join(f"`{d}`" for d in shown_dirs)
            if len(submodule_dirs) > len(shown_dirs):
                dirs_display += (
                    f" ... ({len(submodule_dirs) - len(shown_dirs)} more)"
                )
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

    tail: list[str] = [
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
        recipe = exclude_recipe_lines(path_names)
    else:
        intro = (
            "追記内容 (basename 形 — プロジェクト root を解決できないため"
            " path 形は組み立てられない):"
        )
        recipe = exclude_recipe_lines(basenames)
    tail.append(
        "【恒久除外】「意図的に管理対象とする」が選ばれた場合は、ユーザーの承認を"
        f"得た上で `{LOCAL_PATTERNS_DISPLAY_PATH}` に次を追記します"
        f" ({PROJECT_SECTION_PLACEHOLDER_NOTE})。"
        + EXCLUDE_SCOPE_WARNING.format(scope="同じ名前のファイル")
        + intro
    )
    for line in recipe:
        tail.append(f"  {line}")
    if path_names is not None:
        # basename 形は「同名すべて」を承知の上での明示的な選択として併記する
        alts = " / ".join(
            f"`{line}`" if line.startswith("!") else line
            for line in exclude_recipe_lines(basenames)[1:]
        )
        tail.append(f"同名ファイルをすべて外したい場合だけ basename 形にする: {alts}")
    if session_scoped:
        tail.append("")
        tail.append(
            "このセッションでは同じファイル集合について再度 block しません"
            " (新たな機密ファイルが増えたときのみ再通知)。"
        )

    # 固定部分 (ファイル列挙を除く) の byte 数を求め、残りをファイル列挙に回す。
    skeleton: list[str] = [*head]
    if tracked_header:
        skeleton.append(tracked_header)
        skeleton.extend(tracked_guidance)
        skeleton.append("")
    if untracked_header:
        skeleton.append(untracked_header)
        skeleton.extend(untracked_guidance)
        skeleton.append("")
    skeleton.extend(tail)
    fixed_bytes = len("\n".join(skeleton).encode("utf-8"))
    remaining = max(0, MAX_REASON_BYTES - fixed_bytes)

    # tracked と untracked の両方に該当があるときは、先着順 (tracked が先に
    # 全予算を使い切る) にすると untracked が 0 件表示になりうる —
    # untracked は「.gitignore に追加するだけ」で対処できる分、tracked
    # (`git rm --cached` が要る) と同じくらい実例を見せる価値がある。
    # 予算を半分ずつに分け、tracked が使い切らなかった分だけ untracked に
    # 回す (どちらか一方しか無ければ従来どおり全予算を使う)。
    if tracked and untracked:
        tracked_budget = remaining // 2
    else:
        tracked_budget = remaining
    tracked_lines, tracked_omitted, tracked_leftover = _enumerate_with_budget(
        tracked, tracked_budget
    )
    untracked_lines, untracked_omitted, _ = _enumerate_with_budget(
        untracked, remaining - tracked_budget + tracked_leftover
    )

    sections: list[str] = [*head]
    if tracked_header:
        sections.append(tracked_header)
        sections.extend(tracked_lines)
        if tracked_omitted:
            sections.append(_OMITTED_FILES_TEMPLATE.format(n=tracked_omitted))
        sections.extend(tracked_guidance)
        sections.append("")
    if untracked_header:
        sections.append(untracked_header)
        sections.extend(untracked_lines)
        if untracked_omitted:
            sections.append(_OMITTED_FILES_TEMPLATE.format(n=untracked_omitted))
        sections.extend(untracked_guidance)
        sections.append("")
    sections.extend(tail)
    return "\n".join(sections)


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

    output = {"decision": "block", "reason": reason}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
