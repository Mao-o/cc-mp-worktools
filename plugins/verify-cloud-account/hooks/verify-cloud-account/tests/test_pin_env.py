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
from unittest import mock

import _testutil  # noqa: F401

from scripts import accounts_builder as builder  # noqa: E402
from scripts import pin_env  # noqa: E402

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

    def test_project_only_fallback_has_no_account_pin(self):
        env = self._env({})
        plan = pin_env.plan_gcloud("p1", env)
        self.assertEqual([pin.name for pin in plan.pins], ["CLOUDSDK_CORE_PROJECT"])

    def test_malformed_expected(self):
        self.assertIn("未設定", pin_env.plan_gcloud(None).problem)
        self.assertIn("形が不正", pin_env.plan_gcloud(123).problem)
        self.assertIn("形が不正", pin_env.plan_gcloud({"region": "x"}).problem)


class TestPlanFirebase(_TmpBase):
    def _project(self, aliases: dict | None) -> str:
        root = self.tmp / "fb"
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
        project_dir = self._project(None)
        plan = pin_env.plan_firebase({"prod": "fb-prod", "dev": "fb-dev"}, project_dir)
        self.assertEqual(plan.command, "firebase use dev")
        self.assertTrue(any("dev, prod" in note for note in plan.notes))

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

    def test_dict_alias_that_is_not_a_plain_name_is_a_problem(self):
        for alias in self.HOSTILE + ("dev\n",):
            with self.subTest(alias=alias):
                plan = pin_env.plan_firebase({alias: "fb-prod"}, str(self.root))
                self.assertIsNone(plan.command)
                self.assertIn("渡せる名前がありません", plan.problem or "")

    def test_dict_uses_the_plain_aliases_and_lists_only_them(self):
        plan = pin_env.plan_firebase(
            {"-P": "fb-a", "$(touch pwned)": "fb-b", "dev": "fb-dev", "prod": "fb-prod"},
            str(self.root),
        )
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
        with mock.patch.object(pin_env, "_FIREBASE_TARGET_RE", loose):
            plan = pin_env.plan_firebase({"x; touch pwned": "fb-x"}, str(self.root))
        self.assertEqual(shlex.split(plan.command), ["firebase", "use", "x; touch pwned"])


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
