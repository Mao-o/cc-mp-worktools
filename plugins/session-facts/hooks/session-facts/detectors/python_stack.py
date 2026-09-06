from __future__ import annotations

from typing import List

from core.context import RepoContext


class PythonStackDetector:
    name = "python_stack"
    priority = 50

    def detect(self, ctx: RepoContext) -> List[str]:
        # Every tracked pyproject.toml counts (root or workspace, joa.2):
        # dify keeps its only pyproject under api/.
        pyproject = ctx.all_pyproject_text
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
        if ctx.find_in_manifest_dirs("uv.lock", "uv.toml") is not None:
            found.append("uv")
        if ctx.find_in_manifest_dirs("poetry.lock") is not None:
            found.append("poetry")
        if pyproject:
            lowered = pyproject.lower()
            for fw, label in (
                ("fastapi", "fastapi"),
                ("django", "django"),
                ("flask", "flask"),
                ("pytest", "pytest"),
            ):
                if fw in lowered:
                    found.append(label)
        return found


def register():
    return PythonStackDetector()
