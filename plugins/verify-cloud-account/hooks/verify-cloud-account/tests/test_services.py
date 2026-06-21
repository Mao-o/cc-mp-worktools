"""各 service の verify() テスト。subprocess.run を mock する。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from services import aws, firebase, gcloud, github, kubectl  # noqa: E402


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


GH_GITHUB_COM_ONLY = (
    "github.com\n"
    "  ✓ Logged in to github.com account Mao-o (keyring)\n"
    "  - Active account: true\n"
)

GH_MULTI_HOST = (
    "github.com\n"
    "  ✓ Logged in to github.com account Mao-o\n"
    "  - Active account: true\n"
    "ghe.example.com\n"
    "  ✓ Logged in to ghe.example.com account mao-corp\n"
    "  - Active account: true\n"
)


class TestGithub(unittest.TestCase):
    def test_string_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            self.assertIsNone(github.verify("Mao-o", "/p"))

    def test_string_mismatch(self):
        out = (
            "github.com\n"
            "  ✓ Logged in to github.com account other-user\n"
            "  - Active account: true\n"
        )
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=out)):
            err = github.verify("Mao-o", "/p")
        self.assertIn("不一致", err)
        self.assertIn("Mao-o", err)

    def test_cli_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            err = github.verify("Mao-o", "/p")
        self.assertIn("gh コマンドが見つかりません", err)

    def test_timeout(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
        ):
            err = github.verify("Mao-o", "/p")
        self.assertIn("タイムアウト", err)

    def test_empty_output(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            err = github.verify("Mao-o", "/p")
        self.assertIn("アクティブアカウント", err)

    def test_dict_match_all_hosts(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_MULTI_HOST)):
            self.assertIsNone(
                github.verify(
                    {"github.com": "Mao-o", "ghe.example.com": "mao-corp"},
                    "/p",
                )
            )

    def test_dict_wrong_user_on_ghe(self):
        out = (
            "github.com\n"
            "  ✓ Logged in to github.com account Mao-o\n"
            "  - Active account: true\n"
            "ghe.example.com\n"
            "  ✓ Logged in to ghe.example.com account wrong-user\n"
            "  - Active account: true\n"
        )
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=out)):
            err = github.verify(
                {"github.com": "Mao-o", "ghe.example.com": "mao-corp"},
                "/p",
            )
        self.assertIn("ghe.example.com", err)
        self.assertIn("wrong-user", err)

    def test_dict_host_not_logged_in(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            err = github.verify(
                {"github.com": "Mao-o", "ghe.example.com": "mao-corp"},
                "/p",
            )
        self.assertIn("ghe.example.com", err)
        self.assertIn("ログインしていません", err)

    def test_invalid_expected_type(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            err = github.verify(12345, "/p")
        self.assertIn("文字列または", err)

    def test_dict_empty_object_fail_closed(self):
        """Codex R4 回帰: 空 dict は fail-closed。"""
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            err = github.verify({}, "/p")
        self.assertIsNotNone(err)
        self.assertIn("空", err)


class TestFirebase(unittest.TestCase):
    def test_string_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-project\n")):
            self.assertIsNone(firebase.verify("my-project", "/p"))

    def test_string_mismatch(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="other-proj\n")):
            err = firebase.verify("my-project", "/p")
        self.assertIn("不一致", err)
        self.assertIn("my-project", err)

    def test_firebaserc_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".firebaserc").write_text(
                json.dumps({"projects": {"default": "my-project"}}),
                encoding="utf-8",
            )
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                self.assertIsNone(firebase.verify("my-project", d))

    def test_no_current_project(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                err = firebase.verify("my-project", "/p")
        self.assertIn("取得できません", err)

    def test_cli_not_installed(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            with mock.patch("shutil.which", return_value=None):
                err = firebase.verify("my-project", "/p")
        self.assertIn("firebase コマンドが見つかりません", err)
        self.assertIn("npm install", err)

    def test_dict_default_alias_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="proj-dev\n")):
            self.assertIsNone(
                firebase.verify(
                    {"default": "proj-dev", "prod": "proj-prod"}, "/p"
                )
            )

    def test_dict_prod_alias_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="proj-prod\n")):
            self.assertIsNone(
                firebase.verify(
                    {"default": "proj-dev", "prod": "proj-prod"}, "/p"
                )
            )

    def test_dict_no_alias_matches(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="other\n")):
            err = firebase.verify(
                {"default": "proj-dev", "prod": "proj-prod"}, "/p"
            )
        self.assertIn("不一致", err)
        self.assertIn("proj-dev", err)
        self.assertIn("proj-prod", err)

    def test_verify_ignores_multiline_help_uses_firebaserc(self):
        """firebase use ヘルプメッセージ時、.firebaserc の値で verify が通る (回帰防止)。"""
        help_message = (
            "No project is currently active for this directory.\n"
            "\n"
            "Run firebase use --add to define a new project alias "
            "for the current folder.\n"
        )
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".firebaserc").write_text(
                json.dumps({"projects": {"default": "my-project"}}),
                encoding="utf-8",
            )
            with mock.patch(
                "subprocess.run", return_value=_fake_run(stdout=help_message)
            ):
                self.assertIsNone(firebase.verify("my-project", d))


class TestAws(unittest.TestCase):
    def test_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="123456789012\n")):
            self.assertIsNone(aws.verify("123456789012", "/p"))

    def test_mismatch(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="999999999999\n")):
            err = aws.verify("123456789012", "/p")
        self.assertIn("不一致", err)

    def test_cli_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            err = aws.verify("123456789012", "/p")
        self.assertIn("aws コマンドが見つかりません", err)

    def test_invalid_expected_type(self):
        err = aws.verify({"unsupported": "dict"}, "/p")
        self.assertIn("文字列", err)


class TestGcloud(unittest.TestCase):
    def test_string_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-proj\n")):
            self.assertIsNone(gcloud.verify("my-proj", "/p"))

    def test_string_mismatch(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="other\n")):
            err = gcloud.verify("my-proj", "/p")
        self.assertIn("不一致", err)

    def test_unset(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="(unset)\n")):
            err = gcloud.verify("my-proj", "/p")
        self.assertIn("設定されていません", err)

    def test_cli_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            err = gcloud.verify("my-proj", "/p")
        self.assertIn("gcloud コマンドが見つかりません", err)

    def test_dict_both_match(self):
        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="my-proj\n")
            return _fake_run(stdout="me@example.com\n")
        with mock.patch("subprocess.run", side_effect=side_effect):
            self.assertIsNone(
                gcloud.verify(
                    {"project": "my-proj", "account": "me@example.com"}, "/p"
                )
            )

    def test_dict_project_mismatch(self):
        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="other-proj\n")
            return _fake_run(stdout="me@example.com\n")
        with mock.patch("subprocess.run", side_effect=side_effect):
            err = gcloud.verify(
                {"project": "my-proj", "account": "me@example.com"}, "/p"
            )
        self.assertIn("プロジェクト不一致", err)

    def test_dict_account_mismatch(self):
        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="my-proj\n")
            return _fake_run(stdout="someone-else@example.com\n")
        with mock.patch("subprocess.run", side_effect=side_effect):
            err = gcloud.verify(
                {"project": "my-proj", "account": "me@example.com"}, "/p"
            )
        self.assertIn("アカウント不一致", err)

    def test_dict_empty_object(self):
        err = gcloud.verify({}, "/p")
        self.assertIn("project", err)


class TestKubectl(unittest.TestCase):
    def test_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="prod-cluster\n")):
            self.assertIsNone(kubectl.verify("prod-cluster", "/p"))

    def test_mismatch(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="dev-cluster\n")):
            err = kubectl.verify("prod-cluster", "/p")
        self.assertIn("不一致", err)

    def test_empty(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            err = kubectl.verify("prod", "/p")
        self.assertIn("設定されていません", err)

    def test_cli_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            err = kubectl.verify("prod", "/p")
        self.assertIn("kubectl コマンドが見つかりません", err)


class TestGithubSelfRemediation(unittest.TestCase):
    def test_switch_to_expected_str(self):
        self.assertTrue(github.is_self_remediation(
            "gh auth switch --hostname github.com --user Mao-o", "Mao-o"))

    def test_switch_short_flags(self):
        self.assertTrue(github.is_self_remediation(
            "gh auth switch -h github.com -u Mao-o", "Mao-o"))

    def test_switch_equals_form(self):
        self.assertTrue(github.is_self_remediation(
            "gh auth switch --user=Mao-o", "Mao-o"))

    def test_switch_to_other_user(self):
        self.assertFalse(github.is_self_remediation(
            "gh auth switch --user someone", "Mao-o"))

    def test_switch_without_user_is_not_remediation(self):
        self.assertFalse(github.is_self_remediation("gh auth switch", "Mao-o"))

    def test_non_switch_command(self):
        self.assertFalse(github.is_self_remediation("gh pr create", "Mao-o"))

    def test_dict_expected_matches_host(self):
        expected = {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}
        self.assertTrue(github.is_self_remediation(
            "gh auth switch --hostname ghe.example.com --user mao-corp", expected))

    def test_dict_expected_defaults_to_github_com(self):
        self.assertTrue(github.is_self_remediation(
            "gh auth switch --user Mao-o", {"github.com": "Mao-o"}))

    def test_dict_expected_wrong_host_user_pair(self):
        expected = {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}
        self.assertFalse(github.is_self_remediation(
            "gh auth switch --hostname ghe.example.com --user Mao-o", expected))


class TestGcloudSelfRemediation(unittest.TestCase):
    def test_set_project_to_expected_str(self):
        self.assertTrue(gcloud.is_self_remediation(
            "gcloud config set project my-proj", "my-proj"))

    def test_set_project_to_other(self):
        self.assertFalse(gcloud.is_self_remediation(
            "gcloud config set project other", "my-proj"))

    def test_set_account_with_str_expected_is_not_remediation(self):
        # str 期待値は project のみ検証対象 (verify と同じ解釈)
        self.assertFalse(gcloud.is_self_remediation(
            "gcloud config set account me@example.com", "my-proj"))

    def test_dict_expected_project_and_account(self):
        expected = {"project": "my-proj", "account": "me@example.com"}
        self.assertTrue(gcloud.is_self_remediation(
            "gcloud config set project my-proj", expected))
        self.assertTrue(gcloud.is_self_remediation(
            "gcloud config set account me@example.com", expected))
        self.assertFalse(gcloud.is_self_remediation(
            "gcloud config set account other@example.com", expected))

    def test_extra_flags_fall_through(self):
        self.assertFalse(gcloud.is_self_remediation(
            "gcloud config set project my-proj --quiet", "my-proj"))

    def test_other_gcloud_command(self):
        self.assertFalse(gcloud.is_self_remediation(
            "gcloud run deploy", "my-proj"))


class TestFirebaseSelfRemediation(unittest.TestCase):
    def test_use_expected_str(self):
        self.assertTrue(firebase.is_self_remediation("firebase use my-proj", "my-proj"))

    def test_use_other_project(self):
        self.assertFalse(firebase.is_self_remediation("firebase use other", "my-proj"))

    def test_dict_alias_and_project_id_both_accepted(self):
        expected = {"default": "proj-dev", "prod": "proj-prod"}
        self.assertTrue(firebase.is_self_remediation("firebase use prod", expected))
        self.assertTrue(firebase.is_self_remediation("firebase use proj-dev", expected))
        self.assertFalse(firebase.is_self_remediation("firebase use staging", expected))

    def test_use_with_extra_args_falls_through(self):
        self.assertFalse(firebase.is_self_remediation(
            "firebase use my-proj --add", "my-proj"))

    def test_deploy_is_not_remediation(self):
        self.assertFalse(firebase.is_self_remediation("firebase deploy", "my-proj"))


class TestKubectlSelfRemediation(unittest.TestCase):
    def test_use_context_expected(self):
        self.assertTrue(kubectl.is_self_remediation(
            "kubectl config use-context staging", "staging"))

    def test_use_context_other(self):
        self.assertFalse(kubectl.is_self_remediation(
            "kubectl config use-context prod", "staging"))

    def test_apply_is_not_remediation(self):
        self.assertFalse(kubectl.is_self_remediation(
            "kubectl apply -f x.yaml", "staging"))


class TestAwsHasNoSelfRemediation(unittest.TestCase):
    def test_aws_module_does_not_define_hook(self):
        # AWS は期待値 (Account ID) と切替手段 (profile / SSO) の照合が hook から
        # 不能のため意図的に未実装。dispatcher は getattr fallback で通常検証に落とす
        self.assertFalse(hasattr(aws, "is_self_remediation"))


class TestEnvPropagation(unittest.TestCase):
    """verify(env=...) が subprocess.run に env を渡すことを確認する (要望1)。

    インライン `AWS_PROFILE` 等が検証 subprocess に届かず永久 deny するバグの
    回帰防止。env 未指定時は env=None (= 親環境継承) であることも確認する。
    """

    def test_aws_passes_env(self):
        custom = {"AWS_PROFILE": "prod"}
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="123456789012")
        ) as m:
            self.assertIsNone(aws.verify("123456789012", "/p", env=custom))
        self.assertEqual(m.call_args.kwargs.get("env"), custom)

    def test_aws_default_env_is_none(self):
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="123456789012")
        ) as m:
            aws.verify("123456789012", "/p")
        self.assertIsNone(m.call_args.kwargs.get("env"))

    def test_gcloud_passes_env(self):
        custom = {"CLOUDSDK_ACTIVE_CONFIG_NAME": "work"}
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="my-proj")
        ) as m:
            self.assertIsNone(gcloud.verify("my-proj", "/p", env=custom))
        self.assertEqual(m.call_args.kwargs.get("env"), custom)

    def test_firebase_passes_env(self):
        custom = {"FOO": "bar"}
        # .firebaserc を読まないよう実在しない project_dir を渡し _from_cli 経路へ
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="my-proj")
        ) as m:
            self.assertIsNone(
                firebase.verify("my-proj", "/no/such/dir/xyz", env=custom)
            )
        self.assertEqual(m.call_args.kwargs.get("env"), custom)

    def test_github_passes_env(self):
        custom = {"GH_HOST": "github.com"}
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)
        ) as m:
            self.assertIsNone(github.verify("Mao-o", "/p", env=custom))
        self.assertEqual(m.call_args.kwargs.get("env"), custom)

    def test_kubectl_passes_env(self):
        custom = {"KUBECONFIG": "/tmp/kubeconfig"}
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="prod-ctx")
        ) as m:
            self.assertIsNone(kubectl.verify("prod-ctx", "/p", env=custom))
        self.assertEqual(m.call_args.kwargs.get("env"), custom)


if __name__ == "__main__":
    unittest.main()
