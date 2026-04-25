"""get_active_account() / suggest_accounts_entry() のテスト。

subprocess.run を mock し、各 service が現在のアクティブ値を返すこと、および
builder が書込に使う suggestion を scalar/dict の形状で適切に返すことを検証する。
"""
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


GH_SINGLE_HOST = (
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


class TestGithubActiveAccount(unittest.TestCase):
    def test_get_active_single_host(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_SINGLE_HOST)):
            result = github.get_active_account("/p")
        self.assertEqual(result, {"github.com": "Mao-o"})

    def test_get_active_multi_host(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_MULTI_HOST)):
            result = github.get_active_account("/p")
        self.assertEqual(result, {"github.com": "Mao-o", "ghe.example.com": "mao-corp"})

    def test_get_active_no_login(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            self.assertIsNone(github.get_active_account("/p"))

    def test_get_active_cli_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(github.get_active_account("/p"))

    def test_suggest_entry_single_host_returns_scalar(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_SINGLE_HOST)):
            suggest = github.suggest_accounts_entry("/p")
        self.assertEqual(suggest, "Mao-o")

    def test_suggest_entry_multi_host_returns_dict(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_MULTI_HOST)):
            suggest = github.suggest_accounts_entry("/p")
        self.assertEqual(suggest, {"github.com": "Mao-o", "ghe.example.com": "mao-corp"})

    def test_suggest_entry_none_on_cli_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(github.suggest_accounts_entry("/p"))

    def test_parse_active_accounts_alias(self):
        """後方互換: _parse_active_accounts は parse_active_accounts と同じ関数。"""
        self.assertIs(github._parse_active_accounts, github.parse_active_accounts)


class TestFirebaseActiveAccount(unittest.TestCase):
    def test_get_active_from_cli(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-proj\n")):
            self.assertEqual(firebase.get_active_account("/p"), "my-proj")

    def test_get_active_from_firebaserc(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".firebaserc").write_text(
                json.dumps({"projects": {"default": "fallback-proj"}}),
                encoding="utf-8",
            )
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                self.assertEqual(firebase.get_active_account(d), "fallback-proj")

    def test_get_active_none(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            self.assertIsNone(firebase.get_active_account("/nonexistent"))

    def test_suggest_entry_scalar(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-proj\n")):
            self.assertEqual(firebase.suggest_accounts_entry("/p"), "my-proj")


class TestAwsActiveAccount(unittest.TestCase):
    def test_get_active(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="123456789012\n")):
            self.assertEqual(aws.get_active_account("/p"), "123456789012")

    def test_get_active_cli_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(aws.get_active_account("/p"))

    def test_get_active_no_credentials(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            self.assertIsNone(aws.get_active_account("/p"))

    def test_suggest_entry_scalar(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="123456789012\n")):
            self.assertEqual(aws.suggest_accounts_entry("/p"), "123456789012")


class TestGcloudActiveAccount(unittest.TestCase):
    def test_get_active_both(self):
        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="my-proj\n")
            return _fake_run(stdout="me@example.com\n")
        with mock.patch("subprocess.run", side_effect=side_effect):
            result = gcloud.get_active_account("/p")
        self.assertEqual(result, {"project": "my-proj", "account": "me@example.com"})

    def test_get_active_project_only(self):
        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="my-proj\n")
            return _fake_run(stdout="(unset)\n")
        with mock.patch("subprocess.run", side_effect=side_effect):
            result = gcloud.get_active_account("/p")
        self.assertEqual(result, {"project": "my-proj", "account": None})

    def test_get_active_both_unset(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="(unset)\n")):
            self.assertIsNone(gcloud.get_active_account("/p"))

    def test_get_active_cli_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(gcloud.get_active_account("/p"))

    def test_suggest_entry_project_only_scalar(self):
        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="my-proj\n")
            return _fake_run(stdout="(unset)\n")
        with mock.patch("subprocess.run", side_effect=side_effect):
            self.assertEqual(gcloud.suggest_accounts_entry("/p"), "my-proj")

    def test_suggest_entry_both_dict(self):
        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="my-proj\n")
            return _fake_run(stdout="me@example.com\n")
        with mock.patch("subprocess.run", side_effect=side_effect):
            self.assertEqual(
                gcloud.suggest_accounts_entry("/p"),
                {"project": "my-proj", "account": "me@example.com"},
            )


class TestKubectlActiveAccount(unittest.TestCase):
    def test_get_active(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="prod-cluster\n")):
            self.assertEqual(kubectl.get_active_account("/p"), "prod-cluster")

    def test_get_active_no_context(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            self.assertIsNone(kubectl.get_active_account("/p"))

    def test_get_active_cli_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(kubectl.get_active_account("/p"))

    def test_get_active_timeout(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
        ):
            self.assertIsNone(kubectl.get_active_account("/p"))

    def test_suggest_entry_scalar(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="prod-cluster\n")):
            self.assertEqual(kubectl.suggest_accounts_entry("/p"), "prod-cluster")


if __name__ == "__main__":
    unittest.main()
