from __future__ import annotations

from typing import List

from core.context import RepoContext


class JavaStackDetector:
    name = "java_stack"
    priority = 55

    def detect(self, ctx: RepoContext) -> List[str]:
        root = ctx.root
        found: List[str] = []
        has_gradle = (
            (root / "gradlew").exists()
            or (root / "build.gradle").exists()
            or (root / "build.gradle.kts").exists()
        )
        has_maven = (root / "pom.xml").exists()
        if has_gradle or has_maven:
            found.append("java")
        if has_gradle:
            found.append("gradle")
        if has_maven:
            found.append("maven")
        return found


def register():
    return JavaStackDetector()
