"""git の pointer (``.git`` ファイル / linked worktree の ``commondir``) の読みが止まらないこと (0.36.5)。

``_shared.patterns._read_git_pointer_file`` は 0.36.4 まで ``open(path, "r")`` で開いていたので、
書き手のいない FIFO だと open が書き手を待って戻らなかった。``load_patterns`` は毎回
``_project_section_keys`` → ``_main_repo_root`` で linked worktree の ``commondir`` を読むので、そこが
FIFO だと全ツール呼出で hook が止まり、Claude Code の hook timeout で打ち切られる (打ち切られた hook の
出力は捨てられ、ツール呼出は検証されずに通る)。Bash operand ごとの ``resolve_path_rule_root`` →
``_git_common_dir`` も同じ読みを通る。

0.36.5 からは ``O_NONBLOCK`` で開き、``fstat`` で通常のファイルと確かめてから読む。通常のファイルで
ないもの (FIFO・デバイス・ディレクトリ) は読まずに None = 今までの「読めない pointer」と同じ扱い。

止まる退行でも suite 全体を止めないよう、FIFO を読む呼出は別プロセスで timeout を付けて流す。
止まったら FAIL。例外が抜けたら、その型の名前を返して FAIL にする (ERROR にしない)。
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
from unittest import mock

from _testutil import _HOOKS_DIR

from _shared import patterns as P

# 別プロセスで式の列を順に評価し、結果を JSON の列で出す。例外は型の名前で返す (FAIL にするため)。
_PROBE = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from _shared import patterns as P
args = json.loads(sys.argv[3])
out = []
for expr in json.loads(sys.argv[2]):
    try:
        value = eval(expr, {"P": P, "Path": Path, "args": args, "os": os})
    except Exception as e:
        out.append({"raised": type(e).__name__})
    else:
        out.append({"value": repr(value)})
print(json.dumps(out))
"""

_PROBE_TIMEOUT = 10

_HAS_FIFO = hasattr(os, "mkfifo")
_HAS_PTY = hasattr(os, "openpty") and hasattr(os, "ttyname")


def _is_char_device(path: str) -> bool:
    try:
        return stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


def _make_worktree(tmp: Path) -> tuple[Path, Path, Path]:
    """``git worktree add`` 相当の構成 (test_patterns_loader の ``_make_worktree`` と同じ形)。

    Returns:
        ``(main_root, admin, worktree_root)``。admin は ``<main>/.git/worktrees/wt``
    """
    main_root = tmp / "main"
    admin = main_root / ".git" / "worktrees" / "wt"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    wt_root = main_root / ".claude" / "worktrees" / "wt"
    wt_root.mkdir(parents=True)
    (wt_root / ".git").write_text(f"gitdir: {admin}\n")
    return main_root, admin, wt_root


class _ProbeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        home = self.tmp / "home"
        home.mkdir()
        self.env = {
            k: v for k, v in os.environ.items()
            if k != "CLAUDE_PROJECT_DIR" and not k.startswith("GIT_")
        }
        self.env.update({"HOME": str(home), "USERPROFILE": str(home)})

    def probe(self, exprs: list[str], **args) -> list[dict]:
        try:
            res = subprocess.run(
                [sys.executable, "-c", _PROBE, str(_HOOKS_DIR), json.dumps(exprs), json.dumps(args)],
                capture_output=True, text=True, timeout=_PROBE_TIMEOUT, env=self.env,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"{exprs} が止まった (hook timeout で検証が飛ぶ)")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(len(out), len(exprs), res.stdout)
        return out

    def values(self, exprs: list[str], **args) -> list[str]:
        """式ごとの値の repr。例外が抜けたら FAIL。"""
        out = self.probe(exprs, **args)
        for expr, item in zip(exprs, out):
            self.assertIn("value", item, f"{expr} から例外が抜けた: {item}")
        return [item["value"] for item in out]

    def tty(self) -> str:
        """データの無い端末 (pty の slave) のパス。テストの間は master を開いたままにする。"""
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        return os.ttyname(slave)


class TestReadGitPointerFileNonRegular(_ProbeBase):
    """``_read_git_pointer_file`` は通常のファイルでないものを開いても読まずに None。"""

    def test_non_regular_is_none_without_waiting(self):
        cases = {"absent": str(self.tmp / "absent"), "dir": str(self.tmp)}
        if _HAS_FIFO:
            os.mkfifo(self.tmp / "fifo")
            cases["fifo"] = str(self.tmp / "fifo")
            os.symlink(self.tmp / "fifo", self.tmp / "link-to-fifo")
            cases["symlink-to-fifo"] = str(self.tmp / "link-to-fifo")
        if _is_char_device(os.devnull):
            cases["devnull"] = os.devnull
        if _is_char_device("/dev/zero"):
            # 0.36.4 は NUL を 4096 文字読んでいた (呼出側の結果は「main repo root なし」で同じ)
            cases["dev-zero"] = "/dev/zero"
        if _HAS_PTY:
            cases["tty"] = self.tty()
        labels = list(cases)
        got = self.values(
            [f"P._read_git_pointer_file(args[{label!r}])" for label in labels], **cases
        )
        self.assertEqual(dict(zip(labels, got)), {label: "None" for label in labels})

    def test_regular_file_is_read(self):
        """対照: 通常のファイル (と、それを指す symlink) は今までどおり読む。"""
        f = self.tmp / "commondir"
        f.write_text("../..\n")
        cases = {"file": str(f)}
        if _HAS_FIFO:  # POSIX (Windows の symlink は権限が要る)
            os.symlink(f, self.tmp / "link")
            cases["symlink"] = str(self.tmp / "link")
        labels = list(cases)
        got = self.values(
            [f"P._read_git_pointer_file(args[{label!r}])" for label in labels], **cases
        )
        self.assertEqual(dict(zip(labels, got)), {label: repr("../..\n") for label in labels})


@unittest.skipUnless(_HAS_FIFO, "FIFO は POSIX のみ")
class TestCommondirFifo(_ProbeBase):
    """linked worktree の ``commondir`` が FIFO: 読めない ``commondir`` と同じ結果を、止まらずに返す。

    「読めない」の対照には ``commondir`` をディレクトリにした形を使う (0.36.4 でも open が
    ``IsADirectoryError`` で失敗し、None 扱いになっていた形)。
    """

    _EXPRS = [
        "P._main_repo_root(args['wt'])",
        "P._git_common_dir(args['wt'])",
        "P._project_section_keys(args['wt'])",
        "P.load_patterns(Path(args['default']), cwd=args['wt'])",
        "P.resolve_path_rule_root(args['file'], args['main'])",
    ]

    def setUp(self):
        super().setUp()
        self.main, self.admin, self.wt = _make_worktree(self.tmp)
        self.file = self.wt / "certs" / "fixture.pem"
        self.file.parent.mkdir()
        self.file.write_text("dummy\n")
        self.default = self.tmp / "patterns.txt"
        self.default.write_text("*.pem\n")
        local = self.tmp / "home" / ".claude" / "sensitive-files-guardrail" / "patterns.local.txt"
        local.parent.mkdir(parents=True)
        local.write_text(f"[project:{self.main}]\n!fixture.pem\n")
        self.args = {
            "wt": str(self.wt), "main": str(self.main), "file": str(self.file),
            "default": str(self.default),
        }

    def _results(self) -> list[str]:
        return self.values(self._EXPRS, **self.args)

    def _replace_commondir(self, make) -> None:
        commondir = self.admin / "commondir"
        commondir.unlink()
        make(commondir)

    def test_regular_commondir_is_followed(self):
        """対照: 実物の形の worktree は main repo を辿る (fixture が辿れる形であることの確認)。"""
        self.assertEqual(self._results(), [
            repr(str(self.main)),
            repr(str(self.main / ".git")),
            repr([str(self.wt), str(self.main)]),
            repr([("*.pem", False), ("fixture.pem", True)]),
            repr(str(self.wt)),
        ])

    def _unreadable_results(self) -> list[str]:
        return [
            "None",
            repr(str(self.admin)),
            repr([str(self.wt)]),
            repr([("*.pem", False)]),
            repr(str(self.main)),
        ]

    def test_directory_commondir_is_unreadable(self):
        """「読めない commondir」の結果 (0.36.4 と同じ)。下の FIFO と同じ値になる基準。"""
        self._replace_commondir(lambda p: p.mkdir())
        self.assertEqual(self._results(), self._unreadable_results())

    def test_fifo_commondir_is_read_as_unreadable(self):
        self._replace_commondir(os.mkfifo)
        self.assertEqual(self._results(), self._unreadable_results())

    def test_symlink_to_fifo_commondir_is_read_as_unreadable(self):
        os.mkfifo(self.tmp / "fifo")
        self._replace_commondir(lambda p: os.symlink(self.tmp / "fifo", p))
        self.assertEqual(self._results(), self._unreadable_results())


@unittest.skipUnless(_HAS_FIFO, "FIFO は POSIX のみ")
class TestDotGitFifo(_ProbeBase):
    """``.git`` が FIFO: 呼出側の ``isfile`` で弾かれる (0.36.4 でも止まらない) が、
    ``_gitdir_pointer`` 自身も止まらない (``isfile`` の後に FIFO へ差し替わる形でも止まらない)。"""

    def test_gitdir_pointer_of_fifo_is_none(self):
        _main, _admin, wt = _make_worktree(self.tmp)
        (wt / ".git").unlink()
        os.mkfifo(wt / ".git")
        self.assertEqual(
            self.values(
                ["P._gitdir_pointer(args['g'])", "P._main_repo_root(args['wt'])",
                 "P._git_common_dir(args['wt'])"],
                g=str(wt / ".git"), wt=str(wt),
            ),
            ["None", "None", "None"],
        )


class TestRegularFileReadIsUnchanged(unittest.TestCase):
    """通常のファイルの読みは 0.36.4 の ``open(path, "r", encoding="utf-8", errors="replace")`` と同じ。

    改行の読み替え (``\\r\\n`` / ``\\r`` → ``\\n``) と、上限が**文字数**であること (byte 数ではない)。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _read(self, raw: bytes):
        path = self.tmp / "pointer"
        path.write_bytes(raw)
        try:
            return P._read_git_pointer_file(str(path))
        except Exception as e:  # noqa: BLE001
            self.fail(f"_read_git_pointer_file から {type(e).__name__} が抜けた")

    def test_newlines_are_translated(self):
        self.assertEqual(self._read(b"gitdir: /a\r\nx\ry\n"), "gitdir: /a\nx\ny\n")

    def test_limit_counts_characters(self):
        text = "あ" * (P._GIT_POINTER_READ_LIMIT + 10)
        self.assertEqual(self._read(text.encode("utf-8")), "あ" * P._GIT_POINTER_READ_LIMIT)

    def test_undecodable_bytes_are_replaced(self):
        self.assertEqual(self._read(b"gitdir: /a\xff\n"), "gitdir: /a�\n")

    def test_empty_is_none(self):
        self.assertIsNone(self._read(b""))

    def test_fstat_failure_is_none(self):
        """``fstat`` / 読みの OSError は「読めない」(0.36.4 も読みの OSError は None だった)。"""
        path = self.tmp / "pointer"
        path.write_text("../..\n")
        with mock.patch.object(P.os, "fstat", side_effect=OSError("mock")):
            try:
                got = P._read_git_pointer_file(str(path))
            except Exception as e:  # noqa: BLE001
                self.fail(f"_read_git_pointer_file から {type(e).__name__} が抜けた")
        self.assertIsNone(got)

    @unittest.skipUnless(os.path.isdir("/dev/fd"), "/dev/fd の無い OS")
    def test_fd_is_closed(self):
        """読めた形・読まない形のどちらでも fd を閉じる (hook は 1 回の呼出で何度も読む)。"""
        path = self.tmp / "pointer"
        path.write_text("../..\n")
        before = len(os.listdir("/dev/fd"))
        try:
            for _ in range(20):
                P._read_git_pointer_file(str(path))
                P._read_git_pointer_file(str(self.tmp))
        except Exception as e:  # noqa: BLE001
            self.fail(f"_read_git_pointer_file から {type(e).__name__} が抜けた")
        self.assertEqual(len(os.listdir("/dev/fd")), before)


if __name__ == "__main__":
    unittest.main()
