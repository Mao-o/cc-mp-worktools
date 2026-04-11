from __future__ import annotations

from typing import List

from core.context import RepoContext
from core.fs import read_text


class PythonStackDetector:
    name = "python_stack"
    priority = 50

    def detect(self, ctx: RepoContext) -> List[str]:
        if not (ctx.root / "pyproject.toml").exists():
            return []
        found: List[str] = ["python"]
        if (ctx.root / "uv.lock").exists() or (ctx.root / "uv.toml").exists():
            found.append("uv")
        if (ctx.root / "poetry.lock").exists():
            found.append("poetry")
        pyproject = read_text(ctx.root / "pyproject.toml")
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
