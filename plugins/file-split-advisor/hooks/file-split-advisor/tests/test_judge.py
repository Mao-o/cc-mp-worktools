"""judge.py: 閾値テーブル・tier/emit 判定のテスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

import judge
from metrics import Metrics


def _metrics(
    line_count=0,
    def_count=0,
    import_category_count=0,
    import_categories=(),
    control_flow_density=0.1,  # デフォルトは宣言的緩和が効かない値
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


class TestEffectiveThresholds(unittest.TestCase):
    def test_language_and_role_multiplier_combine(self):
        # python (0.7) x test (1.6) = 1.12
        v = judge.judge(_metrics(line_count=0), "python", "test")
        self.assertAlmostEqual(v.thresholds["note"], 150 * 0.7 * 1.6)
        self.assertAlmostEqual(v.thresholds["review"], 300 * 0.7 * 1.6)
        self.assertAlmostEqual(v.thresholds["warn"], 500 * 0.7 * 1.6)
        self.assertAlmostEqual(v.thresholds["strong"], 800 * 0.7 * 1.6)

    def test_declarative_relaxation_applied_when_density_low(self):
        m = _metrics(line_count=0, control_flow_density=0.01)  # < 0.02
        v = judge.judge(m, "typescript", "normal")
        self.assertAlmostEqual(v.thresholds["review"], 300 * 1.0 * 1.0 * 1.6)

    def test_declarative_relaxation_not_applied_at_boundary(self):
        m = _metrics(line_count=0, control_flow_density=0.02)  # == 0.02, not < 0.02
        v = judge.judge(m, "typescript", "normal")
        self.assertAlmostEqual(v.thresholds["review"], 300 * 1.0 * 1.0)

    def test_unknown_language_uses_generic_multiplier(self):
        v = judge.judge(_metrics(line_count=0), "cobol", "normal")
        self.assertAlmostEqual(v.thresholds["review"], 300 * 1.0)


class TestTierBoundaries(unittest.TestCase):
    """半開区間: note <= x < review, review <= x < warn, ... (係数 1.0 相当の言語/role で確認)。"""

    def _tier_for(self, line_count: int) -> str:
        m = _metrics(line_count=line_count, control_flow_density=0.1)
        return judge.judge(m, "typescript", "normal").tier

    def test_below_note_is_ok(self):
        self.assertEqual(self._tier_for(149), "ok")

    def test_note_lower_bound_inclusive(self):
        self.assertEqual(self._tier_for(150), "note")

    def test_just_below_review_is_note(self):
        self.assertEqual(self._tier_for(299), "note")

    def test_review_lower_bound_inclusive(self):
        self.assertEqual(self._tier_for(300), "review")

    def test_just_below_warn_is_review(self):
        self.assertEqual(self._tier_for(499), "review")

    def test_warn_lower_bound_inclusive(self):
        self.assertEqual(self._tier_for(500), "warn")

    def test_just_below_strong_is_warn(self):
        self.assertEqual(self._tier_for(799), "warn")

    def test_strong_lower_bound_inclusive(self):
        self.assertEqual(self._tier_for(800), "strong")


class TestSignals(unittest.TestCase):
    def test_import_diversity_signal(self):
        m = _metrics(line_count=0, import_category_count=4)
        v = judge.judge(m, "typescript", "normal")
        self.assertIn("import_diversity", v.signals)

    def test_import_diversity_below_threshold_not_signaled(self):
        m = _metrics(line_count=0, import_category_count=3)
        v = judge.judge(m, "typescript", "normal")
        self.assertNotIn("import_diversity", v.signals)

    def test_vague_filename_signal(self):
        m = _metrics(line_count=0, vague_filename=True)
        v = judge.judge(m, "typescript", "normal")
        self.assertIn("vague_filename", v.signals)

    def test_def_count_signal_on_normal_role(self):
        m = _metrics(line_count=0, def_count=20)
        v = judge.judge(m, "typescript", "normal")
        self.assertIn("def_count", v.signals)

    def test_def_count_signal_ignored_on_test_role(self):
        m = _metrics(line_count=0, def_count=999)
        v = judge.judge(m, "typescript", "test")
        self.assertNotIn("def_count", v.signals)

    def test_control_flow_density_signal(self):
        m = _metrics(line_count=0, control_flow_density=0.3)
        v = judge.judge(m, "typescript", "normal")
        self.assertIn("control_flow_density", v.signals)

    def test_control_flow_density_signal_suppressed_when_declarative(self):
        # density < DECLARATIVE_THRESHOLD (0.02) なら宣言的緩和が先に効き、
        # そもそも high-density のしきい値 (0.25) を満たさない領域だが、念のため
        # 「宣言的 かつ high density」の組み合わせが両立しないことを明示する。
        m = _metrics(line_count=0, control_flow_density=0.01)
        v = judge.judge(m, "typescript", "normal")
        self.assertNotIn("control_flow_density", v.signals)


class TestEmitMatrix(unittest.TestCase):
    def test_ok_tier_never_emits(self):
        m = _metrics(line_count=10, import_category_count=7, def_count=999, vague_filename=True)
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "ok")
        self.assertFalse(v.should_emit)

    def test_note_tier_with_fewer_than_two_signals_does_not_emit(self):
        m = _metrics(line_count=200, vague_filename=True)  # 1 signal only
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "note")
        self.assertEqual(len(v.signals), 1)
        self.assertFalse(v.should_emit)

    def test_note_tier_with_two_or_more_signals_emits(self):
        m = _metrics(line_count=200, vague_filename=True, import_category_count=4)
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "note")
        self.assertGreaterEqual(len(v.signals), 2)
        self.assertTrue(v.should_emit)

    def test_review_tier_emits_regardless_of_signals(self):
        m = _metrics(line_count=300)  # no signals at all
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "review")
        self.assertEqual(v.signals, ())
        self.assertTrue(v.should_emit)

    def test_warn_tier_emits_regardless_of_signals(self):
        m = _metrics(line_count=500)
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "warn")
        self.assertTrue(v.should_emit)

    def test_strong_tier_emits_regardless_of_signals(self):
        m = _metrics(line_count=800)
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "strong")
        self.assertTrue(v.should_emit)


if __name__ == "__main__":
    unittest.main()
