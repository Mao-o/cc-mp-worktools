from __future__ import annotations

from typing import List, Protocol

from core.context import RepoContext


class StackDetector(Protocol):
    name: str
    priority: int

    def detect(self, ctx: RepoContext) -> List[str]: ...
