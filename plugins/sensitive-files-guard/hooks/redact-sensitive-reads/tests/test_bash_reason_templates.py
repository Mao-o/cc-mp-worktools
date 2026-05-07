"""bash_deny の 9 カテゴリ別テンプレ (E3, 0.10.0) の単体テスト。

各 category builder が:

- intent を表す note 文を含む
- ``matched_operand:`` / ``first_token:`` の共通 meta 行を含む
- 推奨 suggestion (direnv / 1Password / git rm --cached / --exclude= 等) を含む
- ``file_render`` を受けた時に ``minimal info (Read 同等):`` ラベルを含み
  ``<DATA untrusted="true"`` 包装も内部に含む
- ``dotenv_info`` を受けた時に ``head/tail`` で正しい先頭/末尾切り出しを行う
- ``grep_keys`` + ``dotenv_info`` を受けた時に ``matched_pattern_keys`` を出す

を確認する。
"""
from __future__ import annotations

import unittest

from _testutil import FIXTURES  # noqa: F401

from core import messages as M


# ---- shared fixture helpers ---------------------------------------------


_DUMMY_FILE_RENDER = (
    '<DATA untrusted="true" source="redact-hook" guard="sfg-v1">\n'
    "NOTE: sanitized data from a sensitive file. Real values are NOT in context.\n"
    "file: .env\n"
    "format: dotenv\n"
    "entries: 2\n"
    "keys (in order):\n"
    "  1. DATABASE_URL  <type=url>  <set>  length=42\n"
    "  2. JWT_SECRET    <type=jwt prefix=\"ey\">  <set>  length=287\n"
    "</DATA>"
)


def _dummy_dotenv_info(num_keys: int = 5) -> dict:
    """テスト用の dotenv_info dict を生成する。

    キー: KEY_1 .. KEY_N。1 番目だけ status=<placeholder>、最後だけ <empty>、
    残りは <set>。
    """
    keys: list[dict] = []
    for i in range(1, num_keys + 1):
        if i == 1:
            entry = {
                "name": f"KEY_{i}",
                "type": "str",
                "status": ["<placeholder>"],
                "length": 8,
                "placeholder": "your_secret_here",
            }
        elif i == num_keys:
            entry = {
                "name": f"KEY_{i}",
                "type": "str",
                "status": ["<empty>"],
                "length": 0,
            }
        else:
            entry = {
                "name": f"KEY_{i}",
                "type": "str",
                "status": ["<set>"],
                "length": 12,
            }
        keys.append(entry)
    return {"format": "dotenv", "entries": num_keys, "keys": keys}


# ---- read_full ----------------------------------------------------------


class TestReadFull(unittest.TestCase):
    def test_cat_intent_and_meta(self):
        msg = M.bash_deny(first_token="cat", operand=".env")
        self.assertIn("全体を閲覧", msg)
        self.assertIn("matched_operand: .env", msg)
        self.assertIn("first_token: cat", msg)
        self.assertIn("`!.env`", msg)

    def test_cat_with_file_render_includes_data_block(self):
        msg = M.bash_deny(
            first_token="cat", operand=".env",
            file_render=_DUMMY_FILE_RENDER,
        )
        self.assertIn("minimal info (Read 同等):", msg)
        self.assertIn('<DATA untrusted="true"', msg)
        self.assertIn("DATABASE_URL", msg)

    def test_xxd_uses_same_template(self):
        msg = M.bash_deny(first_token="xxd", operand=".env")
        # read_full category なので intent 文は同一
        self.assertIn("全体を閲覧", msg)
        self.assertIn("first_token: xxd", msg)


# ---- read_partial -------------------------------------------------------


class TestReadPartial(unittest.TestCase):
    def test_head_default_n(self):
        msg = M.bash_deny(first_token="head", operand=".env", command="head .env")
        self.assertIn("先頭 10 行", msg)

    def test_head_n_flag(self):
        msg = M.bash_deny(
            first_token="head", operand=".env",
            command="head -n 3 .env",
            dotenv_info=_dummy_dotenv_info(num_keys=5),
        )
        self.assertIn("先頭 3 行", msg)
        self.assertIn("keys (先頭 3, 全 5 件):", msg)
        self.assertIn("KEY_1", msg)
        self.assertIn("KEY_3", msg)
        self.assertNotIn("KEY_4", msg)
        # placeholder の matched ヒントが入る
        self.assertIn('matched="your_secret_here"', msg)

    def test_tail_n_flag(self):
        msg = M.bash_deny(
            first_token="tail", operand=".env",
            command="tail -n 2 .env",
            dotenv_info=_dummy_dotenv_info(num_keys=5),
        )
        self.assertIn("末尾 2 行", msg)
        self.assertIn("keys (末尾 2, 全 5 件):", msg)
        self.assertIn("KEY_4", msg)
        self.assertIn("KEY_5", msg)
        self.assertNotIn("KEY_1", msg)

    def test_tail_short_form_dash_n(self):
        # `-5` 形式 (BSD-style) で 5 行を抽出
        msg = M.bash_deny(
            first_token="tail", operand=".env", command="tail -5 .env",
            dotenv_info=_dummy_dotenv_info(num_keys=10),
        )
        self.assertIn("末尾 5 行", msg)
        self.assertIn("keys (末尾 5, 全 10 件):", msg)

    def test_head_lines_long_form(self):
        msg = M.bash_deny(
            first_token="head", operand=".env",
            command="head --lines=4 .env",
            dotenv_info=_dummy_dotenv_info(num_keys=6),
        )
        self.assertIn("先頭 4 行", msg)

    def test_head_falls_back_to_file_render_when_no_dotenv_info(self):
        msg = M.bash_deny(
            first_token="head", operand=".env",
            command="head -n 2 .env",
            file_render=_DUMMY_FILE_RENDER,
        )
        # dotenv_info がないので minimal info に降りる
        self.assertIn("minimal info (Read 同等):", msg)
        self.assertIn("DATABASE_URL", msg)


# ---- search (E4) --------------------------------------------------------


class TestSearch(unittest.TestCase):
    def test_grep_with_matched_pattern_key(self):
        info = {
            "format": "dotenv",
            "entries": 2,
            "keys": [
                {"name": "DATABASE_URL", "type": "url",
                 "status": ["<set>"], "length": 42},
                {"name": "JWT_SECRET", "type": "jwt",
                 "status": ["<set>"], "length": 287, "prefix": "ey"},
            ],
        }
        msg = M.bash_deny(
            first_token="grep", operand=".env",
            command="grep DATABASE_URL .env",
            dotenv_info=info,
            grep_keys=["DATABASE_URL"],
        )
        self.assertIn("matched_pattern_keys: [DATABASE_URL]", msg)
        self.assertIn("result:", msg)
        self.assertIn("DATABASE_URL  <type=url>", msg)
        # 全鍵 list は出さない (matched があれば minimal info を抑止)
        self.assertNotIn("minimal info (Read 同等):", msg)

    def test_grep_with_nomatch_pattern_key(self):
        info = {
            "format": "dotenv",
            "entries": 1,
            "keys": [
                {"name": "DATABASE_URL", "type": "url",
                 "status": ["<set>"], "length": 42},
            ],
        }
        msg = M.bash_deny(
            first_token="grep", operand=".env",
            command="grep MISSING_KEY .env",
            dotenv_info=info,
            grep_keys=["MISSING_KEY"],
        )
        self.assertIn("nomatch_pattern_keys: [MISSING_KEY]", msg)

    def test_grep_falls_back_to_minimal_info_when_no_pattern(self):
        info = {"format": "dotenv", "entries": 0, "keys": []}
        msg = M.bash_deny(
            first_token="grep", operand=".env",
            command="grep -i .env",
            dotenv_info=info,
            grep_keys=[],  # pattern 抽出失敗
            file_render=_DUMMY_FILE_RENDER,
        )
        # pattern 抽出失敗なので全鍵 list (minimal info) に降りる
        self.assertIn("minimal info (Read 同等):", msg)

    def test_grep_pattern_keys_without_dotenv_info(self):
        # dotenv_info がない (file_render 失敗) ケース
        msg = M.bash_deny(
            first_token="grep", operand=".env",
            command="grep DATABASE_URL .env",
            grep_keys=["DATABASE_URL"],
        )
        self.assertIn("pattern_keys: [DATABASE_URL]", msg)
        self.assertNotIn("matched_pattern_keys:", msg)

    def test_grep_other_keys_suggestion(self):
        info = _dummy_dotenv_info(num_keys=5)
        # info は KEY_1=<placeholder>, KEY_5=<empty>
        msg = M.bash_deny(
            first_token="grep", operand=".env",
            command="grep KEY_2 .env",
            dotenv_info=info,
            grep_keys=["KEY_2"],
        )
        self.assertIn("matched_pattern_keys: [KEY_2]", msg)
        # placeholder と empty が他にあるので suggestion として出す
        self.assertIn("1 件の <placeholder>", msg)
        self.assertIn("1 件の <empty>", msg)

    def test_grep_intent_note(self):
        msg = M.bash_deny(first_token="rg", operand=".env",
                          command="rg DATABASE .env")
        self.assertIn("検索しよう", msg)
        self.assertIn("first_token: rg", msg)


# ---- mutate -------------------------------------------------------------


class TestMutate(unittest.TestCase):
    def test_awk_intent_note(self):
        msg = M.bash_deny(first_token="awk", operand=".env",
                          command="awk -F= '{print $1}' .env",
                          file_render=_DUMMY_FILE_RENDER)
        self.assertIn("加工", msg)
        self.assertIn("minimal info (Read 同等):", msg)
        self.assertIn("patch / diff", msg)

    def test_sed_intent_note(self):
        msg = M.bash_deny(first_token="sed", operand=".env",
                          command="sed s/X/Y/g .env",
                          file_render=_DUMMY_FILE_RENDER)
        self.assertIn("加工", msg)
        self.assertIn("first_token: sed", msg)


# ---- load ---------------------------------------------------------------


class TestLoad(unittest.TestCase):
    def test_source_recommends_direnv(self):
        msg = M.bash_deny(first_token="source", operand=".env",
                          command="source .env",
                          file_render=_DUMMY_FILE_RENDER)
        self.assertIn("shell に load", msg)
        self.assertIn("direnv", msg)
        self.assertIn("dotenv-cli", msg)
        self.assertIn("1Password CLI", msg)

    def test_dot_alias(self):
        msg = M.bash_deny(first_token=".", operand=".env",
                          command=". .env",
                          file_render=_DUMMY_FILE_RENDER)
        self.assertIn("shell に load", msg)
        self.assertIn("first_token: .", msg)


# ---- move ---------------------------------------------------------------


class TestMove(unittest.TestCase):
    def test_cp_recommends_secrets_manager(self):
        msg = M.bash_deny(first_token="cp", operand=".env",
                          command="cp .env backup.env")
        self.assertIn("コピー / 移動", msg)
        self.assertIn("1Password CLI", msg)
        self.assertIn("git-secret", msg)
        self.assertIn(".env.example", msg)

    def test_mv_intent_note(self):
        msg = M.bash_deny(first_token="mv", operand=".env",
                          command="mv .env old.env")
        self.assertIn("first_token: mv", msg)
        self.assertIn("コピー / 移動", msg)


# ---- history ------------------------------------------------------------


class TestHistory(unittest.TestCase):
    def test_git_show_recommends_rm_cached(self):
        msg = M.bash_deny(first_token="git", operand="HEAD:.env",
                          command="git show HEAD:.env")
        self.assertIn("git", msg)
        self.assertIn("commit", msg)
        self.assertIn("git rm --cached", msg)
        self.assertIn("rotate", msg)
        # basename は HEAD:.env から ".env" を抽出
        self.assertIn("`!.env`", msg)

    def test_git_diff_uses_history_template(self):
        msg = M.bash_deny(first_token="git", operand=".env",
                          command="git diff .env")
        self.assertIn("git rm --cached", msg)


# ---- transfer -----------------------------------------------------------


class TestTransfer(unittest.TestCase):
    def test_curl_intent_note(self):
        msg = M.bash_deny(first_token="curl", operand=".env",
                          command="curl file://.env")
        self.assertIn("転送", msg)
        self.assertIn("非推奨", msg)
        self.assertIn("Vault", msg)

    def test_scp_intent_note(self):
        msg = M.bash_deny(first_token="scp", operand=".env",
                          command="scp .env host:")
        self.assertIn("転送", msg)
        self.assertIn("first_token: scp", msg)


# ---- archive ------------------------------------------------------------


class TestArchive(unittest.TestCase):
    def test_tar_recommends_exclude(self):
        msg = M.bash_deny(first_token="tar", operand=".env",
                          command="tar czf b.tar .env")
        self.assertIn("アーカイブ", msg)
        self.assertIn("--exclude=.env", msg)
        self.assertIn("zip なら `-x .env`", msg)

    def test_zip_uses_archive_template(self):
        msg = M.bash_deny(first_token="zip", operand=".env",
                          command="zip out.zip .env")
        self.assertIn("--exclude=.env", msg)


# ---- generic fallback ---------------------------------------------------


class TestGenericFallback(unittest.TestCase):
    def test_unknown_first_token_uses_generic(self):
        msg = M.bash_deny(first_token="myunknowntool", operand=".env")
        self.assertIn("機密パターンに一致", msg)
        self.assertIn("matched_operand: .env", msg)
        self.assertIn("`!.env`", msg)

    def test_generic_with_file_render(self):
        msg = M.bash_deny(first_token="myunknowntool", operand=".env",
                          file_render=_DUMMY_FILE_RENDER)
        self.assertIn("minimal info (Read 同等):", msg)


# ---- backwards compatibility (positional 2 args) -----------------------


class TestBackwardsCompat(unittest.TestCase):
    """0.7.0〜0.9.0 の `bash_deny(first_token, operand)` 呼び出しを 0.10.0 で
    壊さないこと。新 keyword 引数を渡さなくても動作する。"""

    def test_positional_only_is_generic(self):
        msg = M.bash_deny(first_token="myunknowntool", operand=".env")
        self.assertIn("note:", msg)
        self.assertIn("matched_operand:", msg)
        self.assertIn("first_token:", msg)
        self.assertIn("suggestion:", msg)

    def test_positional_only_known_category_still_works(self):
        # known category でも file_render なしで「素」のテンプレが返る
        msg = M.bash_deny(first_token="cat", operand=".env")
        self.assertIn("note:", msg)
        # file_render なしでも intent note は出る
        self.assertIn("全体を閲覧", msg)
        # minimal info ラベルは file_render があるときのみ
        self.assertNotIn("minimal info (Read 同等):", msg)


if __name__ == "__main__":
    unittest.main()
