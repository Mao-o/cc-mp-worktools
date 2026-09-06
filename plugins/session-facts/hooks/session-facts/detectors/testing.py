from __future__ import annotations

from typing import List

from core.context import RepoContext


class TestingDetector:
    name = "testing"
    priority = 40

    def detect(self, ctx: RepoContext) -> List[str]:
        found: List[str] = []
        deps = ctx.all_deps
        if "zod" in deps:
            found.append("zod")
        if "vitest" in deps:
            found.append("vitest")
        if "jest" in deps:
            found.append("jest")
        if "@playwright/test" in deps or ctx.find_in_manifest_dirs("playwright.config.ts") is not None:
            found.append("playwright")
        if "cypress" in deps or ctx.find_in_manifest_dirs("cypress.config.ts") is not None:
            found.append("cypress")
        root = ctx.root
        if (
            (root / "pnpm-workspace.yaml").exists()
            or (root / "turbo.json").exists()
            or len(ctx.workspace_dirs) >= 2
        ):
            found.append("monorepo")
        return found


def register():
    return TestingDetector()
