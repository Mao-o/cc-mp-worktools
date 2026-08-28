#!/usr/bin/env python3
"""Stop hook: 機密ファイルパターンが残っていないか検出する。

tracked / untracked の両方を検査し、``.gitignore`` 済みでも **tracked は block**
(``git rm --cached`` が必要なため)。同一ターン内の 2 回目以降の Stop は
``stop_hook_active=true`` でスキップするため、**block が見えたら必ず対応する**
必要がある。

0.19.0 (bd_092a232e-snw.2): **session 単位の once-only 化**。報告済みの
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
    resolve_project_root,
)
from checker import (  # noqa: E402
    find_sensitive_files,
    load_patterns,
    repo_context,
    root_offset,
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


def _recipe_names(paths: list[str], root_offset_: str | None) -> list[str] | None:
    """レシピの path 形に使う root 相対 path 一覧を返す (0.24.0)。

    ``paths`` は ``git ls-files`` の cwd 相対 path。``root_offset_`` (cwd の root
    からの相対 prefix、``checker.root_offset``) を前置して root 相対にする。
    None (root 不明 / cwd が root 配下でない) なら path 形は組み立てられない
    ので None を返し、呼出側は basename 形だけを載せる。
    """
    if root_offset_ is None:
        return None
    if not root_offset_:
        return list(paths)
    return [f"{root_offset_}/{p}" for p in paths]


def _build_reason(
    tracked: list[str],
    untracked: list[str],
    session_scoped: bool,
    root_offset_: str | None = None,
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
    """
    sections: list[str] = ["【セキュリティ確認】", ""]
    if tracked:
        sections.append(
            "【tracked】以下のファイルは git で追跡中で、機密パターンに一致します:"
        )
        for path in tracked:
            sections.append(f"  - {path}")
        sections.append(
            "対応: `.gitignore` に追加した上で `git rm --cached <path>` を実行して"
            "ください (index から外すだけで実ファイルは残ります)。"
        )
        sections.append("")
    if untracked:
        sections.append(
            "【untracked】以下のファイルは機密パターンに一致し、まだ `.gitignore` 未登録です:"
        )
        for path in untracked:
            sections.append(f"  - {path}")
        sections.append(
            "対応: `.gitignore` に追加するか、意図的に管理対象とするか確認してください。"
        )
        sections.append("")
    sections.append(
        "AskUserQuestion ツールで各ファイルについてユーザーに確認してください:"
    )
    sections.append("  選択肢1: 「.gitignore に追加」 (Recommended)")
    sections.append("  選択肢2: 「意図的に管理対象とする」")
    sections.append("")
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
    sections.append(
        "【恒久除外】「意図的に管理対象とする」が選ばれた場合は、ユーザーの承認を"
        f"得た上で `{LOCAL_PATTERNS_DISPLAY_PATH}` に次を追記します"
        f" ({PROJECT_SECTION_PLACEHOLDER_NOTE})。"
        + EXCLUDE_SCOPE_WARNING.format(scope="同じ名前のファイル")
        + intro
    )
    for line in recipe:
        sections.append(f"  {line}")
    if path_names is not None:
        # basename 形は「同名すべて」を承知の上での明示的な選択として併記する
        alts = " / ".join(
            f"`{line}`" if line.startswith("!") else line
            for line in exclude_recipe_lines(basenames)[1:]
        )
        sections.append(
            f"同名ファイルをすべて外したい場合だけ basename 形にする: {alts}"
        )
    if session_scoped:
        sections.append("")
        sections.append(
            "このセッションでは同じファイル集合について再度 block しません"
            " (新たな機密ファイルが増えたときのみ再通知)。"
        )
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
    reason = _build_reason(
        tracked,
        untracked,
        session_scoped=session_id is not None,
        root_offset_=root_offset(cwd, root),
    )

    if session_id is not None:
        save_acked(session_id, acked | digests, warn=_warn_stop_ack)

    output = {"decision": "block", "reason": reason}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
