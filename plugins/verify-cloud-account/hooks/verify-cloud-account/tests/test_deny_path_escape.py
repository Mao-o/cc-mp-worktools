"""deny 文面に出す起動ディレクトリ由来のパスは、制御文字をエスケープして 1 行に収める (v0.21.1)。

途中のディレクトリ名はリポジトリ側が決められる (clone しただけのリポジトリでも、改行や
端末の制御シーケンスを含む名前のディレクトリを置ける)。期待値ファイル・起動リポジトリ・親ディレクトリ・
グローバル既定のパスを deny 文面にそのまま出すと、パスの外に偽の行 (指示のように見える行) を
差し込める。0.21.0 で `(検出コマンド: ...)` などは直したが、パスの表示が残っていた。

fixture は、ディレクトリ名に改行 + 偽の行 + ESC を含める。各 case は、その deny 文面の
(1) 偽の行が行頭に現れない (2) エスケープした形 (`\\n` / `\\x1b`) で示される (3) ESC を
含まない、を確かめる。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import paths, shell_word  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

_FAKE = "FAKE_LINE_9f3"
# 改行の後に偽の行、さらに端末の制御シーケンス (ESC) を含むディレクトリ名。
_EVIL = f"evil\n{_FAKE}\x1b[31m"
_ESCAPED = f"evil\\n{_FAKE}\\x1b[31m"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = Path(self.tmp)
        isolation = _testutil.start_isolation(self.root / "cliconfig", self.root / "home")
        self.addCleanup(isolation.stop)
        cache = self.root / "cache_tmp"
        cache.mkdir()
        self.project = self.root / _EVIL
        self.project.mkdir()
        self._launch(self.project)
        # このクラスの検証対象は文面だけ。cache は隔離した tmp に置く
        env = mock.patch.dict(os.environ, {"TMPDIR": str(cache)})
        env.start()
        self.addCleanup(env.stop)

    def _launch(self, project: Path) -> None:
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(project)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _accounts(self, base: Path, data, name="verify-cloud-account/accounts.local.json"):
        path = base / ".claude" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def _reason(self, command: str, cwd=None) -> str:
        result = dispatch(command, str(cwd or self.project))
        self.assertIsNotNone(result, "deny になるはずの入力が通った")
        out = result["hookSpecificOutput"]
        return out.get("permissionDecisionReason") or out.get("additionalContext", "")

    def _assert_one_line(self, reason: str) -> None:
        """偽の行が行頭に出ず、エスケープした形で示され、ESC が残らない。"""
        for line in reason.splitlines():
            self.assertFalse(
                line.lstrip().startswith(_FAKE), f"偽の行が差し込まれた: {line!r}\n---\n{reason}"
            )
        self.assertIn(_ESCAPED, reason)
        self.assertNotIn("\x1b", reason)


class TestAccountsPathInDeny(_Base):
    """期待値ファイルのパス (`accounts_path`) を出す文面。"""

    def test_malformed_json(self):
        self._accounts(self.project, "{not json")
        self._assert_one_line(self._reason("gh pr create"))

    def test_not_utf8(self):
        self._accounts(self.project, b'{"github": "x\xff"}')
        self._assert_one_line(self._reason("gh pr create"))

    def test_too_deep_to_parse(self):
        self._accounts(self.project, '{"github": "x", "pad": ' + "[" * 100000 + "]" * 100000 + "}")
        self._assert_one_line(self._reason("gh pr create"))

    def test_deeper_than_the_limit(self):
        self._accounts(
            self.project,
            '{"github": {"a": "x", "pad": ' + "[" * 40 + "]" * 40 + "}}",
        )
        self._assert_one_line(self._reason("gh pr create"))

    def test_read_failure_escapes_both_path_and_os_error(self):
        """読み込みの OSError は、パスも例外の文面 (`strerror`) もエスケープする。"""
        self._accounts(self.project, {"github": "x"})
        real = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == "accounts.local.json":
                raise PermissionError(13, f"denied\n{_FAKE}_E")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_text):
            reason = self._reason("gh pr create")
        self._assert_one_line(reason)
        self.assertIn(f"denied\\n{_FAKE}_E", reason)
        self.assertNotIn(f"\n{_FAKE}_E", reason)

    def test_not_an_object(self):
        self._accounts(self.project, "[]")
        self._assert_one_line(self._reason("gh pr create"))

    def test_entry_of_the_wrong_type(self):
        self._accounts(self.project, {"github": 5})
        self._assert_one_line(self._reason("gh pr create"))

    def test_unregistered_key(self):
        self._accounts(self.project, {"aws": "123456789012"})
        self._assert_one_line(self._reason("gh pr create"))


class TestPlacementPathsInDeny(_Base):
    """配置パスの競合・stat できない配置パスを出す文面 (`paths.describe_unstattable` を含む)。"""

    def test_conflicting_paths_and_the_rm_guidance(self):
        self._accounts(self.project, {"github": "a"})
        legacy = self._accounts(self.project, {"github": "b"}, "accounts.json")
        reason = self._reason("gh pr create")
        self.assertIn("複数のパスに accounts.local.json が存在します", reason)
        self._assert_one_line(reason)
        # 制御文字を含むパスは、rm のコマンドの形では案内しない (shlex.quote は改行を残す)
        self.assertNotIn("rm ", reason)
        self.assertIn("コマンドの形では案内しません", reason)
        # builder の migrate と同じ注記 (定数を共有する)
        self.assertIn(shell_word.NOT_COMMAND_FORM_REMOVE, reason)
        self.assertNotIn(str(legacy), reason)

    def test_rm_guidance_is_kept_for_a_plain_path_with_a_space(self):
        """制御文字を含まないパスの rm の案内 (quote つき) は変わらない。"""
        project = self.root / "my project"
        project.mkdir()
        self._launch(project)
        self._accounts(project, {"github": "a"})
        legacy = self._accounts(project, {"github": "b"}, "accounts.json")
        reason = self._reason("gh pr create", project)
        self.assertIn(f"rm '{legacy.resolve()}'", reason)

    def test_unstattable_placement_path(self):
        self._accounts(self.project, {"github": "a"})
        legacy = self.project / ".claude" / "accounts.json"
        os.symlink("a" * 300, legacy)  # 1 要素が 255 バイトを超える → stat が ENAMETOOLONG
        self.assertIsNotNone(paths.stat_failure(legacy))
        reason = self._reason("gh pr create")
        self.assertIn("配置パスを確かめられません", reason)
        self._assert_one_line(reason)


class TestLaunchRootInDeny(_Base):
    """起動リポジトリのルート (`root`) を出す文面。"""

    def test_outside_error_when_configured(self):
        self._accounts(self.project, {"github": "a"})
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        reason = self._reason(f"cd {elsewhere} && gh pr create")
        self.assertIn("起動したリポジトリ", reason)
        self._assert_one_line(reason)

    def test_outside_error_when_nothing_is_configured(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        reason = self._reason(f"cd {elsewhere} && gh pr create")
        self.assertIn("起動したリポジトリ", reason)
        self._assert_one_line(reason)


class TestAncestorAndGlobalInDeny(_Base):
    """親ディレクトリ (`resolved_dir`) とグローバル既定のパスを出す文面。"""

    def test_ancestor_note(self):
        child = self.project / "worktree-branch"
        child.mkdir()
        self._launch(child)
        self._accounts(self.project, {"github": "a"})
        with mock.patch("services.github.verify", return_value="GitHub 不一致"):
            reason = self._reason("gh pr create", child)
        self.assertIn("親ディレクトリ", reason)
        self._assert_one_line(reason)

    def _evil_home(self) -> Path:
        home = self.root / _EVIL / "home"
        home.mkdir(parents=True)
        patcher = mock.patch.object(Path, "home", staticmethod(lambda: home))
        patcher.start()
        self.addCleanup(patcher.stop)
        return home

    def test_global_default_note(self):
        home = self._evil_home()
        self._accounts(home, {"github": "a"})
        # プロジェクトは evil の外に置く (グローバル既定を採るのは、プロジェクトに何も無いとき)
        project = self.root / "plain"
        project.mkdir()
        self._launch(project)
        with mock.patch("services.github.verify", return_value="GitHub 不一致"):
            reason = self._reason("gh pr create", project)
        self.assertIn("グローバル既定", reason)
        self._assert_one_line(reason)

    def test_unconfigured_deny_names_the_global_path(self):
        self._evil_home()
        project = self.root / "plain"
        project.mkdir()
        self._launch(project)
        reason = self._reason("gh pr create", project)
        self.assertIn("全プロジェクト共通の既定にするには", reason)
        self._assert_one_line(reason)


if __name__ == "__main__":
    unittest.main()
