from __future__ import annotations

from typing import Optional

from core.context import RepoContext
from core.git import ahead_behind, current_branch, recent_commits, upstream_ref


class GitProgressCollector:
    """Populate ctx.results['git_progress'] for the Project Facts header.

    Surfaces the kind of "current direction" signal the SessionStart injection
    rule rates as high value: which branch, how far it has diverged from its
    upstream, and the most recent commits. All git calls fail silently (no
    upstream, detached HEAD, non-git tree) so the header degrades gracefully.

    recent_commits is config-gated (include_recent_commits): on SessionStart
    the harness already injects the same commits via gitStatus, so hooks.json
    passes --no-recent-commits there and keeps them for subagents only.
    """

    name = "git_progress"
    section_title = ""
    priority = 6  # after dependencies (5), before structure (10)

    def should_run(self, ctx: RepoContext) -> bool:
        return ctx.results.get("is_git_repo") is True

    def collect(self, ctx: RepoContext) -> Optional[str]:
        info: dict = {}

        branch = current_branch(ctx.root)
        if branch:
            info["branch"] = branch
            ab = ahead_behind(ctx.root)
            if ab is not None:
                info["ahead"], info["behind"] = ab
                upstream = upstream_ref(ctx.root)
                if upstream:
                    info["upstream"] = upstream

        if ctx.config.include_recent_commits:
            commits = recent_commits(ctx.root, n=3)
            if commits:
                info["recent_commits"] = commits

        if info:
            ctx.results["git_progress"] = info
        # Contributes to the header, not its own section.
        return None


def register():
    return GitProgressCollector()
