"""各 service の verify() テスト。subprocess.run を mock する。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from services import aws, firebase, gcloud, github, kubectl  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    """実環境の gh / gcloud 設定を読ませない。

    v0.13.0 以降 `verify()` は CLI を起動する前にローカル設定ファイルを読むため、
    隔離しないと**開発者の `~/.config` 配下**が verdict を決めてしまい、CLI モックを
    前提にしたこのモジュールのテストが「CLI を呼ばずに allow」で壊れる / 空振りする。
    """
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


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

# gh 2.40+ で同一 host に複数アカウントが紐付き、非アクティブなものは
# `Active account: false` になる (内部バックログ: 旧テストは全 fixture が
# Active account: true のみだった)。
GH_INACTIVE_SECOND_ACCOUNT = (
    "github.com\n"
    "  ✓ Logged in to github.com account Mao-o (keyring)\n"
    "  - Active account: true\n"
    "  ✓ Logged in to github.com account other-user (keyring)\n"
    "  - Active account: false\n"
)

# `GITHUB_TOKEN` 等で無効な token を渡すとログイン失敗した host ブロックが出る
# (gh 2.98 実測形式に準拠。`Active account:` 行も `Logged in to` 行も無い)。
GH_FAILED_HOST = (
    "github.com\n"
    "  X Failed to log in to github.com using token (GITHUB_TOKEN)\n"
    "  - The token in GITHUB_TOKEN is invalid.\n"
)

# gh < 2.40 (複数アカウント対応前) の単一アカウント形式。`Active account:` marker
# が無く、host あたり常に 1 アカウントのみ (内部バックログ)。
GH_LEGACY_SINGLE_HOST = (
    "github.com\n"
    "  ✓ Logged in to github.com as Mao-o (keyring)\n"
    "  ✓ Git operations for github.com configured to use ssh protocol.\n"
    "  ✓ Token: gho_************************************\n"
    "  ✓ Token scopes: repo, read:org\n"
)

GH_LEGACY_MULTI_HOST = (
    "github.com\n"
    "  ✓ Logged in to github.com as Mao-o (keyring)\n"
    "  ✓ Token: gho_************************************\n"
    "ghe.example.com\n"
    "  ✓ Logged in to ghe.example.com as mao-corp (keyring)\n"
    "  ✓ Token: ghp_************************************\n"
)

# `Logged in to` はあるが両方の正規表現 (`account` / `as`) のどちらにも
# 一致しない未知フォーマット (将来の gh 出力変更を想定した契約テスト用)。
GH_UNPARSEABLE = "github.com\n  Logged in to github.com (Mao-o)\n"

# gh の列挙順は設定ファイル依存で、GHE が先に来ることがある (内部バックログ)。
# scalar 期待値の照合先が「最初の host」だと、この順序だけで verdict が変わる。
GH_MULTI_HOST_GHE_FIRST = (
    "ghe.example.com\n"
    "  ✓ Logged in to ghe.example.com account mao-corp\n"
    "  - Active account: true\n"
    "github.com\n"
    "  ✓ Logged in to github.com account Mao-o\n"
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
        self.assertIsNotNone(err)
        self.assertIn("不一致", err)
        self.assertIn("Mao-o", err)

    def test_cli_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            err = github.verify("Mao-o", "/p")
        self.assertIsNotNone(err)
        self.assertIn("gh コマンドが見つかりません", err)

    def test_timeout(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
        ):
            err = github.verify("Mao-o", "/p")
        self.assertIsNotNone(err)
        self.assertIn("タイムアウト", err)

    def test_empty_output(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
            err = github.verify("Mao-o", "/p")
        self.assertIsNotNone(err)
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
        self.assertIsNotNone(err)
        self.assertIn("ghe.example.com", err)
        self.assertIn("wrong-user", err)

    def test_dict_host_not_logged_in(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            err = github.verify(
                {"github.com": "Mao-o", "ghe.example.com": "mao-corp"},
                "/p",
            )
        self.assertIsNotNone(err)
        self.assertIn("ghe.example.com", err)
        self.assertIn("ログインしていません", err)

    def test_invalid_expected_type(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            err = github.verify(12345, "/p")
        self.assertIsNotNone(err)
        self.assertIn("文字列または", err)

    def test_dict_empty_object_fail_closed(self):
        """Codex R4 回帰: 空 dict は fail-closed。"""
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            err = github.verify({}, "/p")
        self.assertIsNotNone(err)
        self.assertIn("空", err)

    def test_string_match_when_ghe_listed_first(self):
        """gh が GHE を先に列挙しても scalar 期待値は github.com と照合する。

        内部バックログ: 照合先が「最初の host」だと gh の列挙順 (設定ファイル
        依存) で verdict が変わる。`scalar_target_host` に一本化した規則を
        パーサ込みの経路で固定する。
        """
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout=GH_MULTI_HOST_GHE_FIRST)
        ):
            self.assertIsNone(github.verify("Mao-o", "/p"))

    def test_string_mismatch_when_ghe_listed_first(self):
        """GHE 側のユーザー名は scalar 期待値に一致しない (照合先は github.com)。"""
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout=GH_MULTI_HOST_GHE_FIRST)
        ):
            err = github.verify("mao-corp", "/p")
        self.assertIsNotNone(err)
        self.assertIn("github.com", err)


class TestGithubScalarTargetHost(unittest.TestCase):
    """`scalar_target_host` / `matches` の direct unit tests (内部バックログ)。"""

    def test_prefers_github_com_regardless_of_order(self):
        self.assertEqual(
            github.scalar_target_host({"github.com": "a", "ghe.example.com": "b"}),
            "github.com",
        )
        self.assertEqual(
            github.scalar_target_host({"ghe.example.com": "b", "github.com": "a"}),
            "github.com",
        )

    def test_falls_back_to_first_host_without_github_com(self):
        self.assertEqual(
            github.scalar_target_host({"ghe.example.com": "b", "ghe2.example.com": "c"}),
            "ghe.example.com",
        )

    def test_matches_scalar_uses_github_com(self):
        active = {"ghe.example.com": "mao-corp", "github.com": "Mao-o"}
        self.assertTrue(github.matches("Mao-o", active))
        self.assertFalse(github.matches("mao-corp", active))

    def test_matches_dict_requires_every_declared_host(self):
        active = {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}
        self.assertTrue(github.matches({"github.com": "Mao-o"}, active))
        self.assertTrue(github.matches(dict(active), active))
        self.assertFalse(github.matches({"ghe.example.com": "other"}, active))
        self.assertFalse(github.matches({"missing.example.com": "x"}, active))

    def test_matches_rejects_unusable_shapes(self):
        """空 dict / 非文字列は verify() が deny する形なので match ではない。"""
        active = {"github.com": "Mao-o"}
        self.assertFalse(github.matches({}, active))
        self.assertFalse(github.matches({"github.com": 123}, active))
        self.assertFalse(github.matches(123, active))
        self.assertFalse(github.matches("Mao-o", {}))
        self.assertFalse(github.matches("Mao-o", None))


class TestGithubAuthStatusParsing(unittest.TestCase):
    """内部バックログ: gh < 2.40 (単一アカウント形式) の parse fallback と、
    非アクティブ / ログイン失敗 host の除外、解釈不能フォーマットの案内文面。"""

    def test_inactive_second_account_excluded(self):
        """同一 host に 2 アカウントあり、非アクティブ側は拾わない。"""
        result = github.parse_active_accounts(GH_INACTIVE_SECOND_ACCOUNT)
        self.assertEqual(result, {"github.com": "Mao-o"})

    def test_failed_login_host_excluded(self):
        """ログインに失敗した host ブロックは active に含めない (クラッシュもしない)。"""
        result = github.parse_active_accounts(GH_FAILED_HOST)
        self.assertEqual(result, {})

    def test_legacy_single_host_format(self):
        """gh < 2.40: `Active account:` marker が無い単一アカウント形式。"""
        result = github.parse_active_accounts(GH_LEGACY_SINGLE_HOST)
        self.assertEqual(result, {"github.com": "Mao-o"})

    def test_legacy_multi_host_format(self):
        result = github.parse_active_accounts(GH_LEGACY_MULTI_HOST)
        self.assertEqual(
            result, {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}
        )

    def test_legacy_format_verify_matches(self):
        """旧形式でも verify() が通常どおり照合できる (fallback の end-to-end 確認)。"""
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_LEGACY_SINGLE_HOST)):
            self.assertIsNone(github.verify("Mao-o", "/p"))

    def test_legacy_format_verify_mismatch(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_LEGACY_SINGLE_HOST)):
            err = github.verify("other-user", "/p")
        self.assertIsNotNone(err)
        self.assertIn("不一致", err)

    def test_unparseable_output_gives_version_hint(self):
        """`Logged in to` はあるのにどちらの形式にも一致しない場合、
        「未ログイン」ではなく「解釈できません」+ gh --version 案内にする。"""
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_UNPARSEABLE)):
            err = github.verify("Mao-o", "/p")
        self.assertIsNotNone(err)
        self.assertIn("解釈できません", err)
        self.assertIn("gh --version", err)
        self.assertNotIn("gh auth login", err)

    def test_truly_logged_out_keeps_login_guidance(self):
        """`Logged in to` が一切無い (本当に未ログイン) は従来どおり login 案内。"""
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stderr="You are not logged into any GitHub hosts.\n", returncode=1
            ),
        ):
            err = github.verify("Mao-o", "/p")
        self.assertIsNotNone(err)
        self.assertIn("gh auth login --skip-ssh-key", err)
        self.assertNotIn("解釈できません", err)


class TestFirebase(unittest.TestCase):
    def setUp(self):
        # firebase-tools の configstore を実環境 (~/.config) から読まないよう
        # XDG_CONFIG_HOME を空の一時ディレクトリに向ける (テストの hermeticity)。
        self._xdg = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._xdg, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self._xdg})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_configstore(self, active_projects: dict) -> None:
        """firebase-tools の configstore (`firebase use` の切替先) を模擬する。"""
        path = Path(self._xdg) / "configstore" / "firebase-tools.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"activeProjects": active_projects}), encoding="utf-8"
        )

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

    def test_dict_unresolved_lists_alias_commands(self):
        """dict 期待値 + 未解決 (未ログイン等) は placeholder ではなく alias ごとの
        `firebase use <alias>` を案内する (self-remediation で通る形)。"""
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        with mock.patch("subprocess.run", return_value=_fake_run(returncode=1)):
            with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                err = firebase.verify({"default": "proj-dev", "prod": "proj-prod"}, d)
        self.assertIn("firebase login", err)
        self.assertIn("firebase use default  # → proj-dev", err)
        self.assertIn("firebase use prod  # → proj-prod", err)
        self.assertNotIn("YOUR_PROJECT", err)

    def test_invalid_expected_shapes_do_not_run_cli(self):
        """期待値の形が不正なら CLI を叩かずに設定誘導の deny を返す。"""
        with mock.patch("subprocess.run") as run:
            self.assertIn("有効な", firebase.verify({"default": 123}, "/p"))
            self.assertIn("文字列または", firebase.verify(42, "/p"))
        run.assert_not_called()

    def test_firebase_tools_alias_is_self_remediation(self):
        self.assertTrue(firebase.is_self_remediation("firebase-tools use prod", {"prod": "proj-prod"}))
        self.assertTrue(firebase.is_self_remediation("firebase-tools use proj", "proj"))
        self.assertFalse(firebase.is_self_remediation("firebase-tools use other", "proj"))

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

    # --- 解決順 (内部バックログ): firebase use → CLI 不可時のみ .firebaserc ---

    @staticmethod
    def _write_firebaserc(d: str, projects: dict) -> None:
        (Path(d) / ".firebaserc").write_text(
            json.dumps({"projects": projects}), encoding="utf-8"
        )

    _TIMEOUT = subprocess.TimeoutExpired(cmd="firebase use", timeout=10)

    def test_cli_switch_wins_over_firebaserc_default(self):
        """`firebase use prod` で切替済み (CLI=proj-prod) なら .firebaserc の
        default=proj-dev より CLI を優先し、scalar 期待 proj-prod に一致 → allow。
        旧実装 (.firebaserc 優先) では永久 deny になっていた。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "proj-dev", "prod": "proj-prod"})
            with mock.patch(
                "subprocess.run", return_value=_fake_run(stdout="proj-prod\n")
            ):
                self.assertIsNone(firebase.verify("proj-prod", d))

    def test_default_expected_but_cli_switched_denies(self):
        """期待が default の project のまま `firebase use prod` されていれば deny
        (旧実装では .firebaserc の default で照合して allow = false-allow)。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "proj-dev", "prod": "proj-prod"})
            with mock.patch(
                "subprocess.run", return_value=_fake_run(stdout="proj-prod\n")
            ):
                err = firebase.verify("proj-dev", d)
        self.assertIsNotNone(err)
        self.assertIn("現在=proj-prod", err)
        self.assertIn("期待=proj-dev", err)

    def test_dict_default_only_expected_but_cli_switched_denies(self):
        """dict 期待値 {"default": "proj-dev"} でも CLI の切替先 (proj-prod) で照合し deny。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "proj-dev", "prod": "proj-prod"})
            with mock.patch(
                "subprocess.run", return_value=_fake_run(stdout="proj-prod\n")
            ):
                err = firebase.verify({"default": "proj-dev"}, d)
        self.assertIsNotNone(err)
        self.assertIn("現在=proj-prod", err)

    def test_cli_timeout_denies_with_dedicated_message(self):
        """CLI timeout は .firebaserc が期待値に一致していても fallback せず
        専用メッセージで deny (fail-closed)。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "my-project"})
            with mock.patch("subprocess.run", side_effect=self._TIMEOUT):
                err = firebase.verify("my-project", d)
        self.assertIsNotNone(err)
        self.assertIn("firebase use がタイムアウトしました", err)
        self.assertNotIn("firebase login", err)

    def test_cli_timeout_without_firebaserc_same_message(self):
        """.firebaserc が無くても timeout は「firebase login && firebase use」の
        誤案内ではなく timeout 専用メッセージになる。"""
        with mock.patch("subprocess.run", side_effect=self._TIMEOUT):
            with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                err = firebase.verify("my-project", "/p")
        self.assertIn("タイムアウト", err)
        self.assertNotIn("firebase login", err)

    def test_cli_empty_output_falls_back_to_firebaserc(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "my-project"})
            with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
                self.assertIsNone(firebase.verify("my-project", d))

    def test_cli_nonzero_exit_falls_back_to_firebaserc(self):
        """非 TTY の `firebase use` は active project が無いと stderr に
        "No active project" を出して非ゼロ終了 → .firebaserc に fallback。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "my-project"})
            with mock.patch(
                "subprocess.run",
                return_value=_fake_run(
                    stdout="", stderr="Error: No active project\n", returncode=1
                ),
            ):
                self.assertIsNone(firebase.verify("my-project", d))

    def test_cli_nonzero_exit_ignores_stdout(self):
        """非ゼロ終了時は stdout が単一トークンでも採用しない。ローカル設定も無ければ
        「取得できません」(旧実装は終了コードを見ず "other" を採用して allow)。"""
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "subprocess.run",
                return_value=_fake_run(stdout="other\n", returncode=1),
            ):
                with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                    err = firebase.verify("other", d)
        self.assertIsNotNone(err)
        self.assertIn("取得できません", err)

    # --- CLI 不可時の configstore (firebase use の切替先) 参照 ---

    def test_cli_missing_uses_configstore_switch_over_firebaserc_default(self):
        """hook の PATH に firebase が無い (npx 等) 環境でも、configstore に
        `firebase use prod` の切替が残っていれば .firebaserc の default ではなく
        切替先で照合して deny する (false-allow 防止)。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "proj-dev", "prod": "proj-prod"})
            self._write_configstore({os.path.abspath(d): "prod"})
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                err = firebase.verify("proj-dev", d)
        self.assertIsNotNone(err)
        self.assertIn("現在=proj-prod", err)

    def test_cli_missing_configstore_switch_matches_expected(self):
        """同じ状況で期待が切替先 (proj-prod) なら allow (永久 deny 防止)。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "proj-dev", "prod": "proj-prod"})
            self._write_configstore({os.path.abspath(d): "prod"})
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                self.assertIsNone(firebase.verify("proj-prod", d))

    def test_cli_nonzero_uses_configstore_project_id(self):
        """CLI が非ゼロ終了 (認証失敗等) でも configstore の値 (alias に無い
        project ID) をそのまま現在値にする。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "proj-dev"})
            self._write_configstore({os.path.abspath(d): "proj-prod"})
            with mock.patch(
                "subprocess.run",
                return_value=_fake_run(stdout="", stderr="Error: auth\n", returncode=1),
            ):
                err = firebase.verify("proj-dev", d)
        self.assertIsNotNone(err)
        self.assertIn("現在=proj-prod", err)

    def test_configstore_lookup_walks_up_to_ancestor(self):
        """configstore のキーが project_dir の親 (firebase-tools と同じ親方向探索)。"""
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "apps" / "web"
            sub.mkdir(parents=True)
            self._write_firebaserc(str(sub), {"default": "proj-dev", "prod": "proj-prod"})
            self._write_configstore({os.path.abspath(d): "prod"})
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                self.assertIsNone(firebase.verify("proj-prod", str(sub)))

    def test_configstore_lookup_matches_realpath_key(self):
        """configstore のキーが実体パス (symlink 解決済み) でも一致する。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "proj-dev", "prod": "proj-prod"})
            self._write_configstore({os.path.realpath(d): "prod"})
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                self.assertIsNone(firebase.verify("proj-prod", d))

    def test_configstore_path_honours_env_argument(self):
        """verify(env=...) の XDG_CONFIG_HOME を configstore の場所に使う
        (行頭インライン env と CLI の解釈を揃える)。"""
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as xdg:
            self._write_firebaserc(d, {"default": "proj-dev", "prod": "proj-prod"})
            path = Path(xdg) / "configstore" / "firebase-tools.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"activeProjects": {os.path.abspath(d): "prod"}}),
                encoding="utf-8",
            )
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                err = firebase.verify("proj-dev", d, env={"XDG_CONFIG_HOME": xdg})
        self.assertIsNotNone(err)
        self.assertIn("現在=proj-prod", err)

    def test_configstore_unreadable_falls_back_to_firebaserc(self):
        """configstore が壊れている / 形が違う場合は例外にせず .firebaserc の規則へ。"""
        for payload in ("{not json", '{"activeProjects": ["x"]}', "[]"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as d:
                self._write_firebaserc(d, {"default": "proj-dev"})
                path = Path(self._xdg) / "configstore" / "firebase-tools.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
                with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                    self.assertIsNone(firebase.verify("proj-dev", d))

    def test_firebaserc_undecodable_bytes_does_not_crash(self):
        """UTF-8 として読めない .firebaserc も例外にせず未解決扱い。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".firebaserc").write_bytes(b"\xff\xfe\x00")
            with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
                with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                    err = firebase.verify("proj", d)
        self.assertIn("取得できません", err)

    def test_cli_permission_error_falls_back_to_local(self):
        """firebase が PATH にあるが実行できない (PermissionError 等 FileNotFoundError
        以外の OSError) ときも例外にせず CLI 不可としてローカル設定に fallback する。
        例外が漏れると hook が異常終了し PreToolUse は無音 fail-open になる。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"default": "my-project"})
            with mock.patch(
                "subprocess.run",
                side_effect=PermissionError(13, "Permission denied", "firebase"),
            ):
                self.assertIsNone(firebase.verify("my-project", d))

    def test_cli_permission_error_without_local_config_denies(self):
        with mock.patch(
            "subprocess.run",
            side_effect=PermissionError(13, "Permission denied", "firebase"),
        ):
            with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                err = firebase.verify("my-project", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("取得できません", err)

    def test_local_lookup_starts_from_project_root_with_firebase_json(self):
        """firebase-tools と同じく firebase.json のあるディレクトリを起点に .firebaserc
        と configstore を探す。monorepo の子ディレクトリ (CLAUDE_PROJECT_DIR) で起動
        しても親の alias を解決でき、「現在=prod, 期待=proj-prod」の誤 deny にならない。"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            sub = repo / "packages" / "functions"
            sub.mkdir(parents=True)
            (repo / "firebase.json").write_text("{}", encoding="utf-8")
            self._write_firebaserc(str(repo), {"default": "proj-dev", "prod": "proj-prod"})
            self._write_configstore({os.path.abspath(repo): "prod"})
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                self.assertIsNone(firebase.verify("proj-prod", str(sub)))
                err = firebase.verify("proj-dev", str(sub))
        self.assertIsNotNone(err)
        self.assertIn("現在=proj-prod", err)

    # --- firebase use の cwd (Codex R2 P2: builder を project_dir の外から起動) ---

    def test_cli_runs_in_project_root_not_process_cwd(self):
        """`firebase use` は firebase.json を親方向に探した project root を cwd にして
        実行する。hook / builder プロセスの cwd を継承すると、builder を project_dir の
        外から起動したとき無関係なディレクトリの project を報告しうる。"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            sub = repo / "packages" / "functions"
            sub.mkdir(parents=True)
            (repo / "firebase.json").write_text("{}", encoding="utf-8")
            with mock.patch(
                "subprocess.run", return_value=_fake_run(stdout="proj-prod\n")
            ) as m:
                self.assertIsNone(firebase.verify("proj-prod", str(sub)))
            self.assertEqual(m.call_args.kwargs.get("cwd"), os.path.abspath(repo))

    def test_cli_cwd_is_project_dir_without_firebase_json(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "subprocess.run", return_value=_fake_run(stdout="proj\n")
            ) as m:
                self.assertIsNone(firebase.verify("proj", d))
            self.assertEqual(m.call_args.kwargs.get("cwd"), os.path.abspath(d))

    def test_cli_cwd_missing_project_dir_is_treated_as_cli_unavailable(self):
        """project_dir が存在しないと subprocess.run(cwd=...) が OSError を投げる。
        その経路も CLI 不可として扱い例外を漏らさない (ローカル設定も無ければ
        「取得できません」)。"""

        def _run(*_args, **kwargs):
            cwd = kwargs.get("cwd", ".")
            if not os.path.isdir(cwd):
                raise NotADirectoryError(20, "Not a directory", cwd)
            return _fake_run(stdout="proj\n")

        with mock.patch("subprocess.run", side_effect=_run):
            with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                err = firebase.verify("proj", "/no/such/dir/xyz")
        self.assertIsInstance(err, str)
        self.assertIn("取得できません", err)

    def test_firebaserc_single_non_default_alias(self):
        """.firebaserc の alias が 1 つだけなら default 以外の名前でもその値
        (firebase-tools applyRC と同じ規則)。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"staging": "proj-stg"})
            with mock.patch("subprocess.run", side_effect=FileNotFoundError):
                self.assertIsNone(firebase.verify("proj-stg", d))

    def test_firebaserc_multiple_aliases_without_default_unresolved(self):
        """alias が複数で default が無い .firebaserc は解決不能 → 取得できません。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_firebaserc(d, {"staging": "proj-stg", "prod": "proj-prod"})
            with mock.patch("subprocess.run", return_value=_fake_run(stdout="")):
                with mock.patch("shutil.which", return_value="/usr/local/bin/firebase"):
                    err = firebase.verify("proj-stg", d)
        self.assertIn("取得できません", err)

    def test_firebaserc_malformed_shape_does_not_crash(self):
        """projects が dict でない / top-level が list / 値が非文字列でも
        例外にならず未解決扱い。"""
        payloads = (
            '{"projects": ["proj"]}',
            '["proj"]',
            '{"projects": {"default": 1}}',
        )
        with tempfile.TemporaryDirectory() as d:
            for payload in payloads:
                with self.subTest(payload=payload):
                    (Path(d) / ".firebaserc").write_text(payload, encoding="utf-8")
                    with mock.patch(
                        "subprocess.run", return_value=_fake_run(stdout="")
                    ):
                        with mock.patch(
                            "shutil.which", return_value="/usr/local/bin/firebase"
                        ):
                            err = firebase.verify("proj", d)
                    self.assertIn("取得できません", err)


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

    # --- 内部バックログ: 切替案内は export ではなく行頭インライン + sso login ---

    _NO_CONFIG = {"AWS_CONFIG_FILE": "/nonexistent/aws/config"}

    def _mismatch(self, env):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="111111111111\n")):
            return aws.verify("123456789012", "/p", env=env)

    def test_mismatch_guidance_inline_env_first_then_sso_login(self):
        err = self._mismatch(self._NO_CONFIG)
        lines = [line.strip() for line in err.splitlines()]
        inline = next(i for i, l in enumerate(lines) if l.startswith("AWS_PROFILE=<profile> aws ..."))
        sso = next(i for i, l in enumerate(lines) if l.startswith("aws sso login --profile <profile>"))
        self.assertLess(inline, sso)
        self.assertIn("この形のみ検証に反映", err)
        # export はコマンド行 (インデント付き) として案内しない。注記で「効かない」と明示する
        self.assertNotRegex(err, r"(?m)^\s+export\s")
        self.assertIn("export AWS_PROFILE=... は Claude Code の Bash では次の呼出に持ち越されず", err)
        # 不一致時は `aws configure` (認証情報の再設定) は案内しない
        self.assertNotRegex(err, r"(?m)^\s+aws configure\s")
        # profile が引けないときは確認コマンドを案内する
        self.assertIn("aws configure list-profiles", err)

    def test_mismatch_guidance_resolves_profile_from_aws_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config"
            cfg.write_text(
                "[profile dev]\nsso_account_id = 111111111111\n"
                "[profile prod]\nsso_session = corp\nsso_account_id = 123456789012\n"
                "[profile prod-admin]\nrole_arn = arn:aws:iam::123456789012:role/Admin\n"
                "source_profile = prod\n"
                "[sso-session corp]\nsso_start_url = https://corp.awsapps.com/start\n",
                encoding="utf-8",
            )
            err = self._mismatch({"AWS_CONFIG_FILE": str(cfg)})
        self.assertIn("AWS_PROFILE=prod aws ...", err)
        self.assertIn("aws sso login --profile prod", err)
        self.assertIn("profile: prod, prod-admin", err)
        self.assertNotIn("<profile>", err)
        # config の他の値 (sso_start_url 等) は文面に出さない
        self.assertNotIn("awsapps", err)

    def test_no_credentials_guidance_includes_configure_and_placeholder(self):
        fake = _fake_run(
            stdout="",
            stderr="Error loading SSO Token: Token for corp does not exist\n",
            returncode=255,
        )
        with mock.patch("subprocess.run", return_value=fake):
            err = aws.verify("123456789012", "/p", env=self._NO_CONFIG)
        self.assertIn("認証情報を取得できません (Error loading SSO Token", err)
        self.assertIn("AWS_PROFILE=<profile> aws ...", err)
        self.assertIn("aws sso login --profile <profile>", err)
        self.assertRegex(err, r"(?m)^\s+aws configure\s")
        self.assertNotRegex(err, r"(?m)^\s+export\s")

    def test_no_credentials_guidance_resolves_profile(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config"
            cfg.write_text("[profile prod]\nsso_account_id = 123456789012\n", encoding="utf-8")
            with mock.patch("subprocess.run", return_value=_fake_run(stdout="", stderr="", returncode=255)):
                err = aws.verify("123456789012", "/p", env={"AWS_CONFIG_FILE": str(cfg)})
        self.assertIn("aws sso login --profile prod", err)
        self.assertIn("AWS_PROFILE=prod aws ...", err)

    def test_home_lookup_failure_does_not_raise(self):
        """L2 P3: HOME 未設定 + `Path.home()` 不能 (RuntimeError) でも例外を漏らさない
        (漏れると hook が異常終了して無音 fail-open)。"""
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("services.aws.Path.home", side_effect=RuntimeError("no home")), \
             mock.patch("subprocess.run", return_value=_fake_run(stdout="111111111111\n")):
            err = aws.verify("123456789012", "/p")
        self.assertIn("不一致", err)
        self.assertIn("<profile>", err)


class TestAwsProfileScan(unittest.TestCase):
    """profiles_for_account: AWS config から期待 Account ID に対応する profile 名を引く。"""

    def _cfg(self, text: str) -> Path:
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        p = Path(d) / "config"
        p.write_text(text, encoding="utf-8")
        return p

    def test_sso_account_id_role_arn_and_default(self):
        cfg = self._cfg(
            "[default]\nsso_account_id = 123456789012\n"
            "[profile dev]\nsso_account_id = 111111111111\n"
            "[profile admin]\nrole_arn = arn:aws:iam::123456789012:role/Admin\n"
            "[profile cn]\nrole_arn = arn:aws-cn:iam::123456789012:role/X\n"
            "[profile other-role]\nrole_arn = arn:aws:iam::111111111111:role/X\n"
        )
        self.assertEqual(
            aws.profiles_for_account("123456789012", {"AWS_CONFIG_FILE": str(cfg)}),
            ["default", "admin", "cn"],
        )

    def test_nested_keys_and_non_profile_sections_ignored(self):
        cfg = self._cfg(
            "[profile dev]\ns3 =\n  sso_account_id = 123456789012\nregion = us-east-1\n"
            "[sso-session corp]\nsso_account_id = 123456789012\n"
            "[services s3x]\nsso_account_id = 123456789012\n"
        )
        self.assertEqual(
            aws.profiles_for_account("123456789012", {"AWS_CONFIG_FILE": str(cfg)}), []
        )

    def test_comments_spacing_and_duplicates(self):
        cfg = self._cfg(
            "# comment\n; other\n[profile prod]  \n"
            "SSO_ACCOUNT_ID =   123456789012   \n"
            "role_arn = arn:aws:iam::123456789012:role/Admin\n"
        )
        self.assertEqual(
            aws.profiles_for_account("123456789012", {"AWS_CONFIG_FILE": str(cfg)}), ["prod"]
        )

    def test_missing_or_undecodable_file(self):
        self.assertEqual(
            aws.profiles_for_account("123456789012", {"AWS_CONFIG_FILE": "/nonexistent/x"}), []
        )
        cfg = self._cfg("")
        cfg.write_bytes(b"\xff\xfe[profile x]\nsso_account_id = 123456789012\n")
        self.assertEqual(
            aws.profiles_for_account("123456789012", {"AWS_CONFIG_FILE": str(cfg)}), []
        )

    def test_env_none_uses_process_home(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (Path(d) / ".aws").mkdir()
        (Path(d) / ".aws" / "config").write_text(
            "[profile prod]\nsso_account_id = 123456789012\n", encoding="utf-8"
        )
        with mock.patch.dict(os.environ, {"HOME": d}):
            os.environ.pop("AWS_CONFIG_FILE", None)
            self.assertEqual(aws.profiles_for_account("123456789012"), ["prod"])

    def test_env_without_home_returns_empty(self):
        self.assertEqual(aws.profiles_for_account("123456789012", {}), [])

    def test_empty_account_id_returns_empty(self):
        self.assertEqual(
            aws.profiles_for_account("", {"AWS_CONFIG_FILE": "/nonexistent"}), []
        )

    def test_aws_config_file_tilde_expands_with_env_home(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (Path(d) / "cfg").write_text(
            "[profile p]\nsso_account_id = 123456789012\n", encoding="utf-8"
        )
        self.assertEqual(
            aws.profiles_for_account(
                "123456789012", {"AWS_CONFIG_FILE": "~/cfg", "HOME": d}
            ),
            ["p"],
        )


class TestAuthCommandPatterns(unittest.TestCase):
    """各 service の READONLY (認証取得系) と STATE_CHANGING (切替 / ログイン系) の
    パターン表。表示系・write はどちらにも (READONLY の認証系には) 一致しない。"""

    @staticmethod
    def _matches(patterns, cmd: str) -> bool:
        return any(re.search(p, cmd) for p in patterns)

    @classmethod
    def _readonly(cls, svc, cmd: str) -> bool:
        """dispatcher と同じ: READONLY regex または service の is_readonly()。"""
        if cls._matches(svc.READONLY, cmd):
            return True
        fn = getattr(svc, "is_readonly", None)
        return bool(fn and fn(cmd))

    def test_state_changing_positives(self):
        rows = [
            (github, "gh auth switch --user x"),
            (github, "gh auth login"),
            (github, "gh auth logout"),
            # readonly から外した後も STATE_CHANGING には残る (Codex R4 P1)。
            # 外すと refresh 成功後に古い成功 cache が TTL 分残る。
            (github, "gh auth refresh"),
            (github, "gh auth refresh --scopes admin:org"),
            (gcloud, "gcloud config set project x"),
            (gcloud, "gcloud config unset project"),
            (gcloud, "gcloud config configurations activate w"),
            (gcloud, "gcloud config configurations create w"),
            (gcloud, "gcloud auth login"),
            (gcloud, "gcloud auth activate-service-account --key-file=k"),
            (gcloud, "gcloud auth revoke"),
            (gcloud, "gcloud auth application-default login"),
            (gcloud, "gcloud init"),
            # release track 形も同じ操作が同じ副作用で走る (Codex R5 P1-B の自己 sweep)。
            # 実在は SDK 生成物 data/cli/gcloud_completions.py の command tree で確認
            # (config/init は alpha・beta・preview、auth 系は alpha・beta)。
            (gcloud, "gcloud beta config set project x"),
            (gcloud, "gcloud alpha config set project x"),
            (gcloud, "gcloud preview config set project x"),
            (gcloud, "gcloud beta config unset project"),
            (gcloud, "gcloud alpha config configurations activate w"),
            (gcloud, "gcloud beta config configurations create w"),
            (gcloud, "gcloud beta auth login"),
            (gcloud, "gcloud alpha auth login"),
            (gcloud, "gcloud beta auth revoke"),
            (gcloud, "gcloud beta auth activate-service-account --key-file=k"),
            (gcloud, "gcloud alpha auth application-default login"),
            (gcloud, "gcloud beta init"),
            (kubectl, "gcloud beta container clusters get-credentials c --region r"),
            (kubectl, "gcloud alpha container clusters get-credentials c --region r"),
            (firebase, "firebase use prod"),
            (firebase, "firebase use --clear"),
            (firebase, "firebase use --add"),
            (firebase, "firebase login"),
            (firebase, "firebase login:use a@b.c"),
            (firebase, "firebase logout"),
            (kubectl, "kubectl config use-context x"),
            (kubectl, "kubectl config set-context --current --namespace=n"),
            (kubectl, "kubectl config set-credentials u --token=t"),
            (kubectl, "kubectl config unset current-context"),
            (kubectl, "kubectl config delete-context x"),
            (kubectl, "kubectl config rename-context a b"),
            (kubectl, "kubectl ctx other"),
            (kubectl, "kubectx other"),
            (kubectl, "gcloud container clusters get-credentials c --region r"),
            (kubectl, "aws eks update-kubeconfig --name c"),
            (kubectl, "az aks get-credentials -g rg -n c"),
            (firebase, "firebase-tools use prod"),
            (firebase, "firebase-tools login"),
            (aws, "aws sso login --profile p"),
            (aws, "aws sso logout"),
            (aws, "aws login"),
            (aws, "aws logout"),
            (aws, "aws configure"),
            (aws, "aws configure sso"),
            (aws, "aws configure set region us-east-1"),
            (aws, "aws configure import --csv file://c.csv"),
        ]
        for svc, cmd in rows:
            with self.subTest(cmd=cmd):
                self.assertTrue(self._matches(svc.STATE_CHANGING, cmd))

    def test_state_changing_negatives(self):
        rows = [
            (github, "gh auth status"),
            (github, "gh auth setup-git"),
            (github, "gh pr create"),
            (gcloud, "gcloud config get-value project"),
            (gcloud, "gcloud config list"),
            (gcloud, "gcloud auth list"),
            (gcloud, "gcloud auth print-access-token"),
            (gcloud, "gcloud auth application-default print-access-token"),
            (gcloud, "gcloud run deploy"),
            (firebase, "firebase use"),
            (firebase, "firebase-tools use"),
            (firebase, "firebase deploy"),
            (firebase, "firebase projects:list"),
            (kubectl, "kubectl config current-context"),
            (kubectl, "kubectl config view"),
            (kubectl, "kubectl config get-contexts"),
            (kubectl, "kubectl apply -f x"),
            (kubectl, "gcloud container clusters list"),
            (kubectl, "aws eks list-clusters"),
            (aws, "aws sts get-caller-identity"),
            (aws, "aws s3 cp a b"),
            (aws, "aws sso-admin list-instances"),
            (aws, "aws sso list-accounts"),
            (aws, "aws configure list"),
            (aws, "aws configure list-profiles"),
            (aws, "aws configure get region"),
            (aws, "aws configure export-credentials --profile p"),
        ]
        for svc, cmd in rows:
            with self.subTest(cmd=cmd):
                self.assertFalse(self._matches(svc.STATE_CHANGING, cmd))

    def test_readonly_auth_commands(self):
        rows = [
            (aws, "aws sso login --profile p", True),
            (aws, "aws sso logout", True),
            (aws, "aws login", True),
            (aws, "aws configure", True),
            (aws, "aws configure sso", True),
            (aws, "aws configure list-profiles", True),
            (aws, "aws configure export-credentials --profile p", False),
            (aws, "aws sso list-accounts --access-token t", False),
            (aws, "aws sso-admin create-permission-set --name x", False),
            (aws, "aws s3 cp a b", False),
            (github, "gh auth login --with-token", True),
            (github, "gh auth login --skip-ssh-key", True),
            (github, "gh auth login --hostname ghe.example.com --skip-ssh-key", True),
            (github, "gh auth login --git-protocol https", True),
            (github, "gh auth login --git-protocol=https", True),
            (github, "gh auth login -p https", True),
            (github, "gh auth login -p=https", True),
            # SSH 鍵アップロードが起きうる形は readonly ではない (Codex R2 P1-1)
            (github, "gh auth login", False),
            (github, "gh auth login --web", False),
            (github, "gh auth login --hostname ghe.example.com", False),
            (github, "gh auth login --git-protocol ssh", False),
            (github, "gh auth login -p ssh", False),
            (github, "gh auth login --skip-ssh-keys", False),
            # 明示 false は無効 (Codex R3 P1: 実効 boolean を解釈する)
            (github, "gh auth login --git-protocol ssh --skip-ssh-key=false", False),
            (github, "gh auth login --with-token=false", False),
            (github, "gh auth login --skip-ssh-key=true", True),
            # scope 要求はアカウント側の OAuth grant を拡張する (Codex R4 P1 の
            # 論拠を login にも適用)。鍵操作抑止 flag が付いていても readonly でない
            (github, "gh auth login --skip-ssh-key --scopes admin:org", False),
            (github, "gh auth login -s admin:org --skip-ssh-key", False),
            (github, "gh auth logout", True),
            (github, "gh auth setup-git", True),
            # `gh auth refresh` は保存済み認証情報の権限をアカウント側で拡張・修正
            # しうる (`--scopes admin:org`) ため readonly にしない (Codex R4 P1)
            (github, "gh auth refresh", False),
            (github, "gh auth refresh -s repo", False),
            (github, "gh auth refresh --scopes admin:org", False),
            (github, "gh auth refresh --remove-scopes repo", False),
            (github, "gh auth switch --user x", False),
            (github, "gh auth token", False),
            (github, "gh repo create x", False),
            (gcloud, "gcloud auth login", True),
            (gcloud, "gcloud auth application-default login", True),
            (gcloud, "gcloud auth activate-service-account --key-file=k", True),
            (gcloud, "gcloud auth revoke", True),
            (gcloud, "gcloud auth application-default set-quota-project p", True),
            (gcloud, "gcloud auth application-default print-access-token", False),
            (gcloud, "gcloud auth print-access-token", False),
            (gcloud, "gcloud config set project x", False),
            # release track 形も GA 形と同じ扱いに揃える (Codex R5 P1-B の自己 sweep)
            (gcloud, "gcloud beta auth login", True),
            (gcloud, "gcloud alpha auth login", True),
            (gcloud, "gcloud beta auth application-default login", True),
            (gcloud, "gcloud beta auth activate-service-account --key-file=k", True),
            (gcloud, "gcloud alpha auth revoke", True),
            (gcloud, "gcloud beta auth list", True),
            (gcloud, "gcloud beta config get-value project", True),
            # **資格情報を出力するコマンドは track 形でも検証対象のまま**
            # (CHANGELOG 0.8.0 の carve-out を track 追加で広げていないことの固定)
            (gcloud, "gcloud beta auth print-access-token", False),
            (gcloud, "gcloud alpha auth print-access-token", False),
            (gcloud, "gcloud beta auth application-default print-access-token", False),
            (gcloud, "gcloud beta config set project x", False),
            # `init` の \b が別コマンドを巻き込まない
            (gcloud, "gcloud beta interactive", False),
            (firebase, "firebase login", True),
            # **regex の層**では login:ci も READONLY に当たる。tier 判定では
            # DISCLOSING が取り消して検証対象になる (tests/test_tiers.py が固定)。
            # この行は「READONLY regex の形」を見るテストなので True のまま。
            (firebase, "firebase login:ci", True),
            (firebase, "firebase logout", True),
            (firebase, "firebase-tools login", True),
            (firebase, "firebase-tools use", True),
            (firebase, "firebase use prod", False),
            (firebase, "firebase-tools use prod", False),
        ]
        for svc, cmd, expected in rows:
            with self.subTest(cmd=cmd):
                self.assertEqual(self._readonly(svc, cmd), expected)


class TestGithubLoginKeyless(unittest.TestCase):
    """github.is_readonly: `gh auth login` がリモートに何も書かない形か。

    (a) SSH 鍵のアップロードを伴わないか (flag 文字列の有無ではなく実効 boolean を
    解釈する。Codex R3 P1)、かつ (b) `-s` / `--scopes` で OAuth grant scope を
    要求していないか (Codex R4 P1 の論拠を login にも適用)。
    """

    def test_scopes_disqualifies_even_with_keyless_flags(self):
        """`--scopes` / `-s` は値を取る flag なので `=false` で無効化できない。
        鍵操作を抑止する flag が付いていても readonly にはしない
        (`gh auth login --skip-ssh-key --scopes admin:org` はアカウント側の
        OAuth grant を拡張する)。"""
        for cmd in (
            "gh auth login --skip-ssh-key --scopes admin:org",
            "gh auth login --scopes admin:org --skip-ssh-key",
            "gh auth login --scopes=admin:org --skip-ssh-key",
            "gh auth login -s admin:org --skip-ssh-key",
            "gh auth login -s=admin:org --with-token",
            "gh auth login -sadmin:org --with-token",
            "gh auth login -p https -s admin:org",
            "gh auth login --git-protocol https --scopes repo,admin:org",
            "gh auth login --with-token -s repo",
            # 値 token を消費するので、後続を flag と誤解して readonly に転ばない
            "gh auth login -s --skip-ssh-key",
            "gh auth login -s repo --git-protocol https",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(github.is_readonly(cmd))

    def test_scopes_after_double_dash_is_not_a_flag(self):
        """`--` 以降は引数なので scope 要求とはみなさない (既存の `--` 規則に整合)。"""
        self.assertTrue(github.is_readonly("gh auth login --skip-ssh-key -- --scopes x"))

    def test_effective_true_forms_are_keyless(self):
        for cmd in (
            "gh auth login --skip-ssh-key",
            "gh auth login --skip-ssh-key=true",
            "gh auth login --skip-ssh-key=TRUE",
            "gh auth login --skip-ssh-key=1",
            "gh auth login --skip-ssh-key=yes",
            "gh auth login --with-token",
            "gh auth login --with-token=true < token.txt",
            "gh auth login --with-token=1",
            "gh auth login --git-protocol https",
            "gh auth login --git-protocol=https",
            "gh auth login --git-protocol=HTTPS",
            "gh auth login -p https",
            "gh auth login -p=https",
            "gh auth login -phttps",
            "gh auth login --git-protocol ssh --skip-ssh-key",
            "gh auth login --git-protocol ssh --with-token=yes",
            "gh auth login --git-protocol ssh --git-protocol https",  # 後勝ち
            "gh auth login --hostname ghe.example.com --web --git-protocol https",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(github.is_readonly(cmd))

    def test_explicit_false_and_ssh_forms_are_not_keyless(self):
        for cmd in (
            "gh auth login",
            "gh auth login --web",
            "gh auth login --git-protocol ssh",
            "gh auth login --git-protocol ssh --skip-ssh-key=false",
            "gh auth login --skip-ssh-key=0",
            "gh auth login --skip-ssh-key=no",
            "gh auth login --skip-ssh-key=NO",
            "gh auth login --skip-ssh-key=maybe",  # gh がエラーにする値は保守的に無効
            "gh auth login --with-token=false",
            "gh auth login --with-token=0 < token.txt",
            "gh auth login -p ssh",
            "gh auth login -pssh",
            "gh auth login --git-protocol https --git-protocol ssh",  # 後勝ち
            "gh auth login --git-protocol",  # 値なし
            "gh auth login -- --skip-ssh-key",  # `--` 以降は flag ではない
            "gh auth login --skip-ssh-keys",
            "gh auth status",
            "gh pr create",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(github.is_readonly(cmd))

    def test_unbalanced_quote_falls_back_to_split(self):
        self.assertTrue(github.is_readonly('gh auth login --skip-ssh-key "x'))
        self.assertFalse(github.is_readonly('gh auth login --skip-ssh-key=false "x'))


class TestGithubApiIsQuery(unittest.TestCase):
    """github.is_query: `gh api` が読み取りだけと**証明できる**形か。

    QUERY は判定表を緩める唯一の方向なので、危険な option を列挙する denylist では
    なく **安全な option の allow-list** で証明する。denylist だと gh に option が
    増えるたび黙って穴が開く。
    """

    def test_read_only_forms(self):
        for cmd in (
            "gh api repos/o/r",
            "gh api -X GET repos/o/r",
            "gh api --method GET repos/o/r",
            "gh api --method=head repos/o/r",
            "gh api repos/o/r --jq .name",
            "gh api repos/o/r -q .name",
            "gh api repos/o/r -t {{.name}}",
            "gh api repos/o/r --paginate --silent --include",
            "gh api repos/o/r -H Accept:application/vnd.github+json",
            "gh api --hostname ghe.example.com repos/o/r",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(github.is_query(cmd))

    def test_body_options_are_write(self):
        for cmd in (
            "gh api repos/o/r -f title=hi",
            "gh api repos/o/r -F body=@b.txt",
            "gh api repos/o/r --field a=b",
            "gh api repos/o/r --raw-field a=b",
            "gh api repos/o/r --input body.json",
            "gh api graphql -f query=@q.graphql",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(github.is_query(cmd))

    def test_non_read_methods_are_write(self):
        for method in ("POST", "post", "PUT", "PATCH", "DELETE"):
            for cmd in (f"gh api -X {method} repos/o/r", f"gh api --method={method} r"):
                with self.subTest(cmd=cmd):
                    self.assertFalse(github.is_query(cmd))

    def test_unknown_or_ambiguous_options_are_not_proven(self):
        for cmd in (
            # 未知 option は将来の write option かもしれない
            "gh api repos/o/r --brand-new-flag",
            # 短縮の連結形は値と flag の切り分けが曖昧
            "gh api -Xpost repos/o/r",
            "gh api -iq repos/o/r",
            # 値が欠けた option
            "gh api repos/o/r -X",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(github.is_query(cmd))

    def test_body_options_are_not_in_the_safe_allow_list(self):
        """2 層 (allow-list と body option の除外) が矛盾しないことの機械チェック。

        未知 option を弾く allow-list があるので body option の明示除外は**二重の
        防御**で、外しても現在の判定は変わらない (mutation では検出されない)。
        意味を持つのは「うっかり `-F` を safe 側に足した」ときだけなので、
        その矛盾をここで禁じておく。
        """
        safe = github._API_SAFE_OPTIONS_WITH_VALUE | github._API_SAFE_FLAGS
        self.assertEqual(safe & github._API_BODY_OPTIONS, frozenset())

    def test_other_commands_are_not_api(self):
        for cmd in ("gh pr create", "gh apixyz repos/o/r", "gh auth status"):
            with self.subTest(cmd=cmd):
                self.assertFalse(github.is_query(cmd))

    def test_unbalanced_quote_is_not_proven(self):
        self.assertFalse(github.is_query('gh api repos/o/r --jq "x'))


class TestGcloud(unittest.TestCase):
    def test_string_match(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-proj\n")):
            self.assertIsNone(gcloud.verify("my-proj", "/p"))

    def test_string_mismatch(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="other\n")):
            err = gcloud.verify("my-proj", "/p")
        self.assertIsNotNone(err)
        self.assertIn("不一致", err)

    def test_unset(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="(unset)\n")):
            err = gcloud.verify("my-proj", "/p")
        self.assertIsNotNone(err)
        self.assertIn("設定されていません", err)

    def test_cli_not_found(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            err = gcloud.verify("my-proj", "/p")
        self.assertIsNotNone(err)
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
        self.assertIsNotNone(err)
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
        self.assertIsNotNone(err)
        self.assertIn("アカウント不一致", err)

    def test_dict_empty_object(self):
        err = gcloud.verify({}, "/p")
        self.assertIsNotNone(err)
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


class TestGithubChangesIdentity(unittest.TestCase):
    """`gh auth refresh` は scope を足すだけでアクティブアカウントを変えないので、
    連結規則の切替側に数えない。ただし `-u/--user` 付きは切替なので数える
    (subcommand 単位で inert を宣言すると穴になる)。"""

    def test_refresh_without_user_is_inert(self):
        for candidate in (
            "gh auth refresh",
            "gh auth refresh -s project",
            "gh auth refresh --scopes repo,project",
            "gh auth refresh --hostname ghe.example.com",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(github.changes_identity(candidate, "Mao-o"))

    def test_refresh_with_user_is_an_identity_change(self):
        for candidate in (
            "gh auth refresh -u other",
            "gh auth refresh --user other",
            "gh auth refresh --user=other",
            "gh auth refresh -s project --user other",
            # 期待値への切替でも「identity を変える形」ではある
            # (期待値かどうかは is_self_remediation が別に判定する)。
            "gh auth refresh --user Mao-o",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(github.changes_identity(candidate, "Mao-o"))

    def test_switch_login_logout_are_identity_changes(self):
        for candidate in (
            "gh auth switch --user other",
            "gh auth switch",
            "gh auth login --skip-ssh-key",
            "gh auth logout --hostname github.com",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(github.changes_identity(candidate, "Mao-o"))


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

    def test_decoration_flags_are_accepted(self):
        """装飾 option (着地先を変えない) は剥がしてから照合する。案内形に
        `--quiet` を足しただけで通常検証へ落ちると、連結規則の下では誤 deny に
        直結する (v0.15.0 でこの述語が deny を抑止する唯一の出口になった)。"""
        for candidate in (
            "gcloud config set project my-proj --quiet",
            "gcloud config set project my-proj -q",
            "gcloud config set project my-proj --verbosity=debug",
            "gcloud config set project my-proj --verbosity debug",
            "gcloud config set project my-proj --format=json --quiet",
            "gcloud config set project my-proj --no-user-output-enabled",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(gcloud.is_self_remediation(candidate, "my-proj"))

    def test_options_that_move_the_landing_spot_fall_through(self):
        """着地先を変える option は allow-list に入れない (blanket 許容にしない)。
        `--configuration other` は非アクティブな configuration に書くので、
        実行後もアクティブ project は期待値にならない。"""
        for candidate in (
            "gcloud config set project my-proj --configuration other",
            "gcloud config set project my-proj --flags-file f.yaml",
            "gcloud config set project my-proj --account other@example.com",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(gcloud.is_self_remediation(candidate, "my-proj"))

    def test_unknown_option_falls_through(self):
        self.assertFalse(gcloud.is_self_remediation(
            "gcloud config set project my-proj --no-such-flag", "my-proj"))

    def test_release_track_form_is_accepted(self):
        self.assertTrue(gcloud.is_self_remediation(
            "gcloud beta config set project my-proj", "my-proj"))

    def test_other_gcloud_command(self):
        self.assertFalse(gcloud.is_self_remediation(
            "gcloud run deploy", "my-proj"))


class TestGcloudChangesIdentity(unittest.TestCase):
    """`gcloud config set <key>` のうち identity を変えない key は連結規則の
    切替側に数えない (数えると `gcloud config set compute/region x &&
    gcloud run deploy` のような日常形に deny が生える)。"""

    def test_inert_keys(self):
        for candidate in (
            "gcloud config set compute/region us-central1",
            "gcloud config set compute/zone us-central1-a",
            "gcloud config set disable_prompts true",
            "gcloud config set core/disable_usage_reporting true",
            "gcloud config set core/verbosity debug",
            "gcloud config set run/region asia-northeast1",
            "gcloud beta config set compute/region us-central1",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(gcloud.changes_identity(candidate, "my-proj"))

    def test_identity_keys(self):
        for candidate in (
            "gcloud config set project other",
            "gcloud config set core/project other",
            "gcloud config set account other@example.com",
            "gcloud config set core/account other@example.com",
            "gcloud config set auth/impersonate_service_account sa@example.com",
            # `container/cluster` は get-credentials の既定を変えて kubectl 側の
            # identity に波及しうるので inert にしない。
            "gcloud config set container/cluster other",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(gcloud.changes_identity(candidate, "my-proj"))

    def test_inert_key_with_undeclared_option_is_not_inert(self):
        """key が inert でも宣言外の option が付いていたら inert と言い切れない。"""
        self.assertTrue(gcloud.changes_identity(
            "gcloud config set compute/region x --configuration other", "my-proj"))

    def test_inert_key_with_decoration_option_stays_inert(self):
        self.assertFalse(gcloud.changes_identity(
            "gcloud config set compute/region x --quiet", "my-proj"))

    def test_non_config_set_state_changes_are_identity_changes(self):
        for candidate in (
            "gcloud config unset project",
            "gcloud config configurations activate other",
            "gcloud auth login",
            "gcloud auth activate-service-account --key-file=sa.json",
            "gcloud init",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(gcloud.changes_identity(candidate, "my-proj"))


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

    def test_decoration_flags_are_accepted(self):
        """装飾 option (対話 / 出力の制御) は剥がしてから照合する。"""
        for candidate in (
            "firebase use my-proj --non-interactive",
            "firebase use my-proj --debug",
            "firebase use my-proj --json --non-interactive",
            "firebase-tools use my-proj --non-interactive",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(firebase.is_self_remediation(candidate, "my-proj"))

    def test_options_that_move_the_landing_spot_fall_through(self):
        """`--project` / `-P` は照合先を差し替え、`--config` は project の解決先
        (firebase.json) を変えるので allow-list に入れない。"""
        for candidate in (
            "firebase use my-proj --project other",
            "firebase use my-proj -P other",
            "firebase use my-proj --config other/firebase.json",
            "firebase use my-proj --token t",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(firebase.is_self_remediation(candidate, "my-proj"))

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

    def test_decoration_options_are_accepted(self):
        """verbosity / timeout は着地先 (既定 kubeconfig の current-context) を
        変えないので剥がしてから照合する。"""
        for candidate in (
            "kubectl config use-context staging --v=4",
            "kubectl config use-context staging --v 4",
            "kubectl config use-context staging -v 4",
            "kubectl config use-context staging --request-timeout=5s",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(kubectl.is_self_remediation(candidate, "staging"))

    def test_options_that_move_the_landing_spot_fall_through(self):
        """`--kubeconfig` は別ファイルへ書き、`--context` は照合先を差し替えるので
        allow-list に入れない (blanket 許容にしない)。"""
        for candidate in (
            "kubectl config use-context staging --kubeconfig=/tmp/kc",
            "kubectl config use-context staging --kubeconfig /tmp/kc",
            "kubectl config use-context staging --context other",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(kubectl.is_self_remediation(candidate, "staging"))


class TestKubectlChangesIdentity(unittest.TestCase):
    """`kubectl config set-context --current` (位置引数なし) だけを inert とする。
    kubectl の期待値は current-context 名なので、この形は identity を変えない。"""

    def test_set_context_current_is_inert(self):
        for candidate in (
            "kubectl config set-context --current",
            "kubectl config set-context --current --namespace=foo",
            "kubectl config set-context --current --namespace foo",
            "kubectl config set-context --current -n foo",
            "kubectl config set-context --namespace=foo --current",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(kubectl.changes_identity(candidate, "staging"))

    def test_named_context_is_an_identity_change(self):
        """位置引数で context 名を書く形は別 context の定義を書き換える。

        `--current` と名前の併記は kubectl 側でエラーになる形だが、**位置引数が
        残っていれば inert と主張しない**ことを固定しておく (`--current` の有無
        だけで判定すると、名前付きの形が inert 側へ落ちる)。
        """
        for candidate in (
            "kubectl config set-context other --namespace=foo",
            "kubectl config set-context other",
            "kubectl config set-context other --current --namespace=foo",
            "kubectl config set-context --current other",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(kubectl.changes_identity(candidate, "staging"))

    def test_credential_rebinding_is_an_identity_change(self):
        """context 名を変えないまま identity が差し替わる形は inert にしない。"""
        for candidate in (
            "kubectl config set-credentials u --token=t",
            "kubectl config set-cluster c --server=https://example",
            "kubectl config set-context --current --user=other",
            "kubectl config set-context --current --cluster=other",
            "kubectl config set-context --current --kubeconfig=/tmp/kc",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(kubectl.changes_identity(candidate, "staging"))

    def test_switches_and_cross_cli_forms_are_identity_changes(self):
        for candidate in (
            "kubectl config use-context other",
            "kubectl config delete-context other",
            "kubectx other",
            "gcloud container clusters get-credentials c --region r",
            "aws eks update-kubeconfig --name c",
        ):
            with self.subTest(candidate=candidate):
                self.assertTrue(kubectl.changes_identity(candidate, "staging"))


class TestAwsHasNoSelfRemediation(unittest.TestCase):
    def test_aws_module_does_not_define_hook(self):
        # AWS は期待値 (Account ID) と切替手段 (profile / SSO) の照合が hook から
        # 不能のため意図的に未実装。dispatcher は getattr fallback で通常検証に落とす
        self.assertFalse(hasattr(aws, "is_self_remediation"))


class TestCliExecErrors(unittest.TestCase):
    """CLI が PATH にあるが実行できない (PermissionError 等 FileNotFoundError 以外の
    OSError) とき、各 service が例外を漏らさず deny 文字列を返す。例外が漏れると
    hook が異常終了し、PreToolUse は無音 fail-open になる。"""

    _ERR = PermissionError(13, "Permission denied", "cli")

    def test_aws(self):
        with mock.patch("subprocess.run", side_effect=self._ERR):
            err = aws.verify("123456789012", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("実行できません", err)

    def test_gcloud(self):
        with mock.patch("subprocess.run", side_effect=self._ERR):
            err = gcloud.verify("my-proj", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("実行できません", err)

    def test_github(self):
        with mock.patch("subprocess.run", side_effect=self._ERR):
            err = github.verify("Mao-o", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("実行できません", err)

    def test_kubectl(self):
        with mock.patch("subprocess.run", side_effect=self._ERR):
            err = kubectl.verify("prod-ctx", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("実行できません", err)


class TestTimeoutErrors(unittest.TestCase):
    """内部バックログ: `verify()` レベルの `subprocess.TimeoutExpired` テストの
    空白 (github / firebase には既存、aws / gcloud / kubectl には無かった) を埋める。
    timeout は fail-closed (deny) で専用メッセージを返すべき。"""

    _TIMEOUT = subprocess.TimeoutExpired(cmd=[], timeout=10)

    def test_aws(self):
        with mock.patch("subprocess.run", side_effect=self._TIMEOUT):
            err = aws.verify("123456789012", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("タイムアウト", err)

    def test_gcloud(self):
        with mock.patch("subprocess.run", side_effect=self._TIMEOUT):
            err = gcloud.verify("my-proj", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("タイムアウト", err)

    def test_kubectl(self):
        with mock.patch("subprocess.run", side_effect=self._TIMEOUT):
            err = kubectl.verify("prod-ctx", "/p")
        self.assertIsInstance(err, str)
        self.assertIn("タイムアウト", err)


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
        # CLI 優先なので .firebaserc の有無は無関係 (実在しない project_dir でよい)
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


class TestContextOptionOverride(unittest.TestCase):
    """コマンドが `--profile` / `--project` / `--context` で指定した対象を検証する。

    従来は「hook の既定コンテキスト」だけを見ていたため、
    `aws --profile other s3 rm` は既定が期待値なら allow されていた
    (検証は既定 / 実行は other = false-allow)。誤アカウント事故の典型経路。
    """

    def test_aws_passes_profile_to_verification_command(self):
        calls = []

        def rec(argv, **kwargs):
            calls.append(argv)
            return _fake_run(stdout="222222222222\n")

        with mock.patch("subprocess.run", side_effect=rec):
            err = aws.verify("111111111111", "/p", context={"profile": "other"})
        # 検証コマンドにも同じ --profile を付ける (CLI の資格情報解決順を実行時と揃える)。
        self.assertIn("--profile", calls[0])
        self.assertEqual(calls[0][calls[0].index("--profile") + 1], "other")
        self.assertIn("不一致", err)
        self.assertIn("--profile other", err)

    def test_aws_without_context_does_not_pass_profile(self):
        calls = []

        def rec(argv, **kwargs):
            calls.append(argv)
            return _fake_run(stdout="111111111111\n")

        with mock.patch("subprocess.run", side_effect=rec):
            self.assertIsNone(aws.verify("111111111111", "/p"))
        self.assertNotIn("--profile", calls[0])

    def test_gcloud_project_flag_is_compared_directly(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-proj\n")) as m:
            err = gcloud.verify("my-proj", "/p", context={"project": "other"})
        self.assertIn("不一致", err)
        self.assertIn("--project=other", err)
        # flag で決まるので現在値の問い合わせは不要。
        self.assertEqual(m.call_count, 0)

    def test_gcloud_project_flag_matching_expected_is_allowed(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="other\n")):
            self.assertIsNone(gcloud.verify("my-proj", "/p", context={"project": "my-proj"}))

    def test_gcloud_project_flag_does_not_skip_account_check(self):
        """project を flag で上書きしても account は従来どおり照合する。

        キー単位で上書きせず早期 return すると、`gcloud --project ok run deploy` が
        account の不一致を見逃す新たな false-allow になる。
        """
        expected = {"project": "my-proj", "account": "me@example.com"}
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="other@example.com\n")) as m:
            err = gcloud.verify(expected, "/p", context={"project": "my-proj"})
        self.assertIsNotNone(err)
        self.assertIn("アカウント不一致", err)
        # account の現在値だけを問い合わせている。
        self.assertEqual(m.call_count, 1)
        self.assertIn("account", m.call_args.args[0])

    def test_gcloud_configuration_flag_is_forwarded(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-proj\n")) as m:
            self.assertIsNone(
                gcloud.verify("my-proj", "/p", context={"configuration": "alt"})
            )
        argv = m.call_args.args[0]
        self.assertIn("--configuration", argv)
        self.assertEqual(argv[argv.index("--configuration") + 1], "alt")

    def test_kubectl_context_flag_is_compared_directly(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="prod-ctx\n")) as m:
            err = kubectl.verify("prod-ctx", "/p", context={"context": "other"})
        self.assertIn("不一致", err)
        self.assertIn("--context=other", err)
        self.assertEqual(m.call_count, 0)

    def test_kubectl_context_flag_matching_expected_is_allowed(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="other\n")):
            self.assertIsNone(
                kubectl.verify("prod-ctx", "/p", context={"context": "prod-ctx"})
            )

    def test_kubectl_kubeconfig_flag_is_forwarded(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="prod-ctx\n")) as m:
            self.assertIsNone(
                kubectl.verify("prod-ctx", "/p", context={"kubeconfig": "/tmp/kc"})
            )
        argv = m.call_args.args[0]
        self.assertIn("--kubeconfig", argv)
        self.assertEqual(argv[argv.index("--kubeconfig") + 1], "/tmp/kc")

    def test_firebase_project_flag_is_compared_directly(self):
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="my-fb\n")) as m:
            err = firebase.verify("my-fb", "/p", context={"project": "other"})
        self.assertIn("不一致", err)
        self.assertIn("--project other", err)
        self.assertEqual(m.call_count, 0)

    def test_firebase_project_flag_resolves_firebaserc_alias(self):
        """firebase-tools と同じく alias → project ID に解決してから照合する。"""
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".firebaserc").write_text(
                json.dumps({"projects": {"prod": "proj-prod", "default": "proj-dev"}}),
                encoding="utf-8",
            )
            with mock.patch("subprocess.run", return_value=_fake_run(stdout="proj-dev\n")):
                self.assertIsNone(
                    firebase.verify("proj-prod", d, context={"project": "prod"})
                )
                err = firebase.verify("proj-dev", d, context={"project": "prod"})
        self.assertIn("不一致", err)
        self.assertIn("proj-prod", err)

    def test_firebase_dict_expected_with_project_flag(self):
        expected = {"default": "proj-dev", "prod": "proj-prod"}
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="proj-dev\n")):
            self.assertIsNone(
                firebase.verify(expected, "/p", context={"project": "proj-prod"})
            )
            err = firebase.verify(expected, "/p", context={"project": "unknown-proj"})
        self.assertIn("不一致", err)

    def test_flag_mismatch_guidance_targets_the_flag_not_the_active_context(self):
        """`--project` 不一致の案内は flag を直す形にする。

        アクティブ project を切り替える `firebase use <alias>` を案内しても、その
        コマンドは書かれた `--project` の値で実行されるので同じ deny を繰り返す
        (案内どおりに直しても通らない)。kubectl / gcloud の文面と方針を揃える。
        """
        expected = {"default": "proj-dev", "prod": "proj-prod"}
        with mock.patch("subprocess.run", return_value=_fake_run(stdout="proj-dev\n")):
            err = firebase.verify(expected, "/p", context={"project": "unknown-proj"})
        self.assertNotIn("firebase use", err)
        self.assertIn("--project prod", err)
        # str 期待値・kubectl・gcloud も同じ「flag を直す」形になっている。
        for other in (
            firebase.verify("proj-dev", "/p", context={"project": "other"}),
            kubectl.verify("prod-ctx", "/p", context={"context": "other"}),
            gcloud.verify("my-proj", "/p", context={"project": "other"}),
        ):
            self.assertIn("外すか", other)

    def test_github_ignores_context(self):
        # gh の --hostname / --user は操作対象の指定で、照合先は常にアクティブ
        # アカウント (README 既知の制限)。context は受け取るが使わない。
        with mock.patch("subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)):
            self.assertIsNone(
                github.verify("Mao-o", "/p", context={"hostname": "ghe.example.com"})
            )


class TestServiceContextContract(unittest.TestCase):
    """全 service が同じ verify シグネチャと CONTEXT_OPTIONS 規約を守る。"""

    SERVICES = (aws, firebase, gcloud, github, kubectl)

    def test_verify_accepts_context_keyword(self):
        import inspect

        for svc in self.SERVICES:
            with self.subTest(svc=svc.__name__):
                params = inspect.signature(svc.verify).parameters
                self.assertIn("context", params)
                self.assertIs(params["context"].default, None)

    def test_declared_context_options_are_global_or_known(self):
        """CONTEXT_OPTIONS のキーは option 名 (`-` 始まり) で、値は論理名。"""
        for svc in self.SERVICES:
            options = getattr(svc, "CONTEXT_OPTIONS", {})
            with self.subTest(svc=svc.__name__):
                for name, key in options.items():
                    self.assertTrue(name.startswith("-"), name)
                    self.assertTrue(key and not key.startswith("-"), key)

    def test_all_services_declare_accepts_dict(self):
        """builder (scripts/accounts_builder.py) の書込前スキーマ検証が読む
        契約: 全 service が ACCEPTS_DICT を明示宣言する (getattr の暗黙
        デフォルトに頼らない = 新 service 追加時の宣言漏れを検出できる)。"""
        for svc in self.SERVICES:
            with self.subTest(svc=svc.__name__):
                self.assertIn("ACCEPTS_DICT", vars(svc))
                self.assertIsInstance(svc.ACCEPTS_DICT, bool)

    def test_dict_allowed_keys_only_declared_when_accepts_dict(self):
        """DICT_ALLOWED_KEYS は ACCEPTS_DICT=True の service だけが宣言してよい
        (aws/kubectl は scalar 専用なのでキー制限自体が無意味)。宣言する場合は
        frozenset[str] (builder 側は None を「キー制限なし」と解釈する)。"""
        for svc in self.SERVICES:
            with self.subTest(svc=svc.__name__):
                allowed_keys = getattr(svc, "DICT_ALLOWED_KEYS", None)
                if allowed_keys is None:
                    continue
                self.assertTrue(svc.ACCEPTS_DICT)
                self.assertIsInstance(allowed_keys, frozenset)
                self.assertTrue(all(isinstance(k, str) for k in allowed_keys))

    def test_dict_services_declare_dict_value_check(self):
        """ACCEPTS_DICT=True の service は DICT_VALUE_CHECK を明示宣言する
        (builder の緩和モードが「verify() が形で deny する値」を素通ししない
        ための契約。既定値に頼ると新 service の宣言漏れを検出できない)。
        scalar 専用の service は dict 契約自体が無意味なので宣言しない。"""
        for svc in self.SERVICES:
            with self.subTest(svc=svc.__name__):
                if not svc.ACCEPTS_DICT:
                    self.assertNotIn("DICT_VALUE_CHECK", vars(svc))
                    continue
                self.assertIn("DICT_VALUE_CHECK", vars(svc))
                self.assertIn(svc.DICT_VALUE_CHECK, ("all", "truthy", "none"))

    def test_scalar_equivalent_dict_key_only_on_dict_services(self):
        """SCALAR_EQUIVALENT_DICT_KEY は「scalar 期待値と等価になる dict キー」。
        宣言は任意 (firebase のように verify() が dict キーを読まない service は
        宣言しない) だが、宣言する場合は ACCEPTS_DICT=True かつ、
        DICT_ALLOWED_KEYS を持つ service ではその許容キーの 1 つであること。"""
        for svc in self.SERVICES:
            with self.subTest(svc=svc.__name__):
                key = getattr(svc, "SCALAR_EQUIVALENT_DICT_KEY", None)
                if key is None:
                    continue
                self.assertTrue(svc.ACCEPTS_DICT)
                self.assertIsInstance(key, str)
                self.assertTrue(key.strip())
                allowed_keys = getattr(svc, "DICT_ALLOWED_KEYS", None)
                if allowed_keys is not None:
                    self.assertIn(key, allowed_keys)

    def test_github_does_not_declare_scalar_equivalent_dict_key(self):
        """github は SCALAR_EQUIVALENT_DICT_KEY を**宣言してはならない**
        (Codex R4 P1)。

        `github.verify()` の str 分岐は「github.com が active ならそれ、無ければ
        最初の active host」を照合する **実行時の状態に依存する**意味論なので、
        どの静的な hostname キーとも等価にならない。反例: ghe.example.com だけが
        active で値が USER のとき、scalar "USER" は allow だが
        `{"github.com": "USER"}` は「このホストにログインしていません」で deny。
        宣言すると builder の migrate が両者を非衝突扱いにして、明示された
        github.com の要求を無警告で捨てる。再宣言を防ぐための lock。"""
        self.assertNotIn("SCALAR_EQUIVALENT_DICT_KEY", vars(github))

    def test_gcloud_scalar_branch_only_checks_project(self):
        """gcloud が SCALAR_EQUIVALENT_DICT_KEY = "project" を宣言できる根拠を
        実測で固定する: scalar 期待値と `{"project": <同値>}` は verify() の
        呼び出しが一致する (どちらも project だけを照合し、account は見ない)。

        github と違い照合先が active 値の状態で変わらないため、静的なキー宣言が
        成立する。"""
        calls: list[str] = []

        def fake_get(key, env=None, configuration=None):
            calls.append(key)
            return "my-project" if key == "project" else "other@example.com", None

        with mock.patch.object(gcloud, "_get", side_effect=fake_get):
            self.assertIsNone(gcloud.verify("my-project", "/p"))
        self.assertEqual(calls, ["project"])

        calls.clear()
        with mock.patch.object(gcloud, "_get", side_effect=fake_get):
            self.assertIsNone(gcloud.verify({"project": "my-project"}, "/p"))
        self.assertEqual(calls, ["project"])


class TestChangesIdentityContract(unittest.TestCase):
    """`changes_identity` の契約 (v0.15.0)。

    連結規則の切替側を「identity を変える形」に絞るための任意宣言で、
    **`STATE_CHANGING` からは外さない** (成功キャッシュの破棄は inert な形でも
    必要。外すと `gcloud config set compute/region x` 後に古い成功が TTL 分残る)。
    宣言と cache 破棄が同時に成立していることをここで固定する。
    """

    SERVICES = (aws, firebase, gcloud, github, kubectl)

    # 各 service が inert (identity を変えない) と宣言する代表形。
    INERT_FORMS = {
        "github": ["gh auth refresh", "gh auth refresh -s project"],
        "gcloud": [
            "gcloud config set compute/region us-central1",
            "gcloud config set disable_prompts true",
        ],
        "kubectl": [
            "kubectl config set-context --current",
            "kubectl config set-context --current --namespace=foo",
        ],
    }

    def test_inert_forms_are_still_state_changing(self):
        """inert 宣言した形が `STATE_CHANGING` に残っていること (cache 破棄の維持)。"""
        for name, forms in self.INERT_FORMS.items():
            svc = {s.__name__.rsplit(".", 1)[-1]: s for s in self.SERVICES}[name]
            patterns = getattr(svc, "STATE_CHANGING", [])
            for form in forms:
                with self.subTest(service=name, form=form):
                    self.assertTrue(
                        any(re.search(p, form) for p in patterns),
                        f"{form} が STATE_CHANGING から外れている (cache が破棄されない)",
                    )
                    self.assertFalse(svc.changes_identity(form, "x"))

    def test_inert_table_covers_every_declaring_service(self):
        """空振り防止: `changes_identity` を宣言した service は上の表に載っている
        こと (新しい宣言が無検査で入るのを防ぐ)。"""
        declaring = {
            svc.__name__.rsplit(".", 1)[-1]
            for svc in self.SERVICES
            if "changes_identity" in vars(svc)
        }
        self.assertEqual(declaring, set(self.INERT_FORMS))

    def test_undeclared_services_default_to_identity_changing(self):
        """未宣言の service (aws / firebase) は dispatcher 側の既定 True に落ちる
        = 従来どおり `STATE_CHANGING` 全体が切替側に数えられる。"""
        for svc in (aws, firebase):
            with self.subTest(svc=svc.__name__):
                self.assertNotIn("changes_identity", vars(svc))

    def test_declared_predicates_accept_any_expected_shape(self):
        """任意入力 (accounts.local.json の生値) を受けるので例外を投げないこと。"""
        for name in self.INERT_FORMS:
            svc = {s.__name__.rsplit(".", 1)[-1]: s for s in self.SERVICES}[name]
            for expected in ("x", {"a": "b"}, None, 5, [], {}):
                with self.subTest(service=name, expected=expected):
                    self.assertIsInstance(
                        svc.changes_identity("some command", expected), bool
                    )


class TestFirebaseCliNameForms(unittest.TestCase):
    """npm 経由の正当な CLI 名の形を全判定で受け付ける。

    `npx firebase-tools@13.31.0 deploy` は CI / 再現手順で頻出する。
    PATTERNS だけ通して READONLY / STATE_CHANGING が通らないと、`login` が切替として
    認識されず古い成功 cache が残る (v0.9.0 開発中に作り込んだ退行の回帰テスト)。
    """

    FORMS = ("firebase", "firebase-tools", "firebase-tools@13.31.0", "firebase-tools@13")

    def test_all_forms_match_patterns(self):
        for name in self.FORMS:
            with self.subTest(name=name):
                self.assertTrue(
                    any(re.search(p, f"{name} deploy") for p in firebase.PATTERNS), name
                )

    def test_all_forms_are_state_changing_for_login_and_use(self):
        for name in self.FORMS:
            for sub in (f"{name} login", f"{name} use prod"):
                with self.subTest(cmd=sub):
                    self.assertTrue(
                        any(re.search(p, sub) for p in firebase.STATE_CHANGING), sub
                    )

    def test_all_forms_are_readonly_for_login(self):
        for name in self.FORMS:
            with self.subTest(name=name):
                self.assertTrue(
                    any(re.search(p, f"{name} login") for p in firebase.READONLY), name
                )

    def test_all_forms_support_self_remediation(self):
        for name in self.FORMS:
            with self.subTest(name=name):
                self.assertTrue(
                    firebase.is_self_remediation(f"{name} use prod", "prod"), name
                )

    def test_hyphenated_neighbours_still_excluded(self):
        for name in ("firebase-admin", "firebaseX"):
            with self.subTest(name=name):
                self.assertFalse(
                    any(re.search(p, f"{name} deploy") for p in firebase.PATTERNS), name
                )


class _LocalConfigBase(unittest.TestCase):
    """ローカル設定ファイル読取 (v0.13.0) のテスト基盤。

    CLI を呼んだかどうかを `subprocess.run` の mock で観測する。allow は CLI 無しで
    出せること / エラー方向は必ず CLI で取り直すことの両方を固定する。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _env(self, **overrides) -> dict:
        env = dict(os.environ)
        env.update(overrides)
        return env


class TestGithubLocalConfigRead(_LocalConfigBase):
    HOSTS_YML = (
        "github.com:\n"
        "    git_protocol: https\n"
        "    users:\n"
        "        active-user:\n"
        "        other-user:\n"
        "    user: active-user\n"
    )

    def setUp(self):
        super().setUp()
        self.gh_dir = self.tmp / "gh"
        self.gh_dir.mkdir()
        self.hosts = self.gh_dir / "hosts.yml"
        self.hosts.write_text(self.HOSTS_YML, encoding="utf-8")
        self.env = self._env(GH_CONFIG_DIR=str(self.gh_dir))

    def test_match_does_not_run_gh(self):
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(github.verify("active-user", "/p", env=self.env))
        self.assertFalse(run.called, "ローカル読取で済むのに gh を起動している")

    def test_dict_match_does_not_run_gh(self):
        self.hosts.write_text(
            "github.com:\n    user: active-user\n"
            "ghe.example.com:\n    user: corp-user\n",
            encoding="utf-8",
        )
        expected = {"github.com": "active-user", "ghe.example.com": "corp-user"}
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(github.verify(expected, "/p", env=self.env))
        self.assertFalse(run.called)

    def test_inactive_user_in_users_block_is_not_treated_as_active(self):
        """`users:` に並ぶ非アクティブなアカウントで allow しない。

        ローカル読取がアクティブ (`user:`) と紐付き一覧 (`users:`) を混同すると、
        「期待値のアカウントは存在するが今アクティブではない」状態を allow して
        しまう = この plugin が防ぐはずの誤アカウント書込がそのまま通る。
        """
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY.replace("Mao-o", "active-user")),
        ) as run:
            err = github.verify("other-user", "/p", env=self.env)
        self.assertTrue(run.called, "エラー方向は CLI で取り直すこと")
        self.assertIsNotNone(err)
        self.assertIn("不一致", err)

    def test_local_mismatch_is_confirmed_by_cli_before_deny(self):
        """ローカル読取が不一致でも、CLI が一致と言えば allow (誤 deny を作らない)。"""
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stdout=GH_GITHUB_COM_ONLY.replace("Mao-o", "switched-user")
            ),
        ) as run:
            self.assertIsNone(github.verify("switched-user", "/p", env=self.env))
        self.assertTrue(run.called)

    def test_token_env_falls_back_to_cli(self):
        """env トークン運用ではアカウント名が API 経由でしか分からない。"""
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                     "GITHUB_ENTERPRISE_TOKEN"):
            with self.subTest(env=name):
                env = self._env(GH_CONFIG_DIR=str(self.gh_dir), **{name: "x"})
                with mock.patch(
                    "subprocess.run",
                    return_value=_fake_run(
                        stdout=GH_GITHUB_COM_ONLY.replace("Mao-o", "active-user")
                    ),
                ) as run:
                    self.assertIsNone(github.verify("active-user", "/p", env=env))
                self.assertTrue(run.called, f"{name} があるのにローカル読取で済ませている")

    def test_gh_host_env_falls_back_to_cli(self):
        env = self._env(GH_CONFIG_DIR=str(self.gh_dir), GH_HOST="ghe.example.com")
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stdout=GH_GITHUB_COM_ONLY.replace("Mao-o", "active-user")
            ),
        ) as run:
            self.assertIsNone(github.verify("active-user", "/p", env=env))
        self.assertTrue(run.called)

    def test_home_override_falls_back_to_cli(self):
        """`HOME=<other> gh ...` では hook 側の hosts.yml で allow しない。

        `HOME` は gh の設定ディレクトリ解決に参加する (`$HOME/.config/gh`) ので、
        インライン env で差し替えられた gh は**別の hosts.yml** を読む。ここで
        hook 側のファイルを信じると、実行される gh とは違うアカウントで allow
        してしまう (ローカル読取の導入前は検証 subprocess にも同じ env が渡り、
        不一致として deny されていた = 退行にあたる)。
        """
        # hook 側の hosts.yml は期待値と一致している (= 唯一の bail 理由は HOME)。
        env = dict(self.env, HOME=str(self.tmp / "other-home"))
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stdout=GH_GITHUB_COM_ONLY.replace("Mao-o", "other-user")
            ),
        ) as run:
            err = github.verify("active-user", "/p", env=env)
        self.assertTrue(
            run.called, "HOME が上書きされているのにローカル読取で済ませている"
        )
        self.assertIsNotNone(err, "CLI 側の現在値 (other-user) で判定していない")
        self.assertIn("不一致", err)

    def test_same_home_still_reads_locally(self):
        """negative control: `HOME` が hook と同じなら明示されていても読み続ける。"""
        env = dict(self.env, HOME=os.environ["HOME"])
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(github.verify("active-user", "/p", env=env))
        self.assertFalse(run.called)

    def test_missing_hosts_yml_falls_back_to_cli(self):
        self.hosts.unlink()
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stdout=GH_GITHUB_COM_ONLY.replace("Mao-o", "active-user")
            ),
        ) as run:
            self.assertIsNone(github.verify("active-user", "/p", env=self.env))
        self.assertTrue(run.called)

    def test_unparseable_hosts_yml_falls_back_to_cli(self):
        self.hosts.write_text("github.com:\n\tuser: active-user\n", encoding="utf-8")
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stdout=GH_GITHUB_COM_ONLY.replace("Mao-o", "active-user")
            ),
        ) as run:
            self.assertIsNone(github.verify("active-user", "/p", env=self.env))
        self.assertTrue(run.called)

    def test_scalar_with_multiple_non_default_hosts_falls_back_to_cli(self):
        """github.com が無く host が複数なら照合先が記載順依存になるので CLI に委ねる。"""
        self.hosts.write_text(
            "ghe-a.example.com:\n    user: a-user\n"
            "ghe-b.example.com:\n    user: b-user\n",
            encoding="utf-8",
        )
        with mock.patch(
            "subprocess.run",
            return_value=_fake_run(
                stdout=(
                    "ghe-a.example.com\n"
                    "  ✓ Logged in to ghe-a.example.com account a-user\n"
                    "  - Active account: true\n"
                )
            ),
        ) as run:
            self.assertIsNone(github.verify("a-user", "/p", env=self.env))
        self.assertTrue(run.called)

    def test_xdg_config_home_is_used_when_gh_config_dir_absent(self):
        xdg = self.tmp / "xdg"
        (xdg / "gh").mkdir(parents=True)
        (xdg / "gh" / "hosts.yml").write_text(self.HOSTS_YML, encoding="utf-8")
        env = self._env(XDG_CONFIG_HOME=str(xdg))
        env.pop("GH_CONFIG_DIR", None)
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(github.verify("active-user", "/p", env=env))
        self.assertFalse(run.called)

    def test_uses_process_env_when_no_inline_env(self):
        """inline env が無い (env=None) ときは hook プロセスの環境で解決する。"""
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": str(self.gh_dir)}):
            with mock.patch("subprocess.run") as run:
                self.assertIsNone(github.verify("active-user", "/p"))
        self.assertFalse(run.called)

    def test_get_active_account_still_uses_cli(self):
        """builder 経路 (show / --from-cli) は gh 自身の報告値を使う。"""
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout=GH_GITHUB_COM_ONLY)
        ) as run:
            self.assertEqual(
                github.get_active_account("/p"), {"github.com": "Mao-o"}
            )
        self.assertTrue(run.called)


class TestGcloudLocalConfigRead(_LocalConfigBase):
    CONFIG_INI = "[core]\naccount = me@example.com\nproject = my-proj\n"

    def setUp(self):
        super().setUp()
        self.cfg_dir = self.tmp / "gcloud"
        (self.cfg_dir / "configurations").mkdir(parents=True)
        (self.cfg_dir / "active_config").write_text("work", encoding="utf-8")
        self.config_file = self.cfg_dir / "configurations" / "config_work"
        self.config_file.write_text(self.CONFIG_INI, encoding="utf-8")
        self.env = self._env(CLOUDSDK_CONFIG=str(self.cfg_dir))

    def test_project_match_does_not_run_gcloud(self):
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(gcloud.verify("my-proj", "/p", env=self.env))
        self.assertFalse(run.called, "ローカル読取で済むのに gcloud を起動している")

    def test_dict_match_does_not_run_gcloud(self):
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(
                gcloud.verify(
                    {"project": "my-proj", "account": "me@example.com"},
                    "/p",
                    env=self.env,
                )
            )
        self.assertFalse(run.called)

    def test_local_mismatch_is_confirmed_by_cli_before_deny(self):
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="switched-proj\n")
        ) as run:
            self.assertIsNone(gcloud.verify("switched-proj", "/p", env=self.env))
        self.assertTrue(run.called, "エラー方向は CLI で取り直すこと")

    def test_mismatch_in_both_denies_with_cli_value(self):
        """deny 文面の現在値は必ず CLI 実測値 (ローカル読取値ではない)。"""
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="cli-proj\n")
        ) as run:
            err = gcloud.verify("want-proj", "/p", env=self.env)
        self.assertTrue(run.called)
        self.assertIsNotNone(err)
        self.assertIn("現在=cli-proj", err)
        self.assertNotIn("my-proj", err)

    def test_active_config_env_overrides_file(self):
        other = self.cfg_dir / "configurations" / "config_other"
        other.write_text("[core]\nproject = other-proj\n", encoding="utf-8")
        env = self._env(
            CLOUDSDK_CONFIG=str(self.cfg_dir), CLOUDSDK_ACTIVE_CONFIG_NAME="other"
        )
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(gcloud.verify("other-proj", "/p", env=env))
        self.assertFalse(run.called)

    def test_configuration_context_option_selects_file(self):
        other = self.cfg_dir / "configurations" / "config_ctx"
        other.write_text("[core]\nproject = ctx-proj\n", encoding="utf-8")
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(
                gcloud.verify(
                    "ctx-proj", "/p", env=self.env, context={"configuration": "ctx"}
                )
            )
        self.assertFalse(run.called)

    def test_default_config_name_when_active_config_absent(self):
        (self.cfg_dir / "active_config").unlink()
        (self.cfg_dir / "configurations" / "config_default").write_text(
            "[core]\nproject = default-proj\n", encoding="utf-8"
        )
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(gcloud.verify("default-proj", "/p", env=self.env))
        self.assertFalse(run.called)

    def test_property_override_env_falls_back_to_cli(self):
        """値そのものを上書きしうる env があればローカル読取を使わない。

        優先順位をエミュレートしない (取り違えた値で allow するより、CLI を
        呼んで gcloud 自身に決めさせる)。
        """
        for name in ("CLOUDSDK_CORE_PROJECT", "CLOUDSDK_CORE_ACCOUNT",
                     "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
            with self.subTest(env=name):
                env = self._env(CLOUDSDK_CONFIG=str(self.cfg_dir), **{name: "x"})
                with mock.patch(
                    "subprocess.run", return_value=_fake_run(stdout="my-proj\n")
                ) as run:
                    self.assertIsNone(gcloud.verify("my-proj", "/p", env=env))
                self.assertTrue(run.called, f"{name} があるのにローカル読取で済ませている")

    def test_home_override_falls_back_to_cli(self):
        """`HOME=<other> gcloud ...` では hook 側の設定ファイルで allow しない。

        `HOME` は gcloud の設定ディレクトリ解決に参加する
        (`$HOME/.config/gcloud`)。gh 側と同じ理由でローカル読取を諦める。
        """
        # hook 側の config_work は期待値と一致している (= 唯一の bail 理由は HOME)。
        env = dict(self.env, HOME=str(self.tmp / "other-home"))
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="cli-proj\n")
        ) as run:
            err = gcloud.verify("my-proj", "/p", env=env)
        self.assertTrue(
            run.called, "HOME が上書きされているのにローカル読取で済ませている"
        )
        self.assertIsNotNone(err, "CLI 側の現在値 (cli-proj) で判定していない")
        self.assertIn("現在=cli-proj", err)

    def test_same_home_still_reads_locally(self):
        """negative control: `HOME` が hook と同じなら明示されていても読み続ける。"""
        env = dict(self.env, HOME=os.environ["HOME"])
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(gcloud.verify("my-proj", "/p", env=env))
        self.assertFalse(run.called)

    def test_invalid_configuration_name_is_rejected(self):
        """パス要素を含む名前で設定ファイルのパスを組まない。

        「読む先が存在しないから結果的に CLI に落ちる」では守れていない
        (`../` を辿れる配置なら読んでしまう) ため、名前の検査そのものを見る。
        """
        for name in ("../escape", "with/slash", "9starts-with-digit", ".hidden",
                     "a" * 65):
            with self.subTest(name=name):
                self.assertIsNone(
                    gcloud._local_config_name(self.env, self.cfg_dir, name),
                    f"{name!r} を configuration 名として受理している",
                )

    def test_valid_configuration_name_is_accepted(self):
        """negative control: 正当な名前は通る (検査が全部落としていない)。"""
        self.assertEqual(
            gcloud._local_config_name(self.env, self.cfg_dir, "work-2"), "work-2"
        )

    def test_invalid_configuration_name_falls_back_to_cli(self):
        (self.cfg_dir / "active_config").write_text("../escape", encoding="utf-8")
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="my-proj\n")
        ) as run:
            self.assertIsNone(gcloud.verify("my-proj", "/p", env=self.env))
        self.assertTrue(run.called)

    def test_empty_active_config_falls_back_to_default_name(self):
        (self.cfg_dir / "active_config").write_text("\n", encoding="utf-8")
        self.assertEqual(
            gcloud._local_config_name(self.env, self.cfg_dir), "default"
        )

    def test_missing_config_file_falls_back_to_cli(self):
        self.config_file.unlink()
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="my-proj\n")
        ) as run:
            self.assertIsNone(gcloud.verify("my-proj", "/p", env=self.env))
        self.assertTrue(run.called)

    def test_account_missing_locally_falls_back_to_cli(self):
        """ローカルに account が無くても deny しない (installation 設定など
        設定ファイル以外の経路があるため CLI で取り直す)。"""
        self.config_file.write_text("[core]\nproject = my-proj\n", encoding="utf-8")

        def side_effect(args, **_kwargs):
            if args[3] == "project":
                return _fake_run(stdout="my-proj\n")
            return _fake_run(stdout="me@example.com\n")

        with mock.patch("subprocess.run", side_effect=side_effect) as run:
            self.assertIsNone(
                gcloud.verify(
                    {"project": "my-proj", "account": "me@example.com"},
                    "/p",
                    env=self.env,
                )
            )
        self.assertTrue(run.called)

    def test_uses_process_env_when_no_inline_env(self):
        with mock.patch.dict(os.environ, {"CLOUDSDK_CONFIG": str(self.cfg_dir)}):
            with mock.patch("subprocess.run") as run:
                self.assertIsNone(gcloud.verify("my-proj", "/p"))
        self.assertFalse(run.called)

    def test_get_active_account_still_uses_cli(self):
        with mock.patch(
            "subprocess.run", return_value=_fake_run(stdout="cli-proj\n")
        ) as run:
            self.assertEqual(
                gcloud.get_active_account("/p"),
                {"project": "cli-proj", "account": "cli-proj"},
            )
        self.assertTrue(run.called)


if __name__ == "__main__":
    unittest.main()
