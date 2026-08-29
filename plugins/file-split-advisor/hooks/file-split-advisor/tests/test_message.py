"""message.py: additionalContext 文面組み立てのテスト。"""
from __future__ import annotations

import unittest
from pathlib import Path

import _testutil  # noqa: F401

import message
from judge import Verdict
from metrics import Metrics


def _metrics(
    line_count=300,
    def_count=0,
    import_category_count=0,
    import_categories=(),
    control_flow_density=0.1,
    vague_filename=False,
) -> Metrics:
    return Metrics(
        line_count=line_count,
        def_count=def_count,
        import_category_count=import_category_count,
        import_categories=import_categories,
        control_flow_density=control_flow_density,
        vague_filename=vague_filename,
    )


def _verdict(
    tier="review",
    should_emit=True,
    signals=(),
    thresholds=None,
    applied_multipliers=None,
    scale=1.0,
) -> Verdict:
    """test_message.py 専用の Verdict ファクトリ。

    Verdict にフィールドが増えてもここだけ直せばよいように、テストケース側は
    このファクトリ経由でのみ Verdict を組み立てる。
    """
    if thresholds is None:
        thresholds = {"note": 150, "review": 300, "warn": 500, "strong": 800}
    if applied_multipliers is None:
        applied_multipliers = {"language": 1.0, "role": 1.0, "declarative": 1.0}
    return Verdict(
        tier=tier,
        should_emit=should_emit,
        signals=signals,
        thresholds=thresholds,
        applied_multipliers=applied_multipliers,
        scale=scale,
    )


class TestFormatMultiplier(unittest.TestCase):
    def test_integral_value_gets_one_decimal(self):
        self.assertEqual(message._format_multiplier(1.0), "1.0")

    def test_two_decimal_value_preserved(self):
        self.assertEqual(message._format_multiplier(1.15), "1.15")

    def test_one_decimal_values_preserved(self):
        self.assertEqual(message._format_multiplier(1.5), "1.5")
        self.assertEqual(message._format_multiplier(1.6), "1.6")


class TestSignalCountZeroFallback(unittest.TestCase):
    """宣言的コードの推測は実際に緩和が適用されたときだけ表示する。"""

    def test_declarative_relaxation_applied_shows_declarative_guess(self):
        v = _verdict(
            tier="review",
            signals=(),
            applied_multipliers={"language": 1.0, "role": 1.0, "declarative": 1.6},
        )
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics())
        self.assertIn("宣言的なコード", text)

    def test_declarative_relaxation_not_applied_omits_declarative_guess(self):
        # bash_handler.py の回帰ケース: control_flow_density が高く (緩和不適用)
        # でも warn tier は signal 数によらず emit するため、シグナル 0 個で
        # 出力されうる。以前はこのケースでも「宣言的なコードの可能性」と
        # 誤って表示していた。
        v = _verdict(
            tier="warn",
            signals=(),
            applied_multipliers={"language": 1.0, "role": 1.0, "declarative": 1.0},
        )
        text = message.build(
            Path("bash_handler.sh"),
            "shell",
            "normal",
            v,
            _metrics(control_flow_density=0.121),
        )
        self.assertNotIn("宣言的なコード", text)
        self.assertIn("検出された構造シグナル: なし (行数のみが基準に該当)", text)

    def test_signals_present_takes_precedence_over_fallback(self):
        v = _verdict(tier="review", signals=("vague_filename",))
        text = message.build(Path("utils.py"), "python", "normal", v, _metrics())
        self.assertIn("検出シグナル:", text)
        self.assertNotIn("検出された構造シグナル: なし", text)


class TestMultiplierBreakdown(unittest.TestCase):
    def test_language_multiplier_always_shown_even_when_neutral(self):
        v = _verdict(applied_multipliers={"language": 1.0, "role": 1.0, "declarative": 1.0})
        text = message.build(Path("foo.py"), "generic", "normal", v, _metrics())
        self.assertIn("(generic 1.0)", text)

    def test_declarative_multiplier_appended_when_applied(self):
        v = _verdict(
            tier="review",
            applied_multipliers={"language": 1.0, "role": 1.0, "declarative": 1.6},
            thresholds={"note": 240, "review": 480, "warn": 800, "strong": 1280},
        )
        text = message.build(Path("foo.py"), "generic", "normal", v, _metrics())
        self.assertIn("(generic 1.0 × 宣言的 1.6)", text)
        self.assertIn("review=480", text)

    def test_non_neutral_language_multiplier_shown(self):
        v = _verdict(applied_multipliers={"language": 1.5, "role": 1.0, "declarative": 1.0})
        text = message.build(Path("Foo.java"), "java", "normal", v, _metrics())
        self.assertIn("(java 1.5)", text)

    def test_role_multiplier_not_duplicated_in_breakdown(self):
        # role (test 係数) は breakdown に含めない — role_note が別途表示する。
        v = _verdict(applied_multipliers={"language": 1.0, "role": 1.6, "declarative": 1.0})
        text = message.build(Path("foo_test.py"), "python", "test", v, _metrics())
        self.assertIn("(test: 閾値 1.6倍)", text)
        self.assertNotIn("test 1.6", text)
        self.assertIn("(python 1.0)", text)


class TestScaleNote(unittest.TestCase):
    """P2-1 回帰: FILE_SPLIT_ADVISOR_SCALE != 1.0 のとき、目安に表示される
    倍率がどこから来たか (全体倍率) を明示する。scale は breakdown
    (applied_multipliers) には含めないが、role_note と同じ形の専用
    parenthetical で別枠表示する。"""

    def test_scale_note_appended_when_scale_is_not_neutral(self):
        # レビュー PROBE 3 の再現値 (base warn=500/strong=800 に scale=2.0):
        # 目安 warn=1000 strong=1600 (python 1.0) (全体 2.0倍)。
        v = _verdict(
            tier="warn",
            thresholds={"note": 300, "review": 600, "warn": 1000, "strong": 1600},
            scale=2.0,
        )
        text = message.build(
            Path("foo.py"), "python", "normal", v, _metrics(line_count=1200)
        )
        self.assertIn("warn=1000", text)
        self.assertIn("(全体 2.0倍)", text)

    def test_scale_note_omitted_when_scale_is_neutral(self):
        v = _verdict(tier="warn", scale=1.0)
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics())
        self.assertNotIn("全体", text)

    def test_scale_note_combines_with_role_note(self):
        # role_note (test: 閾値 1.6倍) と scale note (全体 N倍) は両立し、
        # 互いを上書きしない。
        v = _verdict(
            tier="warn",
            applied_multipliers={"language": 1.0, "role": 1.6, "declarative": 1.0},
            scale=0.5,
        )
        text = message.build(Path("foo_test.py"), "python", "test", v, _metrics())
        self.assertIn("(全体 0.5倍)", text)
        self.assertIn("(test: 閾値 1.6倍)", text)


class TestPartialThresholds(unittest.TestCase):
    """P3-5 回帰: 0.1.0 は ``if tier in verdict.thresholds`` という防御ガードを
    持っていたが、0.3.0 の ``_display_tiers`` 導入で無条件の
    ``verdict.thresholds[tier]`` になり消えていた。judge.judge() が返す
    Verdict は現状 4 tier 全てを埋めるため到達不能だが、将来 judge 側が
    部分的な thresholds を返すようになったときに KeyError で fail-open
    (メモが消える) にならないよう、表示側にも防御を戻す。
    """

    def test_missing_display_tier_key_does_not_raise(self):
        v = _verdict(tier="review", thresholds={"review": 300})  # note/warn/strong 欠如
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics())
        self.assertIn("review=300", text)
        self.assertNotIn("warn=", text)
        self.assertNotIn("note=", text)


class TestDisplayTiers(unittest.TestCase):
    """目安は判定 tier + 隣接 tier を表示する (以前は review/warn 固定)。"""

    def test_note_tier_shows_note_and_review(self):
        v = _verdict(tier="note")
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics(line_count=200))
        self.assertIn("note=150", text)
        self.assertIn("review=300", text)
        self.assertNotIn("warn=", text)
        self.assertNotIn("strong=", text)

    def test_review_tier_shows_review_and_warn(self):
        v = _verdict(tier="review")
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics())
        self.assertIn("review=300", text)
        self.assertIn("warn=500", text)
        self.assertNotIn("note=", text)
        self.assertNotIn("strong=", text)

    def test_warn_tier_shows_warn_and_strong(self):
        v = _verdict(tier="warn")
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics(line_count=550))
        self.assertIn("warn=500", text)
        self.assertIn("strong=800", text)
        self.assertNotIn("note=", text)
        self.assertNotIn("review=", text)

    def test_strong_tier_shows_warn_and_strong(self):
        # strong には「1 つ上」が無いため、1 つ下 (warn) + 自身を表示する。
        v = _verdict(tier="strong")
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics(line_count=900))
        self.assertIn("warn=500", text)
        self.assertIn("strong=800", text)
        self.assertNotIn("note=", text)
        self.assertNotIn("review=", text)


if __name__ == "__main__":
    unittest.main()
