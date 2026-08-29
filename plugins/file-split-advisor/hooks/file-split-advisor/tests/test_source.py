"""source.py: パス解決・早期 skip・安全な読み込みのテスト。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

import source


def _fake_macos_realpath(path: str) -> str:
    """macOS の ``/tmp`` / ``/var`` symlink (実体は ``/private/tmp`` /
    ``/private/var``) だけを模した ``os.path.realpath`` の fake。それ以外の
    パスは無変換で返す。実機 (macOS) の挙動を決定論的に再現し、CI/実行環境の
    違いに左右されないテストにするため (P1)。
    """
    for prefix in ("/tmp", "/var"):
        if path == prefix or path.startswith(prefix + "/"):
            return "/private" + path
    return path


class TestResolvePath(unittest.TestCase):
    def test_absolute_path_returned_as_is(self):
        result = source.resolve_path("/abs/path/foo.py", "/some/cwd")
        self.assertEqual(result, Path("/abs/path/foo.py"))

    def test_relative_path_joined_with_cwd(self):
        result = source.resolve_path("foo.py", "/some/cwd")
        self.assertEqual(result, Path("/some/cwd/foo.py"))

    def test_relative_path_without_cwd(self):
        result = source.resolve_path("foo.py", "")
        self.assertEqual(result, Path("foo.py"))


class TestIsUnderTempDir(unittest.TestCase):
    def test_slash_tmp_is_temp(self):
        self.assertTrue(source.is_under_temp_dir(Path("/tmp/foo.py")))

    def test_private_tmp_is_temp(self):
        self.assertTrue(source.is_under_temp_dir(Path("/private/tmp/scratchpad/foo.py")))

    def test_var_folders_is_temp(self):
        self.assertTrue(source.is_under_temp_dir(Path("/var/folders/xx/yyyy/T/foo.py")))

    def test_tmpdir_env_root_is_temp(self):
        with mock.patch.dict(os.environ, {"TMPDIR": "/custom/tmp-root"}):
            self.assertTrue(source.is_under_temp_dir(Path("/custom/tmp-root/sub/foo.py")))

    def test_project_path_is_not_temp(self):
        self.assertFalse(source.is_under_temp_dir(Path("/repo/src/foo.py")))

    def test_similar_prefix_is_not_falsely_matched(self):
        # "/tmpfoo" は "/tmp" 配下ではない (前方一致ではなくパスセグメント単位で
        # 判定するため誤検知しない)。
        self.assertFalse(source.is_under_temp_dir(Path("/tmpfoo/bar.py")))

    def test_realpath_resolved_var_folders_form_is_also_temp(self):
        # P1 face A: macOS では /var が /private/var の symlink (`ls -ld /var`
        # で確認できる)。$TMPDIR (mkdtemp 等) は resolved 形
        # (/private/var/folders/...) で渡ってくることがあるが、正規化前は
        # roots に /var/folders しか列挙しておらず検出できなかった。
        # 実機 (macOS) の実際の symlink 解決で確認する (mock を使わない)。
        self.assertTrue(
            source.is_under_temp_dir(Path("/private/var/folders/xx/yyyy/T/foo.py"))
        )


class TestRealpathAliasNormalization(unittest.TestCase):
    """P1: macOS の ``/tmp``/``/var`` symlink による表記揺れを realpath 正規化で
    吸収する。``os.path.realpath`` を fake に差し替え、ホスト環境の実際の
    symlink 構成に依存しない決定論的なテストにする。
    """

    def test_face_a_unresolved_root_matches_resolved_path_form(self):
        # face A: path が resolved 形 (/private/var/folders/...) で来ても、
        # 正規化前の roots (/var/folders) にしか一致していなかった経路。
        with mock.patch("os.path.realpath", side_effect=_fake_macos_realpath):
            self.assertTrue(
                source.is_under_temp_dir(Path("/private/var/folders/xx/T/foo.py"))
            )

    def test_face_b_cwd_resolved_path_unresolved_still_not_skipped(self):
        # face B: cwd が resolved 形 (/private/tmp/proj) で渡り、file_path が
        # unresolved 形 (/tmp/proj/foo.py) のとき、正規化前は
        # path.is_relative_to(Path(cwd)) が False になり「cwd の外の別の
        # 一時ディレクトリ」と誤判定して skip していた (本来は skip しては
        # いけない cwd 内のファイル)。
        with mock.patch("os.path.realpath", side_effect=_fake_macos_realpath):
            self.assertFalse(
                source.should_skip_temp_dir(
                    Path("/tmp/proj/checkout_flow.py"), "/private/tmp/proj"
                )
            )

    def test_face_b_reverse_alias_direction_still_not_skipped(self):
        # 逆方向: path が resolved 形、cwd が unresolved 形。
        with mock.patch("os.path.realpath", side_effect=_fake_macos_realpath):
            self.assertFalse(
                source.should_skip_temp_dir(
                    Path("/private/tmp/proj/checkout_flow.py"), "/tmp/proj"
                )
            )

    def test_is_outside_cwd_not_confused_by_alias(self):
        # FILE_SPLIT_ADVISOR_CWD_ONLY=1 用の is_outside_cwd も同じ alias で
        # 「cwd の外」と誤判定してはいけない。
        with mock.patch("os.path.realpath", side_effect=_fake_macos_realpath):
            self.assertFalse(
                source.is_outside_cwd(Path("/tmp/proj/foo.py"), "/private/tmp/proj")
            )


class TestShouldSkipTempDir(unittest.TestCase):
    def test_temp_path_skipped_when_cwd_is_real_project(self):
        self.assertTrue(
            source.should_skip_temp_dir(Path("/private/tmp/scratch/foo.py"), "/repo")
        )

    def test_temp_path_not_skipped_when_path_is_inside_temp_cwd(self):
        # session 全体が一時領域内 (ephemeral project) で、path がその cwd の
        # 内側にあるときは skip しない。
        self.assertFalse(
            source.should_skip_temp_dir(
                Path("/private/tmp/proj/foo.py"), "/private/tmp/proj"
            )
        )

    def test_sibling_temp_dir_outside_cwd_is_still_skipped(self):
        # cwd 自体は一時領域配下でも、path が cwd の外にある別の一時ディレクトリ
        # (兄弟プロジェクト) なら skip する。「cwd が一時領域かどうか」ではなく
        # 「path が cwd の内側かどうか」で判定する。
        self.assertTrue(
            source.should_skip_temp_dir(
                Path("/private/tmp/projB/foo.py"), "/private/tmp/projA"
            )
        )

    def test_non_temp_path_never_skipped(self):
        self.assertFalse(source.should_skip_temp_dir(Path("/repo/src/foo.py"), "/repo"))

    def test_empty_cwd_does_not_prevent_skip(self):
        self.assertTrue(source.should_skip_temp_dir(Path("/tmp/foo.py"), ""))


class TestIsOutsideCwd(unittest.TestCase):
    def test_path_inside_cwd_is_not_outside(self):
        self.assertFalse(source.is_outside_cwd(Path("/repo/src/foo.py"), "/repo"))

    def test_path_outside_cwd_is_outside(self):
        self.assertTrue(source.is_outside_cwd(Path("/other/foo.py"), "/repo"))

    def test_empty_cwd_never_outside(self):
        self.assertFalse(source.is_outside_cwd(Path("/other/foo.py"), ""))


class TestParseIgnoreGlobs(unittest.TestCase):
    def test_skips_blank_and_comment_lines(self):
        # サンプル pattern は実際に (絶対パスに対して) マッチする書き方を使う
        # (P3-1: 以前は "migrations/*" という anchored fnmatch では決して
        # マッチしない書き方が例として使われていた)。
        text = "\n".join(["# comment", "", "*.generated.py", "  ", "*/migrations/*"])
        self.assertEqual(
            source._parse_ignore_globs(text), ("*.generated.py", "*/migrations/*")
        )

    def test_empty_text_yields_no_patterns(self):
        self.assertEqual(source._parse_ignore_globs(""), ())

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(source._parse_ignore_globs("  test_*.py  \n"), ("test_*.py",))


class TestLoadIgnoreGlobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_ignore_file(self, content: str) -> Path:
        path = Path(self.tmp) / "ignore.local.txt"
        path.write_text(content)
        return path

    def test_merges_env_and_file(self):
        # P3-1: "*/legacy/*" (実際にマッチする書き方) をサンプルに使う。
        ignore_file = self._write_ignore_file("*/legacy/*\n# comment\n")
        patterns = source.load_ignore_globs("*.min.py, foo_*.py", ignore_file)
        self.assertEqual(patterns, ("*.min.py", "foo_*.py", "*/legacy/*"))

    def test_missing_file_is_ignored(self):
        patterns = source.load_ignore_globs("*.foo", Path(self.tmp) / "does-not-exist.txt")
        self.assertEqual(patterns, ("*.foo",))

    def test_empty_env_value_and_no_file(self):
        patterns = source.load_ignore_globs("", Path(self.tmp) / "does-not-exist.txt")
        self.assertEqual(patterns, ())

    def test_env_value_with_only_whitespace_entries_ignored(self):
        patterns = source.load_ignore_globs(" , ,", Path(self.tmp) / "does-not-exist.txt")
        self.assertEqual(patterns, ())

    def test_default_ignore_file_path(self):
        expected = Path.home() / ".claude" / "file-split-advisor" / "ignore.local.txt"
        self.assertEqual(source._default_ignore_file(), expected)

    def test_invalid_utf8_file_is_ignored_not_raised(self):
        # P2-2: UnicodeDecodeError は OSError のサブクラスではないため、
        # 修正前は except OSError だけでは捕まらず main() まで伝播していた。
        # ユーザーが書ける ~/.claude/file-split-advisor/ignore.local.txt が
        # 非UTF-8 だと plugin 全体が全プロジェクトで無言停止する経路 (fail-open
        # の約束が破れる)。env 側の patterns は生き残ることを確認する。
        path = Path(self.tmp) / "ignore.local.txt"
        path.write_bytes(b"\xff\xfe*.min.py\n")
        patterns = source.load_ignore_globs("*.foo", path)
        self.assertEqual(patterns, ("*.foo",))


class TestMatchesIgnoreGlob(unittest.TestCase):
    def test_matches_by_filename(self):
        self.assertTrue(source.matches_ignore_glob(Path("/repo/test_foo.py"), ("test_*.py",)))

    def test_matches_by_full_path(self):
        self.assertTrue(
            source.matches_ignore_glob(
                Path("/repo/migrations/0001_init.py"), ("*/migrations/*",)
            )
        )

    def test_no_match(self):
        self.assertFalse(
            source.matches_ignore_glob(Path("/repo/handler.py"), ("test_*.py",))
        )

    def test_empty_patterns_never_match(self):
        self.assertFalse(source.matches_ignore_glob(Path("/repo/handler.py"), ()))

    def test_relative_style_pattern_never_matches_absolute_path(self):
        # P3-1 (characterization test): fnmatch は完全一致 (anchored) であり、
        # 相対パス形に見えるパターン ("migrations/*") は絶対パスの途中にしか
        # 現れない文字列には決してマッチしない。ディレクトリを狙うには
        # "*/migrations/*" のように先頭に "*" を置く必要がある (README に明記)。
        # これは既存の (意図した) 挙動を固定するテストであり、バグ修正では
        # ない。
        self.assertFalse(
            source.matches_ignore_glob(
                Path("/repo/migrations/0001_init.py"), ("migrations/*",)
            )
        )
        self.assertTrue(
            source.matches_ignore_glob(
                Path("/repo/migrations/0001_init.py"), ("*/migrations/*",)
            )
        )


class TestShouldSkipByName(unittest.TestCase):
    def test_lockfiles_skipped(self):
        for name in (
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "Cargo.lock",
            "Pipfile.lock",
            "poetry.lock",
            "go.sum",
            "composer.lock",
        ):
            with self.subTest(name=name):
                self.assertTrue(source.should_skip_by_name(Path(f"/repo/{name}")))

    def test_minified_skipped(self):
        for name in ("app.min.js", "app.min.css", "app.js.map"):
            with self.subTest(name=name):
                self.assertTrue(source.should_skip_by_name(Path(f"/repo/{name}")))

    def test_generated_patterns_skipped(self):
        for name in (
            "foo.pb.go",
            "foo_pb2.py",
            "foo_pb2_grpc.py",
            "foo.g.dart",
            "foo.freezed.dart",
            "foo_generated.py",
        ):
            with self.subTest(name=name):
                self.assertTrue(source.should_skip_by_name(Path(f"/repo/{name}")))

    def test_normal_file_not_skipped(self):
        self.assertFalse(source.should_skip_by_name(Path("/repo/handler.py")))
        self.assertFalse(source.should_skip_by_name(Path("/repo/lockpicking.py")))


class TestLoadText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: str) -> Path:
        path = Path(self.tmp) / name
        path.write_text(content)
        return path

    def test_normal_file_loaded(self):
        path = self._write("foo.py", "a = 1\nb = 2\n")
        loaded = source.load_text(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.lines, ["a = 1", "b = 2"])
        self.assertEqual(loaded.text, "a = 1\nb = 2\n")

    def test_missing_file_returns_none(self):
        path = Path(self.tmp) / "does-not-exist.py"
        self.assertIsNone(source.load_text(path))

    def test_oversized_bytes_not_read(self):
        path = self._write("big.py", "x = 1\n")
        with mock.patch.object(Path, "read_text") as mock_read:
            loaded = source.load_text(path, max_bytes=1)
            self.assertIsNone(loaded)
            mock_read.assert_not_called()

    def test_oversized_line_count_returns_none(self):
        path = self._write("many-lines.py", "\n".join(f"x{i} = {i}" for i in range(50)))
        loaded = source.load_text(path, max_lines=10)
        self.assertIsNone(loaded)

    def test_within_limits_loaded(self):
        path = self._write("ok.py", "\n".join(f"x{i} = {i}" for i in range(10)))
        loaded = source.load_text(path, max_bytes=10_000, max_lines=10)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.lines), 10)

    def test_symlink_skipped(self):
        target = self._write("target.py", "a = 1\n")
        link = Path(self.tmp) / "link.py"
        os.symlink(target, link)
        self.assertIsNone(source.load_text(link))

    def test_directory_skipped(self):
        directory = Path(self.tmp) / "adir"
        directory.mkdir()
        self.assertIsNone(source.load_text(directory))

    def test_oserror_on_read_returns_none(self):
        path = self._write("foo.py", "a = 1\n")
        with mock.patch.object(Path, "read_text", side_effect=OSError("boom")):
            self.assertIsNone(source.load_text(path))


if __name__ == "__main__":
    unittest.main()
