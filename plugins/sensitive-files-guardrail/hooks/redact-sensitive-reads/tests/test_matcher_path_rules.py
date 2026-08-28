"""path 形 rule (``/`` を含む rule) の root 相対評価 (0.24.0)。

0.23.0 まで ``is_sensitive`` は basename と ``pathlib.parts`` しか見ず、
``!config/prod.pem`` のような rule は格納はされても**一度もマッチしなかった**。
恒久除外レシピを「承認した 1 ファイル」に絞れるよう、``/`` を含む rule だけを
project root 相対 path と比較する階層を足す。

意味論は fnmatch の継承ではなく gitignore 準拠で**明示的に**決めた:

- 単独 ``*`` / ``?`` / ``[...]`` は ``/`` を跨がない
- ``**`` は ``/`` を跨ぐ (``**/`` 先頭 / ``/**`` 末尾 / ``/**/`` 中間)
- 先頭 ``/`` (と ``./``) は root アンカー。``/`` を途中に含む rule は常に root 相対
  (= アンカー付き) で、末尾 ``/`` だけの rule (``fixtures/``) は任意の深さに効く
- 末尾 ``/`` はディレクトリ (配下すべて)。末尾 ``/`` が無い rule は **その path
  1 本だけ**に一致し、同名ディレクトリの配下には及ばない (gitignore との相違点)

このファイルは **修正前に失敗する**ことを確認してから実装した
(``git stash push -- hooks/_shared/matcher.py`` で実装だけ退避して negative control)。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from _shared.matcher import is_sensitive, root_relative
from _shared.patterns import escape_glob

from test_matcher import DEFAULT_RULES

ROOT = "/r"


def _with(*extra: tuple[str, bool]) -> list[tuple[str, bool]]:
    return DEFAULT_RULES + list(extra)


class TestRootRelative(unittest.TestCase):
    """``root_relative`` は lexical (symlink / 実在確認なし) で root 相対 path を返す。"""

    def test_absolute_under_root(self):
        self.assertEqual(root_relative("/r/a/b.pem", "/r"), "a/b.pem")
        # root 側の末尾 / と path 側の冗長な要素は正規化される
        self.assertEqual(root_relative("/r/a/./b.pem", "/r/"), "a/b.pem")

    def test_root_itself_is_none(self):
        self.assertIsNone(root_relative("/r", "/r"))
        self.assertIsNone(root_relative("/r/", "/r"))

    def test_outside_root_is_none(self):
        self.assertIsNone(root_relative("/other/a.pem", "/r"))
        # 文字列 prefix の罠: /rx は /r 配下ではない
        self.assertIsNone(root_relative("/rx/a.pem", "/r"))

    def test_relative_path_is_taken_as_root_relative(self):
        # Stop hook は git ls-files の相対 path を root 相対に組み立てて渡す
        self.assertEqual(root_relative("a/b.pem", "/r"), "a/b.pem")
        self.assertEqual(root_relative("./a/b.pem", "/r"), "a/b.pem")

    def test_relative_path_escaping_root_is_none(self):
        self.assertIsNone(root_relative("../x.pem", "/r"))
        self.assertIsNone(root_relative("a/../../x.pem", "/r"))

    def test_no_root_is_none(self):
        self.assertIsNone(root_relative("/r/a.pem", None))
        self.assertIsNone(root_relative("/r/a.pem", ""))


class TestPathRuleBasics(unittest.TestCase):
    def test_exact_path_exclude_hits_only_that_file(self):
        rules = _with(("config/prod.pem", True))
        self.assertFalse(is_sensitive("/r/config/prod.pem", rules, root=ROOT))
        # 同名の別ファイルは *.pem のまま保護される (これが本機能の目的)
        self.assertTrue(is_sensitive("/r/other/prod.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/prod.pem", rules, root=ROOT))
        # 途中に / を含む rule は root アンカー (gitignore 準拠)
        self.assertTrue(is_sensitive("/r/x/config/prod.pem", rules, root=ROOT))

    def test_without_root_path_rules_are_inert(self):
        # root 不明 (非 git ディレクトリ等) では 0.23.0 までと同じ挙動
        rules = _with(("config/prod.pem", True))
        self.assertTrue(is_sensitive("/r/config/prod.pem", rules))
        self.assertTrue(is_sensitive("/r/config/prod.pem", rules, root=None))

    def test_outside_root_path_rules_are_inert(self):
        rules = _with(("config/prod.pem", True))
        self.assertTrue(
            is_sensitive("/elsewhere/config/prod.pem", rules, root=ROOT)
        )

    def test_relative_subject_is_root_relative(self):
        rules = _with(("config/prod.pem", True))
        self.assertFalse(is_sensitive("config/prod.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("sub/config/prod.pem", rules, root=ROOT))

    def test_root_level_file_needs_leading_slash_to_stay_path_form(self):
        # root 直下のファイルは相対 path に / が無いので、先頭 / を付けて初めて
        # path 形になる。`.env` だけだと basename 形 (同名すべて) — Codex R1 P1
        rules = _with(("/.env", True))
        self.assertFalse(is_sensitive("/r/.env", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/sub/.env", rules, root=ROOT))
        self.assertTrue(is_sensitive("/elsewhere/.env", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/.env", rules))  # root 不明なら効かない
        rules_b = _with((".env", True))
        self.assertFalse(is_sensitive("/r/sub/.env", rules_b, root=ROOT))
        self.assertFalse(is_sensitive("/elsewhere/.env", rules_b, root=ROOT))

    def test_leading_slash_and_dot_slash_anchor(self):
        for pat in ("/config/prod.pem", "./config/prod.pem"):
            with self.subTest(pat=pat):
                rules = _with((pat, True))
                self.assertFalse(is_sensitive("/r/config/prod.pem", rules, root=ROOT))
                self.assertTrue(is_sensitive("/r/x/config/prod.pem", rules, root=ROOT))

    def test_path_include_rule(self):
        rules = _with(("secrets/**", False))
        self.assertTrue(is_sensitive("/r/secrets/a.txt", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/secrets/d/e/f.txt", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/other/a.txt", rules, root=ROOT))
        # 末尾 /** はディレクトリ自身 (と同名ファイル) には一致しない
        self.assertFalse(is_sensitive("/r/secrets", rules, root=ROOT))

    def test_file_rule_does_not_cover_directory_descendants(self):
        # gitignore との相違点: 末尾 / が無い rule は path 1 本だけ
        rules = [("certs/private", False)]
        self.assertTrue(is_sensitive("/r/certs/private", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/certs/private/k.txt", rules, root=ROOT))
        # 配下を含めたいときは末尾 / を書く
        rules_dir = [("certs/private/", False)]
        self.assertTrue(is_sensitive("/r/certs/private/k.txt", rules_dir, root=ROOT))
        self.assertFalse(is_sensitive("/r/certs/private", rules_dir, root=ROOT))


class TestPathRuleWildcards(unittest.TestCase):
    def test_single_star_does_not_cross_slash(self):
        rules = _with(("fixtures/*.pem", True))
        self.assertFalse(is_sensitive("/r/fixtures/a.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/fixtures/deep/a.pem", rules, root=ROOT))

    def test_double_star_crosses_slash(self):
        rules = _with(("fixtures/**/*.pem", True))
        # /**/ は 0 個以上のディレクトリ
        self.assertFalse(is_sensitive("/r/fixtures/a.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/fixtures/deep/a.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/fixtures/d/e/f/a.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/other/a.pem", rules, root=ROOT))

    def test_leading_double_star_matches_any_depth(self):
        rules = _with(("**/fixtures/*.pem", True))
        self.assertFalse(is_sensitive("/r/fixtures/x.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/a/b/fixtures/x.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/a/fixturesx/x.pem", rules, root=ROOT))

    def test_trailing_double_star_matches_everything_inside(self):
        rules = _with(("fixtures/**", True))
        self.assertFalse(is_sensitive("/r/fixtures/a.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/fixtures/d/e.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/fixturesx/a.pem", rules, root=ROOT))

    def test_inner_double_star_is_treated_as_single_star(self):
        # gitignore: "Other consecutive asterisks are considered regular asterisks"
        rules = _with(("cfg/a**b.pem", True))
        self.assertFalse(is_sensitive("/r/cfg/aXb.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/cfg/a/b.pem", rules, root=ROOT))

    def test_question_mark_does_not_cross_slash(self):
        rules = _with(("k?y/a.pem", True))
        self.assertFalse(is_sensitive("/r/key/a.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/k/y/a.pem", rules, root=ROOT))

    def test_char_class(self):
        rules = _with(("certs/[ab].pem", True))
        self.assertFalse(is_sensitive("/r/certs/a.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/certs/b.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/certs/c.pem", rules, root=ROOT))

    def test_negated_char_class_does_not_match_slash(self):
        rules = _with(("c[!x]rts/a.pem", True))
        self.assertFalse(is_sensitive("/r/certs/a.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/c/rts/a.pem", rules, root=ROOT))

    def test_unterminated_bracket_is_literal(self):
        rules = _with(("certs/k[1.pem", True))
        self.assertFalse(is_sensitive("/r/certs/k[1.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/certs/k1.pem", rules, root=ROOT))


class TestDirectoryRules(unittest.TestCase):
    def test_trailing_slash_only_matches_at_any_depth(self):
        rules = _with(("fixtures/", True))
        self.assertFalse(is_sensitive("/r/fixtures/x.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/a/fixtures/x.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/a/fixtures/d/x.pem", rules, root=ROOT))
        # ディレクトリではなく同名ファイル / 前方一致の別名には効かない
        self.assertTrue(is_sensitive("/r/fixtures.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/fixtures2/x.pem", rules, root=ROOT))

    def test_anchored_directory(self):
        for pat in ("/fixtures/", "./fixtures/"):
            with self.subTest(pat=pat):
                rules = _with((pat, True))
                self.assertFalse(is_sensitive("/r/fixtures/x.pem", rules, root=ROOT))
                self.assertTrue(is_sensitive("/r/a/fixtures/x.pem", rules, root=ROOT))

    def test_nested_directory_is_anchored(self):
        rules = _with(("certs/fixtures/", True))
        self.assertFalse(is_sensitive("/r/certs/fixtures/x.pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/a/certs/fixtures/x.pem", rules, root=ROOT))

    def test_directory_exclude_overrides_parts_include(self):
        # venv を .env に作った形: 0.23.0 までは !.env (basename 形) で外していた。
        # path 形 !.env/ でも同じ効果で、しかも他の .env ファイルの保護は残る
        rules = _with((".env/", True))
        self.assertFalse(is_sensitive("/r/.env/bin/activate", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/.env", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/sub/.env", rules, root=ROOT))
        # 配下が別の include 行に単独一致すればそちらが優先 (last-match-wins)
        self.assertFalse(is_sensitive("/r/.env/id_rsa", rules, root=ROOT))
        rules2 = rules + [("id_rsa*", False)]
        self.assertTrue(is_sensitive("/r/.env/id_rsa", rules2, root=ROOT))


class TestOrderingAndTiers(unittest.TestCase):
    def test_last_match_wins_across_forms(self):
        # basename 形と path 形は 1 本のリストとして出現順に評価する
        rules = [("*.pem", False), ("config/prod.pem", True), ("prod.pem", False)]
        self.assertTrue(is_sensitive("/r/config/prod.pem", rules, root=ROOT))
        rules2 = [("config/prod.pem", True), ("*.pem", False)]
        self.assertTrue(is_sensitive("/r/config/prod.pem", rules2, root=ROOT))
        rules3 = [("*.pem", False), ("prod.pem", False), ("config/prod.pem", True)]
        self.assertFalse(is_sensitive("/r/config/prod.pem", rules3, root=ROOT))

    def test_parts_tier_ignores_path_rules(self):
        # 親 dir 名の評価は basename 形だけで行う。path 形は parts に影響しない
        rules = [(".env", False), ("config/prod.pem", True)]
        self.assertTrue(is_sensitive("/r/.env/leak.txt", rules, root=ROOT))
        rules2 = [("config/", False)]
        # path 形 include は rel 全体で評価されるので parts 経由ではなく直接一致
        self.assertTrue(is_sensitive("/r/config/leak.txt", rules2, root=ROOT))
        self.assertFalse(is_sensitive("/r/config/leak.txt", rules2))

    def test_parts_false_still_evaluates_path_rules(self):
        # Bash operand (parts=False) でも path 形は効く — レシピが Bash で
        # 効かなければ絞る意味が無い
        rules = _with(("config/prod.pem", True))
        self.assertFalse(
            is_sensitive("/r/config/prod.pem", rules, parts=False, root=ROOT)
        )
        self.assertTrue(
            is_sensitive("/r/other/prod.pem", rules, parts=False, root=ROOT)
        )

    def test_path_rule_never_matches_basename_or_parts(self):
        rules = [("config/prod.pem", False)]
        self.assertFalse(is_sensitive("/r/config/prod.pem", rules))
        self.assertFalse(is_sensitive("prod.pem", rules))
        self.assertFalse(is_sensitive("/r/config/prod.pem/x", rules, root=ROOT))

    def test_empty_rules(self):
        self.assertFalse(is_sensitive("/r/config/prod.pem", [], root=ROOT))


class TestCaseSensitivity(unittest.TestCase):
    def setUp(self):
        self._env_patcher = mock.patch.dict(os.environ, clear=False)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        os.environ.pop("SFG_CASE_SENSITIVE", None)

    def test_case_insensitive_by_default(self):
        rules = _with(("config/prod.pem", True))
        self.assertFalse(is_sensitive("/r/CONFIG/Prod.PEM", rules, root=ROOT))

    def test_opt_out(self):
        os.environ["SFG_CASE_SENSITIVE"] = "1"
        rules = _with(("config/prod.pem", True))
        # basename は *.pem に一致するが、path 形 rule は case が違うので効かない
        self.assertTrue(is_sensitive("/r/CONFIG/Prod.pem", rules, root=ROOT))
        self.assertFalse(is_sensitive("/r/config/prod.pem", rules, root=ROOT))


class TestGeneratedRecipeRoundTrip(unittest.TestCase):
    """レシピ生成側 (``escape_glob``) と評価側が同じ literal 化で噛み合うこと。"""

    def test_escaped_path_rule_hits_only_itself(self):
        rule = escape_glob("config/k[1].pem")
        self.assertEqual(rule, "config/k[[]1[]].pem")
        rules = _with((rule, True))
        self.assertFalse(is_sensitive("/r/config/k[1].pem", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/config/k1.pem", rules, root=ROOT))

    def test_plain_path_is_untouched(self):
        self.assertEqual(escape_glob("config/prod.pem"), "config/prod.pem")

    def test_generated_rule_is_always_path_form(self):
        """レシピ生成が返す rule は root 直下でも path 形であること (Codex R1 P1)。

        root 相対 path が ``.env`` (``/`` 無し) のとき ``!.env`` と書くと basename
        形に化け、「この 1 ファイルだけ」の案内と矛盾して同名すべての保護が外れる。
        """
        from _shared.matcher import is_path_rule
        from _shared.patterns import path_rule_for

        for rel in (".env", "id_rsa", "key[1].pem", "sub/.env", "a/b/c.pem"):
            with self.subTest(rel=rel):
                self.assertTrue(is_path_rule(path_rule_for(rel)))
        self.assertEqual(path_rule_for(".env"), "/.env")
        self.assertEqual(path_rule_for("sub/.env"), "sub/.env")
        self.assertEqual(path_rule_for(""), "")
        rules = _with((escape_glob(path_rule_for(".env")), True))
        self.assertFalse(is_sensitive("/r/.env", rules, root=ROOT))
        self.assertTrue(is_sensitive("/r/sub/.env", rules, root=ROOT))
        rules2 = _with((escape_glob(path_rule_for("key[1].pem")), True))
        self.assertFalse(is_sensitive("/r/key[1].pem", rules2, root=ROOT))
        self.assertTrue(is_sensitive("/r/key1.pem", rules2, root=ROOT))
        self.assertTrue(is_sensitive("/r/sub/key[1].pem", rules2, root=ROOT))


class TestBackwardCompatibilityFloor(unittest.TestCase):
    """既定 rules に ``/`` を含む行が無い限り、root の有無で verdict は変わらない。"""

    CORPUS = [
        ".env", "/r/.env", "/r/sub/.env", "/r/.env.production", "/r/.env.example",
        "/r/id_rsa", "/r/id_rsa.pub", "/r/certs/a.pem", "/r/cert.pem.template",
        "/r/credentials.json", "/r/credentials.example.json", "/r/README.md",
        "/r/.env/leak.txt", "/r/.env/bin/activate", "/r/x/s/.env/X/p",
        "/elsewhere/.env", "sub/.env", "sub/keys/id_ed25519", "settings.local.json",
        "/r/app.secrets.yaml", "/r/service-account-x.json", "/r/.npmrc",
    ]

    def test_no_slash_in_default_patterns(self):
        patterns_file = (
            FIXTURES.parent.parent.parent / "check-sensitive-files" / "patterns.txt"
        )
        for line in patterns_file.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertNotIn("/", stripped, f"path rule in defaults: {stripped}")

    def test_same_verdict_with_and_without_root(self):
        for path in self.CORPUS:
            for parts in (True, False):
                with self.subTest(path=path, parts=parts):
                    self.assertEqual(
                        is_sensitive(path, DEFAULT_RULES, parts=parts),
                        is_sensitive(path, DEFAULT_RULES, parts=parts, root=ROOT),
                    )


if __name__ == "__main__":
    unittest.main()
