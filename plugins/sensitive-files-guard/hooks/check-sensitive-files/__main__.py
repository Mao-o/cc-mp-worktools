#!/usr/bin/env python3
"""Stop hook: 機密ファイルパターンが残っていないか検出する。

tracked / untracked の両方を検査し、``.gitignore`` 済みでも **tracked は block**
(``git rm --cached`` が必要なため)。2 回目以降の Stop は ``stop_hook_active=true``
でスキップするため、**block が見えたら必ず対応する**必要がある。

patterns.txt の読み取りに失敗した場合は stderr warning のみ出して exit 0
(fail-open)。read 側 hook と異なり、Stop は Claude の応答を止めるため
fail-closed にしない。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_pkg_dir = str(Path(__file__).resolve().parent)
_hooks_dir = str(Path(__file__).resolve().parent.parent)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)
if _hooks_dir not in sys.path:
    sys.path.insert(0, _hooks_dir)

from checker import find_sensitive_files, is_git_repo, load_patterns  # noqa: E402


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        return 0

    # 2回目以降はブロックしない (ループ防止)
    if hook_input.get("stop_hook_active", False):
        return 0

    cwd = hook_input.get("cwd", "")
    if not cwd or not is_git_repo(cwd):
        return 0

    patterns_file = Path(__file__).resolve().parent / "patterns.txt"
    try:
        rules = load_patterns(patterns_file)
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

    tracked = [f["path"] for f in sensitive if f["status"] == "tracked"]
    untracked = [f["path"] for f in sensitive if f["status"] == "untracked"]

    sections: list[str] = ["【セキュリティ確認】", ""]
    if tracked:
        sections.append(
            "【tracked】以下のファイルは git で追跡中で、機密パターンに一致します:"
        )
        for path in tracked:
            sections.append(f"  - {path}")
        sections.append(
            "対応: `.gitignore` に追加した上で `git rm --cached <path>` を実行してください。"
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

    reason = "\n".join(sections)

    output = {"decision": "block", "reason": reason}
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
