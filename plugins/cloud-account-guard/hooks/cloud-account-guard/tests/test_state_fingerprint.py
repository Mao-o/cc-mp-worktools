"""成功 cache を CLI の状態ファイルの指紋で無効にする (v0.35.0)。

hook を通らない切替 (ターミナルでの `gh auth switch`・TMPDIR の違うセッションや cache dir を使えない
セッションでの切替) は epoch を進めない。0.34.0 までは、その後 TTL (30 秒) の間、期待と違う
アカウントでの書き込みが成功 cache の hit で通った。gh は hosts.yml、gcloud は active_config と構成の
ファイルを切替で書き換えるので、その stat の指紋 (`cache.state_fingerprint`) を entry に記録し、
違えば照合し直す。

fixture のファイルは mtime を過去にずらしてから使う (書いた直後は `cache._STATE_SETTLE_NS` の間
指紋を作らず、cache を読まず書かない。その窓は `TestSettleWindow` で固定する)。
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from core import cache  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from services import aws, gcloud, github  # noqa: E402

_PKG_DIR = Path(__file__).resolve().parent.parent
# root は読む権限の無いファイルも読める。
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

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


def _hosts(active: str) -> str:
    """gh 2.40 以上の hosts.yml (1 host に 2 アカウント、`user:` がアクティブ)。"""
    return (
        "github.com:\n"
        "    users:\n"
        "        work:\n"
        "        side:\n"
        "    git_protocol: https\n"
        f"    user: {active}\n"
    )


def _age(path: Path, seconds: float) -> None:
    """path の mtime を seconds 秒前にする (書いた直後の窓 `cache._STATE_SETTLE_NS` の外に出す)。"""
    past = time.time() - seconds
    os.utime(path, (past, past))


def _write(path: Path, text, *, age: float = 100.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    _age(path, age)


# 偽の stat の時刻 (2001 年。書いた直後の窓の外で、呼ぶたびに変わらない)。
_OLD_NS = 1_000_000_000 * 1_000_000_000


def _fake_stat(**over):
    base = {"st_dev": 1, "st_ino": 2, "st_size": 3, "st_mtime_ns": _OLD_NS, "st_ctime_ns": _OLD_NS}
    return SimpleNamespace(**{**base, **over})


def _fp(case: unittest.TestCase, paths):
    """`cache.state_fingerprint`。例外が漏れたら (stat の失敗を握り損ねた) assertion の失敗にする。"""
    try:
        return cache.state_fingerprint(paths)
    except (OSError, ValueError) as e:
        case.fail(f"state_fingerprint から例外が漏れた: {e!r}")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestStateFingerprint(_Tmp):
    def test_unchanged_file_keeps_the_fingerprint(self):
        hosts = self.tmp / "hosts.yml"
        _write(hosts, _hosts("work"))
        first = _fp(self, [hosts])
        self.assertIsNotNone(first)
        self.assertEqual(_fp(self, [hosts]), first)

    def test_rewrite_changes_the_fingerprint(self):
        hosts = self.tmp / "hosts.yml"
        _write(hosts, _hosts("work"), age=100)
        before = _fp(self, [hosts])
        _write(hosts, _hosts("side"), age=50)
        after = _fp(self, [hosts])
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)

    def test_each_stat_field_is_part_of_the_fingerprint(self):
        """(device, inode, 大きさ, mtime_ns, ctime_ns) のどれか 1 つだけが違っても指紋が変わる。"""
        path = self.tmp / "hosts.yml"

        def fingerprint(**over):
            with mock.patch.object(cache.os, "stat", return_value=_fake_stat(**over)):
                return _fp(self, [path])

        reference = fingerprint()
        self.assertIsNotNone(reference)
        base = vars(_fake_stat())
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            with self.subTest(field=field):
                changed = fingerprint(**{field: base[field] + 1})
                self.assertIsNotNone(changed)
                self.assertNotEqual(changed, reference)

    def test_same_size_rewrite_that_restores_the_mtime_changes_the_fingerprint(self):
        """大きさも mtime も同じ書き換え (同じ長さの名前への切替で、mtime を戻した形) も変わる (ctime)。"""
        hosts = self.tmp / "hosts.yml"
        _write(hosts, _hosts("work"), age=100)
        before_stat = os.stat(hosts)
        before = _fp(self, [hosts])
        # 粗い時計で ctime を記録するファイルシステムでも、刻みをまたぐ。
        time.sleep(0.05)
        hosts.write_text(_hosts("wrk2"), encoding="utf-8")
        os.utime(hosts, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        after_stat = os.stat(hosts)
        self.assertEqual(
            (after_stat.st_ino, after_stat.st_size, after_stat.st_mtime_ns),
            (before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns),
        )
        self.assertIsNotNone(before)
        self.assertNotEqual(_fp(self, [hosts]), before)

    def test_missing_file_is_stable_and_creating_it_changes_the_fingerprint(self):
        hosts = self.tmp / "gh" / "hosts.yml"
        missing = _fp(self, [hosts])
        self.assertIsNotNone(missing)
        self.assertEqual(_fp(self, [hosts]), missing)
        _write(hosts, _hosts("work"))
        self.assertNotEqual(_fp(self, [hosts]), missing)

    def test_stat_errors_are_told_apart_by_errno(self):
        path = self.tmp / "hosts.yml"

        def fingerprint(code):
            with mock.patch.object(cache.os, "stat", side_effect=OSError(code, "x")):
                return _fp(self, [path])

        enoent, eacces = fingerprint(errno.ENOENT), fingerprint(errno.EACCES)
        self.assertIsNotNone(enoent)
        self.assertIsNotNone(eacces)
        self.assertNotEqual(enoent, eacces)

    def test_which_file_was_looked_at_is_part_of_the_fingerprint(self):
        """同じ inode の 2 つの名前 (hard link) も、無い 2 つのファイルも、名前で区別する。"""
        a, b = self.tmp / "a", self.tmp / "b"
        self.assertNotEqual(_fp(self, [a]), _fp(self, [b]))
        _write(a, _hosts("work"))
        os.link(a, b)
        self.assertEqual(os.stat(a).st_ino, os.stat(b).st_ino)
        self.assertNotEqual(_fp(self, [a]), _fp(self, [b]))

    def test_contents_are_not_read(self):
        """NUL を含む・UTF-8 でないファイルも、stat できれば指紋を作り、書き換えで変わる。"""
        for name, first, second in (
            ("nul", b"github.com:\n    user: w\x00rk\n", b"github.com:\n    user: s\x00de\n"),
            ("not-utf8", b"github.com:\n    user: \xff\xfe\n", b"github.com:\n    user: \xfe\xff\xfd\n"),
        ):
            with self.subTest(name=name):
                path = self.tmp / name
                _write(path, first, age=100)
                before = _fp(self, [path])
                _write(path, second, age=50)
                after = _fp(self, [path])
                self.assertIsNotNone(before)
                self.assertIsNotNone(after)
                self.assertNotEqual(before, after)

    @unittest.skipIf(_IS_ROOT, "root は読む権限の無いファイルも読める")
    def test_unreadable_file_still_has_a_fingerprint(self):
        hosts = self.tmp / "hosts.yml"
        _write(hosts, _hosts("work"))
        hosts.chmod(0)
        self.addCleanup(hosts.chmod, 0o600)
        with self.assertRaises(PermissionError):
            hosts.read_bytes()
        first = _fp(self, [hosts])
        self.assertIsNotNone(first)
        self.assertEqual(_fp(self, [hosts]), first)

    @unittest.skipIf(_IS_ROOT, "root は権限の無いディレクトリも辿れる")
    def test_unsearchable_directory_is_not_an_error(self):
        locked = self.tmp / "locked"
        _write(locked / "hosts.yml", _hosts("work"))
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        first = _fp(self, [locked / "hosts.yml"])
        self.assertIsNotNone(first)
        self.assertEqual(_fp(self, [locked / "hosts.yml"]), first)

    def test_symlink_is_followed(self):
        """dotfiles の管理で symlink にした hosts.yml: gh は辿った先を書き換える。"""
        target = self.tmp / "dotfiles" / "hosts.yml"
        _write(target, _hosts("work"), age=100)
        link = self.tmp / "hosts.yml"
        link.symlink_to(target)
        before = _fp(self, [link])
        _write(target, _hosts("side"), age=50)
        self.assertIsNotNone(before)
        self.assertNotEqual(_fp(self, [link]), before)

    def test_dangling_symlink_and_symlink_loop_are_not_errors(self):
        dangling = self.tmp / "dangling"
        dangling.symlink_to(self.tmp / "nowhere")
        loop = self.tmp / "loop"
        loop.symlink_to(loop)
        for path in (dangling, loop):
            with self.subTest(path=path.name):
                first = _fp(self, [path])
                self.assertIsNotNone(first)
                self.assertEqual(_fp(self, [path]), first)

    def test_path_with_nul_or_undecodable_bytes_is_not_an_error(self):
        """パスは env (GH_CONFIG_DIR など) から来る。NUL は stat が ValueError、surrogate は名前に残る。"""
        for path in (f"{self.tmp}/gh\x00x/hosts.yml", f"{self.tmp}/gh\udcff/hosts.yml"):
            with self.subTest(path=path):
                first = _fp(self, [path])
                self.assertIsNotNone(first)
                self.assertEqual(_fp(self, [path]), first)


class TestSettleWindow(_Tmp):
    """書かれた直後のファイルでは指紋を作らない (1 秒単位の時刻の FS で、同じ 1 秒の 2 回の書き換えを
    区別できないため)。"""

    NOW = 2_000_000_000 * 1_000_000_000

    def _fingerprint(self, mtime_ns):
        stat = _fake_stat(st_mtime_ns=mtime_ns, st_ctime_ns=mtime_ns)
        with mock.patch.object(cache.os, "stat", return_value=stat), mock.patch.object(
            cache.time, "time_ns", return_value=self.NOW
        ):
            return _fp(self, [self.tmp / "hosts.yml"])

    def test_boundaries(self):
        settle = cache._STATE_SETTLE_NS
        self.assertEqual(settle, 2_000_000_000)
        for label, mtime, has_fingerprint in (
            ("now", self.NOW, False),
            ("just inside", self.NOW - settle + 1, False),
            ("exactly settled", self.NOW - settle, True),
            ("long ago", self.NOW - 100 * settle, True),
            ("in the future", self.NOW + 100 * settle, True),
        ):
            with self.subTest(label=label):
                self.assertEqual(self._fingerprint(mtime) is not None, has_fingerprint)

    def test_file_written_just_now_has_no_fingerprint(self):
        hosts = self.tmp / "hosts.yml"
        hosts.write_text(_hosts("work"), encoding="utf-8")
        self.assertIsNone(_fp(self, [hosts]))
        _age(hosts, 10)
        self.assertIsNotNone(_fp(self, [hosts]))

    def test_one_recent_file_is_enough(self):
        old, new = self.tmp / "active_config", self.tmp / "config_work"
        _write(old, "work\n")
        new.write_text("[core]\n", encoding="utf-8")
        self.assertIsNone(_fp(self, [old, new]))


class TestEntryState(_Tmp):
    """entry の state と、0.34.0 までの entry (state の記録が無い) の読み方。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"TMPDIR": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_state_must_match(self):
        cache.set_success("github", "/p", "work", 1.0, state="aaa")
        self.assertTrue(cache.get_success("github", "/p", "work", 1.0, state="aaa"))
        self.assertFalse(cache.get_success("github", "/p", "work", 1.0, state="bbb"))
        self.assertFalse(cache.get_success("github", "/p", "work", 1.0))

    def test_entry_without_state_reads_as_empty(self):
        """0.34.0 までの entry: 状態ファイルを宣言しない service では今までどおり hit し、宣言した
        service では 1 回照合し直す。"""
        path = cache._cache_path(cache._cache_key("aws", "/p", "123456789012"))
        self.assertIsNotNone(path)
        entry = {"success": True, "accounts_mtime": 1.0, "timestamp": time.time(), "epoch": 0}
        path.write_text(json.dumps(entry), encoding="utf-8")
        self.assertTrue(cache.get_success("aws", "/p", "123456789012", 1.0))
        self.assertFalse(cache.get_success("aws", "/p", "123456789012", 1.0, state="aaa"))


class TestGithubStateFiles(unittest.TestCase):
    """hosts.yml の場所は gh の設定 dir の決まり (GH_CONFIG_DIR → XDG_CONFIG_HOME/gh → HOME/.config/gh)。"""

    def test_config_dir_order(self):
        home = os.environ["HOME"]
        cases = (
            (
                {"GH_CONFIG_DIR": "/vca-gh", "XDG_CONFIG_HOME": "/vca-xdg", "HOME": home},
                [Path("/vca-gh/hosts.yml")],
            ),
            ({"XDG_CONFIG_HOME": "/vca-xdg", "HOME": home}, [Path("/vca-xdg/gh/hosts.yml")]),
            ({"GH_CONFIG_DIR": "", "XDG_CONFIG_HOME": "", "HOME": home},
             [Path.home() / ".config" / "gh" / "hosts.yml"]),
            ({"HOME": home}, [Path.home() / ".config" / "gh" / "hosts.yml"]),
        )
        for env, files in cases:
            with self.subTest(env=env):
                self.assertEqual(github.state_files(env), files)

    def test_inline_home_decides_the_default_dir(self):
        """行頭の `HOME=/other gh ...` の gh は /other/.config/gh を読む。"""
        self.assertNotEqual(os.environ["HOME"], "/vca-other-home")
        self.assertEqual(
            github.state_files({"HOME": "/vca-other-home"}),
            [Path("/vca-other-home/.config/gh/hosts.yml")],
        )

    def test_same_home_as_the_hook_process_uses_path_home(self):
        """env の HOME が hook プロセスと同じなら `Path.home()` (macOS / Linux の決まり。Windows の決まりは
        test_windows_config_dir.py)。"""
        with mock.patch.dict(os.environ, {"HOME": "/vca-same-home"}), mock.patch.object(
            Path, "home", return_value=Path("/vca-profile-home")
        ):
            self.assertEqual(
                github.state_files({"HOME": "/vca-same-home"}),
                [Path("/vca-profile-home/.config/gh/hosts.yml")],
            )

    def test_without_env_uses_the_hook_process_env(self):
        self.assertEqual(
            github.state_files(None), [Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml"]
        )

    def test_unknown_home_gives_no_files(self):
        with mock.patch.object(Path, "home", side_effect=RuntimeError("no home")):
            self.assertEqual(github.state_files({}), [])


class TestGcloudStateFiles(_Tmp):
    """active_config と、ローカル読取と同じ順で決めた構成のファイル。"""

    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "gcloud"
        self.env = {"CLOUDSDK_CONFIG": str(self.cfg), "HOME": os.environ["HOME"]}

    def _files(self, *names):
        return [self.cfg / "active_config"] + [
            self.cfg / "configurations" / f"config_{n}" for n in names
        ]

    def test_active_config_names_the_configuration_file(self):
        _write(self.cfg / "active_config", "work\n")
        self.assertEqual(gcloud.state_files(self.env), self._files("work"))

    def test_default_without_active_config(self):
        self.assertEqual(gcloud.state_files(self.env), self._files("default"))

    def test_env_and_context_choose_the_configuration(self):
        _write(self.cfg / "active_config", "work\n")
        env = {**self.env, "CLOUDSDK_ACTIVE_CONFIG_NAME": "fromenv"}
        self.assertEqual(gcloud.state_files(env), self._files("fromenv"))
        self.assertEqual(
            gcloud.state_files(env, {"configuration": "fromflag"}), self._files("fromflag")
        )

    def test_name_outside_the_naming_rule_keeps_only_active_config(self):
        for raw in ("../work\n", b"wo\x00rk\n"):
            with self.subTest(raw=raw):
                _write(self.cfg / "active_config", raw)
                self.assertEqual(gcloud.state_files(self.env), self._files())

    def test_undecodable_active_config_falls_back_to_default(self):
        """読めない active_config はローカル読取と同じく `default` (`_local_config_name`)。"""
        _write(self.cfg / "active_config", b"\xff\xfe\n")
        self.assertEqual(gcloud.state_files(self.env), self._files("default"))

    def test_unknown_home_gives_no_files(self):
        with mock.patch.object(Path, "home", side_effect=RuntimeError("no home")):
            self.assertEqual(gcloud.state_files({}), [])

    def test_inline_home_decides_the_default_dir(self):
        self.assertNotEqual(os.environ["HOME"], "/vca-other-home")
        base = Path("/vca-other-home/.config/gcloud")
        self.assertEqual(
            gcloud.state_files({"HOME": "/vca-other-home"}),
            [base / "active_config", base / "configurations" / "config_default"],
        )


# 別プロセスで `gcloud.state_files` を呼ぶ (止まる形を timeout で FAIL にするため)。
_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from services import gcloud
env = {"CLOUDSDK_CONFIG": sys.argv[2], "HOME": sys.argv[3]}
if len(sys.argv) > 4:
    env.update(dict(a.split("=", 1) for a in sys.argv[4:]))
print(json.dumps([str(p) for p in gcloud.state_files(env)]))
"""


@unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO の無い OS")
class TestGcloudStateFilesFifo(_Tmp):
    """active_config が FIFO でも止まらない (止まると hook timeout で検証が飛ぶ)。

    `state_files` は cache を見る前に毎回走る。名前を active_config の中身から決める形では
    `cli_config.read_text` で開くので、その読みが止まらないことを固定する。
    """

    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "gcloud"
        (self.cfg / "configurations").mkdir(parents=True)
        os.mkfifo(self.cfg / "active_config")

    def _state_files(self, *extra):
        try:
            res = subprocess.run(
                [sys.executable, "-c", _PROBE, str(_PKG_DIR), str(self.cfg), os.environ["HOME"], *extra],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            self.fail("state_files が FIFO の active_config を開いて止まった")
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_fifo_active_config_does_not_block(self):
        files = self._state_files()
        self.assertGreaterEqual(len(files), 1)
        self.assertEqual(files[0], str(self.cfg / "active_config"))

    def test_fifo_with_env_that_skips_the_local_read(self):
        """0.34.0 がローカル読取を諦めて CLI に委ねていた形 (CLI の timeout で deny した)。"""
        for extra in ("CLOUDSDK_CORE_DISABLE_PROMPTS=1", "GOOGLE_CLOUD_PROJECT=p"):
            with self.subTest(extra=extra):
                files = self._state_files(extra)
                self.assertGreaterEqual(len(files), 1)
                self.assertEqual(files[0], str(self.cfg / "active_config"))

    def test_name_from_env_does_not_open_active_config(self):
        self.assertEqual(
            self._state_files("CLOUDSDK_ACTIVE_CONFIG_NAME=work"),
            [str(self.cfg / "active_config"), str(self.cfg / "configurations" / "config_work")],
        )


class _DispatchBase(_Tmp):
    GH_WRITE = "gh pr create --fill"
    GCLOUD_WRITE = "gcloud run deploy svc --image vca-probe-image"

    def setUp(self):
        super().setUp()
        self.project = self.tmp / "project"
        accounts = self.project / ".claude" / "cloud-account-guard" / "accounts.local.json"
        accounts.parent.mkdir(parents=True)
        accounts.write_text(
            json.dumps({"github": "work", "gcloud": "vca-probe-project", "aws": "123456789012"}),
            encoding="utf-8",
        )
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir(mode=0o700)
        self.gh_dir = self.tmp / "gh"
        self.gcloud_dir = self.tmp / "gcloud"
        patcher = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.project),
                "TMPDIR": str(cache_tmp),
                "GH_CONFIG_DIR": str(self.gh_dir),
                "CLOUDSDK_CONFIG": str(self.gcloud_dir),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_all(self, commands) -> list:
        return [dispatch(command, str(self.project)) for command in commands]


class TestDispatchWithGhHosts(_DispatchBase):
    def test_unchanged_hosts_file_keeps_the_cache_hit(self):
        _write(self.gh_dir / "hosts.yml", _hosts("work"))
        with mock.patch("services.github.verify", return_value=None) as verify:
            self.assertEqual(self.run_all([self.GH_WRITE] * 3), [None, None, None])
        self.assertEqual(verify.call_count, 1)

    def test_missing_hosts_file_keeps_the_cache_hit(self):
        """gh を env のトークンで使う構成など、hosts.yml が無い: 今までどおり hit する。"""
        self.assertFalse((self.gh_dir / "hosts.yml").exists())
        with mock.patch("services.github.verify", return_value=None) as verify:
            self.assertEqual(self.run_all([self.GH_WRITE] * 2), [None, None])
        self.assertEqual(verify.call_count, 1)

    def test_rewritten_hosts_file_is_verified_again(self):
        _write(self.gh_dir / "hosts.yml", _hosts("work"), age=100)
        with mock.patch("services.github.verify", return_value=None) as verify:
            dispatch(self.GH_WRITE, str(self.project))
            _write(self.gh_dir / "hosts.yml", _hosts("side"), age=50)
            dispatch(self.GH_WRITE, str(self.project))
            dispatch(self.GH_WRITE, str(self.project))
        self.assertEqual(verify.call_count, 2)

    def test_switch_outside_the_hook_is_denied(self):
        """チケットの再現: 成功の後にターミナルで切り替えると、次の書き込みは照合し直されて止まる。"""
        hosts = self.gh_dir / "hosts.yml"
        _write(hosts, _hosts("work"), age=100)
        self.assertIsNone(dispatch(self.GH_WRITE, str(self.project)))
        _write(hosts, _hosts("side"), age=50)
        with mock.patch(
            "services.github._fetch_active_accounts", return_value=({"github.com": "side"}, None)
        ):
            result = dispatch(self.GH_WRITE, str(self.project))
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(
            "GitHub [github.com] アカウント不一致: 現在=side, 期待=work — "
            "切り替え: gh auth switch --hostname github.com --user work",
            out["permissionDecisionReason"],
        )

    def test_hosts_file_written_just_now_is_not_cached(self):
        hosts = self.gh_dir / "hosts.yml"
        _write(hosts, _hosts("work"), age=0)
        with mock.patch("services.github.verify", return_value=None) as verify:
            self.assertEqual(self.run_all([self.GH_WRITE] * 2), [None, None])
            self.assertEqual(verify.call_count, 2)
            # 読まないだけでなく、書かない (指紋の無い entry を残さない)。
            self.assertEqual(sorted(cache.dir_path().glob("github-*.json")), [])
            _age(hosts, 100)
            self.assertEqual(self.run_all([self.GH_WRITE] * 2), [None, None])
        self.assertEqual(verify.call_count, 3)

    def test_state_files_that_raise_verify_every_time(self):
        """パスを決める処理の例外は握り、cache を使わずに照合する (dispatch の外に抜けない)。"""
        _write(self.gh_dir / "hosts.yml", _hosts("work"))
        with mock.patch(
            "services.github.state_files", side_effect=RuntimeError("boom")
        ), mock.patch("services.github.verify", return_value=None) as verify:
            try:
                results = self.run_all([self.GH_WRITE] * 2)
            except RuntimeError as e:
                self.fail(f"state_files の例外が dispatch の外に抜けた: {e!r}")
        self.assertEqual(results, [None, None])
        self.assertEqual(verify.call_count, 2)

    def test_switch_during_verification_is_not_recorded_as_the_new_state(self):
        """指紋は検証の前に取る: 検証の最中に切り替えられたら、次は照合し直す。"""
        hosts = self.gh_dir / "hosts.yml"
        _write(hosts, _hosts("work"), age=100)
        calls = []

        def verify_then_switch(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                _write(hosts, _hosts("side"), age=50)
            return None

        with mock.patch("services.github.verify", side_effect=verify_then_switch):
            self.assertEqual(self.run_all([self.GH_WRITE] * 2), [None, None])
        self.assertEqual(len(calls), 2)

    def test_inline_config_dir_is_the_one_fingerprinted(self):
        """行頭の `GH_CONFIG_DIR=...` の gh が読むのはその dir の hosts.yml。"""
        other = self.tmp / "gh-inline"
        _write(other / "hosts.yml", _hosts("work"), age=100)
        _write(self.gh_dir / "hosts.yml", _hosts("work"), age=100)
        command = f"GH_CONFIG_DIR={other} {self.GH_WRITE}"
        with mock.patch("services.github.verify", return_value=None) as verify:
            dispatch(command, str(self.project))
            _write(self.gh_dir / "hosts.yml", _hosts("side"), age=50)
            dispatch(command, str(self.project))
            self.assertEqual(verify.call_count, 1)
            _write(other / "hosts.yml", _hosts("side"), age=40)
            dispatch(command, str(self.project))
        self.assertEqual(verify.call_count, 2)

    def test_no_state_files_keeps_the_cache_hit(self):
        """HOME が決まらず state_files が空: 指紋は一定で、今までどおり hit する (毎回照合に落ちない)。"""
        with mock.patch("services.github.state_files", return_value=[]), mock.patch(
            "services.github.verify", return_value=None
        ) as verify:
            self.assertEqual(self.run_all([self.GH_WRITE] * 2), [None, None])
        self.assertEqual(verify.call_count, 1)

    def test_service_without_state_files_keeps_the_cache_hit(self):
        self.assertFalse(hasattr(aws, "state_files"))
        command = "aws s3 rm s3://vca-probe-bucket/key"
        with mock.patch("services.aws.verify", return_value=None) as verify:
            self.assertEqual(self.run_all([command] * 2), [None, None])
        self.assertEqual(verify.call_count, 1)


class TestDispatchWithGcloudConfig(_DispatchBase):
    def setUp(self):
        super().setUp()
        _write(self.gcloud_dir / "active_config", "work\n")
        for name in ("work", "side", "unrelated"):
            _write(
                self.gcloud_dir / "configurations" / f"config_{name}",
                "[core]\nproject = vca-probe-project\n",
            )

    def _rewrite(self, name: str, age: float) -> None:
        _write(
            self.gcloud_dir / "configurations" / f"config_{name}",
            "[core]\nproject = vca-other-project\n",
            age=age,
        )

    def test_switch_outside_the_hook_is_verified_again(self):
        with mock.patch("services.gcloud.verify", return_value=None) as verify:
            self.assertEqual(self.run_all([self.GCLOUD_WRITE] * 2), [None, None])
            self.assertEqual(verify.call_count, 1)
            # 使っていない構成の書き換えでは照合し直さない (速さを落とさない)。
            self._rewrite("unrelated", age=60)
            dispatch(self.GCLOUD_WRITE, str(self.project))
            self.assertEqual(verify.call_count, 1)
            # `gcloud config set project ...` (使っている構成のファイル)。
            self._rewrite("work", age=50)
            dispatch(self.GCLOUD_WRITE, str(self.project))
            self.assertEqual(verify.call_count, 2)
            # `gcloud config configurations activate side` (active_config)。
            _write(self.gcloud_dir / "active_config", "side\n", age=40)
            dispatch(self.GCLOUD_WRITE, str(self.project))
        self.assertEqual(verify.call_count, 3)

    def test_configuration_flag_decides_the_file(self):
        """`--configuration side` のコマンドは config_side で動く: 使っている構成の書き換えだけを見る。"""
        command = "gcloud --configuration side run deploy svc --image vca-probe-image"
        with mock.patch("services.gcloud.verify", return_value=None) as verify:
            dispatch(command, str(self.project))
            self._rewrite("work", age=50)
            dispatch(command, str(self.project))
            self.assertEqual(verify.call_count, 1)
            self._rewrite("side", age=40)
            dispatch(command, str(self.project))
        self.assertEqual(verify.call_count, 2)


_FAKE_GH = """\
import os
import sys

# ターミナルで打つ gh の代わり。`auth status` は状態ファイルのアカウントをアクティブとして出し、
# `auth switch` は本物の gh と同じく hosts.yml をその場で書き直す (同じ inode に O_TRUNC で書く)。
state = os.environ["VCA_FAKE_GH_STATE"]
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    with open(state, encoding="utf-8") as f:
        active = f.read().strip()
    lines = ["github.com"]
    for user in ("work", "side"):
        lines.append("  \\u2713 Logged in to github.com account " + user + " (keyring)")
        lines.append("  - Active account: " + ("true" if user == active else "false"))
    sys.stdout.buffer.write(("\\n".join(lines) + "\\n").encode("utf-8"))
    sys.exit(0)
if args[:2] == ["auth", "switch"] and "--user" in args:
    user = args[args.index("--user") + 1]
    with open(state, "w", encoding="utf-8") as f:
        f.write(user + "\\n")
    hosts = os.path.join(os.environ["GH_CONFIG_DIR"], "hosts.yml")
    with open(hosts, "w", encoding="utf-8") as f:
        f.write(
            "github.com:\\n    users:\\n        work:\\n        side:\\n"
            "    git_protocol: https\\n    user: " + user + "\\n"
        )
    sys.exit(0)
sys.stderr.write("fake gh: unsupported " + " ".join(args) + "\\n")
sys.exit(1)
"""


class TestTerminalSwitchEndToEnd(_Tmp):
    """実際の hook のプロセス: ターミナルでの `gh auth switch` (偽の gh が hosts.yml を書き換える) の後の
    書き込みは、成功 cache で通らず照合し直されて止まる。切り替えなければ今までどおり hit する。"""

    MISMATCH = (
        "GitHub [github.com] アカウント不一致: 現在=side, 期待=work — "
        "切り替え: gh auth switch --hostname github.com --user work"
    )

    def setUp(self):
        super().setUp()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        self.gh = bindir / "gh"
        self.gh.write_text(f"#!{sys.executable}\n{_FAKE_GH}", encoding="utf-8")
        self.gh.chmod(0o755)
        self.gh_dir = self.tmp / "ghcfg"
        self.hosts = self.gh_dir / "hosts.yml"
        _write(self.hosts, _hosts("work"), age=100)
        self.state = self.tmp / "gh-active"
        self.state.write_text("work\n", encoding="utf-8")
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        accounts = self.project / ".claude" / "cloud-account-guard" / "accounts.local.json"
        accounts.parent.mkdir(parents=True)
        accounts.write_text(json.dumps({"github": "work"}), encoding="utf-8")
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir(mode=0o700)
        home = self.tmp / "home"
        home.mkdir()
        self.env = _testutil.sanitized_env(
            {
                **os.environ,
                **_testutil.cli_config_env(self.tmp / "cliconfig", home),
                "GH_CONFIG_DIR": str(self.gh_dir),
                "TMPDIR": str(cache_tmp),
                "CLAUDE_PROJECT_DIR": str(self.project),
                "PATH": str(bindir),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        self.env["CLOUD_ACCOUNT_GUARD_DEBUG"] = "1"
        self.env["VCA_FAKE_GH_STATE"] = str(self.state)

    def _hook(self, command="gh pr create --fill") -> tuple[str, str, dict]:
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.project),
            "session_id": "vca-session",
        }
        res = subprocess.run(
            [sys.executable, str(_PKG_DIR)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            cwd=str(self.project),
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        traces = [line for line in res.stderr.splitlines() if line.startswith("{")]
        self.assertEqual(len(traces), 1, res.stderr)
        trace = json.loads(traces[0])
        if not res.stdout.strip():
            return "allow", "", trace
        out = json.loads(res.stdout)["hookSpecificOutput"]
        return out.get("permissionDecision", "context"), out.get("permissionDecisionReason", ""), trace

    def _terminal_switch(self, user: str) -> None:
        subprocess.run(
            [str(self.gh), "auth", "switch", "--hostname", "github.com", "--user", user],
            env=self.env,
            check=True,
            capture_output=True,
            timeout=60,
        )

    def test_without_a_switch_the_second_write_hits_the_cache(self):
        decision, _, first = self._hook()
        self.assertEqual((decision, first["cache_hit"]), ("allow", {}))
        decision, _, second = self._hook()
        self.assertEqual((decision, second["cache_hit"]), ("allow", {"github": True}))

    def test_write_after_a_terminal_switch_is_verified_and_denied(self):
        """切替から 2 秒以上経った後 (mtime を戻して再現) は、指紋の違いで照合し直す。"""
        decision, _, _ = self._hook()
        self.assertEqual(decision, "allow")
        self._terminal_switch("side")
        _age(self.hosts, 10)
        decision, reason, trace = self._hook()
        self.assertEqual((decision, trace["cache_hit"]), ("deny", {}))
        self.assertIn(self.MISMATCH, reason)

    def test_write_right_after_a_terminal_switch_is_verified_and_denied(self):
        decision, _, _ = self._hook()
        self.assertEqual(decision, "allow")
        self._terminal_switch("side")
        decision, reason, trace = self._hook()
        self.assertEqual((decision, trace["cache_hit"]), ("deny", {}))
        self.assertIn(self.MISMATCH, reason)

    def test_missing_or_unreadable_hosts_file_works_as_before(self):
        """hosts.yml が無い・読めない: ローカル読取に落ちず gh で照合し、2 回目は今までどおり hit する。"""
        cases = [("missing", lambda: self.hosts.unlink())]
        if not _IS_ROOT:
            cases.append(("unreadable", lambda: self.hosts.chmod(0)))
        for label, prepare in cases:
            with self.subTest(label=label):
                _write(self.hosts, _hosts("work"), age=100)
                self.hosts.chmod(0o600)
                prepare()
                self.addCleanup(lambda: self.hosts.exists() and self.hosts.chmod(0o600))
                cache_tmp = self.tmp / f"cache-{label}"
                cache_tmp.mkdir(mode=0o700)
                self.env["TMPDIR"] = str(cache_tmp)
                decision, _, first = self._hook()
                self.assertEqual((decision, first["cache_hit"]), ("allow", {}))
                decision, _, second = self._hook()
                self.assertEqual((decision, second["cache_hit"]), ("allow", {"github": True}))


if __name__ == "__main__":
    unittest.main()
