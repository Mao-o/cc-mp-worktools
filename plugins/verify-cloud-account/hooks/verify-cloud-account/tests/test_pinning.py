"""v0.19.0: ディレクトリ単位の固定 (期待値の代わり) と起動リポジトリの外での実行。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import workdir  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from services import aws, firebase, gcloud  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    """実環境の `$HOME` / CLI 設定 / モード env を読ませない (test_dispatcher と同じ)。"""
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


def _reason(result) -> str:
    return result["hookSpecificOutput"].get("permissionDecisionReason", "")


def _decision(result):
    if result is None:
        return None
    return result["hookSpecificOutput"].get("permissionDecision")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project_dir = self.tmp / "repo"
        (self.project_dir / ".git").mkdir(parents=True)
        (self.project_dir / "sub").mkdir()
        self.other_dir = self.tmp / "other"
        (self.other_dir / ".git").mkdir(parents=True)
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(self.project_dir), "TMPDIR": str(cache_tmp)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_accounts(self, data: dict, base: Path | None = None) -> None:
        d = (base or self.project_dir) / ".claude" / "verify-cloud-account"
        d.mkdir(parents=True, exist_ok=True)
        (d / "accounts.local.json").write_text(json.dumps(data), encoding="utf-8")

    def _env(self, **values):
        p = mock.patch.dict(os.environ, values)
        p.start()
        self.addCleanup(p.stop)


class TestWorkdir(unittest.TestCase):
    def test_launch_root_is_git_toplevel(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "r" / ".git").mkdir(parents=True)
        (tmp / "r" / "a" / "b").mkdir(parents=True)
        self.assertEqual(
            workdir.launch_root(str(tmp / "r" / "a" / "b")), (tmp / "r").resolve()
        )

    def test_linked_worktree_under_the_repo_is_inside(self):
        """`isolation: worktree` の subagent は main の下の worktree (`.git` はファイル) で動く。"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "r" / ".git").mkdir(parents=True)
        wt = tmp / "r" / ".claude" / "worktrees" / "agent-x"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: ../../../.git/worktrees/agent-x\n", encoding="utf-8")
        root = workdir.launch_root(str(tmp / "r"))
        self.assertTrue(workdir.is_inside(str(wt), root))

    def test_launch_root_does_not_climb_into_home(self):
        """ホームが git 管理 (dotfiles) でも、ホームをルートにしない。"""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".git").mkdir()
        (tmp / "dev" / "a").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(tmp)}):
            root = workdir.launch_root(str(tmp / "dev" / "a"))
        self.assertEqual(root, (tmp / "dev" / "a").resolve())

    def test_launch_root_without_git_is_the_directory(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(workdir.launch_root(str(tmp)), tmp.resolve())

    def test_segment_dirs(self):
        cases = {
            "gh pr create": "/w",
            "cd sub && gh pr create": "/w/sub",
            "cd /x && gh pr create": "/x",
            "pushd ../y; gh pr create": "/y",
            "cd -P /x && gh pr create": "/x",
        }
        for command, want in cases.items():
            with self.subTest(command=command):
                self.assertEqual(
                    workdir.segment_dirs(command, "/w")["gh pr create"], {want}
                )

    def test_unresolvable_cd_is_unknown(self):
        for command in (
            'cd "$D" && gh pr create',
            "cd - && gh pr create",
            "cd ~other && gh pr create",
            "cd x* && gh pr create",
            "popd; gh pr create",
            "cd a b && gh pr create",
            "cd `pwd` && gh pr create",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    workdir.segment_dirs(command, "/w")["gh pr create"], {None}
                )

    def test_bare_cd_goes_home(self):
        with mock.patch.dict(os.environ, {"HOME": "/h"}):
            self.assertEqual(
                workdir.segment_dirs("cd; gh pr create", "/w")["gh pr create"], {"/h"}
            )

    def test_is_inside(self):
        root = Path("/r")
        self.assertTrue(workdir.is_inside("/r", root))
        self.assertTrue(workdir.is_inside("/r/a", root))
        self.assertFalse(workdir.is_inside("/rr", root))
        self.assertFalse(workdir.is_inside(None, root))


class TestPinnedPredicates(unittest.TestCase):
    def test_gcloud_account_env(self):
        self.assertTrue(gcloud.is_pinned({"CLOUDSDK_CORE_ACCOUNT": "a@example.com"}, "/p"))
        self.assertFalse(gcloud.is_pinned({"CLOUDSDK_CORE_ACCOUNT": " "}, "/p"))
        self.assertFalse(gcloud.is_pinned({"CLOUDSDK_CORE_PROJECT": "p"}, "/p"))
        self.assertFalse(gcloud.is_pinned({}, "/p"))

    def test_gcloud_named_configuration_with_account(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "configurations").mkdir()
        (tmp / "configurations" / "config_work").write_text(
            "[core]\naccount = a@example.com\n", encoding="utf-8"
        )
        (tmp / "configurations" / "config_noacct").write_text(
            "[core]\nproject = p\n", encoding="utf-8"
        )
        base = {"CLOUDSDK_CONFIG": str(tmp), "HOME": os.environ.get("HOME", "")}
        self.assertTrue(gcloud.is_pinned({**base, "CLOUDSDK_ACTIVE_CONFIG_NAME": "work"}, "/p"))
        self.assertFalse(gcloud.is_pinned({**base, "CLOUDSDK_ACTIVE_CONFIG_NAME": "noacct"}, "/p"))
        # 空の CLOUDSDK_CORE_ACCOUNT は構成の account を打ち消す
        self.assertFalse(
            gcloud.is_pinned(
                {**base, "CLOUDSDK_ACTIVE_CONFIG_NAME": "work", "CLOUDSDK_CORE_ACCOUNT": ""},
                "/p",
            )
        )

    def test_aws_profile_only(self):
        self.assertTrue(aws.is_pinned({"AWS_PROFILE": "prod"}, "/p"))
        self.assertFalse(aws.is_pinned({"AWS_ACCESS_KEY_ID": "AKIA..."}, "/p"))
        self.assertFalse(aws.is_pinned({"AWS_PROFILE": ""}, "/p"))

    def test_firebase_pin_action(self):
        self.assertTrue(firebase.is_pin_action("firebase use prod"))
        self.assertTrue(firebase.is_pin_action("firebase use prod --non-interactive"))
        self.assertFalse(firebase.is_pin_action("firebase use --add"))
        self.assertFalse(firebase.is_pin_action("firebase use prod --project x"))
        self.assertFalse(firebase.is_pin_action("firebase deploy"))


class TestUnregisteredButPinned(_Base):
    def test_gcloud_pinned_by_env_passes_without_key(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(dispatch("gcloud billing projects link p --billing-account=x", str(self.project_dir)))
        self.assertFalse(run.called)

    def test_gcloud_pinned_without_any_accounts_file(self):
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        self.assertIsNone(dispatch("gcloud run deploy", str(self.project_dir)))

    def test_inline_profile_does_not_count_as_pinned(self):
        """行頭のインライン env はその実行だけの指定で、ディレクトリ単位の固定ではない。"""
        self._write_accounts({"github": "me"})
        result = dispatch("AWS_PROFILE=prod aws s3 rm s3://b/x", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")

    def test_inline_value_equal_to_launch_env_keeps_pinned(self):
        self._write_accounts({"github": "me"})
        self._env(AWS_PROFILE="prod")
        self.assertIsNone(dispatch("AWS_PROFILE=prod aws s3 rm s3://b/x", str(self.project_dir)))

    def test_inline_override_of_pinned_env_is_denied(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        result = dispatch(
            "CLOUDSDK_CORE_ACCOUNT=other@example.com gcloud run deploy", str(self.project_dir)
        )
        self.assertEqual(_decision(result), "deny")

    def test_env_changed_earlier_in_the_command_is_not_pinned(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com", AWS_PROFILE="prod")
        for command in (
            "export CLOUDSDK_CORE_ACCOUNT=other@example.com && gcloud run deploy",
            "CLOUDSDK_CORE_ACCOUNT=other@example.com; gcloud run deploy",
            "unset CLOUDSDK_CORE_ACCOUNT; gcloud run deploy",
            "source .env && gcloud run deploy",
            "declare -x AWS_PROFILE=other; aws s3 rm s3://b/x",
            "export AWS_REGION=x AWS_PROFILE=other; aws s3 rm s3://b/x",
        ):
            with self.subTest(command=command):
                self.assertEqual(_decision(dispatch(command, str(self.project_dir))), "deny")

    def test_unrelated_env_change_keeps_pinned(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        self.assertIsNone(dispatch("export FOO=1 && gcloud run deploy", str(self.project_dir)))

    def test_firebaserc_default_alone_is_not_pinned(self):
        """commit される `.firebaserc` の default は固定とみなさない (利用者の選択ではない)。"""
        self._write_accounts({"github": "me"})
        (self.project_dir / ".firebaserc").write_text(
            '{"projects": {"default": "shared-proj"}}', encoding="utf-8"
        )
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = dispatch("firebase deploy", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")

    def test_firebase_use_record_is_pinned(self):
        self._write_accounts({"github": "me"})
        store = Path(os.environ["XDG_CONFIG_HOME"]) / "configstore"
        store.mkdir(parents=True, exist_ok=True)
        (store / "firebase-tools.json").write_text(
            json.dumps({"activeProjects": {str(self.project_dir.resolve()): "my-proj"}}),
            encoding="utf-8",
        )
        self.assertIsNone(dispatch("firebase deploy", str(self.project_dir)))

    def test_unpinned_gcloud_is_denied_with_pin_hint(self):
        self._write_accounts({"github": "me"})
        result = dispatch("gcloud run deploy", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")
        reason = _reason(result)
        self.assertIn('"gcloud" キーがありません', reason)
        self.assertIn("CLOUDSDK_CORE_ACCOUNT", reason)
        self.assertIn("set --service gcloud", reason)

    def test_gh_has_no_pinning_and_still_requires_a_key(self):
        self._write_accounts({"aws": "123456789012"})
        result = dispatch("gh pr create", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")
        self.assertIn('"github" キーがありません', _reason(result))

    def test_no_accounts_file_mentions_pinning(self):
        result = dispatch("aws s3 rm s3://b/x", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")
        reason = _reason(result)
        self.assertIn("未設定です", reason)
        self.assertIn("AWS_PROFILE", reason)

    def test_firebase_use_is_allowed_while_unregistered(self):
        """固定の案内 (`firebase use <x>`) が未登録の deny で止まらない。"""
        self._write_accounts({"github": "me"})
        self.assertIsNone(dispatch("firebase use my-proj", str(self.project_dir)))

    def test_registered_value_is_still_verified_when_pinned(self):
        self._write_accounts({"gcloud": {"account": "want@example.com"}})
        self._env(CLOUDSDK_CORE_ACCOUNT="other@example.com")
        with mock.patch(
            "subprocess.run",
            return_value=mock.Mock(stdout="other@example.com\n", stderr="", returncode=0),
        ):
            result = dispatch("gcloud run deploy", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")
        self.assertIn("CLOUDSDK_CORE_ACCOUNT", _reason(result))


class TestOutsideLaunchRepository(_Base):
    def test_cd_to_another_repo_is_denied_even_when_pinned(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        result = dispatch(f"cd {self.other_dir} && gcloud run deploy", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")
        reason = _reason(result)
        self.assertIn("起動したリポジトリ", reason)
        self.assertNotIn(str(self.other_dir), reason)

    def test_registered_service_outside_is_denied_without_verifying(self):
        self._write_accounts({"github": "me"})
        with mock.patch("services.github.verify", return_value=None) as v:
            result = dispatch(f"cd {self.other_dir} && gh pr create", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")
        v.assert_not_called()

    def test_bash_cwd_outside_is_denied(self):
        """Bash の作業ディレクトリ自体が外 (hook と Bash の env が食い違う状態)。"""
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        result = dispatch("gcloud run deploy", str(self.other_dir))
        self.assertEqual(_decision(result), "deny")

    def test_unresolvable_cd_is_denied(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        result = dispatch('cd "$D" && gcloud run deploy', str(self.project_dir))
        self.assertEqual(_decision(result), "deny")

    def test_subdirectory_of_launch_repo_is_inside(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        self.assertIsNone(dispatch("cd sub && gcloud run deploy", str(self.project_dir)))
        self.assertIsNone(dispatch("gcloud run deploy", str(self.project_dir / "sub")))

    def test_query_outside_only_warns(self):
        self._write_accounts({"github": "me"})
        with mock.patch("services.github.verify", return_value=None):
            result = dispatch(f"cd {self.other_dir} && gh pr list", str(self.project_dir))
        self.assertNotEqual(_decision(result), "deny")
        self.assertIsNotNone(result)

    def test_outside_without_any_accounts_file(self):
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        result = dispatch(f"cd {self.other_dir} && gcloud run deploy", str(self.project_dir))
        self.assertEqual(_decision(result), "deny")
        self.assertIn("起動したリポジトリ", _reason(result))

    def test_readonly_outside_is_not_checked(self):
        self._write_accounts({"github": "me"})
        self.assertIsNone(dispatch(f"cd {self.other_dir} && gh auth status", str(self.project_dir)))


if __name__ == "__main__":
    unittest.main()


class TestShowPinning(_Base):
    def _show(self) -> str:
        import io

        from scripts import accounts_builder as builder

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(builder.SERVICES[0], "get_active_account", return_value=None):
            builder.main(["show"], stdout=out, stderr=err)
        return out.getvalue()

    def test_lists_unregistered_services_with_pin_state(self):
        self._write_accounts({"github": "me"})
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        text = self._show()
        self.assertIn("gcloud: [未登録 — ディレクトリ単位で固定済み", text)
        self.assertIn("aws: [未登録・未固定", text)
        self.assertIn("kubectl: [未登録 — 固定の仕組みが無い", text)
        self.assertNotIn("github: [未登録", text)
        self.assertNotIn("a@example.com", text)

    def test_listed_without_accounts_file(self):
        self._env(AWS_PROFILE="prod")
        text = self._show()
        self.assertIn("aws: [未登録 — ディレクトリ単位で固定済み", text)
        self.assertNotIn("prod", text.split("===")[-1])
