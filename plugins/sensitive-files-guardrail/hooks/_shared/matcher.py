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
import re
from fnmatch import translate
from pathlib import PurePath

# パターン -> compiled regex のキャッシュ (0.21.0)。
#
# 0.20.0 までは ``fnmatch.fnmatchcase`` を直接呼んでいたが、その内部
# ``fnmatch._compile_pattern`` の ``lru_cache`` は **maxsize が Python
# バージョン依存** (3.9 = 256 / 3.11+ = 32768) で plugin 側から制御できない。
# ``_last_match_verdict`` は rules 全件を operand ごとに走査するため、
# rules 件数が maxsize を超えると **operand 数 x rule 数のオーダーで正規表現が
# 再コンパイル**され、破局的に遅くなる。
#
# 実測 (system python 3.9.6 / 50 operands):
#   256 rules ->    11.0 ms
#   306 rules ->   338.5 ms   (31 倍のジャンプ)
#   856 rules -> 5,380.0 ms
# 同条件の 3.11+ は 856 rules でも 34.3 ms で線形。
#
# 到達経路は「多数プロジェクトへの分散」ではなく **1 つの長寿命 repo が自分の
# ``[project:]`` セクションに 250 件超の承認除外を蓄積する**こと — 本 plugin の
# 恒久除外レシピが誘導する使い方そのもの。
#
# 自前のキャッシュを持つことで Python バージョンへの依存を断つ。
#
# **上限を設けない** — 上限付き LRU では「rules 件数 > maxsize」で同じ崖が
# 再現するため。初版は maxsize=8192 にしていたが、3.11+ の fnmatch (32768) より
# 小さいので **8,192 件を超える rules ではむしろ退行**した
# (実測: 8,002 rules / 10 paths = 139.7ms に対し 8,202 rules = 4,884.3ms)。
# 「rule 数を包含する十分大きな値」を選ぶ方針自体が、上流の rule 数に依存する
# 調整パラメータを増やすだけで筋が悪い。
#
# 上限なしで安全な理由: hook プロセスは **1 ツール呼び出しで終了する短命プロセス**
# で、rules は起動時に patterns ファイルから 1 度読むだけ。したがって dict の
# エントリ数は「patterns ファイルの行数 x case 変種」で有界であり、
# 長時間走るプロセスのような無制限成長は起きない。
_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def _compiled(pattern: str) -> "re.Pattern[str]":
    """``fnmatchcase`` と同じ意味論の compiled regex を返す (キャッシュ付き)。

    ``fnmatch.translate`` は ``\\Z`` 終端付きの完全一致パターンを返すため、
    ``.match()`` で ``fnmatchcase`` と等価になる。
    """
    hit = _PATTERN_CACHE.get(pattern)
    if hit is None:
        hit = re.compile(translate(pattern))
        _PATTERN_CACHE[pattern] = hit
    return hit


def _fnmatch_cached(name: str, pattern: str) -> bool:
    """``fnmatchcase(name, pattern)`` と等価 (自前キャッシュ経由)。"""
    return _compiled(pattern).match(name) is not None


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
        if _fnmatch_cached(target, pat):
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
