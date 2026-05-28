from __future__ import annotations

from typing import List

from core.context import RepoContext


class ClaudePluginDetector:
    name = "claude_plugin"
    priority = 11

    def detect(self, ctx: RepoContext) -> List[str]:
        root = ctx.root
        is_marketplace = (root / "marketplace.json").exists() or (root / ".claude-plugin" / "marketplace.json").exists()
        is_plugin = (root / ".claude-plugin" / "plugin.json").exists()
        if not (is_marketplace or is_plugin):
            return []
        found: List[str] = []
        if is_marketplace:
            found.append("claude-code-marketplace")
        if is_plugin:
            found.append("claude-code-plugin")
        for sub in ("hooks", "skills", "agents", "commands"):
            if (root / sub).is_dir():
                found.append(sub)
        return found


def register():
    return ClaudePluginDetector()
