"""checker.find_sensitive_files / load_patterns の挙動テスト。

ローカル git repo を tmpdir に作り、tracked/untracked と .gitignore の組合せで
期待通り block 対象が列挙されることを確認する。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from checker import (  # noqa: E402
    _parse_patterns_text,
    find_sensitive_files,
    is_git_repo,
    load_patterns,
)


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _init_repo(cwd: str) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.name", "test"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "commit.gpgsign", "false"], cwd)


class BaseWithTmpRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # HOME と XDG を tmpdir に隔離 (ローカル patterns を汚染しない)
        self.home_dir = Path(self.tmp) / "home"
        self.xdg_dir = Path(self.tmp) / "xdg"
        self.home_dir.mkdir()
        self.xdg_dir.mkdir()
        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home_dir),
                "XDG_CONFIG_HOME": str(self.xdg_dir),
            },
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        _init_repo(str(self.repo))
        # plugin 内 patterns.txt を流用
        self.patterns_file = (
            Path(__file__).resolve().parent.parent / "patterns.txt"
        )

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel: str, content: str = "x\n") -> Path:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def _track(self, rel: str) -> None:
        _git(["add", rel], str(self.repo))
        _git(["commit", "-m", "add", rel], str(self.repo))


class TestIsGitRepo(BaseWithTmpRepo):
    def test_true_inside_repo(self):
        self.assertTrue(is_git_repo(str(self.repo)))

    def test_false_outside_repo(self):
        self.assertFalse(is_git_repo(self.tmp))


class TestFindSensitiveFiles(BaseWithTmpRepo):
    def test_tracked_env_blocked_even_if_gitignored(self):
        """#5 回帰: .env が tracked + .gitignore 済みでも block される。"""
        self._write(".env", "DATABASE_URL=x\n")
        self._track(".env")
        # 後から .gitignore に追加
        self._write(".gitignore", ".env\n")
        self._track(".gitignore")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {(r["path"], r["status"]) for r in result}
        self.assertIn((".env", "tracked"), paths)

    def test_tracked_env_production(self):
        self._write(".env.production", "SECRET=y\n")
        self._track(".env.production")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn(".env.production", paths)

    def test_untracked_env_production_blocked(self):
        self._write(".env.production", "SECRET=y\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {(r["path"], r["status"]) for r in result}
        self.assertIn((".env.production", "untracked"), paths)

    def test_example_file_excluded(self):
        self._write(".env.example", "FOO=bar\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertNotIn(".env.example", paths)

    def test_public_key_excluded(self):
        self._write("id_rsa.pub", "ssh-rsa AAA...\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertNotIn("id_rsa.pub", paths)

    def test_private_key_detected(self):
        self._write("id_rsa", "-----BEGIN...\n")
        self._write("foo.pem", "-----BEGIN...\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn("id_rsa", paths)
        self.assertIn("foo.pem", paths)

    def test_credentials_example_excluded(self):
        self._write("credentials.example.json", "{}\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertNotIn("credentials.example.json", paths)

    def test_gitignored_untracked_not_reported(self):
        """untracked なら .gitignore 済みは報告されない (ls-files --exclude-standard の働き)。"""
        self._write(".gitignore", ".env.production\n")
        self._track(".gitignore")
        self._write(".env.production", "SECRET=y\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertNotIn(".env.production", paths)

    def test_parts_match_parent_dir(self):
        """Step 3: Stop 側も parts 評価するようになった。

        親ディレクトリ名が機密 pattern に一致する場合、配下の任意ファイルも検出する。
        (``is_sensitive`` は basename → parts の順に last-match-wins)
        """
        self._write(".env/leak.txt", "secret\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn(".env/leak.txt", paths)

    def test_case_insensitive_by_default(self):
        """Step 3: 既定で case-insensitive。大文字の機密ファイル名も検出。"""
        self._write(".ENV", "KEY=v\n")
        self._write("ID_RSA", "-----BEGIN...\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn(".ENV", paths)
        self.assertIn("ID_RSA", paths)


class TestLocalPatternsLoader(BaseWithTmpRepo):
    def test_local_pattern_adds_new_rule(self):
        # ローカルに `*.foo` を足すと foo.foo が検出される
        local_dir = self.xdg_dir / "sensitive-files-guard"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "patterns.local.txt").write_text("*.foo\n")

        self._write("foo.foo", "x\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn("foo.foo", paths)

    def test_local_overrides_default_exclude(self):
        # 既定 !*.pub をローカル `*.pub` で打ち消す
        local_dir = self.xdg_dir / "sensitive-files-guard"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "patterns.local.txt").write_text("*.pub\n")

        self._write("id_rsa.pub", "ssh-rsa...\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn("id_rsa.pub", paths)

    def test_local_missing_returns_defaults_only(self):
        rules_with_default = load_patterns(self.patterns_file)
        # ローカルを置いたケースと比較
        local_dir = self.xdg_dir / "sensitive-files-guard"
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "patterns.local.txt").write_text("*.extra\n")
        rules_with_local = load_patterns(self.patterns_file)
        self.assertEqual(
            rules_with_local, rules_with_default + [("*.extra", False)]
        )


class TestSubmoduleScan(BaseWithTmpRepo):
    """Step 6: submodule 内 tracked ファイルも検出対象 (``--recurse-submodules``)。

    submodule 内 **untracked** は ``git ls-files --others`` の仕様上範囲外で、
    README の既知制限として明記されている。
    """

    def setUp(self):
        super().setUp()
        # subrepo を独立 repo として初期化
        self.subrepo = Path(self.tmp) / "subrepo"
        self.subrepo.mkdir()
        _init_repo(str(self.subrepo))
        (self.subrepo / ".env").write_text("SUB_SECRET=v\n")
        (self.subrepo / "README.md").write_text("# sub\n")
        _git(["add", ".env"], str(self.subrepo))
        _git(["commit", "-m", "add env"], str(self.subrepo))
        _git(["add", "README.md"], str(self.subrepo))
        _git(["commit", "-m", "add readme"], str(self.subrepo))

    def _try_add_submodule(self) -> bool:
        """親 repo に subrepo を submodule として登録。環境非対応なら False。"""
        try:
            subprocess.run(
                [
                    "git",
                    "-c", "protocol.file.allow=always",
                    "submodule", "add",
                    f"file://{self.subrepo}",
                    "submod",
                ],
                cwd=str(self.repo),
                check=True,
                capture_output=True,
            )
            _git(["commit", "-m", "add submod"], str(self.repo))
            return True
        except subprocess.CalledProcessError:
            return False

    def test_submodule_tracked_env_detected(self):
        if not self._try_add_submodule():
            self.skipTest("git submodule add unsupported in this env")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn("submod/.env", paths)

    def test_submodule_untracked_not_detected(self):
        """submodule 内 untracked は範囲外 (README 既知制限)。"""
        if not self._try_add_submodule():
            self.skipTest("git submodule add unsupported in this env")
        # submod 内 working copy に untracked ファイルを置く
        submod_work = self.repo / "submod"
        (submod_work / ".env.untracked").write_text("X=1\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertNotIn("submod/.env.untracked", paths)


class TestParsePatternsText(unittest.TestCase):
    def test_parse_mixed(self):
        text = "*.pem\n# comment\n\n!*.pub\n  !*.sample\n"
        self.assertEqual(
            _parse_patterns_text(text),
            [("*.pem", False), ("*.pub", True), ("*.sample", True)],
        )

    def test_parse_empty(self):
        self.assertEqual(_parse_patterns_text(""), [])
        self.assertEqual(_parse_patterns_text("\n\n# only comments\n"), [])


if __name__ == "__main__":
    unittest.main()
