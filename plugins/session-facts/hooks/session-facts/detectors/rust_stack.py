from __future__ import annotations

from typing import List

from core.context import RepoContext


class RustStackDetector:
    name = "rust_stack"
    priority = 70

    def detect(self, ctx: RepoContext) -> List[str]:
        if (ctx.root / "Cargo.toml").exists():
            return ["rust"]
        return []


def register():
    return RustStackDetector()
