from __future__ import annotations

from typing import Optional, Protocol

from core.context import RepoContext


class SectionCollector(Protocol):
    name: str
    section_title: str
    priority: int

    def should_run(self, ctx: RepoContext) -> bool: ...
    def collect(self, ctx: RepoContext) -> Optional[str]: ...
