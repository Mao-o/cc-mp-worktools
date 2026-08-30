"""matcher のパターンキャッシュ (0.23.0)。

``fnmatch._compile_pattern`` の ``lru_cache`` は maxsize が Python バージョン依存
(3.9 = 256 / 3.11+ = 32768) で plugin から制御できない。``_last_match_verdict`` は
rules 全件を operand ごとに走査するため、rules が maxsize を超えると
operand 数 x rule 数のオーダーで再コンパイルが起き破局的に遅くなる。

自前キャッシュに置き換えたので、(1) 意味論が ``fnmatchcase`` と等価であること
(2) キャッシュが実際に効いていること (3) rules を増やしても線形の範囲に収まること
を固定する。
"""
from __future__ import annotations

import time
import unittest
from fnmatch import fnmatchcase

from _testutil import FIXTURES  # noqa: F401

from _shared.matcher import (
    _PATTERN_CACHE,
    _compiled,
    _fnmatch_cached,
    is_sensitive,
)

# fnmatchcase との等価性を確認する (name, pattern) の組。
# glob メタ文字 (* ? [] [!]) と、パターン中のドット・記号を網羅する。
_EQUIV_CASES = [
    (".env", ".env"),
    (".env", ".env*"),
    (".env", ".env.*"),
    (".env.local", ".env.*"),
    (".envrc", "*.envrc"),
    (".envrc", ".envrc*"),
    (".env", ".e[n]v"),
    (".env", ".en?"),
    (".env", "[.]env"),
    (".env", "*"),
    ("id_rsa", "id_rsa*"),
    ("cert.pem", "*.pem"),
    ("cert.pem", "*.key"),
    ("a.b.c", "a.*.c"),
    ("file.txt", "[!f]*"),
    ("File.TXT", "file.txt"),
    ("", "*"),
    ("", ""),
    ("x", "??"),
    ("no-match", ".env"),
]


class TestFnmatchEquivalence(unittest.TestCase):
    """自前キャッシュ版が ``fnmatchcase`` と同じ答えを返すこと。"""

    def test_matches_fnmatchcase(self):
        for name, pattern in _EQUIV_CASES:
            with self.subTest(name=name, pattern=pattern):
                self.assertEqual(
                    _fnmatch_cached(name, pattern),
                    fnmatchcase(name, pattern),
                    f"fnmatchcase と不一致: {name!r} vs {pattern!r}",
                )


class TestPatternCacheIsEffective(unittest.TestCase):
    def test_repeated_pattern_compiles_once(self):
        _PATTERN_CACHE.clear()
        first = _compiled("*.env")
        for _ in range(10):
            _fnmatch_cached("some/name", "*.env")
        self.assertIs(_compiled("*.env"), first, "同一パターンが再コンパイルされた")
        self.assertEqual(len(_PATTERN_CACHE), 1)

    def test_cache_has_no_eviction_limit(self):
        """上限付き LRU だと「rules 件数 > maxsize」で同じ崖が再現するため、
        エントリを捨てない実装であることを pin する。

        初版は maxsize=8192 で、3.11+ の fnmatch (32768) より小さいために
        8,192 件超の rules でむしろ退行した (8,202 rules / 10 paths = 4.9 秒)。
        """
        _PATTERN_CACHE.clear()
        for i in range(9000):
            _compiled(f"pattern-{i}-*.cfg")
        self.assertEqual(len(_PATTERN_CACHE), 9000, "エントリが evict された")


class TestNoCliffAcrossRuleCounts(unittest.TestCase):
    """rules 件数を増やしても再コンパイル由来の急激な悪化が無いこと。

    wall-clock を使うが、修正前は system python 3.9.6 で 856 rules /
    50 operands が 5,380ms、修正後は 32ms だった。上限は修正後の実測から
    十分な余裕 (約 60 倍) を取り、環境差で flaky にならない値にする。
    """

    def test_many_rules_stay_within_budget(self):
        rules = [(".env", False), ("*.pem", False)]
        rules += [(f"custom-{i}-*.cfg", False) for i in range(850)]
        operands = [f"/repo/src/file{i}.txt" for i in range(50)]

        _PATTERN_CACHE.clear()
        start = time.perf_counter()
        for op in operands:
            is_sensitive(op, rules)
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertLess(
            elapsed_ms, 2000,
            f"850 rules x 50 operands が {elapsed_ms:.0f}ms — "
            "パターンキャッシュが効いていない可能性",
        )


if __name__ == "__main__":
    unittest.main()
