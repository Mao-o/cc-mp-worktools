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
        # java (1.5) x test (2.5) = 3.75
        v = judge.judge(_metrics(line_count=0), "java", "test")
        self.assertAlmostEqual(v.thresholds["note"], 150 * 1.5 * 2.5)
        self.assertAlmostEqual(v.thresholds["review"], 300 * 1.5 * 2.5)
        self.assertAlmostEqual(v.thresholds["warn"], 500 * 1.5 * 2.5)
        self.assertAlmostEqual(v.thresholds["strong"], 800 * 1.5 * 2.5)

    def test_python_multiplier_is_neutral(self):
        # 0.2.0 で 0.7 → 1.0。review=210 行という突出して厳しい閾値が通常の
        # 実装ファイルを初回編集で発火させる主因だったため。
        self.assertAlmostEqual(judge.LANGUAGE_MULTIPLIER["python"], 1.0)
        v = judge.judge(_metrics(line_count=0), "python", "normal")
        self.assertAlmostEqual(v.thresholds["review"], 300)

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


class TestTestRoleRelaxation(unittest.TestCase):
    """0.6.0: test 係数 1.6 → 2.5、かつ test には宣言的緩和を重ねない。

    0.5.0 までは role (1.6) と declarative (1.6) が独立に掛かり、テストファイル
    だけ 2.56 倍の緩和を受けていた。0.6.0 は role=test を ``ROLE_MULTIPLIER``
    だけで一律に緩和する (cfd に依らず 2.5 倍)。
    """

    def test_role_multiplier_is_2_5(self):
        self.assertAlmostEqual(judge.ROLE_MULTIPLIER["test"], 2.5)

    def test_declarative_is_not_stacked_on_test_role(self):
        m = _metrics(line_count=0, control_flow_density=0.01)  # < 0.02
        v = judge.judge(m, "python", "test")
        self.assertAlmostEqual(v.applied_multipliers["declarative"], 1.0)
        self.assertAlmostEqual(v.applied_multipliers["role"], 2.5)
        self.assertAlmostEqual(v.thresholds["warn"], 500 * 2.5)

    def test_test_role_thresholds_are_identical_regardless_of_density(self):
        # 「test は cfd に依らず一律 2.5 倍」を直接固定する。
        dense = judge.judge(_metrics(line_count=0, control_flow_density=0.30), "python", "test")
        sparse = judge.judge(_metrics(line_count=0, control_flow_density=0.00), "python", "test")
        self.assertEqual(dense.thresholds, sparse.thresholds)

    def test_non_declarative_test_file_no_longer_warns_at_the_old_threshold(self):
        # 旧 warn 閾値 (python 500 × 1.6 = 800 行)。0.5.0 はここで warn =
        # シグナル数によらず emit していた。0.6.0 の warn は 500 × 2.5 = 1250 行
        # なので 800 行は review 止まりで、シグナル 0 個なら emit しない。
        m = _metrics(line_count=800, control_flow_density=0.10)  # 宣言的ではない
        v = judge.judge(m, "python", "test")
        self.assertEqual(v.tier, "review")
        self.assertEqual(v.signals, ())
        self.assertFalse(v.should_emit)

    def test_non_declarative_test_file_warns_at_the_new_threshold(self):
        m = _metrics(line_count=1250, control_flow_density=0.10)
        v = judge.judge(m, "python", "test")
        self.assertAlmostEqual(v.thresholds["warn"], 1250)  # 500 × 2.5
        self.assertEqual(v.tier, "warn")
        self.assertTrue(v.should_emit)

    def test_declarative_test_file_warns_at_the_same_threshold(self):
        # 0.5.0 は role 1.6 × declarative 1.6 = 2.56 倍で warn が 1280 行だった
        # ため、1250 行のテストファイルは review に落ちて (シグナル 0 なら)
        # emit されなかった。0.6.0 は宣言的でも同じ 1250 行で warn。
        m = _metrics(line_count=1250, control_flow_density=0.01)  # 宣言的
        v = judge.judge(m, "python", "test")
        self.assertAlmostEqual(v.thresholds["warn"], 1250)
        self.assertEqual(v.tier, "warn")
        self.assertTrue(v.should_emit)

    def test_normal_role_declarative_relaxation_is_unchanged(self):
        # normal role の宣言的緩和 (1.6 倍) は 0.6.0 でも不変。
        m = _metrics(line_count=0, control_flow_density=0.01)
        v = judge.judge(m, "python", "normal")
        self.assertAlmostEqual(v.applied_multipliers["declarative"], 1.6)
        self.assertAlmostEqual(v.thresholds["review"], 300 * 1.6)
        self.assertAlmostEqual(v.thresholds["warn"], 500 * 1.6)


class TestAppliedMultipliers(unittest.TestCase):
    """message.py が「なぜこの閾値か」を説明するための内訳。"""

    def test_language_and_role_recorded_even_when_neutral(self):
        m = _metrics(line_count=0, control_flow_density=0.1)  # not declarative
        v = judge.judge(m, "python", "normal")
        self.assertAlmostEqual(v.applied_multipliers["language"], 1.0)
        self.assertAlmostEqual(v.applied_multipliers["role"], 1.0)
        self.assertAlmostEqual(v.applied_multipliers["declarative"], 1.0)

    def test_non_neutral_language_and_role_recorded(self):
        m = _metrics(line_count=0, control_flow_density=0.1)
        v = judge.judge(m, "java", "test")
        self.assertAlmostEqual(v.applied_multipliers["language"], 1.5)
        self.assertAlmostEqual(v.applied_multipliers["role"], 2.5)
        self.assertAlmostEqual(v.applied_multipliers["declarative"], 1.0)

    def test_declarative_multiplier_recorded_when_applied(self):
        m = _metrics(line_count=0, control_flow_density=0.01)  # < 0.02
        v = judge.judge(m, "typescript", "normal")
        self.assertAlmostEqual(v.applied_multipliers["declarative"], 1.6)

    def test_declarative_multiplier_is_neutral_when_not_applied(self):
        m = _metrics(line_count=0, control_flow_density=0.3)  # high density, not declarative
        v = judge.judge(m, "typescript", "normal")
        self.assertAlmostEqual(v.applied_multipliers["declarative"], 1.0)


class TestScale(unittest.TestCase):
    """FILE_SPLIT_ADVISOR_SCALE (全閾値への一律倍率)。"""

    def test_default_scale_is_neutral(self):
        v = judge.judge(_metrics(line_count=0), "python", "normal")
        self.assertAlmostEqual(v.thresholds["review"], 300)

    def test_scale_multiplies_all_thresholds(self):
        v = judge.judge(_metrics(line_count=0), "python", "normal", scale=2.0)
        self.assertAlmostEqual(v.thresholds["note"], 300)
        self.assertAlmostEqual(v.thresholds["review"], 600)
        self.assertAlmostEqual(v.thresholds["warn"], 1000)
        self.assertAlmostEqual(v.thresholds["strong"], 1600)

    def test_scale_combines_with_language_and_role_multipliers(self):
        # java (1.5) x test (2.5) x scale (0.5) = 1.875
        v = judge.judge(_metrics(line_count=0), "java", "test", scale=0.5)
        self.assertAlmostEqual(v.thresholds["review"], 300 * 1.5 * 2.5 * 0.5)

    def test_scale_not_recorded_in_applied_multipliers(self):
        # scale はグローバル config であり per-file の推論シグナルではないため、
        # message.py の breakdown 表示対象である applied_multipliers には
        # 含めない (実効閾値の計算にだけ反映する)。ただし表示から完全に
        # 消してよいわけではない (P2-1): 別フィールド Verdict.scale として
        # 保持し、message.py 側の専用 parenthetical 表示に使う。
        v = judge.judge(_metrics(line_count=0), "python", "normal", scale=2.0)
        self.assertNotIn("scale", v.applied_multipliers)
        self.assertAlmostEqual(v.applied_multipliers["language"], 1.0)
        self.assertAlmostEqual(v.scale, 2.0)

    def test_default_scale_is_recorded_on_verdict(self):
        v = judge.judge(_metrics(line_count=0), "python", "normal")
        self.assertAlmostEqual(v.scale, 1.0)


class TestScaleSafety(unittest.TestCase):
    """P2-1 回帰: 巨大だが有限な scale (``math.isfinite`` は通る) は、言語/role/
    宣言的緩和の係数と掛け合わさると実効閾値が ``inf`` に飽和しうる。
    ``is_scale_safe()`` は既知の最悪ケース係数でこれを事前検出する。
    """

    def test_huge_finite_scale_is_unsafe(self):
        # マージ前レビューの指摘で挙がった再現値そのもの。
        self.assertFalse(judge.is_scale_safe(1e308))

    def test_neutral_scale_is_safe(self):
        self.assertTrue(judge.is_scale_safe(1.0))

    def test_moderate_scale_is_safe(self):
        self.assertTrue(judge.is_scale_safe(2.0))

    def test_huge_scale_actually_saturates_thresholds_to_inf(self):
        # is_scale_safe() が検出する「壊れ方」そのものを直接確認する: 1e308 を
        # judge.judge() にそのまま渡すと (ガード無しの経路を再現)、実効閾値が
        # inf になり line_count がどれだけ大きくても tier は ok に留まる
        # (advisor の無言の無効化)。
        m = _metrics(line_count=10_000_000)
        v = judge.judge(m, "python", "normal", scale=1e308)
        self.assertEqual(v.thresholds["strong"], float("inf"))
        self.assertEqual(v.tier, "ok")


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

    def test_review_tier_without_signals_does_not_emit(self):
        # 0.1.0 では emit していた。行数のみを根拠にした通知が emit の大半を
        # 占めていたため、0.2.0 で「シグナル 1 個以上」を要求するようにした。
        m = _metrics(line_count=300)  # no signals at all
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "review")
        self.assertEqual(v.signals, ())
        self.assertFalse(v.should_emit)

    def test_review_tier_with_one_signal_emits(self):
        m = _metrics(line_count=300, vague_filename=True)
        v = judge.judge(m, "typescript", "normal")
        self.assertEqual(v.tier, "review")
        self.assertEqual(len(v.signals), 1)
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
