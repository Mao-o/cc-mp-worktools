from __future__ import annotations

from typing import Optional

from core.context import RepoContext
from core.runtime import build_runtime_info


class RuntimeEnvCollector:
    """Populate ctx.results['runtime'] for the Project Facts header.

    Surfaces the execution context an agent would otherwise miss: which version
    manager pins the toolchain (mise/asdf), whether a local virtualenv exists,
    and the interpreter versions involved. This keeps agents from judging the
    repo against a global baseline (e.g. "kaggle is not installed" when it lives
    in ``.venv``). Like dependencies/git_progress, it contributes to the header
    rather than emitting its own section.
    """

    name = "runtime_env"
    section_title = ""
    priority = 7  # clustered with dependencies (5) / git_progress (6)

    def should_run(self, ctx: RepoContext) -> bool:
        return True

    def collect(self, ctx: RepoContext) -> Optional[str]:
        info = build_runtime_info(ctx.root)
        if info:
            ctx.results["runtime"] = info
        # Contributes to the header, not its own section.
        return None


def register():
    return RuntimeEnvCollector()
