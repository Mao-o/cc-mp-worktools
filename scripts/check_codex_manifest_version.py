#!/usr/bin/env python3
"""Claude 向け plugin.json と Codex 向け .codex-plugin/plugin.json の
version が一致しているかを検証する。

Codex manifest (`<plugin>/.codex-plugin/plugin.json`) を持つ plugin は
現状 session-facts の 1 件のみだが、対象が 0 件でも fail しない
(将来 plugin が増えても・減っても動く形にする)。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"


def main() -> int:
    failures: list[str] = []
    checked = 0

    if not PLUGINS_DIR.is_dir():
        print(f"OK: {PLUGINS_DIR} が存在しないため対象 0 件")
        return 0

    for plugin_dir in sorted(p for p in PLUGINS_DIR.iterdir() if p.is_dir()):
        codex_manifest = plugin_dir / ".codex-plugin" / "plugin.json"
        if not codex_manifest.is_file():
            continue  # Codex manifest を持たない plugin は対象外

        claude_manifest = plugin_dir / ".claude-plugin" / "plugin.json"
        if not claude_manifest.is_file():
            failures.append(
                f"{plugin_dir.name}: .codex-plugin/plugin.json はあるが "
                ".claude-plugin/plugin.json が見つかりません"
            )
            continue

        checked += 1
        claude_version = json.loads(claude_manifest.read_text(encoding="utf-8")).get("version")
        codex_version = json.loads(codex_manifest.read_text(encoding="utf-8")).get("version")
        if claude_version != codex_version:
            failures.append(
                f"{plugin_dir.name}: version 不一致 "
                f"(claude={claude_version!r}, codex={codex_version!r})"
            )

    if failures:
        print("Codex manifest の version 不一致を検出しました:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"OK: Codex manifest を持つ plugin {checked} 件で version 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
