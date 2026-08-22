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
(``[project:$CLAUDE_PROJECT_DIR]`` + ``!<basename>``) を載せる (0.19.0、read 側
deny reason の hint と同じ ``_shared.patterns.exclude_recipe_lines``)。絶対パスは
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
    exclude_recipe_lines,
)
from checker import find_sensitive_files, is_git_repo, load_patterns  # noqa: E402
from stop_ack import (  # noqa: E402
    digest_entries,
    load_acked,
    sanitize_session_id,
    save_acked,
)


def _build_reason(
    tracked: list[str],
    untracked: list[str],
    session_scoped: bool,
) -> str:
    """block reason (LLM 向け plain text) を組み立てる。

    tracked / untracked を別セクションで列挙し、AskUserQuestion の選択肢と
    恒久除外レシピ (``[project:$CLAUDE_PROJECT_DIR]`` + ``!<basename>``) を添える。
    絶対パスは出さない (ヘッダーは環境変数名で示す)。``session_scoped`` なら
    「このセッションでは同じ集合を再 block しない」注記を付ける。
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
    sections.append(
        "【恒久除外】「意図的に管理対象とする」が選ばれた場合は、ユーザーの承認を"
        f"得た上で `{LOCAL_PATTERNS_DISPLAY_PATH}` に次を追記すると、以後の"
        " Stop / Read / Bash で報告されなくなります"
        f" ({PROJECT_SECTION_PLACEHOLDER_NOTE}):"
    )
    basenames = [os.path.basename(p) for p in [*tracked, *untracked]]
    for line in exclude_recipe_lines(basenames):
        sections.append(f"  {line}")
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
    if not cwd or not is_git_repo(cwd):
        return 0

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

    sensitive = find_sensitive_files(cwd, rules)
    if not sensitive:
        return 0

    # 0.19.0: session 単位の once-only。報告済み集合に新規が無ければ黙る。
    # session_id が無い / 不正なら state を使わず従来通り毎回 block する。
    session_id = sanitize_session_id(hook_input.get("session_id"))
    digests = digest_entries(sensitive, scope=os.path.normpath(cwd))
    acked: set[str] = set()
    if session_id is not None:
        acked = load_acked(session_id)
        if digests <= acked:
            return 0

    tracked = [f["path"] for f in sensitive if f["status"] == "tracked"]
    untracked = [f["path"] for f in sensitive if f["status"] == "untracked"]
    reason = _build_reason(
        tracked, untracked, session_scoped=session_id is not None
    )

    if session_id is not None:
        save_acked(session_id, acked | digests)

    output = {"decision": "block", "reason": reason}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
