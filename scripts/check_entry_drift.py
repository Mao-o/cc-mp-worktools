#!/usr/bin/env python3
"""marketplace.json の plugins[] entry と各 plugin.json の description/keywords
が一致しているかを検証する。

plugin.json を single source of truth とする方針のもと、entry 側は
name/source/category(+displayName) に縮約済みで、通常は description/keywords
を持たない。このチェックは、将来誰かが entry に description/keywords を
書き戻してしまい plugin.json と食い違う (drift する) ことを防ぐ回帰ガードである。
entry にそのフィールドが無ければ比較しようがないので何もしない
(= 現状は全 entry で対象 0 件、warning ゼロで通る)。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
COMPARED_FIELDS = ("description", "keywords")


def main() -> int:
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    entries = marketplace.get("plugins", [])
    failures: list[str] = []

    for entry in entries:
        name = entry.get("name", "<unknown>")
        source = entry.get("source")
        if not isinstance(source, str) or not source.startswith("./"):
            # github/url/npm 等の非相対 source はローカルに plugin.json が無いため対象外
            continue

        plugin_json_path = ROOT / source / ".claude-plugin" / "plugin.json"
        if not plugin_json_path.is_file():
            failures.append(f"{name}: plugin.json が見つかりません ({plugin_json_path})")
            continue

        plugin_json = json.loads(plugin_json_path.read_text(encoding="utf-8"))
        for field in COMPARED_FIELDS:
            if field not in entry:
                continue  # entry に無ければ drift しようがない
            if entry[field] != plugin_json.get(field):
                failures.append(f"{name}: entry.{field} と plugin.json.{field} が不一致です")

    if failures:
        print("marketplace entry と plugin.json の drift を検出しました:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"OK: entry {len(entries)} 件で description/keywords の drift なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
