"""Windows の gh / gcloud の設定ディレクトリ (v0.35.1)。

Windows の gh は `GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `%AppData%\\GitHub CLI` (go-gh の `ConfigDir` と
`gh help environment`)、gcloud は `CLOUDSDK_CONFIG` → `%APPDATA%\\gcloud` (gcloud の公式文書の configurations の頁)
を使う。0.35.0 までの hook は Windows でも `~/.config` の下を読み、CLI が使っていない古いファイルで一致と判定し、
成功 cache の指紋も同じ古いファイルで取っていた。

Windows の実機は無いので、`cli_config` から見える `os.name` (と Cygwin / MSYS の判定に使う `sys.platform`) を
差し替え、env を Windows の形にして確かめる。`os.name` そのものを差し替えると、この OS の `Path()` が
`WindowsPath` を作ろうとして失敗するので、`cli_config` の `os` だけを `name` の違う代理に替える
(ほかの属性は本物の `os`)。APPDATA の値はこの OS の一時 dir で、Windows のパスの扱いは確かめていない
(README の既知の制限)。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from core import cache, cli_config, dispatcher  # noqa: E402
from services import gcloud, github  # noqa: E402

_PKG_DIR = Path(__file__).resolve().parent.parent
_README = _PKG_DIR.parent.parent / "README.md"

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


class _OsNamed:
    """`cli_config.os` の代わり。`name` だけを差し替え、ほかの属性は本物の `os` を返す。"""

    def __init__(self, name: str):
        self.name = name

    def __getattr__(self, attr):
        return getattr(os, attr)


def _platform(os_name: str, sys_platform: str):
    """`cli_config` から見える `os.name` と `sys.platform` を差し替える。"""
    patchers = [
        mock.patch.object(cli_config, "os", _OsNamed(os_name)),
        mock.patch.object(cli_config, "sys", SimpleNamespace(platform=sys_platform)),
    ]

    class _Both:
        def __enter__(self):
            for p in patchers:
                p.start()
            return self

        def __exit__(self, *exc):
            for p in reversed(patchers):
                p.stop()
            return False

    return _Both()


def windows():
    return _platform("nt", "win32")


def posix():
    return _platform("posix", "linux")


def _hosts(active: str) -> str:
    return (
        "github.com:\n"
        "    users:\n"
        "        work:\n"
        "        side:\n"
        "    git_protocol: https\n"
        f"    user: {active}\n"
    )


def _gh_status(active: str) -> str:
    """`gh auth status` の出力 (github.com に work と side、active がアクティブ)。"""
    lines = ["github.com"]
    for user in ("work", "side"):
        lines.append(f"  ✓ Logged in to github.com account {user} (keyring)")
        lines.append("  - Active account: " + ("true" if user == active else "false"))
    return "\n".join(lines) + "\n"


def _fake_run(stdout: str):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _write(path: Path, text: str, *, age: float = 100.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    past = time.time() - age
    os.utime(path, (past, past))


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # hook プロセスの HOME (`Path.home()`)。その下の `~/.config` に、CLI が Windows で使っていない
        # 古いファイルを置く。
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.object(Path, "home", staticmethod(lambda: self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.appdata = self.tmp / "AppData" / "Roaming"
        self.appdata.mkdir(parents=True)

    def _env(self, **extra) -> dict:
        """Windows の形の env (GH_CONFIG_DIR / XDG_CONFIG_HOME / CLOUDSDK_CONFIG は無い)。HOME は hook と同じ。"""
        env = {"HOME": os.environ["HOME"], "APPDATA": str(self.appdata)}
        env.update(extra)
        return env


class TestConfigDirRule(unittest.TestCase):
    def test_rule_by_os_name_and_platform(self):
        cases = (
            ("nt", "win32", cli_config.WINDOWS),
            ("posix", "linux", cli_config.POSIX),
            ("posix", "darwin", cli_config.POSIX),
            ("posix", "cygwin", None),
            ("posix", "msys", None),
        )
        for os_name, platform, want in cases:
            with self.subTest(os_name=os_name, platform=platform):
                with _platform(os_name, platform):
                    self.assertEqual(cli_config.config_dir_rule(), want)

    def test_state_files_when_dir_unknown(self):
        """POSIX は 0.35.0 のまま [] (cache は今までどおり)。それ以外は None (cache を使わない)。"""
        cases = (("posix", "linux", []), ("nt", "win32", None), ("posix", "cygwin", None))
        for os_name, platform, want in cases:
            with self.subTest(os_name=os_name, platform=platform):
                with _platform(os_name, platform):
                    self.assertEqual(cli_config.state_files_when_dir_unknown(), want)


class TestWindowsEnv(unittest.TestCase):
    """Windows の env の名前は大文字小文字を区別しない。綴りの違う名前が別の値で並べば決められない (None)。"""

    def test_values(self):
        cases = (
            ({"APPDATA": "a"}, "a"),
            ({"AppData": "a"}, "a"),
            ({"appdata": "a"}, "a"),
            ({"APPDATA": "a", "AppData": "a"}, "a"),
            ({"APPDATA": "a", "AppData": "b"}, None),
            ({"APPDATA": "", "AppData": "b"}, None),
            ({"APPDATA": ""}, ""),
            ({}, ""),
            ({"APPDATA_X": "a", "XAPPDATA": "b"}, ""),
        )
        for env, want in cases:
            with self.subTest(env=env):
                self.assertEqual(cli_config.windows_env(env, "APPDATA"), want)

    def test_name_is_compared_without_case(self):
        """呼び出し側が渡す名前の綴り (gh の `AppData`) にもよらない。"""
        self.assertEqual(cli_config.windows_env({"APPDATA": "a"}, "AppData"), "a")


class TestWindowsConfigDir(unittest.TestCase):
    """candidates の順に、最初に値のある env で決まる。途中の env を決められなければ None。"""

    CANDIDATES = (("FIRST", None), ("SECOND", "sub"), ("THIRD", "Some Dir"))

    def test_order_and_child(self):
        cases = (
            ({"FIRST": "/f", "SECOND": "/s", "THIRD": "/t"}, Path("/f")),
            ({"FIRST": "", "SECOND": "/s", "THIRD": "/t"}, Path("/s/sub")),
            ({"SECOND": "/s", "THIRD": "/t"}, Path("/s/sub")),
            ({"THIRD": "/t"}, Path("/t/Some Dir")),
            ({"FIRST": "", "SECOND": "", "THIRD": ""}, None),
            ({}, None),
            # 途中の env が綴りの違う名前で別の値を持つ: その後ろの env では決めない。
            ({"FIRST": "/a", "first": "/b", "THIRD": "/t"}, None),
            ({"SECOND": "/a", "Second": "/b", "THIRD": "/t"}, None),
            # 決まった後ろの env が決められなくても、前で決まっていれば使う。
            ({"FIRST": "/f", "THIRD": "/a", "third": "/b"}, Path("/f")),
        )
        for env, want in cases:
            with self.subTest(env=env):
                self.assertEqual(cli_config.windows_config_dir(env, self.CANDIDATES), want)

    def test_no_candidates(self):
        self.assertIsNone(cli_config.windows_config_dir({"FIRST": "/f"}, ()))


class TestGithubWindowsConfigDir(_Tmp):
    def setUp(self):
        super().setUp()
        self.gh_app = self.appdata / "GitHub CLI"
        self.stale = self.home / ".config" / "gh" / "hosts.yml"

    def test_order(self):
        """GH_CONFIG_DIR → XDG_CONFIG_HOME/gh → AppData/GitHub CLI (_env は APPDATA を持つ)。"""
        cases = (
            ({"GH_CONFIG_DIR": "/vca-gh", "XDG_CONFIG_HOME": "/vca-xdg"}, Path("/vca-gh")),
            ({"XDG_CONFIG_HOME": "/vca-xdg"}, Path("/vca-xdg/gh")),
            ({"GH_CONFIG_DIR": "", "XDG_CONFIG_HOME": ""}, "appdata"),
            ({}, "appdata"),
        )
        for extra, want in cases:
            with self.subTest(extra=extra):
                if want == "appdata":
                    want = self.gh_app
                with windows():
                    self.assertEqual(github._config_dir(self._env(**extra)), want)

    def test_lowercase_inline_name_is_the_same_variable(self):
        """Windows では `gh_config_dir=... gh ...` も gh の GH_CONFIG_DIR。POSIX では別の変数。"""
        env = self._env(gh_config_dir="/vca-gh")
        with windows():
            self.assertEqual(github._config_dir(env), Path("/vca-gh"))
        with posix():
            self.assertEqual(github._config_dir(env), self.home / ".config" / "gh")

    def test_undetermined(self):
        cases = {
            "APPDATA なし": {"HOME": os.environ["HOME"]},
            "APPDATA が空": self._env(APPDATA=""),
            "綴りの違う APPDATA": self._env(AppData=str(self.tmp / "other")),
        }
        for label, env in cases.items():
            with self.subTest(label=label):
                with windows():
                    self.assertIsNone(github._config_dir(env))
                    self.assertIsNone(github.state_files(env))

    def test_cygwin_python_is_undetermined(self):
        for platform in ("cygwin", "msys"):
            with self.subTest(platform=platform):
                with _platform("posix", platform):
                    self.assertIsNone(github._config_dir(self._env()))
                    self.assertIsNone(github.state_files(self._env()))

    def test_posix_keeps_the_home_config(self):
        """negative control: POSIX では APPDATA があっても ~/.config/gh (0.35.0 と同じ)。"""
        with posix():
            self.assertEqual(github._config_dir(self._env()), self.home / ".config" / "gh")

    def test_state_files(self):
        with windows():
            self.assertEqual(github.state_files(self._env()), [self.gh_app / "hosts.yml"])
            # 行頭の `HOME=...` は Windows の gh の設定ディレクトリを変えない。
            other_home = self._env(HOME=str(self.tmp / "other-home"))
            self.assertEqual(github.state_files(other_home), [self.gh_app / "hosts.yml"])

    def test_local_read_uses_appdata_not_the_stale_home_file(self):
        """チケットの再現: ~/.config の古い hosts.yml は期待値 (work) と一致し、gh が使う AppData の
        hosts.yml は side。Windows では AppData を読み、ローカル読取の不一致から gh に聞き直して止める。"""
        _write(self.stale, _hosts("work"))
        _write(self.gh_app / "hosts.yml", _hosts("side"))
        env = self._env()
        with posix(), mock.patch("subprocess.run") as run:
            # 0.35.0 と同じ読み (POSIX の決まり): 古いファイルで一致と判定し、gh に聞かない。
            self.assertIsNone(github.verify("work", "/p", env=env))
        self.assertFalse(run.called)
        with windows(), mock.patch(
            "subprocess.run", return_value=_fake_run(_gh_status("side"))
        ) as run:
            err = github.verify("work", "/p", env=env)
        self.assertTrue(run.called, "AppData の hosts.yml は不一致なのに gh に聞いていない")
        self.assertIsNotNone(err)
        self.assertIn("アカウント不一致: 現在=side, 期待=work", err)

    def test_local_read_match_in_appdata_does_not_run_gh(self):
        _write(self.stale, _hosts("side"))
        _write(self.gh_app / "hosts.yml", _hosts("work"))
        with windows(), mock.patch("subprocess.run") as run:
            self.assertIsNone(github.verify("work", "/p", env=self._env()))
        self.assertFalse(run.called, "AppData の hosts.yml で決まるのに gh を起動している")

    def test_undetermined_dir_asks_gh(self):
        """場所を決められなければ、~/.config の古いファイルが一致していても gh に聞く。"""
        _write(self.stale, _hosts("work"))
        for label, env in {
            "APPDATA なし": {"HOME": os.environ["HOME"]},
            "綴りの違う APPDATA": self._env(AppData=str(self.tmp / "other")),
        }.items():
            with self.subTest(label=label):
                with windows(), mock.patch(
                    "subprocess.run", return_value=_fake_run(_gh_status("side"))
                ) as run:
                    err = github.verify("work", "/p", env=env)
                self.assertTrue(run.called)
                self.assertIsNotNone(err)
                self.assertIn("現在=side", err)

    def test_identity_env_has_appdata(self):
        self.assertIn("APPDATA", github.IDENTITY_ENV_VARS)


class TestGcloudWindowsConfigDir(_Tmp):
    def setUp(self):
        super().setUp()
        self.app_cfg = self.appdata / "gcloud"
        self.stale_cfg = self.home / ".config" / "gcloud"

    def _configs(self, base: Path, project: str, account: str = "me@example.com") -> None:
        _write(base / "active_config", "work\n")
        _write(
            base / "configurations" / "config_work",
            f"[core]\naccount = {account}\nproject = {project}\n",
        )

    def test_order(self):
        """CLOUDSDK_CONFIG → APPDATA/gcloud (_env は APPDATA を持つ)。"""
        cases = (
            ({"CLOUDSDK_CONFIG": "/vca-gcloud"}, Path("/vca-gcloud")),
            # gcloud は XDG_CONFIG_HOME を見ない。
            ({"XDG_CONFIG_HOME": "/vca-xdg"}, "appdata"),
            ({"CLOUDSDK_CONFIG": ""}, "appdata"),
            ({}, "appdata"),
            ({"cloudsdk_config": "/vca-gcloud"}, Path("/vca-gcloud")),
        )
        for extra, want in cases:
            with self.subTest(extra=extra):
                if want == "appdata":
                    want = self.app_cfg
                with windows():
                    self.assertEqual(gcloud._config_dir(self._env(**extra)), want)

    def test_undetermined(self):
        cases = {
            "APPDATA なし": {"HOME": os.environ["HOME"]},
            "APPDATA が空": self._env(APPDATA=""),
            "綴りの違う APPDATA": self._env(AppData=str(self.tmp / "other")),
        }
        for label, env in cases.items():
            with self.subTest(label=label):
                with windows():
                    self.assertIsNone(gcloud._config_dir(env))
                    self.assertIsNone(gcloud.state_files(env))

    def test_cygwin_python_is_undetermined(self):
        for platform in ("cygwin", "msys"):
            with self.subTest(platform=platform):
                with _platform("posix", platform):
                    self.assertIsNone(gcloud._config_dir(self._env()))
                    self.assertIsNone(gcloud.state_files(self._env()))

    def test_posix_keeps_the_home_config(self):
        with posix():
            self.assertEqual(gcloud._config_dir(self._env()), self.home / ".config" / "gcloud")

    def test_state_files(self):
        self._configs(self.app_cfg, "side-proj")
        want = [self.app_cfg / "active_config", self.app_cfg / "configurations" / "config_work"]
        with windows():
            self.assertEqual(gcloud.state_files(self._env()), want)
            other_home = self._env(HOME=str(self.tmp / "other-home"))
            self.assertEqual(gcloud.state_files(other_home), want)

    def test_local_read_uses_appdata_not_the_stale_home_file(self):
        self._configs(self.stale_cfg, "want-proj")
        self._configs(self.app_cfg, "side-proj")
        env = self._env()
        with posix(), mock.patch("subprocess.run") as run:
            self.assertIsNone(gcloud.verify("want-proj", "/p", env=env))
        self.assertFalse(run.called)
        with windows(), mock.patch(
            "subprocess.run", return_value=_fake_run("side-proj\n")
        ) as run:
            err = gcloud.verify("want-proj", "/p", env=env)
        self.assertTrue(run.called, "APPDATA の構成は不一致なのに gcloud に聞いていない")
        self.assertIsNotNone(err)
        self.assertIn("現在=side-proj, 期待=want-proj", err)

    def test_local_read_match_in_appdata_does_not_run_gcloud(self):
        self._configs(self.stale_cfg, "side-proj")
        self._configs(self.app_cfg, "want-proj")
        with windows(), mock.patch("subprocess.run") as run:
            self.assertIsNone(gcloud.verify("want-proj", "/p", env=self._env()))
        self.assertFalse(run.called, "APPDATA の構成で決まるのに gcloud を起動している")

    def test_undetermined_dir_asks_gcloud(self):
        self._configs(self.stale_cfg, "want-proj")
        with windows(), mock.patch(
            "subprocess.run", return_value=_fake_run("side-proj\n")
        ) as run:
            err = gcloud.verify("want-proj", "/p", env={"HOME": os.environ["HOME"]})
        self.assertTrue(run.called)
        self.assertIsNotNone(err)
        self.assertIn("現在=side-proj", err)

    def test_is_pinned_reads_appdata(self):
        """`CLOUDSDK_ACTIVE_CONFIG_NAME` が指す構成の account は、gcloud が使う APPDATA の下で読む。"""
        _write(self.stale_cfg / "configurations" / "config_pinned", "[core]\naccount = old@example.com\n")
        env = self._env(CLOUDSDK_ACTIVE_CONFIG_NAME="pinned")
        with posix():
            # 0.35.0 と同じ読み: ~/.config の古い構成で固定とみなす。
            self.assertTrue(gcloud.is_pinned(env, "/p"))
        with windows():
            self.assertFalse(gcloud.is_pinned(env, "/p"))
            _write(self.app_cfg / "configurations" / "config_pinned", "[core]\naccount = me@example.com\n")
            self.assertTrue(gcloud.is_pinned(env, "/p"))
            # APPDATA の無い Windows は場所を決められない: 固定とはみなさない (未登録の扱いのまま)。
            no_appdata = {"HOME": os.environ["HOME"], "CLOUDSDK_ACTIVE_CONFIG_NAME": "pinned"}
            self.assertFalse(gcloud.is_pinned(no_appdata, "/p"))

    def test_configurations_matching_lists_appdata(self):
        self._configs(self.stale_cfg, "want-proj")
        with windows():
            self.assertEqual(gcloud.configurations_matching("want-proj", self._env()), [])
            self._configs(self.app_cfg, "want-proj")
            self.assertEqual(gcloud.configurations_matching("want-proj", self._env()), ["work"])
            self.assertEqual(
                gcloud.configurations_matching("want-proj", {"HOME": os.environ["HOME"]}), []
            )

    def test_identity_env_has_appdata_on_windows(self):
        """Windows (と場所を決められない Cygwin / MSYS) で設定ディレクトリを決める APPDATA は、成功 cache の
        キーと固定の判定に入る。macOS / Linux の gcloud は APPDATA を見ないので入れない (0.35.0 と同じ)。"""
        for rule in (cli_config.WINDOWS, None):
            with self.subTest(rule=rule):
                self.assertIn("APPDATA", gcloud._identity_env_vars(rule))
        posix_names = gcloud._identity_env_vars(cli_config.POSIX)
        self.assertNotIn("APPDATA", posix_names)
        self.assertEqual(
            gcloud._identity_env_vars(cli_config.WINDOWS) - posix_names, {"APPDATA"}
        )
        self.assertEqual(
            gcloud.IDENTITY_ENV_VARS, gcloud._identity_env_vars(cli_config.config_dir_rule())
        )
        with mock.patch.object(
            gcloud, "IDENTITY_ENV_VARS", gcloud._identity_env_vars(cli_config.WINDOWS)
        ):
            self.assertEqual(
                cache.identity_env(gcloud, {"APPDATA": "a", "PATH": "p"}), {"APPDATA": "a"}
            )

    def test_inline_appdata_unpins_on_windows_only(self):
        """行頭の `APPDATA=...` は、Windows では gcloud の読む構成を変えるので固定とみなさない (未登録の deny)。
        macOS / Linux では 0.35.0 と同じく固定のまま (gcloud は APPDATA を見ない)。"""
        project = self.tmp / "project"
        (project / ".git").mkdir(parents=True)
        accounts = project / ".claude" / "cloud-account-guard" / "accounts.local.json"
        accounts.parent.mkdir(parents=True)
        accounts.write_text(json.dumps({"github": "work"}), encoding="utf-8")
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir(mode=0o700)
        command = f"APPDATA={self.tmp / 'other'} gcloud run deploy svc --image vca-probe-image"
        env = {
            "CLAUDE_PROJECT_DIR": str(project),
            "TMPDIR": str(cache_tmp),
            "APPDATA": str(self.appdata),
            "CLOUDSDK_CORE_ACCOUNT": "me@example.com",
        }
        for rule, platform, want in (("posix", posix, None), ("windows", windows, "deny")):
            with self.subTest(rule=rule):
                names = gcloud._identity_env_vars(
                    cli_config.POSIX if rule == "posix" else cli_config.WINDOWS
                )
                with mock.patch.dict(os.environ, env), platform(), mock.patch.object(
                    gcloud, "IDENTITY_ENV_VARS", names
                ), mock.patch("subprocess.run", side_effect=AssertionError("CLI は起動しない")):
                    try:
                        result = dispatcher.dispatch(command, str(project))
                    except AssertionError as e:
                        self.fail(f"CLI を起動した: {e}")
                got = None if result is None else result["hookSpecificOutput"]["permissionDecision"]
                self.assertEqual(got, want)
                if want == "deny":
                    self.assertIn('"gcloud" キーがありません', result["hookSpecificOutput"]["permissionDecisionReason"])


class TestWindowsDispatch(_Tmp):
    """dispatcher の成功 cache: Windows の形の env で、指紋を AppData の下のファイルで取る。"""

    GH_WRITE = "gh pr create --fill"
    GCLOUD_WRITE = "gcloud run deploy svc --image vca-probe-image"

    def setUp(self):
        super().setUp()
        self.project = self.tmp / "project"
        accounts = self.project / ".claude" / "cloud-account-guard" / "accounts.local.json"
        accounts.parent.mkdir(parents=True)
        accounts.write_text(
            json.dumps({"github": "work", "gcloud": "vca-probe-project"}), encoding="utf-8"
        )
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir(mode=0o700)
        patcher = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.project),
                "TMPDIR": str(cache_tmp),
                "APPDATA": str(self.appdata),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 隔離が設定する設定ディレクトリの env を外す (Windows の既定の場所を使う形)。
        for name in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "CLOUDSDK_CONFIG"):
            os.environ.pop(name, None)
        self.gh_hosts = self.appdata / "GitHub CLI" / "hosts.yml"
        self.stale_hosts = self.home / ".config" / "gh" / "hosts.yml"

    def _dispatch(self, command):
        try:
            return dispatcher.dispatch(command, str(self.project))
        except Exception as e:  # noqa: BLE001
            self.fail(f"dispatch から例外が漏れた: {e!r}")

    def test_switch_in_appdata_is_verified_again(self):
        _write(self.gh_hosts, _hosts("work"), age=100)
        _write(self.stale_hosts, _hosts("work"), age=100)
        with windows(), mock.patch("services.github.verify", return_value=None) as verify:
            self.assertIsNone(self._dispatch(self.GH_WRITE))
            self.assertIsNone(self._dispatch(self.GH_WRITE))
            self.assertEqual(verify.call_count, 1, "書き換えていないのに cache が効いていない")
            # gh が使っていない ~/.config の書き換えでは照合し直さない。
            _write(self.stale_hosts, _hosts("side"), age=60)
            self._dispatch(self.GH_WRITE)
            self.assertEqual(verify.call_count, 1)
            # ターミナルでの `gh auth switch` (AppData の hosts.yml)。
            _write(self.gh_hosts, _hosts("side"), age=50)
            self._dispatch(self.GH_WRITE)
        self.assertEqual(verify.call_count, 2)

    def test_gcloud_switch_in_appdata_is_verified_again(self):
        base = self.appdata / "gcloud"
        _write(base / "active_config", "work\n", age=100)
        _write(base / "configurations" / "config_work", "[core]\nproject = vca-probe-project\n")
        with windows(), mock.patch("services.gcloud.verify", return_value=None) as verify:
            self._dispatch(self.GCLOUD_WRITE)
            self._dispatch(self.GCLOUD_WRITE)
            self.assertEqual(verify.call_count, 1)
            _write(base / "active_config", "side\n", age=50)
            self._dispatch(self.GCLOUD_WRITE)
        self.assertEqual(verify.call_count, 2)

    def test_undetermined_dir_does_not_use_the_cache(self):
        """APPDATA が無い Windows: 指紋を取れないので、成功 cache を読まず書かない (毎回照合する)。"""
        os.environ.pop("APPDATA")
        _write(self.stale_hosts, _hosts("work"), age=100)
        for service, command in (("github", self.GH_WRITE), ("gcloud", self.GCLOUD_WRITE)):
            with self.subTest(service=service):
                with windows(), mock.patch(
                    f"services.{service}.verify", return_value=None
                ) as verify:
                    self.assertIsNone(self._dispatch(command))
                    self.assertIsNone(self._dispatch(command))
                self.assertEqual(verify.call_count, 2)
                self.assertEqual(sorted(cache.dir_path().glob(f"{service}-*.json")), [])

    def test_posix_unknown_home_keeps_the_cache(self):
        """negative control: POSIX で HOME を決められないときは 0.35.0 のまま cache を使う。"""
        with posix(), mock.patch.object(
            Path, "home", side_effect=RuntimeError("no home")
        ), mock.patch("services.github.verify", return_value=None) as verify:
            self._dispatch(self.GH_WRITE)
            self._dispatch(self.GH_WRITE)
        self.assertEqual(verify.call_count, 1)


class TestReadmeWindowsScope(unittest.TestCase):
    """README の既知の制限に、Windows の場所の決まり (コードの定数と同じ順と名前) と、確かめた範囲・
    確かめていない範囲が書かれている。"""

    def setUp(self):
        text = _README.read_text(encoding="utf-8")
        head = "- **Windows の gh / gcloud の設定ディレクトリ** (v0.35.1)"
        self.assertEqual(text.count(head), 1)
        start = text.index(head)
        end = text.find("\n- ", start + 1)
        self.assertGreater(end, start)
        self.item = re.sub(r"\s*\n\s*", "", text[start:end])

    def _in_order(self, words):
        positions = []
        for word in words:
            self.assertIn(word, self.item)
            positions.append(self.item.index(word))
        self.assertEqual(positions, sorted(positions), words)

    def test_order_matches_the_code(self):
        gh_names = [name for name, _ in github._WINDOWS_CONFIG_DIRS]
        self.assertEqual(gh_names, ["GH_CONFIG_DIR", "XDG_CONFIG_HOME", "APPDATA"])
        gh_child = github._WINDOWS_CONFIG_DIRS[-1][1]
        self._in_order(["`GH_CONFIG_DIR`", "`$XDG_CONFIG_HOME/gh`", f"`%AppData%\\{gh_child}`"])
        gc_names = [name for name, _ in gcloud._WINDOWS_CONFIG_DIRS]
        self.assertEqual(gc_names, ["CLOUDSDK_CONFIG", "APPDATA"])
        gc_child = gcloud._WINDOWS_CONFIG_DIRS[-1][1]
        self._in_order(["`CLOUDSDK_CONFIG`", f"`%APPDATA%\\{gc_child}`"])

    def test_scope_is_written(self):
        self._in_order([
            "確かめた範囲: macOS / Linux の上で `os.name` と env を差し替えたテスト",
            "確かめていない範囲: Windows の実機での動作",
            "AppData も無いときの行き先は組まず、CLI に聞く",
        ])


if __name__ == "__main__":
    unittest.main()
