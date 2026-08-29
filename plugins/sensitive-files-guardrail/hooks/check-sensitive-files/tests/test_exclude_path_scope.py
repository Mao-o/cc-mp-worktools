"""Stop hook で path 形 rule が効き、block reason のレシピが root 相対の path 形に
なること (0.24.0)。

- ``[project:<repo>]`` の ``!config/prod.pem`` で ``config/prod.pem`` だけが
  報告から外れ、``other/prod.pem`` は残る
- レシピは cwd に関わらず **root 相対** (サブディレクトリで発火しても
  ``!sub/.env``)。表示の file 一覧は従来どおり cwd 相対
- root を解決できない (``$HOME`` 直下の repo) / cwd が root 配下でない
  (``$CLAUDE_PROJECT_DIR`` が別ディレクトリ) ときは basename 形のみ
- root 相対で評価するので、サブディレクトリ cwd でも root 発火と同じ verdict
  (cwd と root の間のディレクトリ名も parts で見る)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from _shared.patterns import resolve_project_root
from checker import find_sensitive_files, load_patterns, root_offset

_LOCAL_REL = Path(".claude") / "sensitive-files-guardrail" / "patterns.local.txt"
_PATTERNS = Path(__file__).resolve().parent.parent / "patterns.txt"


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(cwd: str) -> None:
    _git(["init", "--initial-branch=main"], cwd)
    _git(["config", "user.name", "test"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "commit.gpgsign", "false"], cwd)


def _load_entry():
    import importlib.util

    entry_path = Path(__file__).resolve().parent.parent / "__main__.py"
    spec = importlib.util.spec_from_file_location("check_entry_path_scope", entry_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(hook_input: dict) -> str:
    entry = _load_entry()
    old_stdin, old_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(json.dumps(hook_input))
        sys.stdout = io.StringIO()
        rc = entry.main()
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
    assert rc == 0
    return out


def _reason(out: str) -> str:
    return json.loads(out)["reason"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_CONFIG_HOME": str(Path(self.tmp) / "xdg")},
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.repo = Path(self.tmp) / "repo"
        self.repo.mkdir()
        _init_repo(str(self.repo))

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel: str, content: str = "x\n") -> Path:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def _write_local(self, body: str, project: str | None = None) -> None:
        path = self.home / _LOCAL_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[project:{project or self.repo}]\n" + body)


class TestRootOffset(Base):
    def test_offsets(self):
        root = str(self.repo)
        self.assertEqual(root_offset(root, root), "")
        self.assertEqual(root_offset(root + "/", root), "")
        self.assertEqual(root_offset(str(self.repo / "a" / "b"), root), "a/b")
        self.assertIsNone(root_offset(self.tmp, root))
        self.assertIsNone(root_offset(root, None))
        self.assertIsNone(root_offset("", root))


class TestFindSensitiveFilesPathRule(Base):
    def test_path_rule_narrows_to_one_file(self):
        self._write("config/prod.pem")
        self._write("other/prod.pem")
        self._write("prod.pem")
        self._write_local("!config/prod.pem\n")
        rules = load_patterns(_PATTERNS, cwd=str(self.repo))
        root = resolve_project_root(str(self.repo))
        self.assertEqual(root, str(self.repo))
        paths = {r["path"] for r in find_sensitive_files(str(self.repo), rules, root=root)}
        self.assertEqual(paths, {"other/prod.pem", "prod.pem"})

    def test_without_root_the_rule_is_inert(self):
        self._write("config/prod.pem")
        self._write_local("!config/prod.pem\n")
        rules = load_patterns(_PATTERNS, cwd=str(self.repo))
        paths = {r["path"] for r in find_sensitive_files(str(self.repo), rules)}
        self.assertEqual(paths, {"config/prod.pem"})

    def test_subdirectory_cwd_matches_root_relative(self):
        # cwd=repo/config で発火: ls-files は `prod.pem` を返すが、root 相対
        # `config/prod.pem` として評価するので rule が効く
        self._write("config/prod.pem")
        self._write("config/other.pem")
        self._write_local("!config/prod.pem\n")
        cwd = str(self.repo / "config")
        rules = load_patterns(_PATTERNS, cwd=cwd)
        root = resolve_project_root(cwd)
        result = find_sensitive_files(cwd, rules, root=root)
        # 戻り値の path は cwd 相対のまま
        self.assertEqual({r["path"] for r in result}, {"other.pem"})

    def test_subdirectory_cwd_sees_directory_names_between_root_and_cwd(self):
        # 0.24.0: root 相対で parts を評価するので、cwd=repo/.env/sub の
        # ファイルも root 発火時と同じく検出される (0.23.0 までは cwd 相対の
        # `leak.txt` しか見えず、root で発火したときとの verdict が食い違っていた)
        self._write(".env/sub/leak.txt")
        cwd = str(self.repo / ".env" / "sub")
        rules = load_patterns(_PATTERNS, cwd=cwd)
        root = resolve_project_root(cwd)
        result = find_sensitive_files(cwd, rules, root=root)
        self.assertEqual({r["path"] for r in result}, {"leak.txt"})
        # root 不明なら従来どおり (cwd 相対だけを見る)
        self.assertEqual(find_sensitive_files(cwd, rules), [])

    def test_malformed_path_rule_does_not_abort_the_scan(self):
        # 逆順の文字範囲 (Codex R2 P2): Stop が例外で無言終了せず、報告が出る
        self._write("secrets/a.pem")
        self._write("config/prod.pem")
        self._write_local("!secrets/[z-a].pem\n!config/prod.pem\n")
        rules = load_patterns(_PATTERNS, cwd=str(self.repo))
        root = resolve_project_root(str(self.repo))
        paths = {r["path"] for r in find_sensitive_files(str(self.repo), rules, root=root)}
        self.assertEqual(paths, {"secrets/a.pem"})
        reason = _reason(_run_main({"cwd": str(self.repo), "session_id": "s10"}))
        self.assertIn("secrets/a.pem", reason)

    def test_directory_rule_excludes_descendants(self):
        self._write("fixtures/a.pem")
        self._write("fixtures/deep/b.pem")
        self._write("real.pem")
        self._write_local("!fixtures/\n")
        rules = load_patterns(_PATTERNS, cwd=str(self.repo))
        root = resolve_project_root(str(self.repo))
        paths = {r["path"] for r in find_sensitive_files(str(self.repo), rules, root=root)}
        self.assertEqual(paths, {"real.pem"})


class TestStopReasonRecipe(Base):
    def test_recipe_is_path_form_with_basename_alternative(self):
        self._write("sub/.env", "KEY=v\n")
        self._write("config/prod.pem")
        reason = _reason(_run_main({"cwd": str(self.repo), "session_id": "s1"}))
        self.assertIn("path 形 — 承認した 1 ファイルだけを外す", reason)
        self.assertIn("[project:$CLAUDE_PROJECT_DIR]", reason)
        self.assertIn("  !sub/.env", reason)
        self.assertIn("  !config/prod.pem", reason)
        # 並びはレシピと同じ (git ls-files の出力順)
        self.assertIn(
            "同名ファイルをすべて外したい場合だけ basename 形にする: `!prod.pem` / `!.env`",
            reason,
        )
        # 絶対パスは出さない
        self.assertNotIn(str(self.repo), reason)
        self.assertNotIn(str(self.home), reason)

    def test_recipe_is_root_relative_from_subdirectory(self):
        self._write("sub/deep/.env", "KEY=v\n")
        cwd = str(self.repo / "sub")
        reason = _reason(_run_main({"cwd": cwd, "session_id": "s2"}))
        # 表示は cwd 相対、レシピは root 相対
        self.assertIn("  - deep/.env", reason)
        self.assertIn("  !sub/deep/.env", reason)
        self.assertNotIn("  !deep/.env", reason)

    def test_recipe_from_reason_silences_only_that_file(self):
        """Stop が案内した path 形をそのまま書けば、次の Stop でそのファイル
        だけが消える (Stop のレシピと matcher の噛み合わせを固定)。"""
        self._write("config/prod.pem")
        self._write("other/prod.pem")
        reason = _reason(_run_main({"cwd": str(self.repo), "session_id": "s3"}))
        self.assertIn("  !config/prod.pem", reason)
        self._write_local("!config/prod.pem\n")
        reason2 = _reason(_run_main({"cwd": str(self.repo), "session_id": "s4"}))
        self.assertNotIn("config/prod.pem", reason2)
        self.assertIn("other/prod.pem", reason2)

    def test_env_project_dir_is_the_root_for_recipes(self):
        # monorepo 想定: $CLAUDE_PROJECT_DIR がサブディレクトリ。レシピはその
        # root 相対 (git toplevel 相対ではない) — Read / Edit / Bash と同じ基準
        self._write("pkg/a/.env", "KEY=v\n")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo / "pkg" / "a")
        reason = _reason(
            _run_main({"cwd": str(self.repo / "pkg" / "a"), "session_id": "s5"})
        )
        # root 直下なので先頭 / 付き (path 形を保つ)
        self.assertIn("  !/.env", reason)
        self.assertNotIn("!pkg/a/.env", reason)

    def test_root_level_file_recipe_is_path_form(self):
        """root 直下の `.env` は `!/.env` で案内する (Codex R1 P1)。`!.env` だと
        basename 形に化けて同名すべての保護が外れる。書けば root の `.env` だけが
        次の Stop から消え、`sub/.env` は残る。"""
        self._write(".env", "KEY=v\n")
        self._write("sub/.env", "KEY=v\n")
        reason = _reason(_run_main({"cwd": str(self.repo), "session_id": "s8"}))
        self.assertIn("  !/.env\n", reason)
        self.assertIn("  !sub/.env\n", reason)
        self.assertNotIn("  !.env\n", reason)
        self.assertIn("basename 形にする: `!.env`", reason)
        self._write_local("!/.env\n")
        reason2 = _reason(_run_main({"cwd": str(self.repo), "session_id": "s9"}))
        self.assertNotIn("  - .env\n", reason2)
        self.assertIn("  - sub/.env\n", reason2)

    def test_cwd_outside_project_dir_falls_back_to_basename_form(self):
        self._write("sub/.env", "KEY=v\n")
        os.environ["CLAUDE_PROJECT_DIR"] = str(Path(self.tmp) / "another")
        reason = _reason(_run_main({"cwd": str(self.repo), "session_id": "s6"}))
        self.assertIn("basename 形 — プロジェクト root を解決できない", reason)
        self.assertIn("  !.env", reason)
        self.assertNotIn("!sub/.env", reason)
        self.assertNotIn("同名ファイルをすべて外したい場合だけ", reason)

    def test_repo_at_home_has_no_root_and_uses_basename_form(self):
        # _resolve_project_key は $HOME 自体を project にしない → root None
        home_repo = self.home
        _init_repo(str(home_repo))
        (home_repo / "sub").mkdir()
        (home_repo / "sub" / ".env").write_text("KEY=v\n")
        reason = _reason(_run_main({"cwd": str(home_repo), "session_id": "s7"}))
        self.assertIn("basename 形 — プロジェクト root を解決できない", reason)
        self.assertIn("  !.env", reason)


if __name__ == "__main__":
    unittest.main()
