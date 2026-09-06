from __future__ import annotations

from typing import List

from core.context import RepoContext


class ElixirStackDetector:
    name = "elixir_stack"
    priority = 75

    def detect(self, ctx: RepoContext) -> List[str]:
        if ctx.find_in_manifest_dirs("mix.exs") is not None:
            return ["elixir"]
        return []


def register():
    return ElixirStackDetector()
