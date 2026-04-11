from __future__ import annotations

from typing import List

from core.context import RepoContext


class DenoDetector:
    name = "deno"
    priority = 12

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / "deno.json").exists() or (ctx.root / "deno.jsonc").exists():
            return ["deno"]
        return []


def register():
    return DenoDetector()
