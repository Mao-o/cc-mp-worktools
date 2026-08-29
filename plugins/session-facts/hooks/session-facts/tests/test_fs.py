"""非 UTF-8 ファイル名 / 非 UTF-8 stdout ターゲットに対する耐性テスト (v0.7):
core/git.py::git_ls_files の decode (surrogateescape -> replace) と、
cli.py::main() の stdout 強制 UTF-8 化 (backslashreplace)。

invalid-UTF-8 バイトを含む実ファイルは、このテスト環境 (macOS / APFS) では
作成できない (APFS はファイル名に有効な UTF-8 を要求し、そうでないバイト列は
OSError: Illegal byte sequence で拒否される。git ls-files -z が生の
バイト列を返す Linux 環境や、そういう履歴を持つ repo とは前提が異なる)。
そのため git_ls_files 単体のテストは subprocess.run の戻り値をモックして
「git が返した生バイト」を直接注入する。end-to-end テストは tracked_files
自体をモックすることで、実ファイル作成を経由せずに描画・出力経路を検証する。"""
from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)

from cli import main
from core.fs import has_project_markers
from core.git import git_ls_files


def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _make_repo(tmp) -> Path:
    root = Path(tmp)
    _git(["init", "-b", "main"], root)
    (root / "a.txt").write_text("1\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "first commit"], root)
    return root


class GitLsFilesNonUtf8Test(unittest.TestCase):
    """git_ls_files must not hand back a string containing a lone surrogate:
    that decodes fine (surrogateescape round-trips) but crashes any later
    strict-UTF-8 print of the rendered Markdown."""

    def test_invalid_utf8_byte_decodes_without_lone_surrogates(self):
        # `git ls-files -z` emits raw NUL-separated bytes; 0xE9 alone is not
        # valid UTF-8 (it is a continuation byte with no lead byte before
        # it). Fabricate exactly that stdout rather than creating a real
        # file with such a name — the current OS's filesystem (APFS)
        # enforces valid UTF-8 filenames and would reject the write outright,
        # but git itself treats filenames as opaque bytes and does not.
        fake_stdout = b"caf\xe9.txt\x00normal.txt\x00"
        fake_result = subprocess.CompletedProcess(
            args=["git", "ls-files", "-z"], returncode=0, stdout=fake_stdout, stderr=b"",
        )
        with mock.patch("core.git.subprocess.run", return_value=fake_result):
            files = git_ls_files(Path("/unused-because-subprocess-is-mocked"))

        self.assertEqual(len(files), 2)
        self.assertTrue(any(f.startswith("caf") and f.endswith(".txt") for f in files))
        for f in files:
            # Must not raise: surrogateescape's U+DCxx output fails this
            # under strict errors, which is exactly what a plain print() to
            # a UTF-8 stdout does. errors="replace" instead maps the
            # invalid byte to U+FFFD, always safely encodable.
            f.encode("utf-8")

    def test_git_failure_still_returns_empty_list(self):
        # Pre-existing behavior, pinned so the isolation added around this
        # decode doesn't accidentally change the non-zero-returncode path.
        fake_result = subprocess.CompletedProcess(
            args=["git", "ls-files", "-z"], returncode=128, stdout=b"", stderr=b"fatal: not a git repository",
        )
        with mock.patch("core.git.subprocess.run", return_value=fake_result):
            files = git_ls_files(Path("/unused-because-subprocess-is-mocked"))
        self.assertEqual(files, [])


class MainSurvivesHostileEncodingTest(unittest.TestCase):
    """End-to-end defense in depth: even if a lone surrogate reaches
    ctx.tracked_files (from any source — not just git_ls_files; the non-git
    walk_files() fallback inherits the same surrogateescape behavior from
    os.walk on POSIX), main() must still render and print successfully."""

    def test_lone_surrogate_in_tracked_files_does_not_crash_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            # Inject a lone surrogate directly into what summarize_repo()
            # sees as tracked_files, simulating any upstream source that
            # could still hand one back, independent of the git_ls_files
            # fix above.
            with mock.patch("cli.git_ls_files", return_value=["caf\udce9/x.py", "a.txt"]):
                raw = io.BytesIO()
                ascii_stdout = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
                try:
                    with redirect_stdout(ascii_stdout):
                        rc = main(["--root", str(root)])
                    ascii_stdout.flush()
                finally:
                    ascii_stdout.detach()  # keep the BytesIO readable after the wrapper closes

            self.assertEqual(rc, 0)
            decoded = raw.getvalue().decode("utf-8")
            self.assertIn("## Project Facts", decoded)

    def test_plain_tree_drawing_characters_do_not_crash_ascii_stdout(self):
        # Narrower repro matching the ticket's second scenario: no invalid
        # bytes anywhere, just an ASCII-only stdout target meeting the
        # ordinary (non-ASCII) box-drawing characters in ## Structure —
        # the same crash class under PYTHONIOENCODING=ascii, without any
        # surrogate involved at all.
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            (root / "dirA").mkdir()
            (root / "dirB").mkdir()
            (root / "dirA" / "a.py").write_text("a = 1\n")
            (root / "dirB" / "b.py").write_text("b = 1\n")
            _git(["add", "-A"], root)
            _git(["commit", "-m", "add two dirs"], root)

            raw = io.BytesIO()
            ascii_stdout = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
            try:
                with redirect_stdout(ascii_stdout):
                    rc = main(["--root", str(root)])
                ascii_stdout.flush()
            finally:
                ascii_stdout.detach()

            self.assertEqual(rc, 0)
            decoded = raw.getvalue().decode("utf-8")
            self.assertIn("## Structure", decoded)
            self.assertIn("dirA", decoded)


class HasProjectMarkersGlobTest(unittest.TestCase):
    """internal backlog P2-1: two PROJECT_MARKERS entries (*.csproj, *.tf)
    have no fixed filename, so has_project_markers() must also support
    glob patterns, not just a literal exists() check."""

    def test_glob_marker_matches_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MyApp.csproj").write_text("<Project />\n")
            self.assertTrue(has_project_markers(root, ["*.csproj"]))

    def test_glob_marker_does_not_match_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("x\n")
            self.assertFalse(has_project_markers(root, ["*.csproj", "*.tf"]))

    def test_literal_marker_still_uses_exists_check(self):
        # A non-glob entry must keep working exactly as before (a literal
        # relative-path exists() check, not accidentally glob-matched).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module x\n")
            self.assertTrue(has_project_markers(root, ["go.mod"]))
            self.assertFalse(has_project_markers(root, ["Cargo.toml"]))

    def test_directory_marker_matches_via_exists(self):
        # detectors/prisma.py checks a bare "prisma" directory, not a file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prisma").mkdir()
            self.assertTrue(has_project_markers(root, ["prisma"]))


if __name__ == "__main__":
    unittest.main()
