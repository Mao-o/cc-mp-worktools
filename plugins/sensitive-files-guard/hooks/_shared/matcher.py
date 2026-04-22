"""機密パス判定 (last-match-wins 版) — 両 hook 共通実装。

basename と ``pathlib.parts`` の両方に対してマッチングを試みる:
- basename 一致: ``.env``, ``secrets.yaml`` 等の典型ケース
- parts 一致: ``/foo/.env/bar`` のように親ディレクトリが機密名でも検出
  (現実的な用途は少ないが、symlink race 等で偽装されたケースを拾う)

rules は ``list[tuple[str, bool]]`` 形式で、各 tuple は ``(pattern, is_exclude)``。
評価はリスト先頭から全件走査し、**最後にマッチしたルールの符号**を採用する
(gitignore 風 last-match-wins)。既定 patterns.txt 末尾に書いた exclude を、
ユーザーが ``patterns.local.txt`` 側で再び include に差し戻せるようにするため。
"""
from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import PurePath


def _is_case_sensitive() -> bool:
    """環境変数 ``SFG_CASE_SENSITIVE=1`` で case-sensitive にフォールバック可能。

    既定は case-insensitive。旧挙動 (0.1.x 系互換) に戻したいときだけ
    ``SFG_CASE_SENSITIVE=1`` を設定する。
    """
    return os.environ.get("SFG_CASE_SENSITIVE") == "1"


def _last_match_verdict(name: str, rules: list[tuple[str, bool]]) -> str:
    """最後にマッチしたルールの符号を返す。

    ``SFG_CASE_SENSITIVE=1`` 未設定時は lower 比較で case-insensitive に評価する。
    旧 `fnmatch.fnmatch` (OS 依存) ではなく `fnmatchcase` を使い、lower 化だけで
    挙動を正規化する (OS の大文字小文字扱いに依存しない)。

    Returns:
        "include" / "exclude" / "nomatch"
    """
    cs = _is_case_sensitive()
    target = name if cs else name.lower()
    last: str | None = None
    for pattern, is_exclude in rules:
        pat = pattern if cs else pattern.lower()
        if fnmatchcase(target, pat):
            last = "exclude" if is_exclude else "include"
    return last or "nomatch"


def is_sensitive(
    path: str | PurePath,
    rules: list[tuple[str, bool]],
) -> bool:
    """path が機密パターンに該当するか判定。

    1. basename を last-match-wins で評価。
       - include 決着 → True
       - exclude 決着 → False (basename 単位の明示除外を優先)
       - nomatch → parts へ fall through
    2. 親 dir 名を順に評価し、どれか 1 つでも include 決着なら True。
    3. どこにもマッチしなければ False。
    """
    if not rules:
        return False

    p = PurePath(path)
    basename = p.name

    basename_verdict = _last_match_verdict(basename, rules)
    if basename_verdict == "include":
        return True
    if basename_verdict == "exclude":
        return False

    for part in p.parts[:-1]:
        if _last_match_verdict(part, rules) == "include":
            return True

    return False
