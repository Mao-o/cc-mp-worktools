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
from core.fs import has_project_markers, walk_files
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

    def test_fsproj_vbproj_glob_markers_match(self):
        # merge-review finding (round 3): PROJECT_MARKERS gained *.fsproj/
        # *.vbproj alongside *.csproj/*.sln, but the glob branch itself
        # (the "*" in marker check above) is generic -- this pins that a
        # solutionless F#/VB.NET repo's marker actually reaches it, not
        # just that the pattern string was added to the tuple.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.fsproj").write_text("<Project />\n")
            self.assertTrue(has_project_markers(root, ["*.fsproj"]))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.vbproj").write_text("<Project />\n")
            self.assertTrue(has_project_markers(root, ["*.vbproj"]))

    def test_slnx_glob_marker_matches(self):
        # merge-review finding (round 5): PROJECT_MARKERS gained *.slnx
        # (the newer XML solution format) alongside *.sln -- same pin as
        # the *.fsproj/*.vbproj case above, for the glob branch itself.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App.slnx").write_text("<Solution />\n")
            self.assertTrue(has_project_markers(root, ["*.slnx"]))

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


class WalkFilesTest(unittest.TestCase):
    """core/fs.py::walk_files() (the non-git file-walk fallback, used when
    the marker gate lets a non-git directory through) had zero test
    coverage: SKIP_DIRS filtering, dotfile/dotdir filtering, the
    respect_subgit nested-.git boundary (lines ~176-180), and the result
    limit were all unverified."""

    def test_finds_files_in_root_and_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("1\n")
            (root / "src").mkdir()
            (root / "src" / "b.py").write_text("2\n")
            found = walk_files(root, skip_dirs=())
            self.assertEqual(set(found), {"a.py", "src/b.py"})

    def test_skip_dirs_are_not_descended_into(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("1\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "pkg.js").write_text("2\n")
            found = walk_files(root, skip_dirs=("node_modules",))
            self.assertEqual(found, ["app.py"])

    def test_dotdirs_are_skipped_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("1\n")
            (root / ".hidden_dir").mkdir()
            (root / ".hidden_dir" / "x.py").write_text("2\n")
            found = walk_files(root, skip_dirs=())
            self.assertEqual(found, ["app.py"])

    def test_dotfiles_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("1\n")
            (root / ".env").write_text("SECRET=1\n")
            found = walk_files(root, skip_dirs=())
            self.assertEqual(found, ["app.py"])

    def test_nested_git_stops_descent_but_keeps_its_own_top_level_files(self):
        # respect_subgit=True (the default): a subdirectory that itself
        # contains a nested ".git" is treated as a sub-repo boundary --
        # os.walk still yields that directory's own top-level files, but
        # its dirnames are cleared so nothing beneath it (including files
        # further down, or the .git dir itself) is walked.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "top.py").write_text("1\n")
            nested = root / "vendor" / "sub_repo"
            nested.mkdir(parents=True)
            (nested / ".git").mkdir()
            (nested / "README.md").write_text("nested repo\n")
            deeper = nested / "src"
            deeper.mkdir()
            (deeper / "inner.py").write_text("2\n")
            found = walk_files(root, skip_dirs=())
            self.assertEqual(
                set(found), {"top.py", "vendor/sub_repo/README.md"}
            )
            self.assertNotIn("vendor/sub_repo/src/inner.py", found)

    def test_respect_subgit_false_walks_past_the_nested_git_boundary(self):
        # With respect_subgit=False, a nested ".git" is filtered out only
        # by the ordinary dot-prefix rule (like any other dotdir) -- the
        # rest of the sub-repo's tree (other subdirectories) is still
        # walked, unlike the True (default) case above.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "vendor" / "sub_repo"
            nested.mkdir(parents=True)
            (nested / ".git").mkdir()
            deeper = nested / "src"
            deeper.mkdir()
            (deeper / "inner.py").write_text("2\n")
            found = walk_files(root, skip_dirs=(), respect_subgit=False)
            self.assertEqual(found, ["vendor/sub_repo/src/inner.py"])

    def test_root_level_git_dir_is_always_skipped_regardless_of_respect_subgit(self):
        # dirpath == root_str for the root itself, so the nested-.git
        # special case never applies there -- root's own .git is filtered
        # by the plain dot-prefix rule either way.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("x\n")
            (root / "app.py").write_text("1\n")
            self.assertEqual(walk_files(root, skip_dirs=()), ["app.py"])
            self.assertEqual(
                walk_files(root, skip_dirs=(), respect_subgit=False), ["app.py"]
            )

    def test_limit_stops_at_the_requested_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(10):
                (root / f"f{i}.py").write_text("1\n")
            found = walk_files(root, skip_dirs=(), limit=3)
            self.assertEqual(len(found), 3)

    def test_default_limit_is_5000(self):
        import inspect

        sig = inspect.signature(walk_files)
        self.assertEqual(sig.parameters["limit"].default, 5000)

    def test_xcodeproj_bundle_internals_are_not_walked(self):
        # merge-review finding: *.xcodeproj/*.xcworkspace becoming a
        # PROJECT_MARKERS entry means a non-git Xcode-only root now passes
        # the marker gate and reaches walk_files directly. The bundle's
        # stem is project-specific ("App" here), so SKIP_DIRS (exact-name
        # matching) cannot express it -- this pins the suffix-based skip
        # instead, and that no files under the bundle leak into the result.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App" / "App.swift").parent.mkdir(parents=True)
            (root / "App" / "App.swift").write_text("struct App {}\n")
            bundle = root / "App.xcodeproj"
            (bundle / "project.xcworkspace" / "xcshareddata").mkdir(parents=True)
            (bundle / "project.pbxproj").write_text("// pbxproj\n")
            (bundle / "project.xcworkspace" / "contents.xcworkspacedata").write_text(
                "<Workspace/>\n"
            )
            found = walk_files(root, skip_dirs=())
            self.assertEqual(found, ["App/App.swift"])

    def test_xcworkspace_bundle_internals_are_not_walked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App" / "App.swift").parent.mkdir(parents=True)
            (root / "App" / "App.swift").write_text("struct App {}\n")
            bundle = root / "App.xcworkspace"
            bundle.mkdir()
            (bundle / "contents.xcworkspacedata").write_text("<Workspace/>\n")
            found = walk_files(root, skip_dirs=())
            self.assertEqual(found, ["App/App.swift"])


if __name__ == "__main__":
    unittest.main()
