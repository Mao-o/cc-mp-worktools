"""各 service の verify() テスト。subprocess.run を mock する。"""
from __future__ import annotations

import json
import os
import shutil
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

    # --- 解決順 (bd_092a232e-629.1): firebase use → CLI 不可時のみ .firebaserc ---

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


if __name__ == "__main__":
    unittest.main()
