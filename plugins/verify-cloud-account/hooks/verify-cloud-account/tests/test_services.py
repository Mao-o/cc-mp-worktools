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
            err = firebase.verify("my-project", "/p")
        self.assertIn("取得できません", err)

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


if __name__ == "__main__":
    unittest.main()
