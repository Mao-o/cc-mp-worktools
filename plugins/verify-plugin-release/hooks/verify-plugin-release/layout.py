"""repo 内の plugin ディレクトリを見つけ、変更ファイルをどの plugin のものか振り分ける。

plugin の場所は marketplace.json の相対 source (`metadata.pluginRoot` も考慮) と、
`.claude-plugin/plugin.json` を持つディレクトリ (repo 直下から 2 階層まで) の
両方から集める。marketplace に未登録の新規 plugin も検査対象に入れるため。
"""
from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from pathlib import Path

MARKETPLACE = ".claude-plugin/marketplace.json"
PLUGIN_MANIFEST = ".claude-plugin/plugin.json"

# 配布に影響しない (version bump / CHANGELOG 更新を求めない) ファイル。
# SKILL.md など機能に効く Markdown があるため「*.md 全部」にはしない。
_DOC_BASENAMES = {"README.md", "CHANGELOG.md", "LICENSE", "LICENSE.md", "LICENSE.txt"}
_DOC_DIRS = ("docs/",)


@dataclass(frozen=True)
class Plugin:
    name: str
    dir: str  # repo root からの相対 POSIX パス。repo 自体が plugin なら ""
    in_marketplace: bool


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _norm(rel: str) -> str:
    rel = posixpath.normpath(rel.replace("\\", "/"))
    return "" if rel in (".", "") else rel


def _manifest_name(root: Path, rel: str) -> str | None:
    data = _read_json(root / rel / PLUGIN_MANIFEST) if rel else _read_json(root / PLUGIN_MANIFEST)
    if isinstance(data, dict) and isinstance(data.get("name"), str):
        return data["name"]
    return None


def _marketplace_dirs(root: Path) -> dict[str, str]:
    data = _read_json(root / MARKETPLACE)
    if not isinstance(data, dict):
        return {}
    meta = data.get("metadata")
    plugin_root = meta.get("pluginRoot") if isinstance(meta, dict) else None
    dirs: dict[str, str] = {}
    for entry in data.get("plugins") or []:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, str):
            continue  # github / npm などリモート source は repo 内に実体が無い
        if not source.startswith("./") and isinstance(plugin_root, str):
            source = posixpath.join(plugin_root, source)
        rel = _norm(source)
        if rel.startswith("../"):
            continue
        dirs[rel] = str(entry.get("name") or posixpath.basename(rel))
    return dirs


def is_plugin_repo(root: Path) -> bool:
    return (root / MARKETPLACE).is_file() or (root / PLUGIN_MANIFEST).is_file()


def discover(root: Path) -> list[Plugin]:
    listed = _marketplace_dirs(root)
    found: dict[str, Plugin] = {}
    for rel, name in listed.items():
        # ディレクトリが消えていても登録は残す。削除した plugin の entry が残っていないかを
        # 検査するため、変更ファイルの持ち主として引き当てられる必要がある
        found[rel] = Plugin(_manifest_name(root, rel) or name, rel, True)

    for pattern in ("*/" + PLUGIN_MANIFEST, "*/*/" + PLUGIN_MANIFEST):
        for manifest in root.glob(pattern):
            rel = _norm(manifest.parent.parent.relative_to(root).as_posix())
            if rel.split("/", 1)[0].startswith("."):
                continue  # .git / .claude 配下は対象外
            if rel not in found:
                found[rel] = Plugin(_manifest_name(root, rel) or posixpath.basename(rel), rel, False)

    if not found and (root / PLUGIN_MANIFEST).is_file() and not (root / MARKETPLACE).is_file():
        # repo 自体が単一 plugin
        found[""] = Plugin(_manifest_name(root, "") or root.name, "", False)
    return sorted(found.values(), key=lambda p: p.dir)


def owner(path: str, plugins: list[Plugin]) -> Plugin | None:
    """path (repo 相対 POSIX) を含む最も深い plugin を返す。"""
    best: Plugin | None = None
    for p in plugins:
        if p.dir == "" or path == p.dir or path.startswith(p.dir + "/"):
            if best is None or len(p.dir) > len(best.dir):
                best = p
    return best


def is_doc_only(path: str, plugin: Plugin) -> bool:
    rel = path[len(plugin.dir) + 1 :] if plugin.dir else path
    if posixpath.basename(rel) in _DOC_BASENAMES:
        return True
    return rel.startswith(_DOC_DIRS)
