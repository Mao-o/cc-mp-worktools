"""プロジェクトごとの固定の提案 (`scripts/pin_env.py` と builder の `pin-env`) のテスト。

`pin-env` は読み取り専用 (builder の D16): settings.local.json を含め何も書かない。
期待値は D3 に従い既定で隠し、profile 名・構成名・alias 名だけを出す。
"""
from __future__ import annotations

import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
