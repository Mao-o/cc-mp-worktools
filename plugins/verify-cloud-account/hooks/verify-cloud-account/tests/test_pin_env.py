"""プロジェクトごとの固定の提案 (`scripts/pin_env.py` と builder の `pin-env`) のテスト。

`pin-env` は読み取り専用 (builder の D16): settings.local.json を含め何も書かない。
期待値は D3 に従い既定で隠し、profile 名・構成名・alias 名だけを出す。
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from scripts import accounts_builder as builder  # noqa: E402
from scripts import pin_env  # noqa: E402
from services import aws, firebase, gcloud  # noqa: E402

ACCOUNT = "111122223333"
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "probe",
    "GIT_AUTHOR_EMAIL": "probe@example.invalid",
    "GIT_COMMITTER_NAME": "probe",
    "GIT_COMMITTER_EMAIL": "probe@example.invalid",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, **_GIT_ENV},
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


_SNIPPET_HEADER = 'settings.local.json の "env" に足す内容 (既存のキーは残す):'


def env_snippet(test: unittest.TestCase, lines: list[str]) -> dict:
    """出力の「env に足す内容」を JSON として読む。読めなければ test を失敗にする。"""
    body = "\n".join(lines[lines.index(_SNIPPET_HEADER) + 1:])
    try:
        return json.loads("{" + body + "}")
    except ValueError as e:
        test.fail(f"env の断片が JSON として読めない ({e}): {body!r}")


class _TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._isolation = _testutil.start_isolation(self.tmp / "isolated")
        self.addCleanup(self._isolation.stop)


class TestPlanAws(_TmpBase):
    def _env(self, text: str) -> dict:
        path = self.tmp / "aws_config"
        path.write_text(text, encoding="utf-8")
        return {"AWS_CONFIG_FILE": str(path)}

    def test_single_profile_is_the_value(self):
        env = self._env(f"[profile dev]\nsso_account_id = {ACCOUNT}\n")
        plan = pin_env.plan_aws(ACCOUNT, env)
        self.assertIsNone(plan.problem)
        self.assertEqual(plan.pins, (pin_env.Pin("AWS_PROFILE", "dev", ("dev",)),))

    def test_several_profiles_leave_the_choice_to_the_user(self):
        env = self._env(
            f"[profile admin]\nsso_account_id = {ACCOUNT}\n"
            f"[profile readonly]\nrole_arn = arn:aws:iam::{ACCOUNT}:role/ro\n"
            "[profile other]\nsso_account_id = 999988887777\n"
        )
        plan = pin_env.plan_aws(ACCOUNT, env)
        (pin,) = plan.pins
        self.assertIsNone(pin.value)
        self.assertEqual(pin.candidates, ("admin", "readonly"))
        self.assertTrue(any("ユーザーに確かめて" in note for note in plan.notes))

    def test_no_profile_is_a_problem_not_a_guess(self):
        env = self._env("[profile other]\nsso_account_id = 999988887777\n")
        plan = pin_env.plan_aws(ACCOUNT, env)
        self.assertEqual(plan.pins, ())
        self.assertIn("見つかりません", plan.problem)

    def test_missing_or_malformed_expected(self):
        self.assertIn("未設定", pin_env.plan_aws(None).problem)
        self.assertIn("文字列", pin_env.plan_aws({"a": ACCOUNT}).problem)


class TestPlanGcloud(_TmpBase):
    def _env(self, configs: dict[str, str]) -> dict:
        base = self.tmp / "gcloud"
        (base / "configurations").mkdir(parents=True)
        for name, body in configs.items():
            (base / "configurations" / f"config_{name}").write_text(body, encoding="utf-8")
        return {"CLOUDSDK_CONFIG": str(base)}

    def test_matching_configuration_is_preferred(self):
        env = self._env(
            {
                "work": "[core]\nproject = p1\naccount = a@example.invalid\n",
                "home": "[core]\nproject = p2\n",
            }
        )
        plan = pin_env.plan_gcloud("p1", env)
        self.assertEqual(
            plan.pins, (pin_env.Pin("CLOUDSDK_ACTIVE_CONFIG_NAME", "work", ("work",)),)
        )

    def test_dict_expected_needs_every_key_to_match(self):
        env = self._env(
            {
                "a": "[core]\nproject = p1\naccount = other@example.invalid\n",
                "b": "[core]\nproject = p1\naccount = me@example.invalid\n",
            }
        )
        plan = pin_env.plan_gcloud({"project": "p1", "account": "me@example.invalid"}, env)
        self.assertEqual(plan.pins[0].value, "b")

    def test_several_matches_leave_the_choice_to_the_user(self):
        env = self._env({"a": "[core]\nproject = p1\n", "b": "[core]\nproject = p1\n"})
        (pin,) = pin_env.plan_gcloud("p1", env).pins
        self.assertIsNone(pin.value)
        self.assertEqual(pin.candidates, ("a", "b"))

    def test_no_match_falls_back_to_core_properties_as_secrets(self):
        env = self._env({"other": "[core]\nproject = zzz\n"})
        plan = pin_env.plan_gcloud({"project": "p1", "account": "me@example.invalid"}, env)
        self.assertEqual(
            plan.pins,
            (
                pin_env.Pin("CLOUDSDK_CORE_PROJECT", "p1", secret=True),
                pin_env.Pin("CLOUDSDK_CORE_ACCOUNT", "me@example.invalid", secret=True),
            ),
        )
        self.assertTrue(any("get-value" in note for note in plan.notes))

    def test_configuration_that_cannot_be_statted_is_skipped(self):
        """stat できない構成ファイル (長すぎる名前を指す symlink など) は、読めない構成と同じく
        候補にしない (v0.19.0)。旧版は存在確認に `Path.is_file()` を使い、Python 3.13 までは
        例外が pin-env の外まで抜けていた。3.14 以降でも再現するため `Path.is_file` を差し替える。
        """
        env = self._env({"work": "[core]\nproject = p1\n"})
        broken = Path(env["CLOUDSDK_CONFIG"]) / "configurations" / "config_broken"
        os.symlink("a" * 300, broken)  # 1 要素が 255 バイトを超える → stat が ENAMETOOLONG
        patcher = _testutil.patch_is_file_like_py313()
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(OSError):  # 前提: 3.13 までの失敗を再現できている
            broken.is_file()
        _testutil.assert_real_is_file_on_this_version(self, broken)
        try:
            plan = pin_env.plan_gcloud("p1", env)
        except OSError as e:
            self.fail(f"stat できない構成で plan_gcloud から {type(e).__name__} が抜けた")
        self.assertEqual(
            plan.pins, (pin_env.Pin("CLOUDSDK_ACTIVE_CONFIG_NAME", "work", ("work",)),)
        )

    def test_project_only_fallback_has_no_account_pin(self):
        env = self._env({})
        plan = pin_env.plan_gcloud("p1", env)
        self.assertEqual([pin.name for pin in plan.pins], ["CLOUDSDK_CORE_PROJECT"])

    def test_malformed_expected(self):
        self.assertIn("未設定", pin_env.plan_gcloud(None).problem)
        self.assertIn("形が不正", pin_env.plan_gcloud(123).problem)
        self.assertIn("形が不正", pin_env.plan_gcloud({"region": "x"}).problem)

    def test_bad_field_is_not_dropped_to_pin_with_the_other(self):
        """truthy で文字列でない値や空白だけの値を、黙って落として残りで固定しない。

        通常の検証 (verify) は同じ期待値を、現在値が残りの項目と一致していても拒否する
        ので、片方だけで固定しても通らない (マージ前レビューの指摘)。構成が一致する経路と、
        CLOUDSDK_CORE_* に落ちる経路の両方で見る。
        """
        account = "me@example.invalid"
        bad = (
            {"project": 123, "account": account},
            {"project": True, "account": account},
            {"project": {"id": "p1"}, "account": account},
            {"project": "   ", "account": account},
            {"project": "p1", "account": [account]},
        )
        with_config = self._env({"me": f"[core]\nproject = p1\naccount = {account}\n"})
        current = {"project": "p1", "account": account}
        for expected in bad:
            with self.subTest(expected=expected):
                for env in (with_config, {"CLOUDSDK_CONFIG": str(self.tmp / "no-gcloud")}):
                    plan = pin_env.plan_gcloud(expected, env)
                    self.assertEqual(plan.pins, ())
                    self.assertIn("形が不正", plan.problem or "")
                self.assertIsNotNone(
                    gcloud._verify_against(expected, {}, lambda key: (current[key], None))
                )

    def test_falsy_fields_are_ignored_as_verify_does(self):
        """None / "" / 0 は、verify と同じく書かれていないものとして扱う。"""
        env = self._env({"me": "[core]\nproject = p1\naccount = me@example.invalid\n"})
        for falsy in (None, "", 0, []):
            with self.subTest(project=falsy):
                plan = pin_env.plan_gcloud({"project": falsy, "account": "me@example.invalid"}, env)
                self.assertEqual(
                    plan.pins, (pin_env.Pin("CLOUDSDK_ACTIVE_CONFIG_NAME", "me", ("me",)),)
                )


class TestPlanFirebase(_TmpBase):
    def _project(self, aliases: dict | None, name: str = "fb") -> str:
        root = self.tmp / name
        root.mkdir()
        (root / "firebase.json").write_text("{}", encoding="utf-8")
        if aliases is not None:
            (root / ".firebaserc").write_text(
                json.dumps({"projects": aliases}), encoding="utf-8"
            )
        return str(root)

    def test_alias_is_used_instead_of_the_project_id(self):
        project_dir = self._project({"default": "fb-dev", "staging": "fb-stg"})
        plan = pin_env.plan_firebase("fb-stg", project_dir)
        self.assertEqual(plan.command, "firebase use staging")
        self.assertFalse(plan.command_secret)
        self.assertEqual(plan.pins, ())

    def test_without_alias_the_project_id_is_secret(self):
        project_dir = self._project({"default": "fb-dev"})
        plan = pin_env.plan_firebase("fb-other", project_dir)
        self.assertEqual(plan.command, "firebase use fb-other")
        self.assertTrue(plan.command_secret)

    def test_dict_expected_uses_its_aliases(self):
        project_dir = self._project({"prod": "fb-prod", "dev": "fb-dev"})
        plan = pin_env.plan_firebase({"prod": "fb-prod", "dev": "fb-dev"}, project_dir)
        self.assertEqual(plan.command, "firebase use dev")
        self.assertFalse(plan.command_secret)
        self.assertTrue(any("dev, prod" in note for note in plan.notes))

    def test_dict_alias_that_firebaserc_points_elsewhere_is_not_used(self):
        """期待値の alias が `.firebaserc` で別の project を指すなら、alias ではなく
        期待値の project ID で案内する (案内どおりにすると別の project に切り替わり、
        続く検証が deny するため。マージ前レビューの指摘)。"""
        project_dir = self._project({"prod": "wrong-project"})
        plan = pin_env.plan_firebase({"prod": "right-project"}, project_dir)
        self.assertEqual(plan.command, "firebase use right-project")
        self.assertTrue(plan.command_secret)
        self.assertIsNone(plan.problem)
        self.assertTrue(any("解決されない alias は使いません" in note for note in plan.notes))

    def test_dict_alias_missing_from_firebaserc_is_not_used(self):
        """`.firebaserc` に無い alias は project ID として扱われるので、案内しない。"""
        for i, aliases in enumerate((None, {"default": "other-project"})):
            with self.subTest(firebaserc=aliases):
                project_dir = self._project(aliases, name=f"fb{i}")
                plan = pin_env.plan_firebase({"prod": "right-project"}, project_dir)
                self.assertEqual(plan.command, "firebase use right-project")
                self.assertTrue(plan.command_secret)

    def test_dict_uses_only_the_aliases_that_resolve_to_their_project(self):
        project_dir = self._project({"dev": "fb-dev", "prod": "fb-elsewhere", "stg": "fb-stg"})
        plan = pin_env.plan_firebase(
            {"dev": "fb-dev", "prod": "fb-prod", "stg": "fb-stg"}, project_dir
        )
        self.assertEqual(plan.command, "firebase use dev")
        joined = "\n".join(plan.notes)
        self.assertIn("どの alias でも通ります: dev, stg", joined)
        self.assertIn("解決されない alias は使いません", joined)

    def test_project_id_shadowed_by_an_alias_is_not_used(self):
        """project ID と同じ名前の alias が別の project を指すと、`firebase use <ID>` は
        その alias に切り替わる (firebase-tools は alias を先に解決する)。"""
        project_dir = self._project({"fb-prod": "fb-elsewhere"})
        plan = pin_env.plan_firebase("fb-prod", project_dir)
        self.assertIsNone(plan.command)
        self.assertIn("別の project を指している", plan.problem or "")
        plan = pin_env.plan_firebase({"prod": "fb-prod"}, project_dir)
        self.assertIsNone(plan.command)
        self.assertIn("別の project を指す alias と同じ名前", plan.problem or "")

    def test_dict_with_several_project_ids_asks_the_user_to_choose(self):
        """alias が使えず project ID で案内するとき、候補が複数なら名前順の先頭を黙って
        選ばず、案内した先頭の ID でよいかをユーザーに確かめる注記を添える (値は既定で
        隠すので一覧は出さない。マージ前レビューの指摘)。同じ project を指す alias が
        複数でも候補は 1 つ。"""
        project_dir = self._project(None)
        plan = pin_env.plan_firebase({"dev": "fb-a", "prod": "fb-b"}, project_dir)
        self.assertEqual(plan.command, "firebase use fb-a")
        self.assertTrue(plan.command_secret)
        joined = "\n".join(plan.notes)
        self.assertIn("project ID は 2 個あり", joined)
        self.assertIn("名前順で先頭の 1 つを案内しています", joined)
        self.assertIn("その ID でよいかをユーザーに確かめてください", joined)
        self.assertNotIn("fb-b", joined)
        plan = pin_env.plan_firebase({"dev": "fb-a", "dev2": "fb-a"}, project_dir)
        self.assertEqual(plan.command, "firebase use fb-a")
        self.assertNotIn("個あり", "\n".join(plan.notes))

    def test_several_project_ids_note_does_not_point_to_a_list_with_excluded_ids(self):
        """複数 ID の注記は、pin-env の確認を通っていない ID も並ぶ一覧へ誘導しない。

        「案内できる期待値の project ID は N 個」の N は、`.firebaserc` の同名の alias が別の
        project を指す ID (`firebase use <その ID>` が別の project に切り替わる) を除いた数。
        accounts-show の --show-values は期待値をすべて出すので、そこから選ばせると除いた ID も
        選べてしまう (マージ前レビューの指摘)。
        """
        project_dir = self._project({"mm-shadowed": "evil-project"})
        plan = pin_env.plan_firebase(
            {"dev": "aa-dev", "prod": "zz-prod", "x": "mm-shadowed"}, project_dir
        )
        self.assertEqual(plan.command, "firebase use aa-dev")
        joined = "\n".join(plan.notes)
        self.assertIn("project ID は 2 個あり", joined)  # mm-shadowed は除かれている
        self.assertNotIn("accounts-show", joined)

    def test_notes_say_it_is_per_directory_and_account_is_not_verified(self):
        plan = pin_env.plan_firebase("fb-dev", self._project({"default": "fb-dev"}))
        joined = "\n".join(plan.notes)
        self.assertIn("作業ディレクトリごと", joined)
        self.assertIn("worktree", joined)
        self.assertIn("VCA は firebase のアカウントは検証しません", joined)

    def test_malformed_expected(self):
        project_dir = self._project(None)
        self.assertIn("未設定", pin_env.plan_firebase(None, project_dir).problem)
        self.assertIn("有効な project", pin_env.plan_firebase({"x": ""}, project_dir).problem)


class TestPinEnvReadsFirebasercLikeFirebaseTools(_TmpBase):
    """firebase-tools と違う内容に読む `.firebaserc` では `firebase use` を案内しない。

    firebase-tools は `.firebaserc` を cjson で読む: ファイル中のすべての U+FEFF を除き、
    `//` / `/* */` のコメントを除いてから JSON.parse する (不正な UTF-8 は置換文字になり、
    `NaN` 等は JSON.parse が拒否して alias 0 件)。cjson はバックスラッシュで終わる文字列の
    直後で文字列の内外を取り違える。厳密な JSON で読むと、どの入力でも案内した
    `firebase use` の行き先の予測が食い違う (マージ前レビューの指摘)。コメントは
    firebase-tools (cjson 0.3.3) で読んだ内容と、そのときの行き先。
    """

    FIREBASERC = (
        # alias 0 件と読み、同名の alias の確認をすり抜ける (right-project -> wrong-project)
        (
            "line comment",
            b'{\n  // aliases\n'
            b'  "projects": {"default": "right-project", "right-project": "wrong-project"}\n}\n',
        ),
        (
            "block comment",
            b'{"projects": {/* aliases */ "default": "right-project",'
            b' "right-project": "wrong-project"}}\n',
        ),
        # U+FEFF を除くと 2 つ目のキーも "prod" になり後勝ち (prod -> wrong-project)
        (
            "U+FEFF in an alias",
            '{"projects": {"prod": "right-project", "pr\ufeffod": "wrong-project"}}\n'.encode(),
        ),
        # 先頭の U+FEFF は json.loads が拒否して alias 0 件 (right-project -> wrong-project)
        (
            "leading U+FEFF",
            '\ufeff{"projects": {"default": "right-project", "right-project": "wrong-project"}}\n'.encode(),
        ),
        # UTF-8 でないバイトは置換文字になるだけ (right-project -> wrong-project)
        (
            "not UTF-8",
            b'{"projects": {"default": "right-project", "right-project": "wrong-project"},'
            b' "note": "\xff"}\n',
        ),
        # json.loads は NaN を読むが JSON.parse は拒否して alias 0 件
        # (other-project は project ID として扱われる)
        ("NaN", b'{"projects": {"other-project": "right-project"}, "x": NaN}\n'),
        # `\\` で終わる文字列の後の `//` をコメントと読み、残りが読めず alias 0 件
        (
            "// in a string",
            b'{"note": "a\\\\", "x": "//", "projects": {"other-project": "right-project"}}\n',
        ),
        # 同じく `/*` から `*/` までを除き、後ろの "projects" が消える (prod -> wrong-project)
        (
            "/* in a string",
            b'{"note": "a\\\\", "projects": {"prod": "wrong-project"}, "x": "/*",'
            b' "projects": {"prod": "right-project"}, "y": "*/"}\n',
        ),
    )

    NOT_PINNED = ".firebaserc を firebase-tools と同じ内容に読めると確かめられません"

    def _project(self, data: bytes, name: str) -> str:
        root = self.tmp / name
        root.mkdir()
        (root / "firebase.json").write_text("{}", encoding="utf-8")
        (root / ".firebaserc").write_bytes(data)
        return str(root)

    def test_firebase_use_is_not_guided(self):
        for i, (label, data) in enumerate(self.FIREBASERC):
            root = self._project(data, f"fb{i}")
            for expected in ("right-project", {"prod": "right-project"}):
                with self.subTest(firebaserc=label, expected=expected):
                    plan = pin_env.plan_firebase(expected, root)
                    self.assertIsNone(plan.command, plan)
                    self.assertIn(self.NOT_PINNED, plan.problem or "")

    def test_alias_values_that_are_not_strings_are_not_guided(self):
        """firebase-tools の resolveAlias は `projects[alias] || alias` なので、真になる文字列でない
        値 (配列・数値・true・オブジェクト) も alias の行き先に使う。このモジュールは文字列の値
        だけを alias と読むので、`firebase use right-project` は firebase-tools では alias
        right-project (→ 配列など) を選んでしまう (マージ前レビューの指摘)。"""
        for i, value in enumerate((["wrong-project"], 123, True, {"a": 1})):
            text = json.dumps({"projects": {"right-project": value, "other": "x"}})
            root = self._project(text.encode(), f"fb{i}")
            for expected in ("right-project", {"x": "right-project"}):
                with self.subTest(value=value, expected=expected):
                    plan = pin_env.plan_firebase(expected, root)
                    self.assertIsNone(plan.command, plan)
                    self.assertIn(self.NOT_PINNED, plan.problem or "")

    def test_projects_key_that_is_not_an_object_is_not_guided(self):
        """`projects` がオブジェクトでない (null・配列・文字列) と、firebase-tools は添字で引く
        (配列の "0" など) か読めずに失敗し、このモジュールは alias 0 件と読む。"""
        for i, projects in enumerate((None, ["wrong-project"], "wrong-project")):
            root = self._project(json.dumps({"projects": projects}).encode(), f"fb{i}")
            for expected in ("right-project", {"x": "right-project"}):
                with self.subTest(projects=projects, expected=expected):
                    plan = pin_env.plan_firebase(expected, root)
                    self.assertIsNone(plan.command, plan)
                    self.assertIn(self.NOT_PINNED, plan.problem or "")

    def test_firebaserc_without_projects_is_still_guided(self):
        """`projects` の無い .firebaserc (hosting の targets だけ等) は、`projects` の形の判定の
        対象外で、案内が出る (判定が `projects` を無条件に引くと KeyError で落ちる。マージ前
        レビューの指摘)。"""
        text = json.dumps({"targets": {"right-project": {"hosting": {"main": ["site"]}}}})
        root = self._project(text.encode(), "targets")
        for expected in ("right-project", {"prod": "right-project"}):
            with self.subTest(expected=expected):
                try:
                    plan = pin_env.plan_firebase(expected, root)
                except Exception as e:  # noqa: BLE001
                    self.fail(f"projects の無い .firebaserc で pin-env が落ちた: {type(e).__name__}")
                self.assertEqual(plan.command, "firebase use right-project", plan)

    def test_deeply_nested_firebaserc_is_a_problem_not_a_traceback(self):
        """厳密な JSON の判定 (`json.loads`) が入れ子の深さで RecursionError を出しても、pin-env は
        落ちずに「固定できません」を返す。"""
        text = '{"projects": {"prod": "p"}, "x": ' + "[" * 100000 + "]" * 100000 + "}"
        root = self._project(text.encode(), "deep")
        try:
            plan = pin_env.plan_firebase({"prod": "p"}, root)
        except RecursionError:
            self.fail("深い入れ子の .firebaserc で pin-env が RecursionError のまま落ちた")
        self.assertIsNone(plan.command)
        self.assertIn(self.NOT_PINNED, plan.problem or "")

    def test_strict_json_with_a_url_is_not_guided_either(self):
        """判定は保守的で、文字列の中の `//` (URL) だけでも案内しない。firebase-tools とは同じ
        内容に読めるファイルだが、cjson のコメントの除去を再現しない代償として受け入れた。
        理由の文はこの場合も事実どおり (「厳密な JSON に直せば案内できる」とは言わない)。"""
        text = json.dumps(
            {"projects": {"prod": "right-project"}, "docs": "https://example.com/firebase"}
        )
        root = self._project(text.encode(), "url")
        for expected in ("right-project", {"prod": "right-project"}):
            with self.subTest(expected=expected):
                plan = pin_env.plan_firebase(expected, root)
                self.assertIsNone(plan.command, plan)
                self.assertIn(self.NOT_PINNED, plan.problem or "")
                self.assertIn("文字列の中の URL なども含む", plan.problem or "")
                self.assertNotIn("厳密な JSON に直す", plan.problem or "")


class TestFirebaseCommandIsShellSafe(_TmpBase):
    """`firebase use <x>` の x がシェルの構文や option として走らないこと。

    出したコマンドは skill の手順で Claude がそのまま実行する。x は期待値
    (accounts.local.json) の alias / project ID か、リポジトリの `.firebaserc` の
    alias (clone しただけのリポジトリでも中身を決められる) から来る
    (マージ前レビューの指摘)。
    """

    # 修正後はどれもコマンドに出ない。strip() で無害になる形 (末尾の改行だけ) は
    # 別のテストで alias (strip しない経路) として見る。
    HOSTILE = (
        "x; touch pwned",
        "$(touch pwned)",
        "`touch pwned`",
        "a b",
        "dev\nrm -rf ~",
        "it's",
        '"q"',
        "a|b",
        "a&b",
        "a>b",
        "-P",
        "--project=other",
        ".hidden",
        "_x",
        "ｄｅｖ",
    )
    PLAIN = ("dev", "fb-prod.v2", "A_1")

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "fb"
        self.root.mkdir()
        (self.root / "firebase.json").write_text("{}", encoding="utf-8")

    def _firebaserc(self, aliases: dict) -> None:
        (self.root / ".firebaserc").write_text(
            json.dumps({"projects": aliases}), encoding="utf-8"
        )

    def _assert_one_plain_argument(self, plan) -> None:
        if plan.command is None:
            return
        try:
            parts = shlex.split(plan.command)
        except ValueError:
            self.fail(f"シェルとして閉じていないコマンドを出した: {plan.command!r}")
        self.assertEqual(len(parts), 3, f"引数が 1 つではない: {plan.command!r}")
        self.assertEqual(parts[:2], ["firebase", "use"])
        self.assertRegex(parts[2], r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
        self.assertEqual(plan.command, f"firebase use {parts[2]}")

    def test_every_emitted_command_has_one_plain_argument(self):
        """3 つの経路 (dict の alias / `.firebaserc` の alias / scalar の project) すべて。"""
        for value in self.HOSTILE + self.PLAIN:
            (self.root / ".firebaserc").unlink(missing_ok=True)
            plans = {
                "dict alias": pin_env.plan_firebase({value: "fb-x"}, str(self.root)),
                "scalar project": pin_env.plan_firebase(value, str(self.root)),
            }
            self._firebaserc({value: "fb-rc"})
            plans[".firebaserc alias"] = pin_env.plan_firebase("fb-rc", str(self.root))
            for label, plan in plans.items():
                with self.subTest(path=label, value=value):
                    self._assert_one_plain_argument(plan)
                    if value in self.PLAIN:
                        self.assertIsNotNone(plan.command, plan.problem)

    def test_dict_alias_that_is_not_a_plain_name_falls_back_to_the_project_id(self):
        """渡せない alias は使わず、期待値の project ID で案内する (`.firebaserc` に同じ
        alias があっても)。0.17.0 は「固定できません」だった (alias だけを見ていたため)。"""
        for alias in self.HOSTILE + ("dev\n",):
            with self.subTest(alias=alias):
                self._firebaserc({alias: "fb-prod"})
                plan = pin_env.plan_firebase({alias: "fb-prod"}, str(self.root))
                self.assertEqual(plan.command, "firebase use fb-prod")
                self.assertTrue(plan.command_secret)
                self.assertNotIn(alias, "\n".join(plan.notes))

    def test_dict_with_no_usable_alias_or_project_id_is_a_problem(self):
        for alias in self.HOSTILE:
            with self.subTest(alias=alias):
                plan = pin_env.plan_firebase({alias: "$(touch pwned)"}, str(self.root))
                self.assertIsNone(plan.command)
                self.assertIn("切り替えられる形になりません", plan.problem or "")

    def test_dict_uses_the_plain_aliases_and_lists_only_them(self):
        expected = {"-P": "fb-a", "$(touch pwned)": "fb-b", "dev": "fb-dev", "prod": "fb-prod"}
        self._firebaserc(expected)
        plan = pin_env.plan_firebase(expected, str(self.root))
        self.assertEqual(plan.command, "firebase use dev")
        joined = "\n".join(plan.notes)
        self.assertIn("どの alias でも通ります: dev, prod", joined)
        self.assertNotIn("pwned", joined)
        self.assertIn("除きました", joined)

    def test_firebaserc_alias_that_is_not_a_plain_name_is_not_used(self):
        """リポジトリの `.firebaserc` の alias は使わず、alias が無いときと同じ扱いにする。"""
        self._firebaserc({"$(touch pwned)": "fb-stg", "-P": "fb-stg"})
        plan = pin_env.plan_firebase("fb-stg", str(self.root))
        self.assertEqual(plan.command, "firebase use fb-stg")
        self.assertTrue(plan.command_secret)
        self.assertTrue(any(".firebaserc" in note for note in plan.notes))

        self._firebaserc({"-x": "fb-stg", "staging": "fb-stg"})
        plan = pin_env.plan_firebase("fb-stg", str(self.root))
        self.assertEqual(plan.command, "firebase use staging")
        self.assertFalse(plan.command_secret)

    def test_project_id_that_is_not_a_plain_name_is_a_problem(self):
        for project in self.HOSTILE:
            with self.subTest(project=project):
                plan = pin_env.plan_firebase(project, str(self.root))
                self.assertIsNone(plan.command)
                self.assertIn("渡せない文字", plan.problem or "")

    def test_quoting_still_holds_if_the_name_rule_is_loosened(self):
        """許容形の検証とクォートは二重化: 検証を緩めても、シェルの構文は 1 引数に収まる。"""
        loose = re.compile(r".+", re.DOTALL)
        self._firebaserc({"x; touch pwned": "fb-x"})
        with mock.patch.object(pin_env, "_FIREBASE_TARGET_RE", loose):
            plan = pin_env.plan_firebase({"x; touch pwned": "fb-x"}, str(self.root))
        self.assertEqual(shlex.split(plan.command), ["firebase", "use", "x; touch pwned"])


class TestPaddedExpectedIsNotPinned(_TmpBase):
    """前後に空白のある期待値は「固定できません」にする。

    通常の検証は CLI が出した (前後の空白を除いた) 現在値と期待値を完全一致で照合する
    ので、前後に空白のある期待値はどの現在値とも一致しない。0.17.0 の pin-env は空白を
    除いた値で構成を照合・固定を案内していたので、案内どおりに固定しても deny が続いた
    (マージ前レビューの指摘)。各ケースで、通常の検証が実際に deny することも見る。
    """

    @staticmethod
    def _run(stdout: str):
        return mock.patch(
            "subprocess.run",
            return_value=SimpleNamespace(stdout=stdout, stderr="", returncode=0),
        )

    def test_gcloud(self):
        config_dir = self.tmp / "gcloud"
        (config_dir / "configurations").mkdir(parents=True)
        (config_dir / "configurations" / "config_work").write_text(
            "[core]\nproject = my-project\naccount = me@example.invalid\n", encoding="utf-8"
        )
        current = {"project": "my-project", "account": "me@example.invalid"}
        for expected in (
            " my-project ",
            "my-project\n",
            "\tmy-project",
            {"project": " my-project "},
            {"project": "my-project", "account": "me@example.invalid "},
        ):
            with self.subTest(expected=expected):
                for config in (config_dir, self.tmp / "no-gcloud"):
                    plan = pin_env.plan_gcloud(expected, {"CLOUDSDK_CONFIG": str(config)})
                    self.assertEqual(plan.pins, ())
                    self.assertIn("前後に空白", plan.problem or "")
                self.assertIsNone(gcloud.pin_fields(expected))
                self.assertIsNotNone(
                    gcloud._verify_against(expected, {}, lambda key: (current[key], None))
                )

    def test_aws(self):
        aws_config = self.tmp / "aws_config"
        aws_config.write_text(f"[profile dev]\nsso_account_id = {ACCOUNT}\n", encoding="utf-8")
        env = {"AWS_CONFIG_FILE": str(aws_config)}
        for expected in (f" {ACCOUNT} ", f"{ACCOUNT}\n", f"\t{ACCOUNT}"):
            with self.subTest(expected=expected):
                plan = pin_env.plan_aws(expected, env)
                self.assertEqual(plan.pins, ())
                self.assertIn("前後に空白", plan.problem or "")
                with self._run(f"{ACCOUNT}\n"):
                    self.assertIsNotNone(aws.verify(expected, str(self.tmp), env))

    def test_firebase(self):
        root = self.tmp / "fb"
        root.mkdir()
        (root / "firebase.json").write_text("{}", encoding="utf-8")
        (root / ".firebaserc").write_text(
            json.dumps({"projects": {"prod": "fb-prod"}}), encoding="utf-8"
        )
        for expected in (" fb-prod ", "fb-prod\n", {"prod": " fb-prod "}, {"prod": "fb-prod\t"}):
            with self.subTest(expected=expected):
                plan = pin_env.plan_firebase(expected, str(root))
                self.assertIsNone(plan.command)
                self.assertIn("前後に空白", plan.problem or "")
                with self._run("fb-prod\n"):
                    self.assertIsNotNone(firebase.verify(expected, str(root)))

    def test_firebase_dict_keeps_the_exact_entries(self):
        root = self.tmp / "fb"
        root.mkdir()
        (root / "firebase.json").write_text("{}", encoding="utf-8")
        (root / ".firebaserc").write_text(
            json.dumps({"projects": {"prod": "fb-prod", "dev": "fb-dev"}}), encoding="utf-8"
        )
        plan = pin_env.plan_firebase({"prod": "fb-prod", "dev": " fb-dev "}, str(root))
        self.assertEqual(plan.command, "firebase use prod")
        self.assertTrue(any("前後に空白のある project ID" in note for note in plan.notes))


class TestSettingsLocalTarget(_TmpBase):
    def test_repository_root(self):
        repo = _init_repo(self.tmp / "repo")
        path, note = pin_env.settings_local_target(str(repo))
        self.assertEqual(path.resolve(), (repo / ".claude" / "settings.local.json").resolve())
        self.assertIn("リポジトリのルート", note)

    def test_subdirectory_uses_the_root(self):
        repo = _init_repo(self.tmp / "repo")
        sub = repo / "pkg" / "deep"
        sub.mkdir(parents=True)
        path, _note = pin_env.settings_local_target(str(sub))
        self.assertEqual(path.resolve(), (repo / ".claude" / "settings.local.json").resolve())

    def test_worktree_uses_the_main_checkout(self):
        repo = _init_repo(self.tmp / "repo")
        worktree = self.tmp / "wt"
        _git(repo, "worktree", "add", "-q", str(worktree), "-b", "wt")
        path, note = pin_env.settings_local_target(str(worktree))
        self.assertEqual(path.resolve(), (repo / ".claude" / "settings.local.json").resolve())
        self.assertIn("main checkout", note)

    def test_outside_git_is_not_guessed(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        path, note = pin_env.settings_local_target(str(plain))
        self.assertIsNone(path)
        self.assertIn("git リポジトリの外", note)

    def test_repository_at_home_is_not_guessed(self):
        repo = _init_repo(self.tmp / "home-repo")
        with mock.patch.object(Path, "home", staticmethod(lambda: repo)):
            path, note = pin_env.settings_local_target(str(repo))
        self.assertIsNone(path)
        self.assertIn("ホームディレクトリ", note)


class TestSettingsEnv(_TmpBase):
    def test_missing_file_is_empty(self):
        self.assertEqual(pin_env.settings_env(self.tmp / "none.json"), ({}, None))
        self.assertEqual(pin_env.settings_env(None), ({}, None))

    def test_reads_only_string_env_values(self):
        path = self.tmp / "s.json"
        path.write_text(
            json.dumps({"permissions": {"allow": []}, "env": {"AWS_PROFILE": "dev", "N": 1}}),
            encoding="utf-8",
        )
        self.assertEqual(pin_env.settings_env(path), ({"AWS_PROFILE": "dev"}, None))

    def test_broken_file_is_reported(self):
        path = self.tmp / "s.json"
        path.write_text("{not json", encoding="utf-8")
        env, problem = pin_env.settings_env(path)
        self.assertEqual(env, {})
        self.assertIn("JSON", problem)
        path.write_text(json.dumps({"env": ["x"]}), encoding="utf-8")
        self.assertIn('"env"', pin_env.settings_env(path)[1])

    def test_non_object_top_level_is_reported(self):
        """`[]` などは JSON として読めても、env を足せるオブジェクトが無い (マージ前レビューの指摘)。"""
        path = self.tmp / "s.json"
        for text in ("[]", '"x"', "1", "null", "true", '[{"env": {"AWS_PROFILE": "dev"}}]'):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                env, problem = pin_env.settings_env(path)
                self.assertEqual(env, {})
                self.assertIn("最上位がオブジェクトではありません", problem or "")


class TestRender(unittest.TestCase):
    PLANS = [
        pin_env.Plan("aws", pins=(pin_env.Pin("AWS_PROFILE", None, ("a", "b")),)),
        pin_env.Plan(
            "gcloud",
            pins=(pin_env.Pin("CLOUDSDK_CORE_PROJECT", "secret-project", secret=True),),
        ),
        pin_env.Plan("firebase", command="firebase use secret-fb", command_secret=True),
    ]

    def _render(self, *, show_values: bool, session=None, file_env=None) -> str:
        return "\n".join(
            pin_env.render(
                self.PLANS,
                Path("/repo/.claude/settings.local.json"),
                "note",
                session or {},
                file_env or {},
                None,
                show_values=show_values,
            )
        )

    def test_secrets_are_hidden_by_default(self):
        text = self._render(
            show_values=False,
            session={"CLOUDSDK_CORE_PROJECT": "secret-session"},
            file_env={"CLOUDSDK_CORE_PROJECT": "secret-file"},
        )
        for secret in ("secret-project", "secret-fb", "secret-session", "secret-file"):
            self.assertNotIn(secret, text)
        self.assertIn(pin_env.HIDDEN, text)
        self.assertIn('"AWS_PROFILE": "<a / b のどれか>"', text)

    def test_show_values_reveals(self):
        text = self._render(show_values=True)
        self.assertIn('"CLOUDSDK_CORE_PROJECT": "secret-project"', text)
        self.assertIn("firebase use secret-fb", text)

    def test_names_are_shown_without_show_values(self):
        plans = [pin_env.Plan("aws", pins=(pin_env.Pin("AWS_PROFILE", "dev", ("dev",)),))]
        text = "\n".join(
            pin_env.render(plans, None, "理由", {"AWS_PROFILE": "old"}, {}, None, show_values=False)
        )
        self.assertIn('"AWS_PROFILE": "dev"', text)
        self.assertIn("このセッション=old", text)
        self.assertIn("決められません — 理由", text)

    def test_env_snippet_stays_one_json_value_per_key(self):
        """値の `"` / 改行 / `\\` で、別のキーが足されたり JSON が壊れたりしないこと。

        Claude はこの断片を settings.local.json に写す。値は CLI の設定 (profile 名・
        構成名) や期待値 (accounts.local.json) から来る (マージ前レビューの指摘)。
        """
        for value in (
            'p", "BASH_ENV": "/tmp/evil',
            "line1\nline2",
            "C:\\new\\dir",
            "tail\\",
            'q"\\n"',
        ):
            with self.subTest(value=value):
                plans = [
                    pin_env.Plan(
                        "gcloud",
                        pins=(pin_env.Pin("CLOUDSDK_CORE_PROJECT", value, secret=True),),
                    )
                ]
                lines = pin_env.render(plans, None, "理由", {}, {}, None, show_values=True)
                self.assertEqual(env_snippet(self, lines), {"CLOUDSDK_CORE_PROJECT": value})
        candidates = ('a"b', "c\nd", "e\\f")
        plans = [pin_env.Plan("aws", pins=(pin_env.Pin("AWS_PROFILE", None, candidates),))]
        lines = pin_env.render(plans, None, "理由", {}, {}, None, show_values=False)
        self.assertEqual(
            env_snippet(self, lines), {"AWS_PROFILE": '<a"b / c\nd / e\\f のどれか>'}
        )


class TestPinEnvCommand(_TmpBase):
    def setUp(self):
        super().setUp()
        self.repo = _init_repo(self.tmp / "repo")
        aws_config = self.tmp / "aws_config"
        aws_config.write_text(f"[profile dev]\nsso_account_id = {ACCOUNT}\n", encoding="utf-8")
        patcher = mock.patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(self.repo), "AWS_CONFIG_FILE": str(aws_config)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_accounts(self, data: dict) -> None:
        d = self.repo / ".claude" / "verify-cloud-account"
        d.mkdir(parents=True, exist_ok=True)
        (d / "accounts.local.json").write_text(json.dumps(data), encoding="utf-8")

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = builder.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def _snapshot(self) -> dict:
        return {
            str(p.relative_to(self.tmp)): (p.stat().st_mtime_ns, p.read_bytes())
            for p in self.tmp.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }

    def test_writes_nothing(self):
        self._write_accounts({"aws": ACCOUNT, "github": "someone"})
        before = self._snapshot()
        code, out, err = self._run(["pin-env"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.repo / ".claude" / "settings.local.json").exists())

    def test_default_covers_only_services_with_expected_values(self):
        self._write_accounts({"aws": ACCOUNT, "github": "someone"})
        code, out, err = self._run(["pin-env"])
        self.assertEqual(code, 0, err)
        self.assertIn("[aws]", out)
        self.assertNotIn("[gcloud]", out)
        self.assertNotIn("[github]", out)
        self.assertIn('"AWS_PROFILE": "dev"', out)
        self.assertIn("書き込み先:", out)

    def test_service_filter_and_missing_expected(self):
        self._write_accounts({"aws": ACCOUNT})
        code, out, err = self._run(["pin-env", "--service", "gcloud", "--service", "aws"])
        self.assertEqual(code, 0, err)
        self.assertLess(out.index("[gcloud]"), out.index("[aws]"))
        self.assertIn("gcloud の期待値が未設定", out)

    def test_expected_value_is_hidden_unless_show_values(self):
        self._write_accounts({"gcloud": "secret-project-id"})
        code, out, _err = self._run(["pin-env"])
        self.assertEqual(code, 0)
        self.assertNotIn("secret-project-id", out)
        code, out, _err = self._run(["pin-env", "--show-values"])
        self.assertIn('"CLOUDSDK_CORE_PROJECT": "secret-project-id"', out)

    def test_existing_settings_value_is_reported(self):
        self._write_accounts({"aws": ACCOUNT})
        (self.repo / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"AWS_PROFILE": "dev"}}), encoding="utf-8"
        )
        code, out, _err = self._run(["pin-env"])
        self.assertEqual(code, 0)
        self.assertIn("書き込み先=dev", out)

    def test_refuses_without_expected_values(self):
        code, _out, err = self._run(["pin-env"])
        self.assertEqual(code, 1)
        self.assertIn("accounts-init", err)
        self._write_accounts({"github": "someone"})
        code, _out, err = self._run(["pin-env"])
        self.assertEqual(code, 1)
        self.assertIn("aws / gcloud / firebase の期待値がありません", err)

    def test_refuses_when_several_accounts_files_exist(self):
        self._write_accounts({"aws": ACCOUNT})
        (self.repo / ".claude" / "accounts.local.json").write_text(
            json.dumps({"aws": ACCOUNT}), encoding="utf-8"
        )
        code, _out, err = self._run(["pin-env"])
        self.assertEqual(code, 1)
        self.assertIn("複数のパス", err)

    def test_firebase_line_never_carries_a_repository_alias_with_shell_syntax(self):
        """`.firebaserc` の alias は clone したリポジトリが決められる (マージ前レビューの指摘)。"""
        (self.repo / "firebase.json").write_text("{}", encoding="utf-8")
        (self.repo / ".firebaserc").write_text(
            json.dumps({"projects": {"$(touch pwned)": "fb-stg"}}), encoding="utf-8"
        )
        self._write_accounts({"firebase": "fb-stg"})
        code, out, err = self._run(["pin-env", "--show-values"])
        self.assertEqual(code, 0, err)
        self.assertIn("このディレクトリで 1 回実行: firebase use fb-stg\n", out)
        self.assertNotIn("pwned", out)

    def test_env_snippet_from_expected_value_adds_no_other_key(self):
        """期待値の `"` から、BASH_ENV のような別の env が断片に入らないこと。"""
        value = 'p", "BASH_ENV": "/tmp/evil'
        self._write_accounts({"gcloud": value})
        code, out, err = self._run(["pin-env", "--show-values"])
        self.assertEqual(code, 0, err)
        self.assertEqual(env_snippet(self, out.splitlines()), {"CLOUDSDK_CORE_PROJECT": value})


if __name__ == "__main__":
    unittest.main()
