"""v0.36.6: 固定のある世界で、固定と違う値を指す context option のコマンドを固定で通さない。

0.36.5 までは、期待値が未登録の aws / gcloud / firebase のディレクトリ単位の固定の判定が context option を
見ず、`aws --profile prod s3 rm ...` (`AWS_PROFILE=dev` の固定) や `gcloud --account b@example.com run deploy`
(a@example.com の固定)、`firebase --project other deploy` (`firebase use` の記録) を固定で通していた。
CLI は option の値で動くので、そのコマンドは固定の値では動かない。固定の無い世界では同じコマンドが
未登録の deny になる。固定と同じ値を指す option のコマンドは固定のまま通す。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core.dispatcher import dispatch  # noqa: E402
from services import aws, gcloud  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    """実環境の `$HOME` / CLI 設定 / モード env を読ませない (test_pinning と同じ)。"""
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


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
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir()
        self._env(CLAUDE_PROJECT_DIR=str(self.project_dir), TMPDIR=str(cache_tmp))
        d = self.project_dir / ".claude" / "cloud-account-guard"
        d.mkdir(parents=True)
        (d / "accounts.local.json").write_text(json.dumps({"github": "me"}), encoding="utf-8")

    def _env(self, **values):
        p = mock.patch.dict(os.environ, values)
        p.start()
        self.addCleanup(p.stop)

    def _drop_env(self, *names):
        for name in names:
            if name in os.environ:
                p = mock.patch.dict(os.environ)
                p.start()
                self.addCleanup(p.stop)
                del os.environ[name]

    def _dispatch(self, command: str):
        """CLI を起動しないことも確かめる (固定の判定も未登録の deny も CLI を呼ばない)。"""
        with mock.patch("subprocess.run", side_effect=AssertionError("CLI を呼ばない")) as run:
            try:
                result = dispatch(command, str(self.project_dir))
            except AssertionError as e:  # 例外が漏れる変異も FAIL に数える
                self.fail(f"dispatch が例外で抜けた: {e!r}")
        self.assertFalse(run.called)
        return result

    def assert_denied(self, commands, key: str):
        for command in commands:
            with self.subTest(command=command):
                result = self._dispatch(command)
                self.assertEqual(_decision(result), "deny")
                reason = result["hookSpecificOutput"].get("permissionDecisionReason", "")
                self.assertIn(f'"{key}" キーがありません', reason)

    def assert_allowed(self, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(self._dispatch(command))


class TestPredicates(unittest.TestCase):
    """service の `is_pinned` が context を見る (dispatcher を通さない単体の確認)。"""

    def test_aws_profile_option(self):
        env = {"AWS_PROFILE": "dev"}
        self.assertFalse(aws.is_pinned(env, "/p", context={"profile": "prod"}))
        self.assertTrue(aws.is_pinned(env, "/p", context={"profile": "dev"}))
        self.assertTrue(aws.is_pinned(env, "/p", context={}))
        self.assertTrue(aws.is_pinned(env, "/p"))
        # 固定が無ければ option が同じ値でも固定ではない
        self.assertFalse(aws.is_pinned({"AWS_PROFILE": ""}, "/p", context={"profile": ""}))

    def test_gcloud_account_option_with_env_pin(self):
        env = {"CLOUDSDK_CORE_ACCOUNT": "a@example.com"}
        self.assertFalse(gcloud.is_pinned(env, "/p", context={"account": "b@example.com"}))
        self.assertTrue(gcloud.is_pinned(env, "/p", context={"account": "a@example.com"}))
        self.assertTrue(gcloud.is_pinned(env, "/p", context={}))
        self.assertTrue(gcloud.is_pinned(env, "/p"))

    def test_gcloud_configuration_reader_guards(self):
        """構成の account を読む helper (構成名による固定と `--configuration` の両方が使う) の守り。

        0.36.5 の `is_pinned` にあった条件を helper に移した。空の account・gcloud の命名規則から
        外れる名前 (パス要素の混入)・HOME の上書きでは、構成の account を読まない。
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "configurations" / "config_bad").mkdir(parents=True)
        (tmp / "configurations" / "config_bad" / "name").write_text(
            "[core]\naccount = b@example.com\n", encoding="utf-8"
        )
        (tmp / "configurations" / "config_work").write_text(
            "[core]\naccount = a@example.com\n", encoding="utf-8"
        )
        (tmp / "configurations" / "config_blank").write_text(
            "[core]\naccount =\n", encoding="utf-8"
        )
        base = {"CLOUDSDK_CONFIG": str(tmp), "HOME": os.environ.get("HOME", "")}
        self.assertTrue(gcloud.is_pinned({**base, "CLOUDSDK_ACTIVE_CONFIG_NAME": "work"}, "/p"))
        self.assertFalse(gcloud.is_pinned({**base, "CLOUDSDK_ACTIVE_CONFIG_NAME": "blank"}, "/p"))
        self.assertFalse(gcloud.is_pinned({**base, "CLOUDSDK_ACTIVE_CONFIG_NAME": "bad/name"}, "/p"))
        self.assertFalse(
            gcloud.is_pinned(
                {**base, "HOME": base["HOME"] + "-other", "CLOUDSDK_ACTIVE_CONFIG_NAME": "work"}, "/p"
            )
        )
        # `--configuration` の先も同じ守りで読む (読めなければ違うと確定しない)
        work = {**base, "CLOUDSDK_ACTIVE_CONFIG_NAME": "work"}
        for name in ("blank", "bad/name"):
            with self.subTest(configuration=name):
                self.assertTrue(gcloud.is_pinned(work, "/p", context={"configuration": name}))

    def test_gcloud_project_option_only_against_a_project_pin(self):
        env = {"CLOUDSDK_CORE_ACCOUNT": "a@example.com"}
        self.assertTrue(gcloud.is_pinned(env, "/p", context={"project": "p2"}))
        env = {**env, "CLOUDSDK_CORE_PROJECT": "p1"}
        self.assertFalse(gcloud.is_pinned(env, "/p", context={"project": "p2"}))
        self.assertTrue(gcloud.is_pinned(env, "/p", context={"project": "p1"}))


class TestAwsProfileOption(_Base):
    def setUp(self):
        super().setUp()
        self._env(AWS_PROFILE="dev")

    def test_profile_other_than_the_pin_is_not_pinned(self):
        self.assert_denied(
            [
                "aws --profile prod s3 rm s3://b/k",
                "aws s3 rm s3://b/k --profile prod",
                "aws s3 rm s3://b/k --profile=prod",
                # 同じコマンドの、option の無いセグメントと 1 つにまとめない
                "aws --profile prod s3 rm s3://b/k && aws s3 rm s3://b/k",
                "aws s3 rm s3://b/k && aws --profile prod s3 rm s3://b/k",
            ],
            "aws",
        )

    def test_profile_equal_to_the_pin_stays_pinned(self):
        self.assert_allowed(
            [
                "aws --profile dev s3 rm s3://b/k",
                "aws s3 rm s3://b/k --profile=dev",
                "aws s3 rm s3://b/k",
            ]
        )

    def test_query_with_other_profile_warns(self):
        """リモート read は、固定の無い世界と同じく未登録の警告 (止めない)。"""
        result = self._dispatch("aws s3 ls --profile prod")
        self.assertIsNotNone(result)
        self.assertNotEqual(_decision(result), "deny")
        self.assertIsNone(self._dispatch("aws s3 ls --profile dev"))

    def test_unreadable_profile_value_stays_pinned(self):
        """値を静的に読めない `--profile` は固定の値と違うと確定しないので、固定のまま (照合しない)。"""
        self.assert_allowed(["aws --profile $P s3 rm s3://b/k"])


class TestGcloudContextOptions(_Base):
    def setUp(self):
        super().setUp()
        self._drop_env("CLOUDSDK_CORE_ACCOUNT", "CLOUDSDK_CORE_PROJECT", "CLOUDSDK_ACTIVE_CONFIG_NAME")
        self.config_dir = self.tmp / "gcloud"
        (self.config_dir / "configurations").mkdir(parents=True)
        for name, account in (
            ("work", "a@example.com"), ("other", "b@example.com"), ("same", "a@example.com")
        ):
            (self.config_dir / "configurations" / f"config_{name}").write_text(
                f"[core]\naccount = {account}\nproject = p-{name}\n", encoding="utf-8"
            )
        (self.config_dir / "configurations" / "config_noacct").write_text(
            "[core]\nproject = p-noacct\n", encoding="utf-8"
        )
        self._env(CLOUDSDK_CONFIG=str(self.config_dir))

    def test_account_other_than_the_env_pin_is_not_pinned(self):
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        self.assert_denied(
            [
                "gcloud --account b@example.com run deploy s",
                "gcloud run deploy s --account=b@example.com",
            ],
            "gcloud",
        )
        self.assert_allowed(
            ["gcloud --account a@example.com run deploy s", "gcloud run deploy s"]
        )

    def test_env_pin_is_compared_without_surrounding_spaces(self):
        """`CLOUDSDK_CORE_ACCOUNT` / `CLOUDSDK_CORE_PROJECT` はローカル読取と同じく前後の空白を除いて比べる。"""
        self._env(CLOUDSDK_CORE_ACCOUNT=" a@example.com ", CLOUDSDK_CORE_PROJECT=" p1 ")
        self.assert_allowed(
            ["gcloud --account a@example.com run deploy s", "gcloud --project p1 run deploy s"]
        )

    def test_configuration_does_not_override_the_env_pin(self):
        """`CLOUDSDK_CORE_ACCOUNT` は `--configuration` の構成より勝つので、構成の account が違っても固定のまま。"""
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com", CLOUDSDK_ACTIVE_CONFIG_NAME="work")
        self.assert_allowed(["gcloud --configuration other run deploy s"])

    def test_configuration_pin(self):
        self._env(CLOUDSDK_ACTIVE_CONFIG_NAME="work")
        self.assert_denied(
            [
                "gcloud --account b@example.com run deploy s",
                "gcloud --configuration other run deploy s",
                "gcloud run deploy s --configuration=other",
            ],
            "gcloud",
        )
        self.assert_allowed(
            [
                "gcloud run deploy s",
                "gcloud --account a@example.com run deploy s",
                # 構成の名前は違っても account が同じ
                "gcloud --configuration same run deploy s",
                "gcloud --configuration work run deploy s",
                # 構成が無い・account が無い: 違う account と確定しない
                "gcloud --configuration missing run deploy s",
                "gcloud --configuration noacct run deploy s",
                # `--account` は構成の account より勝つ
                "gcloud --account a@example.com --configuration other run deploy s",
            ]
        )

    def test_project_other_than_the_project_pin_is_not_pinned(self):
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com", CLOUDSDK_CORE_PROJECT="p1")
        self.assert_denied(
            ["gcloud --project p2 run deploy s", "gcloud run deploy s --project=p2"], "gcloud"
        )
        self.assert_allowed(["gcloud --project p1 run deploy s", "gcloud run deploy s"])

    def test_project_is_free_without_a_project_pin(self):
        """固定の条件は project を問わない (project を固定していなければ `--project` は見ない)。"""
        self._env(CLOUDSDK_CORE_ACCOUNT="a@example.com")
        self.assert_allowed(["gcloud --project p2 run deploy s"])


class TestFirebaseProjectOption(_Base):
    def setUp(self):
        super().setUp()
        (self.project_dir / "firebase.json").write_text("{}", encoding="utf-8")
        (self.project_dir / "sub" / "firebase.json").write_text("{}", encoding="utf-8")
        self.xdg = self.tmp / "xdg"
        self._env(XDG_CONFIG_HOME=str(self.xdg))

    def _record(self, **by_dir: str) -> None:
        store = self.xdg / "configstore" / "firebase-tools.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        active = {
            os.path.realpath(self.project_dir / d if d != "." else self.project_dir): v
            for d, v in by_dir.items()
        }
        store.write_text(json.dumps({"activeProjects": active}), encoding="utf-8")

    def _firebaserc(self, text: str, base: Path | None = None) -> None:
        ((base or self.project_dir) / ".firebaserc").write_text(text, encoding="utf-8")

    def test_project_other_than_the_record_is_not_pinned(self):
        self._record(**{".": "my-proj"})
        self._firebaserc(json.dumps({"projects": {"prod": "prod-id", "mine": "my-proj"}}))
        self.assert_denied(
            [
                "firebase --project other deploy",
                "firebase -P other deploy",
                "firebase -Pother deploy",
                "firebase deploy --project=other",
                # alias の行き先が記録と違う project
                "firebase --project prod deploy",
            ],
            "firebase",
        )
        self.assert_allowed(
            [
                "firebase deploy",
                "firebase --project my-proj deploy",
                # alias の行き先が記録と同じ project
                "firebase --project mine deploy",
            ]
        )

    def test_alias_record_is_resolved_before_comparing(self):
        self._record(**{".": "mine"})
        self._firebaserc(json.dumps({"projects": {"prod": "prod-id", "mine": "my-proj"}}))
        self.assert_allowed(["firebase --project my-proj deploy", "firebase --project mine deploy"])
        self.assert_denied(["firebase --project prod deploy"], "firebase")

    def test_without_firebaserc_names_are_project_ids(self):
        self._record(**{".": "my-proj"})
        self.assert_denied(["firebase --project other deploy"], "firebase")
        self.assert_allowed(["firebase --project my-proj deploy"])

    def test_unconfirmed_firebaserc_stays_pinned(self):
        """`.firebaserc` を firebase-tools と同じ内容に読めると確かめられないと、行き先が違うと確定しない。"""
        self._record(**{".": "my-proj"})
        self._firebaserc('{"projects": {"other": "my-proj"}} // comment\n')
        self.assert_allowed(["firebase --project other deploy"])

    def test_config_option_uses_the_record_of_its_directory(self):
        self._record(sub="sub-proj")
        self.assert_denied(["firebase --config sub/firebase.json --project other deploy"], "firebase")
        self.assert_allowed(["firebase --config sub/firebase.json --project sub-proj deploy"])

    def test_config_option_resolves_aliases_with_its_firebaserc(self):
        """alias は `--config` のファイルのあるディレクトリ (project root) の `.firebaserc` で解決する。"""
        self._record(sub="sub-proj")
        self._firebaserc(json.dumps({"projects": {"s": "other-id"}}))
        self._firebaserc(json.dumps({"projects": {"s": "sub-proj"}}), base=self.project_dir / "sub")
        self.assert_allowed(["firebase --config sub/firebase.json --project s deploy"])


if __name__ == "__main__":
    unittest.main()
