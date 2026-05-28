from __future__ import annotations

from typing import List

from core.context import RepoContext


class PythonStackDetector:
    name = "python_stack"
    priority = 50

    def detect(self, ctx: RepoContext) -> List[str]:
        pyproject = ctx.pyproject_toml
        found: List[str] = []
        if pyproject:
            found.append("python")
        else:
            py_count = sum(1 for p in ctx.tracked_files if p.endswith(".py"))
            total = len(ctx.tracked_files)
            if py_count >= 10 and total > 0 and py_count / total >= 0.2:
                found.append("python")
        if not found:
            return []
        if (ctx.root / "uv.lock").exists() or (ctx.root / "uv.toml").exists():
            found.append("uv")
        if (ctx.root / "poetry.lock").exists():
            found.append("poetry")
        if pyproject:
            for fw, label in (
                ("fastapi", "fastapi"),
                ("django", "django"),
                ("flask", "flask"),
                ("pytest", "pytest"),
            ):
                if fw in pyproject.lower():
                    found.append(label)
        return found


def register():
    return PythonStackDetector()
