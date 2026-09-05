"""core/messages.py の builder 単体テスト (M1 / H1 / H3)。

各 builder が:
- 必須の情報 (operand / basename) を文中に含めること
- ``!<basename>`` 案内に **実 basename を展開**して埋め込むこと
- dotenv 系の代替案 (``.env.example``) や extra_note を所定の位置に置くこと
を保証する。

文言の細部 (動詞ルール) は M2 / M4 で再調整するため、ここでは情報伝達の
本質要件のみ検証する。
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401

from _shared.patterns import (
    PROJECT_SECTION_HEADER_HINT,
    exclude_recipe_lines,
)
from core import messages as M
from core import output


class TestExcludeHintBasename(unittest.TestCase):
    """``_exclude_hint`` が basename を実展開していることの保証。"""

    def test_exclude_hint_with_basename(self):
        out = M._exclude_hint(".env")
        self.assertIn("`!.env`", out)
        self.assertIn("patterns.local.txt", out)
        self.assertNotIn("<basename>", out)

    def test_exclude_hint_guides_project_section_by_default(self):
        """0.19.0: 既定で [project:] セクション配下への追記を
        案内し、ヘッダー無し (全プロジェクト共通) は明示的な選択にする。
        絶対パスは reason に出さない (環境変数名で示す)。"""
        out = M._exclude_hint(".env")
        self.assertIn("`[project:$CLAUDE_PROJECT_DIR]`", out)
        self.assertIn("全プロジェクト共通", out)
        self.assertIn("ヘッダー無し", out)
        self.assertIn("承認なしに", out)
        self.assertNotIn(str(Path.home()), out)
        self.assertNotIn(os.getcwd(), out)

    def test_exclude_hint_without_relpath_does_not_overclaim_root_unresolved(self):
        """relpath が空のとき、文言は「root 相対 path を確定できない」に限定し、
        「root を解決できない」と一律に断定しない (0.24.0 の不正確な文言を修正、
        内部バックログ)。relpath が空になるのは root 不明のほかに path が root
        配下でない / glob operand / コロンを含む pathspec でも起きるため。"""
        out = M._exclude_hint(".env", relpath="")
        self.assertIn("root 相対 path を確定できないため path 形は案内できません", out)
        self.assertNotIn("root を解決できないため", out)

    def test_exclude_hint_without_basename_also_guides_project_section(self):
        out = M._exclude_hint("")
        self.assertIn("`[project:$CLAUDE_PROJECT_DIR]`", out)
        self.assertIn("<basename>", out)

    def test_exclude_hint_shares_header_with_shared_recipe(self):
        # Stop hook の block reason と同じ定数 (_shared.patterns) を使う
        out = M._exclude_hint(".env")
        self.assertIn(PROJECT_SECTION_HEADER_HINT, out)
        self.assertEqual(
            exclude_recipe_lines([".env", ".env", "", "ca.pem"]),
            [PROJECT_SECTION_HEADER_HINT, "!.env", "!ca.pem"],
        )

    def test_exclude_recipe_lines_caps_long_lists(self):
        lines = exclude_recipe_lines([f"k{i}.pem" for i in range(25)], limit=20)
        self.assertEqual(len(lines), 22)
        self.assertEqual(lines[1], "!k0.pem")
        self.assertEqual(lines[-1], "... (5 more)")

    def test_exclude_recipe_lines_is_linear_for_large_inputs(self):
        # 重複除去が list membership の二次計算だと 5 万件で Stop の 15s timeout
        # を超えうる (Codex R5 P2-2)。10 万件 (5 万 unique × 2) で 1 秒未満
        import time

        names = [f"k{i}.pem" for i in range(50_000)] * 2
        started = time.perf_counter()
        lines = exclude_recipe_lines(names, limit=20)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(lines), 22)
        self.assertEqual(lines[1], "!k0.pem")
        self.assertEqual(lines[-1], "... (49980 more)")
        self.assertLess(elapsed, 1.0)

    def test_exclude_hint_without_basename(self):
        out = M._exclude_hint("")
        # basename が無いケースは plain プレースホルダを出す
        self.assertIn("<basename>", out)

    def test_exclude_hint_strips_backtick(self):
        # backtick が name に混じっていても markdown を壊さない
        out = M._exclude_hint(".env`evil")
        self.assertNotIn(".env`evil", out)
        self.assertIn(".envevil", out)


class TestGitSubcommandOf(unittest.TestCase):
    """``_git_subcommand_of`` (0.19.0): history builder の subcommand 推定。"""

    def test_simple(self):
        self.assertEqual(
            M._git_subcommand_of("git show HEAD:.env", "HEAD:.env"), "show"
        )

    def test_skips_global_options(self):
        self.assertEqual(
            M._git_subcommand_of(
                "git -C /repo -c k=v --git-dir=/x --no-pager diff .env", ".env"
            ),
            "diff",
        )

    def test_prefers_window_containing_operand(self):
        self.assertEqual(
            M._git_subcommand_of("git log && git add .env", ".env"), "add"
        )

    def test_falls_back_to_first_git_without_operand_match(self):
        self.assertEqual(
            M._git_subcommand_of("git log && git status", ".env"), "log"
        )

    def test_opt_equals_value_operand_matches_window(self):
        # ``--pathspec-from-file=.env`` 由来の operand でも後段の git を選ぶ
        self.assertEqual(
            M._git_subcommand_of(
                "git log && git rm --cached --pathspec-from-file=.env", ".env"
            ),
            "rm",
        )

    def test_empty_command(self):
        self.assertEqual(M._git_subcommand_of("", ".env"), "")

    def test_unparseable_command_does_not_raise(self):
        # shlex 失敗 → 空白 split fallback。operate set に入らないので閲覧文面
        sub = M._git_subcommand_of("git 'unterminated .env", ".env")
        self.assertNotIn(sub, M._GIT_OPERATE_SUBCOMMANDS)


class TestBashDeny(unittest.TestCase):
    """0.7.0: kind / form 引数を撤廃し、operand と first_token のみで build する。"""

    def test_literal_basic(self):
        msg = M.bash_deny(first_token="cat", operand=".env")
        # 必須情報
        self.assertIn("cat", msg)
        self.assertIn(".env", msg)
        # H3: basename 展開
        self.assertIn("`!.env`", msg)
        # 種別の表現
        self.assertIn("operand", msg)
        # 0.7.0: GUARDRAIL_DENY 構造化包装は plain text に戻したため出ない
        self.assertNotIn("<GUARDRAIL_DENY", msg)

    def test_path_operand_basename_extraction(self):
        msg = M.bash_deny(first_token="head", operand="/abs/path/to/.env")
        self.assertIn("/abs/path/to/.env", msg)
        # basename のみ案内に出る
        self.assertIn("`!.env`", msg)
        # フル path はそのまま `!...` には埋めない
        self.assertNotIn("`!/abs/path/to/.env`", msg)

    def test_glob_operand_uses_same_template(self):
        msg = M.bash_deny(first_token="cat", operand="*.env*")
        self.assertIn("cat", msg)
        self.assertIn("*.env*", msg)
        self.assertIn("`!*.env*`", msg)

    def test_first_token_omitted_no_first_token_line(self):
        # first_token を空で渡すと body に first_token 行を出さない
        msg = M.bash_deny(first_token="", operand=".env")
        self.assertIn(".env", msg)
        self.assertNotIn("first_token:", msg)


# ``<DATA>`` ブロックの **明細行** (1 エントリ 1 行) を数える。実装の
# ``core.messages._count_detail_lines`` とは独立に、外形 (2 space インデント
# かつ省略マーカーでない) だけで数える — 実装を参照すると「実装が壊れたら
# テストも同じように壊れる」ため。コーパス計測で使っている正規表現と同一。
_DETAIL_LINE_RE = re.compile(r"^  (?!\.\.\.)\S")


def _detail_lines(lines: list[str]) -> int:
    return sum(1 for line in lines if _DETAIL_LINE_RE.match(line))


def _real_dotenv_block(n: int = 50) -> str:
    """``redaction.engine.redact`` が実際に組む dotenv の ``<DATA>`` ブロック。

    手書きの fixture と違い ``format:`` / ``entries:`` の**見出し行**を持つ。
    フォールバック条件の検証はこの形でしか成立しない — 見出しが常に載る
    せいで「1 行も採用できない」条件が発火しなくなるのが 0.26.0 初版の欠陥
    だったため。
    """
    from io import BytesIO

    from redaction.engine import redact

    text = "".join(f"KEY_{i:03d}=value_{i:03d}_padding\n" for i in range(n))
    data = text.encode("utf-8")
    return redact(BytesIO(data), ".env", len(data))


class TestDataBlockAssumptions(unittest.TestCase):
    """``_fit_data_block`` が置いている ``<DATA>`` 包装の前提を固定する (E6)。

    ``_DATA_HEADER_LINES`` / ``_DATA_CLOSING_TAG`` は
    ``redaction.engine.build_reason`` の出力形に暗黙依存している。ここが
    ずれると「header だけ載せて中身ゼロ」や「閉じタグを落とした包装」が
    **予算超過時にだけ** 静かに出るので、生成側と突合しておく。
    """

    def test_build_reason_header_line_count_matches_constant(self):
        from redaction.engine import build_reason

        lines = build_reason(".env", "dotenv", "BODY_MARKER").split("\n")
        self.assertEqual(lines.index("BODY_MARKER"), M._DATA_HEADER_LINES)

    def test_build_reason_ends_with_closing_tag_constant(self):
        from redaction.engine import build_reason

        lines = build_reason(".env", "dotenv", "BODY_MARKER").split("\n")
        self.assertEqual(lines[-1], M._DATA_CLOSING_TAG)

    def test_detail_line_indent_matches_every_formatter(self):
        """``_count_detail_lines`` の「2 space インデント = 明細行」規約を
        生成側の実出力と突合する (0.26.0 レビュー P3-4)。

        ここがずれると、フォールバック判定が「明細行 0」を検出できず
        **予算超過時にだけ** 中身ゼロの ``<DATA>`` ブロックが静かに出る
        (0.26.0 の初版がまさにこの状態だった)。dotenv / keys-only /
        yaml / jsonlike の 4 形式で、明細行が entries と一致し、見出し行と
        末尾 note が数に入らないことを固定する。
        """
        from redaction.dotenv import format_dotenv, redact_dotenv
        from redaction.engine import build_reason
        from redaction.jsonlike import format_jsonlike, redact_jsonlike
        from redaction.keyonly_scan import format_keyonly
        from redaction.opaque import _format_yaml

        n = 5
        bodies = {
            "dotenv": format_dotenv(
                redact_dotenv("".join(f"KEY_{i}=v{i}\n" for i in range(n)))
            ),
            "keyonly": format_keyonly(
                [f"KEY_{i}" for i in range(n)], 100, fmt_hint="opaque"
            ),
            "yaml": _format_yaml(
                {"format": "yaml", "entries": n, "nested_count": 0,
                 "keys": [f"key_{i}" for i in range(n)]}
            ),
            "jsonlike": format_jsonlike(
                redact_jsonlike(
                    '{' + ", ".join(f'"k{i}": "v{i}"' for i in range(n)) + '}'
                )
            ),
        }
        for label, body in bodies.items():
            with self.subTest(format=label):
                lines = build_reason(".env", label, body).split("\n")
                self.assertEqual(M._count_detail_lines(lines), n)
                # 見出し行・note 行は明細行として数えない。
                self.assertTrue(
                    any(line.startswith("format:") for line in lines)
                )
                self.assertTrue(any(line.startswith("note:") for line in lines))

    def test_entries_line_sits_inside_the_scan_window(self):
        """``_entries_total`` の走査窓 (header 3 行 + 2) を生成側と突合する。

        ``entries:`` は本文 2 行目 (``format:`` の直後) という前提で読んでいる。
        renderer が前に 1 行足すと総数を読めなくなり、省略マーカーの件数が
        **予算超過時にだけ** 静かに「落とした行数」へ戻る (0.26.0 外部レビュー
        R1 で直した挙動そのものに逆戻りする)。
        """
        from redaction.engine import build_reason
        from redaction.keyonly_scan import format_keyonly

        keys = [f"KEY_{i}" for i in range(7)]
        block = build_reason(
            ".env", "opaque", format_keyonly(keys, 100, fmt_hint="opaque")
        )
        lines = block.split("\n")
        self.assertTrue(
            lines[M._DATA_HEADER_LINES + 1].startswith(M._DATA_ENTRIES_PREFIX),
            lines[: M._DATA_HEADER_LINES + 2],
        )
        self.assertEqual(M._entries_total(block), len(keys))
        # 明細行に ``entries:`` が現れても窓の外なので拾わない
        self.assertEqual(M._entries_total(block.replace("KEY_0", "entries: 99")),
                         len(keys))

    def test_omit_marker_is_not_counted_as_detail_line(self):
        """省略マーカーは明細行と同じインデントを持つが明細ではない。"""
        marker = M._omit_marker(3, unit=M._OMIT_UNIT_KEYS)
        self.assertTrue(marker.startswith(M._OMIT_MARKER_PREFIX))
        self.assertTrue(marker.startswith(M._DATA_DETAIL_INDENT))
        header = ["<DATA x>", "NOTE: x", "file: .env"]
        self.assertEqual(M._count_detail_lines(header + [marker]), 0)
        self.assertEqual(
            M._count_detail_lines(header + ["  1. KEY", marker]), 1
        )


class TestFitDataBlockNoteProtection(unittest.TestCase):
    """0.26.0: ``_fit_data_block`` は閉じタグと一緒に末尾 ``note:`` 行も守る。

    0.26.0 以前は ``</DATA>`` だけを固定 tail 扱いし、直前の per-format note
    (``format_dotenv`` 等が必ず最後に置く「実値は無い」の免責事項) を可変長
    body の一部として畳んでいた。実測で 20 key 前後から note が消えることを
    確認した (edit_deny 経由の折り畳みで実証済み)。

    ただし note を守ろうとして key 行が **前より減る**のは本末転倒なので、
    「note を守れないなら守らずに済ませる (旧来動作)」の 2 段構えを固定する。

    フォールバックの発火条件は **明細行 (key 行) が 0 になるとき** であって
    「1 行も採用できないとき」ではない。0.26.0 の初版は後者で書かれており、
    見出し行が常に載るせいで発火しなかった (下の
    ``test_never_zero_detail_lines_when_dropping_note_would_show_some`` /
    ``test_falls_back_when_note_protection_leaves_zero_detail_lines``)。
    """

    _BLOCK = "\n".join(
        ['<DATA untrusted="true" source="redact-hook" guard="g">',
         "NOTE: x", "file: .env"]
        + [f"  {i}. KEY_{i}  <type=str>  <set>  length=8" for i in range(50)]
        + ["note: real values are not in context. only key names are returned."]
        + ["</DATA>"]
    )

    def test_note_survives_when_budget_allows(self):
        fitted = M._fit_data_block(self._BLOCK, 2000)
        joined = "\n".join(fitted)
        self.assertIn("real values are not in context", joined)
        self.assertEqual(fitted[-1], "</DATA>")
        self.assertEqual(fitted[-2], "note: real values are not in context. only key names are returned.")

    def test_never_regresses_to_empty_when_legacy_would_show_something(self):
        """note 保護によって「以前は見えていたのに何も見えなくなる」budget が
        存在しないこと。

        note 保護に使った budget 分だけ key 行が数行減ることは意図した
        トレードオフ (note を残す方を優先する設計) なので regression としては
        扱わない。regression として避けたいのは「旧アルゴリズムなら鍵情報を
        出せていたのに、note を守ろうとして情報がゼロになる」ケース。
        ここは**空リストに落ちる**段を固定し、**明細行 0 に落ちる**段は
        ``test_never_zero_detail_lines_when_dropping_note_would_show_some``
        が固定する (0.26.0 初版が取り逃していたのは後者)。
        """
        for budget in range(0, 400, 5):
            with self.subTest(budget=budget):
                legacy = M._fit_data_block_core(
                    self._BLOCK, budget, protect_note=False,
                )
                current = M._fit_data_block(self._BLOCK, budget)
                if legacy:
                    self.assertTrue(
                        current,
                        f"budget={budget}: 旧アルゴリズムは非空なのに"
                        " 2 段構え版が空を返した (regression)",
                    )

    def test_falls_back_to_no_note_when_note_protection_yields_nothing(self):
        """note を守ると中身ゼロになる budget では、note を諦めて中身を出す。"""
        # header 3 行 + key 1 行 + closing はギリギリ入るが、note 行
        # (約 70 byte) まで追加で確保しようとすると header だけになる budget。
        narrow_block = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: x", "file: .env",
             "  1. KEY_1  <type=str>  <set>  length=8",
             "note: real values are not in context. only key names are returned.",
             "</DATA>"]
        )
        found_fallback_case = False
        for budget in range(40, 250, 1):
            protected = M._fit_data_block_core(
                narrow_block, budget, protect_note=True,
            )
            unprotected = M._fit_data_block_core(
                narrow_block, budget, protect_note=False,
            )
            if not protected and unprotected:
                found_fallback_case = True
                two_tier = M._fit_data_block(narrow_block, budget)
                self.assertEqual(two_tier, unprotected)
                self.assertTrue(
                    any(line.startswith("  1. KEY_1") for line in two_tier),
                )
        self.assertTrue(
            found_fallback_case,
            "note 保護が中身ゼロに追い込む budget 帯が見つからなかった"
            " (テスト fixture の調整が必要)",
        )

    def test_never_zero_detail_lines_when_dropping_note_would_show_some(self):
        """**明細行 0** に後退する budget が存在しないこと (0.26.0 レビュー P3-4)。

        0.26.0 の初版はフォールバック条件を「1 行も採用できない」で書いて
        いたが、``<DATA>`` ブロックには header 3 行と ``format:`` /
        ``entries:`` の見出しが必ず入るので空リストにはならず、フォールバック
        が発火しなかった。結果、note 保護の固定費 (約 120 byte) に押されて
        **見出しと省略マーカーだけ**のブロックが出ていた (コーパス実測で
        Edit / Write の 114 件)。docstring が言う「key 行が 0 行に後退する
        なら諦める」を実装が満たしていない状態。

        fixture 単位ではサイズ依存で再混入を取り逃すので、budget 掃引の
        **性質**として固定する。実装の ``_count_detail_lines`` ではなく
        外形 (``_detail_lines``) で数える。
        """
        block = _real_dotenv_block()
        for budget in range(0, 800, 5):
            with self.subTest(budget=budget):
                legacy = M._fit_data_block_core(
                    block, budget, protect_note=False,
                )
                current = M._fit_data_block(block, budget)
                if _detail_lines(legacy):
                    self.assertGreater(
                        _detail_lines(current),
                        0,
                        f"budget={budget}: note 保護を外せば明細行を出せるのに"
                        " 見出しだけのブロックになった (regression)",
                    )

    def test_falls_back_when_note_protection_leaves_zero_detail_lines(self):
        """明細行 0 に追い込まれる budget 帯が実在し、そこで note を諦めること。

        「1 行も採用できない」条件との差を示す negative control。この帯では
        note 保護版も **非空** (見出しは載る) なので、旧条件ではフォールバック
        しない。
        """
        block = _real_dotenv_block()
        found = False
        for budget in range(0, 800, 1):
            protected = M._fit_data_block_core(
                block, budget, protect_note=True,
            )
            relaxed = M._fit_data_block_core(
                block, budget, protect_note=False,
            )
            if (protected and not _detail_lines(protected)
                    and _detail_lines(relaxed)):
                found = True
                self.assertEqual(M._fit_data_block(block, budget), relaxed)
        self.assertTrue(
            found,
            "note 保護版が「非空だが明細行 0」になる budget 帯が見つからな"
            "かった (テスト fixture の調整が必要)",
        )

    def test_header_disclosure_survives_every_fold(self):
        """どの budget でも ``<DATA>`` header の 3 行は落ちない。

        フォールバックが手放すのは per-format の補足説明 (``note:``) であって
        「実値は context に無い」という**開示そのもの**ではない、という
        CHANGELOG / DESIGN.md の記述を固定する。header 3 行が入らなければ
        ``_fit_data_block_core`` は何も返さない実装なので構造的な保証だが、
        条件を緩める改変で静かに崩れうる。
        """
        block = _real_dotenv_block()
        header = block.split("\n")[:3]
        self.assertIn("Real values are NOT in context", header[1])
        seen_folded = False
        for budget in range(0, 900, 5):
            fitted = M._fit_data_block(block, budget)
            if not fitted:
                continue
            self.assertEqual(fitted[:3], header, f"budget={budget}")
            if any(x.startswith(M._OMIT_MARKER_PREFIX) for x in fitted):
                seen_folded = True
        self.assertTrue(seen_folded, "折り畳みが起きる budget 帯が無い")

    def test_keeps_note_when_dropping_it_gains_no_detail_line(self):
        """明細行を持ちえないブロックでは note を落とさない。

        ``(no entries)`` / ``(no keys matched)`` や JSON の root が配列・
        スカラーのブロックは、note 保護を外しても明細行が増えない。ここで
        「明細行 0 なら常に保護を外す」と書くと、得るものが無いのに免責事項
        だけ消える純損失になる (条件を ``>`` 比較にしている理由)。
        """
        block = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: x", "file: .env",
             "format: dotenv",
             "entries: 0",
             "(no entries)",
             "note: real values are not in context.",
             "</DATA>"]
        )
        kept_note = 0
        for budget in range(0, 300, 1):
            fitted = M._fit_data_block(block, budget)
            if not fitted:
                continue
            self.assertLessEqual(len("\n".join(fitted).encode("utf-8")), budget)
            relaxed = M._fit_data_block_core(block, budget, protect_note=False)
            if any(x.startswith("note:") for x in fitted):
                kept_note += 1
            elif any(x.startswith("note:") for x in relaxed):
                self.fail(
                    f"budget={budget}: 明細行が増えないのに note を落とした",
                )
        self.assertGreater(kept_note, 0, "note を保持する budget 帯が無い")

    def test_omit_marker_carries_next_action_when_provided(self):
        fitted = M._fit_data_block(self._BLOCK, 400, next_action="do X")
        joined = "\n".join(fitted)
        self.assertRegex(joined, r"\.\.\. \(\d+ more lines; do X\)")

    def test_omit_marker_has_no_next_action_by_default(self):
        fitted = M._fit_data_block(self._BLOCK, 400)
        joined = "\n".join(fitted)
        self.assertRegex(joined, r"\.\.\. \(\d+ more lines\)")
        self.assertNotIn(";", joined.split("more lines")[1].split(")")[0])


class TestFitDataBlockAcrossFormats(unittest.TestCase):
    """0.26.0 で範囲補正した「dotenv / jsonlike / opaque の 3 形式すべてを対象に」
    を実データで固定する。

    ``build_reason`` が組む実物のブロックを ``_fit_data_block`` に通し、
    形式によらず閉じタグと末尾 note が保護されることを確認する。
    """

    def _reason_for(self, fmt: str, n: int) -> str:
        from io import BytesIO

        from redaction.engine import redact

        if fmt == "dotenv":
            text = "".join(f"KEY_{i:03d}=value_{i:03d}_padding\n" for i in range(n))
            basename = ".env"
        elif fmt == "json":
            import json as _json

            text = _json.dumps(
                {f"key_{i:03d}": f"value_{i:03d}_padding" for i in range(n)}
            )
            basename = "credentials.json"
        else:  # opaque/yaml
            text = "".join(f"key_{i:03d}: value_{i:03d}_padding\n" for i in range(n))
            basename = "secrets.local.yaml"
        data = text.encode("utf-8")
        return redact(BytesIO(data), basename, len(data))

    def test_dotenv_fold_keeps_closing_and_note(self):
        reason = self._reason_for("dotenv", 90)
        fitted = "\n".join(M._fit_data_block(reason, 900))
        self.assertTrue(fitted.endswith("</DATA>"))
        self.assertIn("real values are not in context", fitted)

    def test_jsonlike_fold_keeps_closing_and_note(self):
        reason = self._reason_for("json", 90)
        fitted = "\n".join(M._fit_data_block(reason, 900))
        self.assertTrue(fitted.endswith("</DATA>"))
        self.assertIn(
            "string scalar values are summarized to status tags", fitted,
        )

    def test_opaque_yaml_fold_keeps_closing_and_note(self):
        reason = self._reason_for("yaml", 200)
        fitted = "\n".join(M._fit_data_block(reason, 900))
        self.assertTrue(fitted.endswith("</DATA>"))
        self.assertIn("nested structure not parsed", fitted)


class TestEditDeny(unittest.TestCase):
    def test_minimal_no_keys(self):
        msg = M.edit_deny("Edit", ".env", new_keys=None, kind="new")
        self.assertIn("Edit", msg)
        self.assertIn(".env", msg)
        self.assertIn("`!.env`", msg)
        # block と書く方針 (M2 で再検討)
        self.assertIn("block", msg)

    def test_with_dotenv_keys(self):
        msg = M.edit_deny(
            "Write",
            ".env",
            new_keys=["DATABASE_URL", "JWT_SECRET", "DEBUG"],
            kind="new",
        )
        self.assertIn("Write", msg)
        # キー名がそれぞれ別行で出る
        self.assertIn("DATABASE_URL=", msg)
        self.assertIn("JWT_SECRET=", msg)
        self.assertIn("DEBUG=", msg)
        # 代替案として .env.example 案内
        self.assertIn(".env.example", msg)
        # basename 展開
        self.assertIn("`!.env`", msg)

    def test_with_extra_note_no_keys(self):
        msg = M.edit_deny(
            "Edit", ".env", new_keys=None, extra_note="NOTE: symlink でした。",
            kind="symlink",
        )
        self.assertIn("symlink", msg)
        self.assertIn(".env", msg)

    def test_with_extra_note_and_keys(self):
        msg = M.edit_deny(
            "Write",
            ".env",
            new_keys=["FOO"],
            extra_note="NOTE: 特殊ファイルでした。",
            kind="special",
        )
        self.assertIn("FOO=", msg)
        self.assertIn("特殊ファイル", msg)

    def test_truncation_marker_for_many_keys(self):
        keys = [f"KEY_{i}" for i in range(40)]
        msg = M.edit_deny(
            "Edit", ".env", new_keys=keys, kind="new", max_suggested_keys=30,
        )
        self.assertIn("KEY_0=", msg)
        self.assertIn("KEY_29=", msg)
        # 30 個以上は切り詰め
        self.assertNotIn("KEY_30=", msg)
        self.assertIn("(10 more)", msg)

    def test_overwrite_without_render_reports_unavailable(self):
        """``existing_render`` が空なら黙って省略せず unavailable + next action。

        ``existing_render_status`` 未指定 (0.26.0 以前の呼び出し形) は既知の
        status に該当しないため、従来どおりの汎用ラベルにフォールバックする。
        next action は render 失敗系の新文言 (Read へ誘導しない) になる —
        旧文言「Read も block されますが同じ minimal info が返ります」は
        render 失敗時には二重に事実と違ったため。
        """
        msg = M.edit_deny("Edit", ".env", kind="overwrite")
        self.assertIn(
            "minimal info: unavailable (既存ファイルの読み取り / 解析に失敗)",
            msg,
        )
        self.assertIn("同じ理由で失敗し、情報は返りません", msg)
        self.assertNotIn("Read tool に", msg)
        self.assertNotIn("block されますが", msg)

    def test_overwrite_status_specific_labels(self):
        """``existing_render_status`` の値ごとに理由ラベルが分岐する (0.26.0)。"""
        cases = {
            "unresolved": "既存ファイルが見つからない",
            "not_regular": "通常ファイルではない",
            "stat_failed": "ファイル状態の確認に失敗した",
            "open_failed": "安全な open に失敗した",
            "redact_failed": "内容の解析に失敗した",
            "normalize_failed": "パスの正規化に失敗した",
        }
        for status, expect in cases.items():
            with self.subTest(status=status):
                msg = M.edit_deny(
                    "Edit", ".env", kind="overwrite",
                    existing_render_status=status,
                )
                self.assertIn(expect, msg)

    def test_overwrite_budget_case_keeps_read_redirect(self):
        """budget 超過 (情報自体は取れている) は従来どおり Read 誘導のまま。

        render 自体は成功しているので、``existing_render_status`` を渡しても
        (実際には budget 分岐が優先されるので) Read へ誘導する旧来の
        next action を維持する。render 失敗と budget 超過を混同しないこと
        の確認。
        """
        render = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: " + "x" * 4000, "file: .env",
             "  1. FOO  <type=str>", "</DATA>"]
        )
        msg = M.edit_deny(
            "Edit", ".env", kind="overwrite", existing_render=render,
            existing_render_status="open_failed",  # 無視されるべき
        )
        self.assertIn(
            "minimal info: unavailable (reason の byte 予算に収まらないため省略)",
            msg,
        )
        self.assertIn("Read tool に", msg)
        self.assertIn("block されますが", msg)

    def test_overwrite_many_lines_are_folded_not_dropped(self):
        """行数が多いだけなら入る分を載せ、残りを ``... (N more lines)`` に畳む。"""
        render = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: x", "file: .env"]
            + [f"  {i}. KEY_{i}  <type=str>  <set>  length=8"
               for i in range(400)]
            + ["</DATA>"]
        )
        msg = M.edit_deny(
            "Edit", ".env", kind="overwrite", existing_render=render,
        )
        self.assertLessEqual(len(msg.encode("utf-8")), output.MAX_REASON_BYTES)
        self.assertIn("上書き対象の既存ファイル", msg)
        self.assertIn("  0. KEY_0", msg)
        self.assertRegex(msg, r"  \.\.\. \(\d+ more lines")
        # 閉じタグは畳んでも残す (外殻破壊防御の前提)
        self.assertTrue(msg.split("\n")[-1].startswith("suggestion:"))
        self.assertIn("</DATA>", msg)

    def test_overwrite_render_too_large_reports_budget_reason(self):
        """内容行が 1 行も入らないときは理由を「予算」と明示して 2 行に降りる。

        ``<DATA>`` の header 3 行だけを載せても情報量ゼロの包装が残るだけなので、
        ``_fit_data_block`` は空リストを返し unavailable 分岐に降ろす。
        """
        render = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: " + "x" * 4000, "file: .env",
             "  1. FOO  <type=str>", "</DATA>"]
        )
        msg = M.edit_deny(
            "Edit", ".env", kind="overwrite", existing_render=render,
        )
        self.assertIn(
            "minimal info: unavailable (reason の byte 予算に収まらないため省略)",
            msg,
        )
        self.assertNotIn("<DATA", msg)
        self.assertLessEqual(len(msg.encode("utf-8")), output.MAX_REASON_BYTES)

    def test_overwrite_budget_exhausted_by_keys_omits_section(self):
        """``suggested_keys`` で予算を使い切ると minimal info を丸ごと落とす。

        末尾の除外案内を残す方を優先する設計 (``_edit_existing_info_lines``)。
        """
        keys = ["K" * 120 + str(i) for i in range(30)]
        render = "\n".join(
            ['<DATA untrusted="true" source="redact-hook" guard="g">',
             "NOTE: x", "file: .env", "  1. FOO  <type=str>", "</DATA>"]
        )
        msg = M.edit_deny(
            "Write", ".env", new_keys=keys, kind="overwrite",
            existing_render=render,
        )
        self.assertNotIn("上書き対象の既存ファイル", msg)
        self.assertNotIn("minimal info: unavailable", msg)

    def test_kind_is_required_keyword(self):
        """E6: ``kind`` を省略すると TypeError (誤った文面の silent 選択を防ぐ)。

        既定値を置くと「新規作成しようとしたため block」という **事実と違う説明**
        を返しうるため、呼び忘れを実行時に落とす設計にしている。
        """
        with self.assertRaises(TypeError):
            M.edit_deny("Edit", ".env")  # type: ignore[call-arg]

    def test_overwrite_note_parenthetical_is_tool_neutral(self):
        """``overwrite`` の note 括弧内は tool 中立 (Edit でも事実に反しない)。

        直上のコメントは動詞 (「書き換え」) を tool 中立にした理由を説明するが、
        旧文言は括弧内の rationale だけ「既存の値の喪失」という Write 前提の
        まま取り残されていた (Edit は対象を絞った置換なので「喪失」は起きうる
        破壊の一部でしかなく、tool clause 側の「ファイル全体は失われません」と
        同じ reason 内で自己矛盾していた)。
        """
        for tool_label in ("Edit", "Write"):
            with self.subTest(tool=tool_label):
                msg = M.edit_deny(
                    tool_label, ".env", kind="overwrite", is_dotenv=True,
                )
                self.assertIn("意図しない値の破壊と機密流出を防ぐため", msg)
                self.assertNotIn("既存の値の喪失", msg)


class TestPolicyUnavailable(unittest.TestCase):
    """M3: patterns.txt 読込失敗時の reason 文。"""

    def test_deny_severity_for_bash(self):
        msg = M.policy_unavailable("deny")
        self.assertIn("patterns.txt", msg)
        self.assertIn("Bash", msg)
        # H2: 動詞 "block しました" を採用
        self.assertIn("block しました", msg)
        # LLM が取れる action として「設定を確認」を含む
        self.assertIn("設定を確認", msg)
        # 「管理者に連絡してください」は LLM が取れない指示なので削除済み
        self.assertNotIn("管理者", msg)

    def test_pause_severity_default(self):
        msg = M.policy_unavailable("pause")
        self.assertIn("patterns.txt", msg)
        # H2: 動詞 "再試行してください" (ask_or_deny 系の next action)
        self.assertIn("再試行", msg)
        self.assertNotIn("管理者", msg)

    def test_pause_with_tool_label(self):
        msg = M.policy_unavailable("pause", tool_label="Edit")
        self.assertTrue(msg.startswith("Edit:"))


class TestReadAsk(unittest.TestCase):
    """M2: Read handler の judgement-pause reason 文。"""

    def test_symlink(self):
        msg = M.read_ask("symlink")
        self.assertIn("symlink", msg)
        # 「続行しますか？」(人間 UI 語) は使わない
        self.assertNotIn("続行しますか", msg)
        # LLM が取れる next action
        self.assertIn("再試行", msg)

    def test_special(self):
        msg = M.read_ask("special")
        self.assertIn("FIFO", msg)
        self.assertNotIn("続行しますか", msg)
        self.assertIn("再試行", msg)

    def test_directory(self):
        """内部バックログ: ディレクトリは special (FIFO/socket/device) とは
        別の専用文言を持つこと。"""
        msg = M.read_ask("directory")
        self.assertIn("ディレクトリ", msg)
        self.assertNotIn("FIFO", msg)
        self.assertNotIn("続行しますか", msg)
        self.assertIn("再試行", msg)

    def test_io_error(self):
        msg = M.read_ask("io_error")
        self.assertIn("権限", msg)
        self.assertIn("再試行", msg)

    def test_normalize_failed(self):
        msg = M.read_ask("normalize_failed")
        self.assertIn("正規化", msg)
        self.assertIn("再試行", msg)

    def test_redaction_failed(self):
        msg = M.read_ask("redaction_failed")
        self.assertIn("redaction", msg)

    def test_open_failed(self):
        msg = M.read_ask("open_failed")
        self.assertIn("symlink race", msg)
        self.assertIn("再試行", msg)


class TestEditPause(unittest.TestCase):
    """Edit/Write の judgement-pause reason 文。"""

    def test_normalize_failed_default_label(self):
        msg = M.edit_pause("normalize_failed")
        self.assertTrue(msg.startswith("Edit/Write:"))
        self.assertIn("正規化", msg)
        self.assertIn("再試行", msg)

    def test_io_error_with_label(self):
        msg = M.edit_pause("io_error", tool_label="Write")
        self.assertTrue(msg.startswith("Write:"))
        self.assertIn("権限", msg)

    def test_parent_not_directory(self):
        msg = M.edit_pause("parent_not_directory", tool_label="Edit")
        self.assertIn("親ディレクトリ", msg)
        # H2: ask_or_deny 系の next action
        self.assertIn("再試行", msg)


class TestBashLenient(unittest.TestCase):
    """H2: Bash の静的解析不能ケース (ask_or_allow) の reason 文。"""

    LENIENT_SUFFIX = "判定不能のため確認を挟みます"

    def test_hard_stop(self):
        msg = M.bash_lenient("hard_stop")
        self.assertIn("動的展開", msg)
        # H2: 共通 suffix
        self.assertIn(self.LENIENT_SUFFIX, msg)
        # autonomous モードで通過する旨を文中で明示
        self.assertIn("auto", msg)
        self.assertIn("bypass", msg)

    def test_opaque_prefix(self):
        msg = M.bash_lenient("opaque_prefix")
        self.assertIn("wrapper", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_glob_uncertain(self):
        """内部バックログ: glob operand の不確定 ask が opaque_prefix (wrapper /
        インタプリタ文言) を誤流用していた。専用 kind で文言を分ける。"""
        msg = M.bash_lenient("glob_uncertain")
        self.assertIn("glob", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)
        # 誤流用していた opaque_prefix 側の文言が紛れ込んでいないこと
        self.assertNotIn("wrapper", msg)
        self.assertNotIn("インタプリタ", msg)

    def test_residual_metachar(self):
        msg = M.bash_lenient("residual_metachar")
        self.assertIn("metachar", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_shell_keyword_with_detail(self):
        msg = M.bash_lenient("shell_keyword", detail="if")
        self.assertIn("予約語", msg)
        self.assertIn("(if)", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_shell_keyword_without_detail(self):
        # detail 省略でも壊れない
        msg = M.bash_lenient("shell_keyword")
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_tokenize_failed(self):
        msg = M.bash_lenient("tokenize_failed")
        self.assertIn("tokenize", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)

    def test_normalize_failed(self):
        msg = M.bash_lenient("normalize_failed")
        self.assertIn("正規化", msg)
        self.assertIn(self.LENIENT_SUFFIX, msg)


class TestHookErrorMessages(unittest.TestCase):
    """__main__ wrapper 系の reason 文。LLM が取れる action を明示する。"""

    def test_hook_invocation_error(self):
        msg = M.hook_invocation_error()
        self.assertIn("settings.json", msg)
        # 旧文言「管理者に連絡してください」は LLM が取れる action ではない
        self.assertNotIn("管理者", msg)

    def test_stdin_parse_failed(self):
        msg = M.stdin_parse_failed()
        self.assertIn("hook", msg)
        self.assertIn("envelope", msg)
        # 「安全側で deny します」のような不要な揺れ表現を含めない
        self.assertNotIn("安全側で deny", msg)

    def test_unsupported_platform(self):
        msg = M.unsupported_platform()
        self.assertIn("UNIX", msg)
        self.assertIn("README", msg)

    def test_handler_internal_error_with_type(self):
        msg = M.handler_internal_error("bash", "ValueError")
        self.assertIn("bash", msg)
        self.assertIn("ValueError", msg)
        # ログファイルへの導線を明示
        self.assertIn("redact-hook.log", msg)

    def test_handler_internal_error_without_type(self):
        msg = M.handler_internal_error("read")
        self.assertIn("read", msg)
        self.assertIn("redact-hook.log", msg)


class TestDenyPlainText(unittest.TestCase):
    """0.7.0: deny 系 reason は plain text 出力 (GUARDRAIL_DENY 構造化包装を撤廃)。

    各 deny builder の戻り値が:
    - ``<GUARDRAIL_DENY`` / ``</GUARDRAIL_DENY>`` を含まないこと
    - ``note:`` / ``matched_operand:`` / ``first_token:`` / ``basename:`` /
      ``suggested_keys:`` / ``extra_note:`` / ``suggestion:`` の各行を必要に応じて
      含むこと
    """

    def test_bash_deny_no_envelope(self):
        msg = M.bash_deny(first_token="cat", operand=".env")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertNotIn("</GUARDRAIL_DENY>", msg)
        self.assertIn("note:", msg)
        self.assertIn("matched_operand: .env", msg)
        self.assertIn("first_token: cat", msg)
        self.assertIn("suggestion:", msg)
        self.assertIn("`!.env`", msg)

    def test_edit_deny_no_envelope(self):
        msg = M.edit_deny("Edit", ".env", kind="new")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("basename: .env", msg)
        self.assertIn("suggestion:", msg)

    def test_edit_deny_with_keys_no_envelope(self):
        msg = M.edit_deny("Write", ".env", new_keys=["A", "B"], kind="new")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("suggested_keys:", msg)
        self.assertIn("  A=", msg)
        self.assertIn("  B=", msg)
        self.assertIn("suggestion_alt:", msg)
        self.assertIn(".env.example", msg)

    def test_edit_deny_extra_note_lines(self):
        msg = M.edit_deny(
            "Edit", ".env",
            extra_note="NOTE: symlink 経由だったため",
            kind="symlink",
        )
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("extra_note:", msg)
        self.assertIn("symlink", msg)

    def test_policy_unavailable_deny_plain_text(self):
        msg = M.policy_unavailable("deny")
        self.assertNotIn("<GUARDRAIL_DENY", msg)
        self.assertIn("patterns.txt", msg)

    def test_ask_messages_remain_plain_text(self):
        # 0.6.x 以前から ask 系は plain text。0.7.0 でも継続。
        for kind in ("symlink", "special", "directory", "io_error"):
            msg = M.read_ask(kind)
            self.assertNotIn("<GUARDRAIL_DENY", msg)
        for kind in ("normalize_failed", "io_error", "parent_not_directory"):
            msg = M.edit_pause(kind, tool_label="Edit")
            self.assertNotIn("<GUARDRAIL_DENY", msg)
        for kind in ("hard_stop", "opaque_prefix", "shell_keyword",
                     "program_dynamic", "glob_uncertain"):
            msg = M.bash_lenient(kind)
            self.assertNotIn("<GUARDRAIL_DENY", msg)


class TestVocabularyConsistency(unittest.TestCase):
    """H2: 動詞ルール (block / 一時停止 / 確認を挟む) の最終確認。"""

    def test_deny_uses_block(self):
        # bash_deny
        msg = M.bash_deny(first_token="cat", operand=".env")
        self.assertIn("block しました", msg)
        # edit_deny — E6 の分岐すべてが語彙ルールを守ること
        for kind in ("new", "overwrite", "symlink", "special", "directory"):
            with self.subTest(kind=kind):
                msg2 = M.edit_deny("Write", ".env", kind=kind)
                self.assertIn("block しました", msg2)
        # policy_unavailable(deny)
        msg3 = M.policy_unavailable("deny")
        self.assertIn("block しました", msg3)

    def test_ask_or_deny_uses_retry(self):
        for kind in ("symlink", "special", "directory", "io_error",
                     "normalize_failed", "open_failed"):
            msg = M.read_ask(kind)
            self.assertIn(
                "再試行", msg,
                msg=f"read_ask({kind!r}) lacks 再試行 in: {msg!r}",
            )
        for kind in ("normalize_failed", "io_error", "parent_not_directory"):
            msg = M.edit_pause(kind, tool_label="Edit")
            self.assertIn(
                "再試行", msg,
                msg=f"edit_pause({kind!r}) lacks 再試行 in: {msg!r}",
            )

    def test_ask_or_allow_uses_pause_phrase(self):
        for kind in (
            "hard_stop", "opaque_prefix", "residual_metachar",
            "tokenize_failed", "normalize_failed", "program_dynamic",
            "glob_uncertain",
        ):
            msg = M.bash_lenient(kind)
            self.assertIn(
                "確認を挟みます", msg,
                msg=f"bash_lenient({kind!r}) lacks 確認を挟みます in: {msg!r}",
            )


class TestFitReadReason(unittest.TestCase):
    """``fit_read_reason`` (0.26.0): Read handler 専用の budget 折り畳み。

    Read の reason は他の固定行を持たず ``<DATA>`` ブロックそのものなので、
    budget を素通しで ``_fit_data_block`` に渡すだけでよい。以前は Read だけ
    折り畳み機構が無く、``core.output._truncate`` の盲目 cut に単独で
    晒されていた (Bash は 0.23.0 の除外案内保護、Edit は 0.20.0 の
    ``_fit_data_block`` 配線があったが、Read には同等のものが無かった)。
    """

    def _big_dotenv_reason(self, n: int) -> str:
        from io import BytesIO

        from redaction.engine import redact

        text = "".join(f"KEY_{i:03d}=value_{i:03d}_padding_padding\n" for i in range(n))
        data = text.encode("utf-8")
        return redact(BytesIO(data), ".env", len(data))

    def test_within_budget_is_returned_unchanged(self):
        reason = self._big_dotenv_reason(5)
        self.assertEqual(M.fit_read_reason(reason), reason)

    def test_over_budget_keeps_closing_tag_and_note(self):
        reason = self._big_dotenv_reason(90)
        self.assertGreater(len(reason.encode("utf-8")), output.MAX_REASON_BYTES)
        fitted = M.fit_read_reason(reason)
        self.assertLessEqual(len(fitted.encode("utf-8")), output.MAX_REASON_BYTES)
        self.assertTrue(fitted.endswith("</DATA>"))
        self.assertIn("real values are not in context", fitted)
        self.assertRegex(fitted, r"\.\.\. \(\d+ more lines\)")

    def test_omit_marker_does_not_suggest_read_tool(self):
        """Read の中で「Read tool を使え」は自己参照で無意味。

        Bash/Edit の折り畳み (``_fold_data_block`` / ``_edit_existing_info_lines``)
        は omit marker に Read への誘導を付けるが、``fit_read_reason`` 自身は
        付けない設計であることを固定する。
        """
        reason = self._big_dotenv_reason(90)
        fitted = M.fit_read_reason(reason)
        self.assertNotIn("use Read tool", fitted)

    def test_custom_budget_is_respected(self):
        reason = self._big_dotenv_reason(30)
        fitted = M.fit_read_reason(reason, budget=800)
        self.assertLessEqual(len(fitted.encode("utf-8")), 800)
        self.assertTrue(fitted.endswith("</DATA>"))

    def test_unfoldable_extreme_case_returns_input_unchanged(self):
        """header 3 行すら budget に収まらない極端なケースは折り畳みを諦める。

        ``core.output.make_deny`` の ``_truncate`` が最終防御として働くので、
        ここで無理に何かを作ろうとせず入力をそのまま返す。
        """
        reason = self._big_dotenv_reason(5)
        self.assertEqual(M.fit_read_reason(reason, budget=5), reason)


def _keyonly_block(n: int = 200, name_len: int = 46) -> tuple[list[str], str]:
    """keys-only scan の ``<DATA>`` ブロックを実物の builder で組む。

    鍵名の長さは ``sanitize_key`` の上限 (128) 未満で、レビューの実測 fixture
    (``.env.longkeys``: 200 keys / 70,600 byte) と同程度のものを使う。

    Returns:
        ``(keys, block)`` — 鍵名リストと ``build_reason`` 済みブロック。
    """
    from redaction.engine import build_reason
    from redaction.keyonly_scan import format_keyonly

    pad = "X" * max(0, name_len - 20)
    keys = [f"SERVICE_API_KEY_{pad}_{i:03d}" for i in range(n)]
    body = format_keyonly(keys, 70600, fmt_hint="opaque")
    return keys, build_reason(".env.longkeys", "opaque", body)


class TestKeyonlyFoldKeepsKeys(unittest.TestCase):
    """0.26.0 レビュー P1-1: 予算が余っているのに鍵名 0 個、を作らない。

    ``format_keyonly`` が全鍵名を 1 行に並べていた頃は、その 1 行が残予算に
    入らないと行ごと落ち、Read で 3,072 byte 中 2,753 byte を使い残したまま
    鍵名が 0 個になっていた (旧 0.25.0 は盲目 cut だったので 55 個見えていた
    = 本 PR の退行)。fixture 単位の assertion だけだと別サイズでの再混入を
    拾えないので、**性質**として固定する。
    """

    def setUp(self):
        self.keys, self.block = _keyonly_block()

    def _visible_keys(self, fitted: list[str]) -> int:
        joined = "\n".join(fitted)
        return sum(1 for k in self.keys if k in joined)

    def test_no_budget_left_over_with_zero_keys(self):
        for budget in range(400, 3300, 25):
            with self.subTest(budget=budget):
                fitted = M._fit_data_block(self.block, budget)
                if not fitted:
                    continue
                used = len("\n".join(fitted).encode("utf-8"))
                self.assertLessEqual(used, budget)
                leftover = budget - used
                if leftover >= 1024:
                    self.assertGreater(
                        self._visible_keys(fitted),
                        0,
                        f"budget={budget}: {leftover} byte 余らせたまま鍵名 0 個",
                    )

    def test_full_reason_budget_shows_many_keys(self):
        fitted = M._fit_data_block(self.block, output.MAX_REASON_BYTES)
        self.assertGreater(self._visible_keys(fitted), 20)
        self.assertEqual(fitted[-1], "</DATA>")
        self.assertTrue(fitted[-2].startswith("note:"))

    def test_fit_read_reason_path_shows_many_keys(self):
        fitted = M.fit_read_reason(self.block)
        self.assertLessEqual(
            len(fitted.encode("utf-8")), output.MAX_REASON_BYTES
        )
        self.assertGreater(sum(1 for k in self.keys if k in fitted), 20)


class TestOmitMarkerUnit(unittest.TestCase):
    """omit marker の省略単位ラベル (0.26.0 レビュー P1-1)。

    ``... (2 more lines)`` では「鍵名を何個落としたか」が読み手に伝わらない。
    keys-only scan のブロックだけ単位を ``keys`` に切り替える。単位の判定は
    ``format:`` 行の marker を見て行う (``core`` → ``redaction`` の依存を
    作らないため、marker 文字列は両側に置いて下の assumption test で突合する)。
    """

    def test_keyonly_block_marker_counts_keys(self):
        _keys, block = _keyonly_block()
        joined = "\n".join(M._fit_data_block(block, 900))
        self.assertRegex(joined, r"\.\.\. \(\d+ more keys\)")

    def test_keyonly_marker_keeps_next_action(self):
        _keys, block = _keyonly_block()
        joined = "\n".join(M._fit_data_block(block, 900, next_action="do X"))
        self.assertRegex(joined, r"\.\.\. \(\d+ more keys; do X\)")

    def test_non_keyonly_block_marker_still_counts_lines(self):
        from io import BytesIO

        from redaction.engine import redact

        text = "".join(f"KEY_{i:03d}=value_{i:03d}_padding\n" for i in range(90))
        data = text.encode("utf-8")
        reason = redact(BytesIO(data), ".env", len(data))
        joined = "\n".join(M._fit_data_block(reason, 900))
        self.assertRegex(joined, r"\.\.\. \(\d+ more lines\)")

    def test_keyonly_marker_counts_hidden_keys_not_dropped_lines(self):
        """**0.26.0 外部レビュー R1 の本体**。

        折り畳みは preview 上限 (``PREVIEW_CAP`` = 60) の **さらに内側**で
        効くので、「落とした行数」を出すと preview 段階で隠れた分が数から
        消える。500 鍵の 60 行 preview から 23 行しか残らなくても
        ``... (37 more keys)`` としか言わず、477 件不可視という事実を隠して
        いた (レビュー指摘そのもの)。
        """
        keys, block = _keyonly_block(n=500)
        fitted = M._fit_data_block(block, 1200)
        visible = M._count_detail_lines(fitted)
        self.assertGreater(visible, 0, "前提: 鍵行が残る budget で測る")
        self.assertLess(visible, len(keys), "前提: 一部しか見えていない")
        n = self._marker_count(fitted)
        self.assertEqual(
            n,
            len(keys) - visible,
            f"marker が {n} 件と言っているが、見えていない鍵は"
            f" {len(keys) - visible} 件 (entries={len(keys)} / 表示={visible})",
        )

    def test_marker_and_visible_keys_account_for_every_key(self):
        """全 budget 帯で「見えている鍵 + marker 件数 == entries」を固定する。

        単一 fixture の assertion だと別サイズで再混入するため、**性質**として
        押さえる (``TestKeyonlyFoldKeepsKeys`` と同じ趣旨)。
        """
        for n in (61, 100, 200, 500):
            keys, block = _keyonly_block(n=n)
            for budget in range(400, 3300, 50):
                with self.subTest(n=n, budget=budget):
                    fitted = M._fit_data_block(block, budget)
                    if not fitted:
                        continue
                    self.assertLessEqual(
                        len("\n".join(fitted).encode("utf-8")), budget
                    )
                    count = self._marker_count(fitted)
                    if count is None:
                        continue
                    self.assertEqual(
                        count + M._count_detail_lines(fitted), len(keys)
                    )

    def test_marker_does_not_overstate_when_non_key_lines_are_dropped(self):
        """``omitted`` (落とした行数) は明細行以外も数えるので鍵数を過大に言う。

        鍵 5 個のファイルをきつく畳むと ``scanned_bytes:`` / ``keys (...)``
        見出し / ``note:`` も落ちるため、行数ベースでは ``entries: 5`` と
        ``... (9 more keys)`` が同居していた。
        """
        keys, block = _keyonly_block(n=5)
        for budget in range(200, 700, 5):
            with self.subTest(budget=budget):
                count = self._marker_count(M._fit_data_block(block, budget))
                if count is not None:
                    self.assertLessEqual(count, len(keys))

    def test_omit_count_rules(self):
        """件数の決め方 3 通りを直接固定する (``_omit_count``)。"""
        kept = [
            '<DATA untrusted="true">', "NOTE: x", "file: .env",
            "format: opaque (large, keys-only scan)", "entries: 500",
            "  1. A", "  2. B",
        ]
        self.assertEqual(M._count_detail_lines(kept), 2)
        # 総数が読めない → 従来どおり「落とした行数」
        self.assertEqual(M._omit_count(7, kept, None), 7)
        # 読めた → 総数 − 残した明細行数 (preview 段階で隠れた分も含む)
        self.assertEqual(M._omit_count(7, kept, 500), 498)
        # 総数より明細行が多い = 前提が崩れたブロック → 行数に戻す
        self.assertEqual(M._omit_count(7, kept, 2), 7)

    def test_falls_back_to_line_count_when_entries_is_unreadable(self):
        """``entries:`` を読めないブロックは従来どおり「落とした行数」。

        件数の意味が静かに入れ替わるより、読めた時だけ厳密解にする。
        """
        _keys, block = _keyonly_block(n=200)
        broken = block.replace("entries: 200", "entries: n/a", 1)
        self.assertIsNone(M._entries_total(broken))

        fitted = M._fit_data_block(broken, 1200)
        # note 保護つきの段が採用される前提 (明細行が残る budget を選んでいる)
        self.assertTrue(fitted[-2].startswith("note:"), fitted[-2:])
        body = broken.split("\n")[:-2]  # note と閉じタグは body ではない
        kept_body = [
            x for x in fitted
            if not x.startswith(M._OMIT_MARKER_PREFIX)
            and not x.startswith("note:")
            and x != M._DATA_CLOSING_TAG
        ]
        self.assertEqual(
            self._marker_count(fitted), len(body) - len(kept_body)
        )

    def test_marker_width_reserve_is_not_inflated_by_the_total(self):
        """件数の**桁が変わらない**限り、表示できる鍵行を 1 行も減らさない。

        marker の予約幅は件数の桁数で決まる。``entries:`` の総数 (最大
        ``MAX_KEYS`` = 500) で常に予約すると、桁が増えたぶん (67 → 500 で
        +1 byte) だけ収まっていた鍵行が落ちる。``_fit_data_block_core`` は
        「落とした行数」の幅で予約し、実際に出す件数がその幅に収まらない
        ときだけ畳み直す。

        比較対象は **同じ byte 数で ``entries:`` だけ読めなくしたブロック**
        (``n/a`` は ``100`` と同じ 3 文字)。100 鍵なら件数は行数ベースでも
        鍵数ベースでも 2 桁なので、予約幅は同じでなければならない = 明細行数
        は全 budget で一致する。
        """
        _keys, block = _keyonly_block(n=100)
        broken = block.replace("entries: 100", "entries: n/a", 1)
        self.assertEqual(len(block.encode("utf-8")), len(broken.encode("utf-8")))
        for budget in range(300, 3300, 5):
            with self.subTest(budget=budget):
                self.assertEqual(
                    M._count_detail_lines(M._fit_data_block(block, budget)),
                    M._count_detail_lines(M._fit_data_block(broken, budget)),
                )

    def test_wider_count_costs_at_most_one_detail_line(self):
        """件数の桁が実際に増えるケースの代償を **1 行以内**に押さえる。

        500 鍵では鍵数ベースの件数 (3 桁) が行数ベース (2 桁) より 1 byte
        太るので、きつい budget では鍵行が 1 行減りうる (逆に増える budget も
        ある)。ここを 2 行以上に広げる変更は情報量の退行なので落とす。
        """
        _keys, block = _keyonly_block(n=500)
        broken = block.replace("entries: 500", "entries: n/a", 1)
        for budget in range(300, 3300, 5):
            with self.subTest(budget=budget):
                self.assertLessEqual(
                    abs(
                        M._count_detail_lines(M._fit_data_block(block, budget))
                        - M._count_detail_lines(M._fit_data_block(broken, budget))
                    ),
                    1,
                )

    def test_fold_terminates_on_pathological_input(self):
        """予約の畳み直しループが止まる (hook は 2 秒 timeout の下で動く)。

        件数の桁が最大 (``MAX_KEYS``) / next_action が長い / budget が極小、の
        組合せでも戻ってくること。実測では畳み直しは最大 2 周。
        """
        from redaction.engine import build_reason
        from redaction.keyonly_scan import MAX_KEYS, format_keyonly

        block = build_reason(
            ".env",
            "opaque",
            format_keyonly([f"K{i:03d}" for i in range(MAX_KEYS)], 10**9,
                           fmt_hint="opaque"),
        )
        long_action = M._OMIT_ACTION_USE_READ * 4
        for budget in range(50, 900, 7):
            with self.subTest(budget=budget):
                fitted = M._fit_data_block(block, budget, next_action=long_action)
                self.assertLessEqual(
                    len("\n".join(fitted).encode("utf-8")), budget
                )

    @staticmethod
    def _marker_count(fitted: list[str]) -> int | None:
        """省略マーカーの件数 (無ければ None)。"""
        import re

        for line in fitted:
            m = re.match(r"^  \.\.\. \((\d+) more ", line)
            if m:
                return int(m.group(1))
        return None

    def test_keyonly_marker_constant_matches_format_keyonly_output(self):
        """``core.messages`` 側の sniff 用定数を生成側の出力と突合する。

        ``TestDataBlockAssumptions`` と同じ趣旨 — ここがずれると、単位ラベルが
        **予算超過時にだけ** 静かに ``lines`` へ戻る。
        """
        from redaction.keyonly_scan import format_keyonly

        header = format_keyonly(["A"], 10, fmt_hint="opaque").split("\n")[0]
        self.assertTrue(header.startswith("format:"))
        self.assertIn(M._KEYONLY_SCAN_MARKER, header)


class TestEditSuggestionAltUpdateBudget(unittest.TestCase):
    """0.26.0 レビュー P2-1: ``suggestion_alt`` が minimal info を押し出さない。

    ``edit_deny`` は tail (``suggestion_alt`` を含む) を先に組んでから残りを
    minimal info の予算に回すため、alt 文言が長いとセクションが丸ごと消える
    (``minimal info: unavailable`` すら出ない)。文言の長さそのものが予算の
    安全余裕なので、**既定文言との差**として上限を固定する。
    """

    def test_update_alt_is_not_materially_longer_than_default(self):
        default_len = len(M._EDIT_SUGGESTION_ALT_DEFAULT.encode("utf-8"))
        update_len = len(M._EDIT_SUGGESTION_ALT_UPDATE.encode("utf-8"))
        self.assertLessEqual(
            update_len,
            default_len + 32,
            "suggestion_alt (update) が既定文言より大幅に長い。"
            " minimal info セクションを押し出す (レビュー実測: corpus 30 件)",
        )


if __name__ == "__main__":
    unittest.main()
