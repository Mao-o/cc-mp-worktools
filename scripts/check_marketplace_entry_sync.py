#!/usr/bin/env python3
"""marketplace.json の plugin entry と各 plugin.json の metadata 同期を検証する。

## なぜ entry 側にも metadata を持つのか

`claude plugin list --available` が返す install 前の一覧は **marketplace entry の
値だけ**を見ている。entry から `description` / `keywords` を省いても plugin.json に
fallback はせず、キー自体が欠落する (CLI 2.1.251 で A/B 実測)。`strict` の
「plugin.json が権威」はコンポーネント定義 (skills / agents / hooks / MCP /
output styles) の話で、metadata は対象外。

したがって entry の metadata は**必須**であり、plugin.json と二重に持つしかない。
二重に持つ以上ドリフトするので、このスクリプトで「存在すること」と「一致すること」の
両方を検証する。

## 検証内容

1. `plugins/` の各ディレクトリと marketplace entry が 1:1 で対応すること
   (entry を書き忘れた plugin / 実体の無い entry を検出)
2. `description` / `keywords` が **entry 側にも plugin.json 側にも存在**し、
   空でないこと (片側でも欠けたら fail)
3. 両者の値が一致すること (plugin.json を single source of truth として扱い、
   ずれていたら entry 側を plugin.json に合わせる)

2 を独立した検査にしているのは、「フィールドが無ければ比較をスキップ」する実装だと
両側から消えた瞬間に**構造的に常に 0 件で通過する空チェック**になるため。

## 使い方

    python3 scripts/check_marketplace_entry_sync.py [--root <repo root>]

`--root` は検証用に用意したツリーを指すためのもの (既定はこのスクリプトから見た
repo root)。exit 0 = 同期済み、exit 1 = 差分あり。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

# plugin.json と entry で一致していることを要求するフィールド。
# 「存在すること」も同時に要求する (上の docstring 参照)。
SYNCED_FIELDS = ("description", "keywords")


def _load_json(path: pathlib.Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_filled(value: object) -> bool:
    """空でない str / list か (0 件の list や空文字は「未設定」と同じ扱い)。"""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--root",
        default=str(pathlib.Path(__file__).resolve().parent.parent),
        help="検証対象の repo root (既定: このスクリプトの 1 つ上)",
    )
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    marketplace_path = root / ".claude-plugin" / "marketplace.json"
    plugins_dir = root / "plugins"

    failures: list[str] = []

    if not marketplace_path.is_file():
        print(f"ERROR: {marketplace_path} が見つかりません", file=sys.stderr)
        return 1

    marketplace = _load_json(marketplace_path)
    if not isinstance(marketplace, dict) or not isinstance(
        marketplace.get("plugins"), list
    ):
        print("ERROR: marketplace.json に plugins 配列がありません", file=sys.stderr)
        return 1

    entries: dict[str, dict] = {}
    for index, entry in enumerate(marketplace["plugins"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            failures.append(f"plugins[{index}]: name が無い / 文字列でない")
            continue
        if entry["name"] in entries:
            failures.append(f"{entry['name']}: entry が重複しています")
            continue
        entries[entry["name"]] = entry

    plugin_dirs = (
        sorted(p for p in plugins_dir.iterdir() if p.is_dir())
        if plugins_dir.is_dir()
        else []
    )
    if not plugin_dirs:
        print(f"ERROR: {plugins_dir} に plugin がありません", file=sys.stderr)
        return 1

    manifests: dict[str, dict] = {}
    for plugin_dir in plugin_dirs:
        manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
        if not manifest_path.is_file():
            failures.append(f"{plugin_dir.name}: .claude-plugin/plugin.json が無い")
            continue
        manifest = _load_json(manifest_path)
        if not isinstance(manifest, dict):
            failures.append(f"{plugin_dir.name}: plugin.json が object でない")
            continue
        manifests[plugin_dir.name] = manifest

    for name in sorted(set(manifests) - set(entries)):
        failures.append(f"{name}: plugin 実体はあるが marketplace entry が無い")
    for name in sorted(set(entries) - set(manifests)):
        failures.append(f"{name}: marketplace entry はあるが plugins/{name} が無い")

    for name in sorted(set(entries) & set(manifests)):
        entry = entries[name]
        manifest = manifests[name]
        for field in SYNCED_FIELDS:
            entry_value = entry.get(field)
            manifest_value = manifest.get(field)
            missing = [
                label
                for label, value in (
                    ("marketplace entry", entry_value),
                    ("plugin.json", manifest_value),
                )
                if not _is_filled(value)
            ]
            if missing:
                failures.append(
                    f"{name}: {field} が未設定または空 ({' / '.join(missing)} 側)"
                )
                continue
            if entry_value != manifest_value:
                failures.append(
                    f"{name}: {field} が不一致 "
                    f"(entry={entry_value!r} / plugin.json={manifest_value!r}) "
                    f"— entry 側を plugin.json に合わせてください"
                )

    if failures:
        print("marketplace entry と plugin.json の同期に問題を検出しました:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"OK: plugin {len(manifests)} 件で "
        f"{' / '.join(SYNCED_FIELDS)} が entry と plugin.json で一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
