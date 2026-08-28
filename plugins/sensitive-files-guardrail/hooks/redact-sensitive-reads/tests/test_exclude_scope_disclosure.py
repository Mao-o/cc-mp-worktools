"""恒久除外レシピの影響範囲を開示していること (0.23.0)。

除外行は basename 単位で評価されるため、承認した 1 ファイルではなく
プロジェクト内の同名ファイル全部の保護が落ちる。しかも効果は Stop の報告に
留まらず Read / Bash / Edit / Write の保護そのものに及ぶ。

範囲を狭める作業とは別に、**黙った過剰付与を informed consent に変える**ため
案内文がその 2 点を述べていることを固定する。
"""
from __future__ import annotations

import unittest

from _shared.patterns import EXCLUDE_SCOPE_WARNING
from core.messages import _exclude_hint


class TestExcludeScopeWarningText(unittest.TestCase):
    def test_states_basename_granularity(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        self.assertIn("basename 単位", text)
        self.assertIn("すべて", text)

    def test_states_directory_descendants_are_affected(self):
        """is_sensitive は親ディレクトリ名も評価するため、同名ディレクトリの
        配下も保護から外れる。開示文がその事実に言及していること。"""
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        self.assertIn("同名ディレクトリの配下", text)
        self.assertIn("配下", text)
        # 過大主張しないこと: basename が独立一致すれば保護は残る
        self.assertIn("別の include 行", text)

    def test_states_cross_project_operand_scope(self):
        """[project:] は rule の読込先を決めるだけで、読み込まれた後は
        セッションが触る絶対パス全部に効く。開示文がその事実に言及すること。"""
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        self.assertIn("絶対パス全部", text)
        self.assertIn("他プロジェクト", text)

    def test_states_protection_not_just_reporting(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        # 「報告が止まる」だけでなく「保護が外れる」ことを述べていること
        self.assertIn("保護そのもの", text)
        for tool in ("Read", "Bash", "Edit", "Write"):
            self.assertIn(tool, text)

    def test_says_single_file_cannot_be_scoped(self):
        text = EXCLUDE_SCOPE_WARNING.format(scope="X")
        self.assertIn("1 ファイルだけの除外は不可", text)


class TestGeneratedRuleIsLiteral(unittest.TestCase):
    """実ファイル名から作るレシピは fnmatch の literal にすること (Codex R9)。

    ``key[1].pem`` をそのまま ``!key[1].pem`` と出すと、fnmatch では文字クラスに
    なるため **その file 自身にはマッチせず** ``key1.pem`` 等を巻き込む。
    承認したファイルの保護が残り、無関係なファイルの保護が外れるので、
    影響範囲の開示と正反対の結果になる。
    """

    def test_escape_glob_wraps_metacharacters(self):
        from _shared.patterns import escape_glob

        self.assertEqual(escape_glob("key[1].pem"), "key[[]1[]].pem")
        self.assertEqual(escape_glob("a*b.pem"), "a[*]b.pem")
        self.assertEqual(escape_glob("q?.pem"), "q[?].pem")

    def test_escape_glob_leaves_plain_names_untouched(self):
        """メタ文字が無ければ見た目を変えない (通常のレシピは従来どおり)。"""
        from _shared.patterns import escape_glob

        for name in (".env", ".envrc", "normal.pem", "id_rsa"):
            self.assertEqual(escape_glob(name), name)

    def test_escaped_rule_actually_excludes_the_file(self):
        """escape 後の rule が対象ファイルを除外し、他を巻き込まないこと。"""
        from _shared.matcher import is_sensitive
        from _shared.patterns import escape_glob

        rules = [("*.pem", False)]
        excluded = rules + [(escape_glob("key[1].pem"), True)]
        self.assertFalse(is_sensitive("/r/key[1].pem", excluded))
        self.assertTrue(is_sensitive("/r/key1.pem", excluded))

    def test_stop_recipe_escapes(self):
        from _shared.patterns import exclude_recipe_lines

        lines = exclude_recipe_lines(["key[1].pem"])
        self.assertIn("!key[[]1[]].pem", lines)

    def test_edit_read_hint_escapes(self):
        """実ファイル名を扱う経路 (literal_name=True) は escape する。"""
        hint = _exclude_hint("key[1].pem", literal_name=True)
        self.assertIn("!key[[]1[]].pem", hint)

    def test_bash_glob_operand_is_not_escaped(self):
        """Bash の operand はユーザーが書いた glob でありうるのでそのまま。

        ``cat .env*`` の deny で ``!.env[*]`` を出すと、意図した glob 除外が
        「``.env*`` という名前のファイル」の除外に化ける。
        """
        hint = _exclude_hint(".env*")
        self.assertIn("!.env*", hint)
        self.assertNotIn("!.env[*]", hint)


class TestExcludeHintCarriesWarning(unittest.TestCase):
    """Read / Bash の deny reason に載る除外案内が開示を含むこと。"""

    def test_named_basename_hint_includes_scope(self):
        hint = _exclude_hint(".env")
        self.assertIn("basename 単位", hint)
        self.assertIn("保護そのもの", hint)
        # 対象の basename が範囲説明に埋め込まれていること
        self.assertIn("`.env` という名前のファイル", hint)

    def test_generic_hint_includes_scope(self):
        hint = _exclude_hint("")
        self.assertIn("basename 単位", hint)
        self.assertIn("同名のファイル", hint)

    def test_hint_still_requires_user_approval(self):
        """既存の「承認なしに追加しない」規律が消えていないこと。"""
        self.assertIn("承認なしに自分で追加しないこと", _exclude_hint(".env"))

    def test_hint_does_not_claim_project_containment(self):
        """「このプロジェクト内」という誤った限定が残っていないこと。"""
        self.assertNotIn("このプロジェクト内", _exclude_hint(".env"))

    def test_warning_survives_a_full_reason_budget(self):
        """minimal info が大きくても警告文が truncate されないこと (Codex R8 P1)。

        レシピ (!.env) を見せながら影響範囲を隠すのは informed consent の逆。
        可変長側を先に削って案内の場所を確保する。
        """
        from core.messages import _join_with_exclude_hint
        from core.output import MAX_REASON_BYTES

        # 予算を大きく超える本文を与える
        lines = [f"line {i}: " + "x" * 200 for i in range(60)]
        reason = _join_with_exclude_hint(lines, ".env")

        self.assertLessEqual(len(reason.encode("utf-8")), MAX_REASON_BYTES)
        # 案内は全文残る (レシピと警告の両方)
        self.assertIn("`!.env`", reason)
        self.assertIn("basename 単位", reason)
        self.assertIn("保護そのもの", reason)
        self.assertIn("承認なしに自分で追加しないこと", reason)
        # 削られたのは本文側
        self.assertIn("[truncated]", reason)

    def test_short_body_is_not_truncated(self):
        """本文が小さいときは何も削らないこと。"""
        from core.messages import _join_with_exclude_hint

        reason = _join_with_exclude_hint(["note: short"], ".env")
        self.assertNotIn("[truncated]", reason)
        self.assertIn("note: short", reason)

    def test_hint_does_not_claim_report_only(self):
        """「報告されなくなります」だけで済ませる旧文面に戻っていないこと。"""
        hint = _exclude_hint(".env")
        self.assertNotIn("で報告されなくなります", hint)


if __name__ == "__main__":
    unittest.main()
