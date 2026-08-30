"""checker.find_sensitive_files / load_patterns の挙動テスト。

ローカル git repo を tmpdir に作り、tracked/untracked と .gitignore の組合せで
期待通り block 対象が列挙されることを確認する。
"""
from __future__ import annotations

import io
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
    in_submodule,
    is_git_repo,
    load_patterns,
    repo_context,
    submodule_paths,
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


class TestRepoContext(BaseWithTmpRepo):
    """``repo_context`` (0.19.0): toplevel と cwd prefix を 1 回の rev-parse で得る。"""

    def test_root_has_empty_prefix(self):
        ctx = repo_context(str(self.repo))
        self.assertIsNotNone(ctx)
        toplevel, prefix = ctx
        self.assertEqual(Path(toplevel).resolve(), self.repo.resolve())
        self.assertEqual(prefix, "")

    def test_subdirectory_prefix_has_trailing_slash(self):
        sub = self.repo / "sub" / "deep"
        sub.mkdir(parents=True)
        ctx = repo_context(str(sub))
        self.assertIsNotNone(ctx)
        toplevel, prefix = ctx
        self.assertEqual(Path(toplevel).resolve(), self.repo.resolve())
        self.assertEqual(prefix, "sub/deep/")

    def test_non_git_dir_is_none(self):
        self.assertIsNone(repo_context(self.tmp))


class TestGitFailureVisibility(BaseWithTmpRepo):
    """内部バックログ: git 呼出自体の失敗 (git 未インストール / 応答なし) を
    「機密ファイルなし」(cwd が git 管理外、または単に該当ファイルが無い) と
    区別して stderr に報告する。fail-open の挙動 (呼出元は空リストのまま扱う)
    自体は変えない。
    """

    def test_file_not_found_reports_git_unavailable(self):
        with mock.patch(
            "checker.subprocess.run", side_effect=FileNotFoundError("no git")
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            result = is_git_repo(self.tmp)
        self.assertFalse(result)
        self.assertIn(
            "git_unavailable: FileNotFoundError", fake_err.getvalue()
        )

    def test_timeout_reports_git_unavailable(self):
        with mock.patch(
            "checker.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10),
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            result = repo_context(str(self.repo))
        self.assertIsNone(result)
        self.assertIn("git_unavailable: TimeoutExpired", fake_err.getvalue())

    def test_non_repo_cwd_stays_silent(self):
        # 「cwd が git 管理外」は git 呼出の失敗ではなく正常系。誤って
        # git_unavailable を出してはいけない (可視性を足すだけで判定に
        # 影響しないことの回帰確認)。
        with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            result = repo_context(self.tmp)
        self.assertIsNone(result)
        self.assertNotIn("git_unavailable", fake_err.getvalue())

    def test_successful_call_stays_silent(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            result = repo_context(str(self.repo))
        self.assertIsNotNone(result)
        self.assertNotIn("git_unavailable", fake_err.getvalue())


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
    def _local_dir(self) -> Path:
        """0.6.0 の preferred パス (`~/.claude/sensitive-files-guardrail/`)。"""
        d = self.home_dir / ".claude" / "sensitive-files-guardrail"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_local_pattern_adds_new_rule(self):
        # ローカルに `*.foo` を足すと foo.foo が検出される
        (self._local_dir() / "patterns.local.txt").write_text("*.foo\n")

        self._write("foo.foo", "x\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn("foo.foo", paths)

    def test_local_overrides_default_exclude(self):
        # 既定 !*.pub をローカル `*.pub` で打ち消す
        (self._local_dir() / "patterns.local.txt").write_text("*.pub\n")

        self._write("id_rsa.pub", "ssh-rsa...\n")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn("id_rsa.pub", paths)

    def test_local_missing_returns_defaults_only(self):
        rules_with_default = load_patterns(self.patterns_file)
        # ローカルを置いたケースと比較
        (self._local_dir() / "patterns.local.txt").write_text("*.extra\n")
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


class TestNestedSubmoduleGuidancePaths(BaseWithTmpRepo):
    """P2-1 回帰: ネストした submodule (repo -> vendor -> vendor/deep) で
    ``submodule_paths`` / ``in_submodule`` が実際に `git rm --cached` の効く
    ディレクトリ (= 最も深い submodule) を返すこと。

    修正前は ``git ls-files --stage -z`` を cwd 直下でしか呼ばないため
    ``vendor/deep`` (孫 submodule) が集合に入らず、``in_submodule`` は
    ``vendor/deep/.env`` に対して ``vendor`` (親 repo からの `git rm --cached`
    が効かない、実際には無意味な案内) を返していた。
    """

    def setUp(self):
        super().setUp()
        self.leaf = Path(self.tmp) / "leafrepo"
        self.leaf.mkdir()
        _init_repo(str(self.leaf))
        (self.leaf / ".env").write_text("LEAF_SECRET=v\n")
        _git(["add", ".env"], str(self.leaf))
        _git(["commit", "-m", "add env"], str(self.leaf))

        self.mid = Path(self.tmp) / "midrepo"
        self.mid.mkdir()
        _init_repo(str(self.mid))
        (self.mid / "README.md").write_text("# mid\n")
        _git(["add", "README.md"], str(self.mid))
        _git(["commit", "-m", "init"], str(self.mid))

    def _try_add_nested_submodules(self, init_nested: bool = False) -> bool:
        """midrepo に leafrepo を `deep` として、repo に midrepo を `vendor`
        として登録する。``init_nested`` なら ``submodule update --init
        --recursive`` まで行い ``vendor/deep/.env`` を実ファイルとして展開する
        (gitlink だけなら ``submodule_paths`` の検証に十分で init 不要)。
        """
        try:
            subprocess.run(
                [
                    "git", "-c", "protocol.file.allow=always",
                    "submodule", "add", f"file://{self.leaf}", "deep",
                ],
                cwd=str(self.mid), check=True, capture_output=True,
            )
            _git(["commit", "-m", "add deep submodule"], str(self.mid))
            subprocess.run(
                [
                    "git", "-c", "protocol.file.allow=always",
                    "submodule", "add", f"file://{self.mid}", "vendor",
                ],
                cwd=str(self.repo), check=True, capture_output=True,
            )
            _git(["commit", "-m", "add vendor submodule"], str(self.repo))
            if init_nested:
                subprocess.run(
                    [
                        "git", "-c", "protocol.file.allow=always",
                        "submodule", "update", "--init", "--recursive",
                    ],
                    cwd=str(self.repo), check=True, capture_output=True,
                )
            return True
        except subprocess.CalledProcessError:
            return False

    def test_submodule_paths_includes_nested_gitlink(self):
        if not self._try_add_nested_submodules():
            self.skipTest("git submodule add unsupported in this env")
        paths = submodule_paths(str(self.repo))
        self.assertEqual(paths, {"vendor", "vendor/deep"})

    def test_in_submodule_returns_longest_match_for_nested_file(self):
        if not self._try_add_nested_submodules():
            self.skipTest("git submodule add unsupported in this env")
        paths = submodule_paths(str(self.repo))
        self.assertEqual(
            in_submodule("vendor/deep/.env", paths), "vendor/deep"
        )
        # vendor 直下のファイルは vendor (deep ではない) が返ること
        self.assertEqual(in_submodule("vendor/README.md", paths), "vendor")

    def test_symlinked_submodule_cycle_terminates_without_recursion_error(self):
        """壊れた/悪意ある submodule 構成が symlink で祖先を指す場合の防御
        (``_visited``) が実際に無限再帰を止めること。

        ``vendor/deep`` の checkout を ``vendor`` 自身への symlink に差し
        替える (git の index 上は ``deep`` が gitlink のまま変わらない)。
        ``os.path.realpath(vendor/deep)`` は ``os.path.realpath(vendor)``
        と一致し、``vendor`` は先行する再帰で既に visited 済みのため、
        ``submodule_paths`` はここで安全に止まる (RecursionError にならず、
        余分な要素も混入しない)。
        """
        import shutil

        if not self._try_add_nested_submodules(init_nested=True):
            self.skipTest("git submodule add unsupported in this env")
        vendor_dir = self.repo / "vendor"
        deep_dir = vendor_dir / "deep"
        shutil.rmtree(deep_dir)
        os.symlink(str(vendor_dir), str(deep_dir))
        paths = submodule_paths(str(self.repo))
        # 循環を検出して安全に止まり、素直な結果だけを返す (無限再帰も
        # 「vendor/deep/./」のような不正な要素の混入もしない)。
        self.assertEqual(paths, {"vendor", "vendor/deep"})

    def test_nested_submodule_tracked_env_detected_when_initialized(self):
        # 検出側 (--recurse-submodules) はネストも元々拾える前提の確認
        # (P2-1 は「案内先」のバグであり「検出漏れ」ではない)。
        if not self._try_add_nested_submodules(init_nested=True):
            self.skipTest("git submodule add unsupported in this env")
        rules = load_patterns(self.patterns_file)
        result = find_sensitive_files(str(self.repo), rules)
        paths = {r["path"] for r in result}
        self.assertIn("vendor/deep/.env", paths)


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
