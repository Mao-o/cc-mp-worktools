from __future__ import annotations

from typing import List

from core.context import RepoContext
from core.firebase import has_firebase, has_firebase_functions


class FirebaseDetector:
    name = "firebase"
    priority = 30

    def detect(self, ctx: RepoContext) -> List[str]:
        # has_firebase() also covers firebase.json/.firebaserc,
        # firebase-admin (npm and Python), and pubspec firebase_core --
        # previously this only checked the "firebase" and "@firebase/*" npm
        # dependency names, so a Cloud Functions-only or Python-backend repo
        # never got the "firebase" stack tag at all (internal backlog).
        if not has_firebase(ctx):
            return []
        stack = ["firebase"]
        if has_firebase_functions(ctx):
            stack.append("firebase-functions")
        return stack


def register():
    return FirebaseDetector()
