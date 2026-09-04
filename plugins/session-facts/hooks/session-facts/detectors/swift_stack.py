from __future__ import annotations

from pathlib import Path
from typing import List

from core.context import RepoContext

# App.xcodeproj/App.xcworkspace are directories (Xcode's bundle format), not
# files -- Path.glob() matches on name regardless of file/dir type, so no
# is_file()/is_dir() filter is needed here (unlike detectors/dotnet_stack.py's
# is_file() guard, which exists specifically to *exclude* directories that
# happen to share a *.csproj-style suffix).
_XCODE_MARKER_GLOBS = ("*.xcodeproj", "*.xcworkspace")


def _has_xcode_project(root: Path) -> bool:
    try:
        return any(any(root.glob(pattern)) for pattern in _XCODE_MARKER_GLOBS)
    except OSError:
        return False


class SwiftStackDetector:
    name = "swift_stack"
    priority = 65

    def detect(self, ctx: RepoContext) -> List[str]:
        # Package.swift (SwiftPM) and *.xcodeproj/*.xcworkspace (an
        # Xcode-only app with no package manifest) are both recognized as
        # "stack: swift" -- an Xcode-only project used to report no Swift
        # stack at all, and (via PROJECT_MARKERS) was also rejected outright
        # by the non-git marker gate (merge-review finding).
        #
        # collectors/scripts.py deliberately does NOT treat "swift" stack
        # membership alone as license to suggest `swift build`/`swift
        # test` -- those are SwiftPM commands that only apply when
        # Package.swift actually exists. An Xcode-only project needs
        # `xcodebuild`, which requires an explicit -scheme this detector
        # cannot safely infer, so scripts.py checks for Package.swift
        # itself (directly, or via package_manager == "swift", which
        # core/pm.py sets off Package.swift alone) before adding those
        # commands.
        if (ctx.root / "Package.swift").exists() or _has_xcode_project(ctx.root):
            return ["swift"]
        return []


def register():
    return SwiftStackDetector()
