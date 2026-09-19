"""message.py: additionalContext 文面組み立てのテスト。"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import judge
import message
from judge import Verdict
from metrics import Metrics, TopLevelDef


def _metrics(
    line_count=300,
    def_count=0,
    import_category_count=0,
    import_categories=(),
    control_flow_density=0.1,
    vague_filename=False,
    top_level_defs=(),
    import_modules=(),
) -> Metrics:
    return Metrics(
        line_count=line_count,
        def_count=def_count,
        import_category_count=import_category_count,
        import_categories=import_categories,
        control_flow_density=control_flow_density,
        vague_filename=vague_filename,
        top_level_defs=top_level_defs,
        import_modules=import_modules,
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

    def test_small_value_preserves_significant_digits(self):
        # P2-2 回帰: 固定小数点2桁 (`.2f`) 表示だと 0.004 は "0.00" に潰れ、
        # 末尾ゼロ除去処理を経て "0.0" になっていた。judge は 0.004 を
        # そのまま実効閾値の計算に使っているため、表示が実態と食い違う。
        self.assertEqual(message._format_multiplier(0.004), "0.004")

    def test_near_neutral_value_preserves_significant_digits(self):
        # P2-2 回帰: 1.004 も同じ理由で "1.00" → "1.0" に潰れ、中立値 1.0 と
        # 見分けがつかなくなっていた。
        self.assertEqual(message._format_multiplier(1.004), "1.004")


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
        v = _verdict(applied_multipliers={"language": 1.0, "role": 2.5, "declarative": 1.0})
        text = message.build(Path("foo_test.py"), "python", "test", v, _metrics())
        self.assertIn("(test: 閾値 2.5倍)", text)
        self.assertNotIn("test 2.5", text)
        self.assertIn("(python 1.0)", text)

    def test_role_note_reflects_the_multiplier_judge_actually_applied(self):
        # 0.6.0: role_note の係数を message.py 側にハードコードしていると、
        # ``ROLE_MULTIPLIER`` を動かしたときに表示だけ旧値のまま残る。judge を
        # 実際に通した Verdict で「表示 = 適用値」を固定する。
        m = _metrics(line_count=1250, control_flow_density=0.01)
        v = judge.judge(m, "python", "test")
        text = message.build(Path("test_foo.py"), "python", "test", v, m)
        expected = message._format_multiplier(judge.ROLE_MULTIPLIER["test"])
        self.assertIn(f"(test: 閾値 {expected}倍)", text)
        # 宣言的緩和は test には重ねないので breakdown に出ない。
        self.assertNotIn("宣言的", text)


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
        # role_note (test: 閾値 N倍) と scale note (全体 N倍) は両立し、
        # 互いを上書きしない。
        v = _verdict(
            tier="warn",
            applied_multipliers={"language": 1.0, "role": 2.5, "declarative": 1.0},
            scale=0.5,
        )
        text = message.build(Path("foo_test.py"), "python", "test", v, _metrics())
        self.assertIn("(全体 0.5倍)", text)
        self.assertIn("(test: 閾値 2.5倍)", text)

    def test_scale_note_preserves_significant_digits_for_small_scale(self):
        # P2-2 回帰: 0.004 は固定2桁表示だと "0.00" → "0.0" に潰れ、judge が
        # 実際に使っている極小の倍率と食い違って見えていた。
        v = _verdict(tier="warn", scale=0.004)
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics())
        self.assertIn("(全体 0.004倍)", text)
        self.assertNotIn("(全体 0.0倍)", text)

    def test_scale_note_preserves_significant_digits_near_neutral(self):
        # P2-2 回帰: 1.004 は固定2桁表示だと中立値 1.0 と区別がつかない
        # "(全体 1.0倍)" になり、「倍率をかけている」という主張自体が
        # 読み取れなくなっていた。
        v = _verdict(tier="warn", scale=1.004)
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics())
        self.assertIn("(全体 1.004倍)", text)
        self.assertNotIn("(全体 1.0倍)", text)


class TestFormatSignal(unittest.TestCase):
    """内部バックログ: ``_format_signal`` の 4 分岐 + 未知キーのフォールバックを
    直接固定する。従来は ``message.build`` 経由の間接テスト (``TestSignalCountZeroFallback``
    など) しかなく、各シグナル文面そのもの (件数・パーセンテージの整形) は
    どのテストにも現れていなかった。"""

    def test_import_diversity_lists_category_names_and_count(self):
        m = _metrics(import_category_count=5, import_categories=("network", "db", "ui"))
        self.assertEqual(
            message._format_signal("import_diversity", m),
            "import カテゴリ多様性 5種 (network, db, ui)",
        )

    def test_vague_filename_returns_fixed_explanation(self):
        m = _metrics(vague_filename=True)
        self.assertEqual(
            message._format_signal("vague_filename", m),
            "命名が抽象的 (utils/common/helper 等の総称語のみ)",
        )

    def test_def_count_shows_the_actual_count(self):
        m = _metrics(def_count=27)
        self.assertEqual(message._format_signal("def_count", m), "定義数 27")

    def test_control_flow_density_shown_as_rounded_percentage(self):
        m = _metrics(control_flow_density=0.268)
        self.assertEqual(message._format_signal("control_flow_density", m), "制御フロー密度 27%")

    def test_unknown_signal_key_falls_back_to_the_raw_key(self):
        # judge.py が現在 emit する signal key (import_diversity/vague_filename/
        # def_count/control_flow_density) は上の 4 分岐で全て処理されるため、
        # ``_SIGNAL_FALLBACK_LABELS`` への到達は現状のコードパスでは起きない
        # (将来 judge 側が新しい signal key を追加したときの防御)。未登録キー
        # は辞書にも無いため、キー文字列がそのまま返ることを固定する。
        self.assertEqual(message._format_signal("future_signal", _metrics()), "future_signal")

    def test_fallback_labels_dict_covers_the_four_known_signal_keys(self):
        # _SIGNAL_FALLBACK_LABELS の各キーは _format_signal の明示分岐で
        # 先取りされるため、辞書経由の値は現状のコードパスでは一切使われない
        # (dead code)。``_format_signal`` を経由すると常に明示分岐の結果に
        # なり辞書の内容自体はテストできないため、辞書のキー/値をここで
        # 直接固定する。将来 judge.py が新しい signal key を追加し、
        # ``_format_signal`` 側の対応分岐を書き忘れたときに、この辞書だけが
        # 頼りになる。
        self.assertEqual(
            message._SIGNAL_FALLBACK_LABELS,
            {
                "import_diversity": "import カテゴリ多様性",
                "vague_filename": "命名が抽象的",
                "def_count": "定義数過多",
                "control_flow_density": "制御フロー密度高",
            },
        )


class TestRoleNoteAbsentForNormalRole(unittest.TestCase):
    """role_note ("(test: 閾値 N倍)") は role=="test" のときだけ表示される。
    既存テストは test ロールでの表示は確認済みだが、normal ロールで出ない
    ことを直接確認するテストが無かった。"""

    def test_normal_role_omits_role_note(self):
        v = _verdict(applied_multipliers={"language": 1.0, "role": 1.0, "declarative": 1.0})
        text = message.build(Path("foo.py"), "python", "normal", v, _metrics())
        self.assertNotIn("test: 閾値", text)


class TestDefHighlightLine(unittest.TestCase):
    """0.5.0: 分割候補の境界 (大きい定義) をメモに付記する。"""

    def _defs(self, *specs) -> tuple[TopLevelDef, ...]:
        return tuple(
            TopLevelDef(name=name, start_line=start, span=span) for name, start, span in specs
        )

    def test_defs_listed_in_descending_span_order(self):
        defs = self._defs(("small", 10, 5), ("big", 100, 200), ("mid", 50, 40))
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(top_level_defs=defs)
        )
        line = next(l for l in text.splitlines() if l.startswith("大きい定義"))
        self.assertIn("big(200行, 100行目〜)", line)
        self.assertLess(line.index("big"), line.index("mid"))
        self.assertLess(line.index("mid"), line.index("small"))

    def test_only_the_first_five_defs_are_listed(self):
        defs = self._defs(*[(f"f{i}", i * 10 + 1, 100 - i) for i in range(12)])
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(top_level_defs=defs)
        )
        line = next(l for l in text.splitlines() if l.startswith("大きい定義"))
        self.assertIn("上位5:", line)
        self.assertEqual(line.count(" / "), 4)
        self.assertNotIn("f5(", line)

    def test_equal_spans_keep_source_order(self):
        defs = self._defs(("later", 200, 30), ("earlier", 20, 30))
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(top_level_defs=defs)
        )
        line = next(l for l in text.splitlines() if l.startswith("大きい定義"))
        self.assertLess(line.index("earlier"), line.index("later"))

    def test_no_defs_omits_the_line(self):
        text = message.build(Path("foo.py"), "python", "normal", _verdict(), _metrics())
        self.assertNotIn("大きい定義", text)

    def test_long_identifier_is_truncated(self):
        defs = self._defs(("x" * 200, 1, 50))
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(top_level_defs=defs)
        )
        line = next(l for l in text.splitlines() if l.startswith("大きい定義"))
        self.assertIn("…", line)
        self.assertNotIn("x" * 200, line)


class TestImportClusterLine(unittest.TestCase):
    """0.5.0: どの依存がカテゴリを立てたかをメモに付記する。"""

    def test_clusters_listed_with_their_modules(self):
        modules = (("network", ("requests", "urllib")), ("filesystem", ("pathlib", "shutil")))
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(import_modules=modules)
        )
        self.assertIn(
            "import クラスタ: network(requests, urllib) / filesystem(pathlib, shutil)", text
        )

    def test_single_category_omits_the_line(self):
        # 単一カテゴリの列挙は「どこで切るか」の手掛かりにならない。
        modules = (("network", ("requests", "httpx")),)
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(import_modules=modules)
        )
        self.assertNotIn("import クラスタ", text)

    def test_categories_without_sampled_modules_are_not_counted(self):
        modules = (("network", ("requests",)), ("db", ()))
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(import_modules=modules)
        )
        self.assertNotIn("import クラスタ", text)


class TestMemoCharLimit(unittest.TestCase):
    """0.5.0: 付記行が additionalContext の上限を押し上げないこと。"""

    def _rich_metrics(self):
        defs = tuple(
            TopLevelDef(name=f"name_{i}", start_line=i + 1, span=500 - i) for i in range(50)
        )
        modules = tuple(
            (f"cat{i}", tuple(f"module_{i}_{j}" for j in range(6))) for i in range(3)
        )
        return _metrics(top_level_defs=defs, import_modules=modules)

    def _build(self, metrics):
        return message.build(Path("foo.py"), "python", "normal", _verdict(), metrics)

    def test_oversized_extras_are_dropped_before_the_verdict_lines(self):
        # 上限に収まらない付記行は落ちるが、判定根拠 (行数/tier/シグナル) と
        # footer は常に残る。
        bare = self._build(_metrics())
        with mock.patch.object(message, "MAX_MEMO_CHARS", len(bare) + 5):
            text = self._build(self._rich_metrics())
        self.assertIn("行数: 300", text)
        self.assertIn("行数は分割要否の直接的根拠ではなく", text)
        self.assertNotIn("大きい定義", text)
        self.assertNotIn("import クラスタ", text)

    def test_a_short_later_extra_is_kept_when_an_earlier_one_does_not_fit(self):
        # 「収まらない付記行だけ落とす」— 先の長い行が入らなくても後の短い行は残る。
        long_defs = tuple(
            TopLevelDef(name="d" * 40, start_line=i + 1, span=900 - i) for i in range(5)
        )
        short_modules = (("a", ("x",)), ("b", ("y",)))
        cluster_only = self._build(_metrics(import_modules=short_modules))
        with mock.patch.object(message, "MAX_MEMO_CHARS", len(cluster_only)):
            text = self._build(
                _metrics(top_level_defs=long_defs, import_modules=short_modules)
            )
        self.assertNotIn("大きい定義", text)
        self.assertIn("import クラスタ", text)

    def test_an_extra_that_only_fits_without_the_footer_is_still_dropped(self):
        # 予算計算に footer を含めないと、footer 長ぶんだけ上限が緩む。
        defs = (TopLevelDef(name="parse", start_line=1, span=40),)
        with_def = self._build(_metrics(top_level_defs=defs))
        with mock.patch.object(message, "MAX_MEMO_CHARS", len(with_def) - 1):
            text = self._build(_metrics(top_level_defs=defs))
        self.assertNotIn("大きい定義", text)
        self.assertLessEqual(len(text), len(with_def) - 1)

    def test_extras_that_fit_are_kept_even_if_a_later_one_does_not(self):
        # 「入るものだけ入れる」— 先の行が入っても後の長い行で全体が上限を
        # 超えるなら、後の行だけを落とす。
        rich = self._rich_metrics()
        with_defs_only = self._build(_metrics(top_level_defs=rich.top_level_defs))
        with mock.patch.object(message, "MAX_MEMO_CHARS", len(with_defs_only) + 5):
            text = self._build(rich)
        self.assertIn("大きい定義", text)
        self.assertNotIn("import クラスタ", text)
        self.assertIn("行数は分割要否の直接的根拠ではなく", text)

    def test_short_extras_fit_within_the_limit(self):
        defs = (TopLevelDef(name="parse", start_line=1, span=40),)
        text = message.build(
            Path("foo.py"), "python", "normal", _verdict(), _metrics(top_level_defs=defs)
        )
        self.assertLessEqual(len(text), message.MAX_MEMO_CHARS)
        self.assertIn("大きい定義 上位1: parse(40行, 1行目〜)", text)


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


class TestThresholdDisplayRounding(unittest.TestCase):
    """P3 回帰: 判定は ``line_count >= threshold`` (半開区間) なので、閾値に
    最初に到達する整数行数は ``ceil(threshold)`` である。以前は ``round()``
    を使っており、Python の偶数丸めで閾値ちょうど .5 のとき 1 小さい値を
    表示していた。この不正確さは倍率機能 (SCALE) に限らず、言語係数のみの
    既定経路 (SCALE 未設定) でも起きる — 基準 150 (note) に非整数係数の
    言語を掛けると同じ形の閾値になる。
    """

    def test_fractional_threshold_rounds_up_not_to_even(self):
        # 172.5 ちょうどのとき、round(172.5) は Python の偶数丸めで 172 に
        # なるが、実際に note tier へ最初に到達するのは 173 行 (173 >= 172.5
        # は True, 172 >= 172.5 は False)。
        v = _verdict(
            tier="note", thresholds={"note": 172.5, "review": 300, "warn": 500, "strong": 800}
        )
        text = message.build(Path("foo.jsx"), "javascriptreact", "normal", v, _metrics(line_count=173))
        self.assertIn("note=173", text)
        self.assertNotIn("note=172", text)

    def test_default_path_jsx_note_threshold_rounds_up(self):
        # 倍率機能 (SCALE) を一切使わない既定経路の実例: 基準 150 (note) ×
        # javascriptreact の言語係数 1.15 = 172.5。judge.judge() が実際に
        # 返す Verdict をそのまま message.build() に通して確認する。
        m = _metrics(line_count=173)
        v = judge.judge(m, "javascriptreact", "normal")
        self.assertEqual(v.thresholds["note"], 172.5)
        text = message.build(Path("Foo.jsx"), "javascriptreact", "normal", v, m)
        self.assertIn("note=173", text)
        self.assertNotIn("note=172", text)

    def test_default_path_float_dust_threshold_rounds_up_to_actual_boundary(self):
        # 既定経路のもう1つの実例 (宣言的緩和との組み合わせ): 基準 150 ×
        # java の言語係数 1.5 × 宣言的緩和 1.6 は数式上は 360 だが、
        # float 演算の丸め誤差で実際には 360.00000000000006 になる
        # (150*1.5*1.6 は二進浮動小数点では厳密な整数にならない)。
        # ``_compute_tier`` が使う実際の比較 (line_count >= threshold) では
        # 360 行は note tier に届かず、361 行で初めて届く。ceil() はこの
        # 「実際の境界」に忠実で、round() が示す 360 は実際には note に
        # 届かない値を「届く」と誤って表示することになる。
        declarative = dict(control_flow_density=0.01)  # < DECLARATIVE_THRESHOLD
        below = judge.judge(_metrics(line_count=360, **declarative), "java", "normal")
        at = judge.judge(_metrics(line_count=361, **declarative), "java", "normal")
        self.assertEqual(below.tier, "ok")  # 360 行はまだ note に届かない
        self.assertEqual(at.tier, "note")  # 361 行で届く
        text = message.build(
            Path("Routes.java"), "java", "normal", at, _metrics(line_count=361, **declarative)
        )
        self.assertIn("note=361", text)
        self.assertNotIn("note=360", text)


if __name__ == "__main__":
    unittest.main()
