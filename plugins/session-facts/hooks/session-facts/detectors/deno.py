from __future__ import annotations

from typing import List

from core.context import RepoContext


class DenoDetector:
    name = "deno"
    priority = 12

    def detect(self, ctx: RepoContext) -> List[str]:
        if ctx.find_in_manifest_dirs("deno.json", "deno.jsonc") is not None:
            return ["deno"]
        return []


def register():
    return DenoDetector()
