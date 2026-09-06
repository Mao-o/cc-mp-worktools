from __future__ import annotations

from typing import List

from core.context import RepoContext


class RubyStackDetector:
    name = "ruby_stack"
    priority = 80

    def detect(self, ctx: RepoContext) -> List[str]:
        if ctx.find_in_manifest_dirs("Gemfile") is not None:
            return ["ruby"]
        return []


def register():
    return RubyStackDetector()
