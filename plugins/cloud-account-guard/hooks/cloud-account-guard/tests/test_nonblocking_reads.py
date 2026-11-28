"""読むと止まりうるファイル (書き手のいない FIFO・データの無い端末) を、hook の中の読みが待たずに扱う (v0.36.3)。

hook の中の読み取りには subprocess の timeout (検証時間の予算) が効かず、Claude Code の hook timeout で
打ち切られると出力が捨てられて検証が飛ぶ (公式仕様上の fail-open)。0.35.0 で gh / gcloud の読み
(`cli_config.read_text`) を止まらない読み方にした。0.36.3 で、同じ型の読みが残っていた 3 か所
(firebase の configstore・aws の config・linked worktree の gitdir と commondir) を同じ読み方にした。
どれも FIFO は「読めない設定」と同じに扱う (判定は 0.36.2 で読めなかったときと同じ)。

止まる退行でも suite 全体を止めないよう、読みは別プロセスで timeout を付けて呼ぶ。止まったら FAIL。
例外が抜けたら、その型の名前を返して FAIL にする (ERROR にしない)。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _testutil

from core import cli_config  # noqa: E402
from services import aws  # noqa: E402

_PKG_DIR = Path(__file__).resolve().parent.parent
_ACCOUNT = "123456789012"

# 別プロセスで式を評価し、結果を JSON で出す。例外は型の名前で返す (FAIL にするため)。
_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from core import cli_config, paths
from services import aws, firebase
args = json.loads(sys.argv[3])
names = {"cli_config": cli_config, "paths": paths, "aws": aws, "firebase": firebase, "Path": Path, "args": args}
try:
    value = eval(sys.argv[2], names)
except Exception as e:
    print(json.dumps({"raised": type(e).__name__, "oserror": isinstance(e, OSError)}))
else:
    print(json.dumps({"value": repr(value)}))
"""

_HAS_FIFO = hasattr(os, "mkfifo")
_HAS_PTY = hasattr(os, "openpty") and hasattr(os, "ttyname")


def _is_char_device(path: str) -> bool:
    try:
        return stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


class _ProbeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = _testutil.sanitized_env({**os.environ, "HOME": str(self.home)})

    def probe(self, expr: str, **args) -> dict:
        try:
            res = subprocess.run(
                [sys.executable, "-c", _PROBE, str(_PKG_DIR), expr, json.dumps(args)],
                capture_output=True, text=True, timeout=10, env=self.env,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"{expr} が止まった (hook timeout で検証が飛ぶ)")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.strip(), res.stderr)
        return json.loads(res.stdout)

    def value(self, expr: str, **args) -> str:
        """式の値の repr。例外が抜けたら FAIL。"""
        out = self.probe(expr, **args)
        self.assertIn("value", out, f"{expr} から例外が抜けた: {out}")
        return out["value"]

    def tty(self) -> str:
        """データの無い端末 (pty の slave) のパス。テストの間は master を開いたままにする。"""
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        return os.ttyname(slave)


class TestReadBoundedText(_ProbeBase):
    """firebase の configstore などを読む `read_bounded_text`。FIFO だけは読まない。"""

    @unittest.skipUnless(_HAS_FIFO, "FIFO の無い OS")
    def test_fifo_is_not_read(self):
        fifo = self.tmp / "store.json"
        os.mkfifo(fifo)
        self.assertEqual(
            self.probe("cli_config.read_bounded_text(args['p'])", p=str(fifo)),
            {"raised": "NotRegularFileError", "oserror": True},
        )

    @unittest.skipUnless(_HAS_PTY, "pty の無い OS")
    def test_tty_without_data_does_not_wait(self):
        self.assertEqual(
            self.probe("cli_config.read_bounded_text(args['p'])", p=self.tty()),
            {"raised": "BlockingIOError", "oserror": True},
        )

    @unittest.skipUnless(_is_char_device(os.devnull) and _is_char_device("/dev/zero"),
                         "/dev/null と /dev/zero の無い OS")
    def test_devices_are_read_as_before(self):
        """デバイスは 0.36.2 と同じく読む (configstore では「大きすぎる」と「読めない」で結果が違う)。"""
        self.assertEqual(self.value("cli_config.read_bounded_text(args['p'])", p=os.devnull), "''")
        self.assertEqual(
            self.probe("cli_config.read_bounded_text(args['p'])", p="/dev/zero"),
            {"raised": "FileTooLargeError", "oserror": False},
        )


class TestReadText(_ProbeBase):
    """gh / gcloud の設定を読む `read_text` (0.35.0) も `read_regular_bytes` で読む。読めないものは None で、例外を出さない。"""

    def test_unreadable_is_none(self):
        cases = {"absent": str(self.tmp / "absent"), "dir": str(self.tmp)}
        if _HAS_FIFO:
            os.mkfifo(self.tmp / "fifo")
            cases["fifo"] = str(self.tmp / "fifo")
        if _is_char_device(os.devnull):
            cases["devnull"] = os.devnull
        for label, path in cases.items():
            with self.subTest(label):
                self.assertEqual(self.value("cli_config.read_text(args['p'])", p=path), "None")


class TestReadRegularBytes(_ProbeBase):
    """aws の config と linked worktree のメタファイルを読む `read_regular_bytes`。通常のファイルだけを読む。"""

    def test_non_regular_is_not_read(self):
        cases = {"dir": str(self.tmp)}
        if _HAS_FIFO:
            os.mkfifo(self.tmp / "fifo")
            cases["fifo"] = str(self.tmp / "fifo")
        if _is_char_device(os.devnull):
            cases["devnull"] = os.devnull
        if _HAS_PTY:
            cases["tty"] = self.tty()
        for label, path in cases.items():
            with self.subTest(label):
                self.assertEqual(
                    self.probe("cli_config.read_regular_bytes(args['p'])", p=path),
                    {"raised": "NotRegularFileError", "oserror": True},
                )

    def test_regular_file_is_read_whole_or_up_to_limit(self):
        path = self.tmp / "big"
        data = b"x" * (cli_config.MAX_FILE_BYTES * 2)
        path.write_bytes(data)
        self.assertEqual(cli_config.read_regular_bytes(path), data)
        self.assertEqual(cli_config.read_regular_bytes(path, 10), b"x" * 10)


@unittest.skipUnless(_HAS_FIFO, "FIFO の無い OS")
class TestFirebaseConfigstoreFifo(_ProbeBase):
    """configstore が FIFO: 読めない configstore と同じ (切替先なし = `.firebaserc` の default)。

    0.36.2 までは `firebase use` の固定の判定 (期待値が未登録のとき) と、CLI が答えないときの
    ローカル設定の解決で、configstore を開いて止まっていた。
    """

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "proj"
        self.root.mkdir()
        (self.root / "firebase.json").write_text("{}\n", encoding="utf-8")
        (self.root / ".firebaserc").write_text(
            json.dumps({"projects": {"default": "p-default", "staging": "p-staging"}}), encoding="utf-8"
        )
        self.xdg = self.tmp / "xdg"
        self.store = self.xdg / "configstore" / "firebase-tools.json"
        self.store.parent.mkdir(parents=True)
        self.args = {"root": str(self.root), "env": {"XDG_CONFIG_HOME": str(self.xdg), "HOME": str(self.home)}}

    def _results(self) -> list[str]:
        return [
            self.value("firebase._from_configstore(args['root'], args['env'])", **self.args),
            self.value("firebase.is_pinned(args['env'], args['root'])", **self.args),
            self.value("firebase._from_local(args['root'], args['env'])", **self.args),
        ]

    def test_fifo_is_read_as_unreadable(self):
        # 対照: 通常のファイルの切替先は読む (fixture が読める形であることの確認)
        self.store.write_text(json.dumps({"activeProjects": {str(self.root): "staging"}}), encoding="utf-8")
        self.assertEqual(self._results(), ["'staging'", "True", "'p-staging'"])
        self.store.unlink()
        os.mkfifo(self.store)
        self.assertEqual(self._results(), ["''", "False", "'p-default'"])


@unittest.skipUnless(_HAS_FIFO, "FIFO の無い OS")
class TestAwsConfigFifo(_ProbeBase):
    """`AWS_CONFIG_FILE` が FIFO: 読めない config と同じ (profile が見つからない)。

    0.36.2 までは、CLI が答えなかったとき・不一致のときの deny の案内を作る途中で開いて止まっていた
    (aws CLI の botocore は通常のファイルでない config を「無い」と読むので、CLI は止まらずに答える)。
    """

    def test_fifo_is_read_as_unreadable(self):
        cfg = self.tmp / "config"
        env = {"AWS_CONFIG_FILE": str(cfg), "HOME": str(self.home)}
        cfg.write_text(f"[profile prod]\nsso_account_id = {_ACCOUNT}\n", encoding="utf-8")
        self.assertEqual(
            self.value("aws.profiles_for_account(args['a'], args['env'])", a=_ACCOUNT, env=env), "['prod']"
        )
        cfg.unlink()
        os.mkfifo(cfg)
        self.assertEqual(
            self.value("aws.profiles_for_account(args['a'], args['env'])", a=_ACCOUNT, env=env), "[]"
        )
        guidance = self.value(
            "aws._switch_guidance(args['a'], args['env'], include_configure=True)", a=_ACCOUNT, env=env
        )
        self.assertIn("AWS_PROFILE=<profile> aws", guidance)


class TestAwsConfigRead(unittest.TestCase):
    """aws の config の読み方を変えても、通常のファイルの結果は 0.36.2 と同じ。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        self.cfg = d / "config"

    def _profiles(self) -> list[str]:
        try:
            return aws.profiles_for_account(_ACCOUNT, {"AWS_CONFIG_FILE": str(self.cfg)})
        except Exception as e:  # noqa: BLE001
            self.fail(f"profiles_for_account から {type(e).__name__} が抜けた (hook は検証をスキップする)")

    def test_no_size_limit(self):
        """大きさの上限は置かない (0.36.2 と同じ)。上限の後ろにある profile も読む。"""
        self.cfg.write_bytes(
            b"# " + b"x" * (cli_config.MAX_FILE_BYTES + 10) + b"\n"
            + f"[profile prod]\nsso_account_id = {_ACCOUNT}\n".encode()
        )
        self.assertEqual(self._profiles(), ["prod"])

    def test_line_endings(self):
        """0.36.2 は改行を変換するテキストとして読んでいた。CRLF と CR だけの改行も同じに読む。"""
        text = f"[profile prod]\nsso_account_id = {_ACCOUNT}\n[profile other]\nsso_account_id = 1\n"
        for label, sep in {"crlf": "\r\n", "cr": "\r"}.items():
            with self.subTest(label):
                self.cfg.write_bytes(text.replace("\n", sep).encode())
                self.assertEqual(self._profiles(), ["prod"])

    def test_undecodable_is_empty(self):
        # 不正なバイトは解析に効かないコメント行に置く (見出しに置くと、置換して読んでも profile に
        # ならず、strict な decode と区別できない)
        self.cfg.write_bytes(b"# \xff\xfe\n[profile x]\nsso_account_id = " + _ACCOUNT.encode() + b"\n")
        self.assertEqual(self._profiles(), [])


@unittest.skipUnless(_HAS_FIFO, "FIFO の無い OS")
class TestWorktreeMetaFifo(_ProbeBase):
    """linked worktree の gitdir / commondir が FIFO: 読めないメタファイルと同じ (裏付けが取れない = 境界)。

    0.36.2 までは accounts.local.json の探索の途中で開いて止まっていた (gh などすべての service の
    検証の前に走る)。
    """

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "ws" / "repo"
        (self.repo / ".git").mkdir(parents=True)
        accounts = self.repo / ".claude" / "cloud-account-guard"
        accounts.mkdir(parents=True)
        (accounts / "accounts.local.json").write_text('{"github": "u"}', encoding="utf-8")
        self.worktree = self.repo / ".worktrees" / "wt"
        self.worktree.mkdir(parents=True)
        self.meta = self.repo / ".git" / "worktrees" / "wt"
        self.meta.mkdir(parents=True)
        (self.worktree / ".git").write_text(f"gitdir: {self.meta}\n", encoding="utf-8")
        (self.meta / "gitdir").write_text(f"{self.worktree / '.git'}\n", encoding="utf-8")
        (self.meta / "commondir").write_text("../..\n", encoding="utf-8")
        (self.meta / "HEAD").write_text("ref: refs/heads/wt\n", encoding="utf-8")

    def _resolved(self) -> str:
        return self.value(
            "str(paths.discover_accounts_files_with_ancestors(args['d'])[1])", d=str(self.worktree)
        )

    def _check(self, name: str):
        # 対照: 実物の形の worktree は親 repo の accounts.local.json を継承する
        self.assertEqual(self._resolved(), repr(str(self.repo)))
        (self.meta / name).unlink()
        os.mkfifo(self.meta / name)
        self.assertEqual(self._resolved(), repr("None"))

    def test_fifo_back_pointer_is_read_as_unreadable(self):
        self._check("gitdir")

    def test_fifo_commondir_is_read_as_unreadable(self):
        self._check("commondir")


if __name__ == "__main__":
    unittest.main()
