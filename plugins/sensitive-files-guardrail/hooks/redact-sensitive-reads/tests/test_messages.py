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


class TestFitDataBlockNoteProtection(unittest.TestCase):
    """0.26.0: ``_fit_data_block`` は閉じタグと一緒に末尾 ``note:`` 行も守る。

    0.26.0 以前は ``</DATA>`` だけを固定 tail 扱いし、直前の per-format note
    (``format_dotenv`` 等が必ず最後に置く「実値は無い」の免責事項) を可変長
    body の一部として畳んでいた。実測で 20 key 前後から note が消えることを
    確認した (edit_deny 経由の折り畳みで実証済み)。

    ただし note を守ろうとして key 行が **前より減る**のは本末転倒なので、
    「note を守れないなら守らずに済ませる (旧来動作)」の 2 段構えを固定する。
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

        note 保護に使った budget 分だけ key 行が 1 行分減ることは意図した
        トレードオフ (note を残す方を優先する設計) なので regression としては
        扱わない。regression として絶対に避けたいのは「旧アルゴリズムなら
        鍵情報を出せていたのに、note を守ろうとして中身ゼロ (unavailable) に
        落ちる」ケースだけ — これは advisor 指摘の 2 段構えフォールバックで
        防いでいる。
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
        for kind in ("symlink", "special", "io_error"):
            msg = M.read_ask(kind)
            self.assertNotIn("<GUARDRAIL_DENY", msg)
        for kind in ("normalize_failed", "io_error", "parent_not_directory"):
            msg = M.edit_pause(kind, tool_label="Edit")
            self.assertNotIn("<GUARDRAIL_DENY", msg)
        for kind in ("hard_stop", "opaque_prefix", "shell_keyword",
                     "program_dynamic"):
            msg = M.bash_lenient(kind)
            self.assertNotIn("<GUARDRAIL_DENY", msg)


class TestVocabularyConsistency(unittest.TestCase):
    """H2: 動詞ルール (block / 一時停止 / 確認を挟む) の最終確認。"""

    def test_deny_uses_block(self):
        # bash_deny
        msg = M.bash_deny(first_token="cat", operand=".env")
        self.assertIn("block しました", msg)
        # edit_deny — E6 の 4 分岐すべてが語彙ルールを守ること
        for kind in ("new", "overwrite", "symlink", "special"):
            with self.subTest(kind=kind):
                msg2 = M.edit_deny("Write", ".env", kind=kind)
                self.assertIn("block しました", msg2)
        # policy_unavailable(deny)
        msg3 = M.policy_unavailable("deny")
        self.assertIn("block しました", msg3)

    def test_ask_or_deny_uses_retry(self):
        for kind in ("symlink", "special", "io_error", "normalize_failed",
                     "open_failed"):
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


if __name__ == "__main__":
    unittest.main()
