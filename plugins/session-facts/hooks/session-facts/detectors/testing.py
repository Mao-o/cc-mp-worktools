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
        if "@playwright/test" in deps or (ctx.root / "playwright.config.ts").exists():
            found.append("playwright")
        if "cypress" in deps or (ctx.root / "cypress.config.ts").exists():
            found.append("cypress")
        if (ctx.root / "pnpm-workspace.yaml").exists() or (ctx.root / "turbo.json").exists():
            found.append("monorepo")
        return found


def register():
    return TestingDetector()
