"""path 形 rule が Read / Edit / Bash の 3 handler で効き、deny reason の除外案内が
path 形を既定にすること (0.24.0)。

``patterns.local.txt`` の ``[project:<root>]`` セクションに ``!config/prod.pem``
を書いた状態で:

- ``<root>/config/prod.pem`` は 3 handler とも allow (承認した 1 ファイル)
- ``<root>/other/prod.pem`` / ``<root>/prod.pem`` は従来どおり deny
  (basename 形 ``!prod.pem`` なら全部 allow になっていた)
- deny reason は ``!other/prod.pem`` (path 形) を既定に、``!prod.pem``
  (basename 形) を併記する
- root 不明 (非 git かつ ``$CLAUDE_PROJECT_DIR`` 未設定) では path 形 rule は
  効かず、案内も basename 形のみ (0.23.0 までと同じ)

root は ``$CLAUDE_PROJECT_DIR`` で固定する (hook の実環境と同じ経路。テストの
tmpdir は git repo ではないので、これが無いと root は None)。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import output
from handlers import bash_handler, edit_handler, read_handler

_LOCAL_REL = Path(".claude") / "sensitive-files-guardrail" / "patterns.local.txt"


def _decision(resp: dict) -> str | None:
    """allow は空 dict (``make_allow``) なので "allow" に正規化して比較しやすくする。"""
    if output.is_allow(resp):
        return "allow"
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecision")


def _reason(resp: dict) -> str:
    hook = resp.get("hookSpecificOutput") or {}
    return hook.get("permissionDecisionReason") or ""


class BaseProjectScoped(unittest.TestCase):
    """HOME を隔離し、``$CLAUDE_PROJECT_DIR`` を tmp の ``proj`` に固定する。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        self.root = Path(self.tmp) / "proj"
        self.root.mkdir()
        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(Path(self.tmp) / "xdg"),
                "CLAUDE_PROJECT_DIR": str(self.root),
            },
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        os.environ.pop("SFG_CASE_SENSITIVE", None)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_local(self, body: str, project: str | None = None) -> None:
        """``patterns.local.txt`` を書く。``project`` (既定: root) のセクション配下。"""
        path = self.home / _LOCAL_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"[project:{project or self.root}]\n"
        path.write_text(header + body)

    def _file(self, rel: str, content: str = "SECRET=x\n") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p


# -- Read --------------------------------------------------------------------


class TestReadHandlerPathRule(BaseProjectScoped):
    def _read(self, path: Path, cwd: str | None = None) -> dict:
        return read_handler.handle({
            "tool_name": "Read",
            "tool_input": {"file_path": str(path)},
            "cwd": cwd or str(self.root),
            "permission_mode": "default",
        })

    def test_excluded_path_is_allowed_and_siblings_stay_denied(self):
        self._write_local("!config/prod.pem\n")
        approved = self._file("config/prod.pem")
        other = self._file("other/prod.pem")
        top = self._file("prod.pem")
        self.assertEqual(_decision(self._read(approved)), "allow")
        self.assertEqual(_decision(self._read(other)), "deny")
        self.assertEqual(_decision(self._read(top)), "deny")

    def test_relative_file_path_from_subdirectory(self):
        # file_path が相対でも cwd で絶対化してから root 相対にする
        self._write_local("!config/prod.pem\n")
        self._file("config/prod.pem")
        resp = read_handler.handle({
            "tool_name": "Read",
            "tool_input": {"file_path": "prod.pem"},
            "cwd": str(self.root / "config"),
            "permission_mode": "default",
        })
        self.assertEqual(_decision(resp), "allow")

    def test_rule_is_inert_without_project_root(self):
        self._write_local("!config/prod.pem\n")
        approved = self._file("config/prod.pem")
        os.environ.pop("CLAUDE_PROJECT_DIR")
        # 非 git の tmpdir なので root は None → path 形 rule は一度も一致しない。
        # (共通行として書き直しても同じ — セクションではなく matcher の話)
        (self.home / _LOCAL_REL).write_text("!config/prod.pem\n")
        self.assertEqual(_decision(self._read(approved)), "deny")

    def test_rule_is_inert_for_files_outside_root(self):
        self._write_local("!config/prod.pem\n")
        outside = Path(self.tmp) / "elsewhere" / "config" / "prod.pem"
        outside.parent.mkdir(parents=True)
        outside.write_text("SECRET=x\n")
        self.assertEqual(_decision(self._read(outside)), "deny")

    def test_basename_form_still_widens_to_all_same_names(self):
        # 0.23.0 までの挙動 (同名すべて) は basename 形で維持される
        self._write_local("!prod.pem\n")
        self.assertEqual(_decision(self._read(self._file("config/prod.pem"))), "allow")
        self.assertEqual(_decision(self._read(self._file("other/prod.pem"))), "allow")


# -- Edit / Write ------------------------------------------------------------


class TestEditHandlerPathRule(BaseProjectScoped):
    def _write(self, path: Path, cwd: str | None = None) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        return edit_handler.handle(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(path), "content": "KEY=v\n"},
                "cwd": cwd or str(self.root),
                "permission_mode": "default",
            },
            tool_label="Write",
        )

    def test_excluded_path_is_allowed_and_siblings_stay_denied(self):
        self._write_local("!config/prod.pem\n")
        self.assertEqual(_decision(self._write(self.root / "config" / "prod.pem")), "allow")
        self.assertEqual(_decision(self._write(self.root / "other" / "prod.pem")), "deny")
        self.assertEqual(_decision(self._write(self.root / "prod.pem")), "deny")

    def test_deny_reason_offers_path_form_first_then_basename(self):
        resp = self._write(self.root / "other" / "prod.pem")
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!other/prod.pem` (この 1 ファイルだけ)", reason)
        self.assertIn("同名ファイルをすべて外したい場合だけ `!prod.pem`", reason)
        # 絶対パスは reason に出さない (0.19.0 からの方針)
        self.assertNotIn(str(self.root), reason)
        self.assertNotIn("`!/", reason)

    def test_deny_reason_for_existing_file_keeps_path_form(self):
        # overwrite 経路 (minimal info を埋め込む builder) でも案内は path 形
        self._file("other/.env", "KEY=v\n")
        resp = self._write(self.root / "other" / ".env")
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!other/.env` (この 1 ファイルだけ)", reason)
        self.assertIn("`!.env`", reason)

    def test_deny_reason_without_root_is_basename_only(self):
        os.environ.pop("CLAUDE_PROJECT_DIR")
        resp = self._write(self.root / "other" / "prod.pem")
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!prod.pem`", reason)
        self.assertNotIn("`!other/prod.pem`", reason)
        self.assertIn("案内できません", reason)

    def test_recipe_from_reason_actually_excludes_only_that_file(self):
        """deny reason が案内した path 形をそのまま書けば、その 1 ファイルだけが
        allow になる (レシピと matcher の噛み合わせを handler 経路で固定)。"""
        resp = self._write(self.root / "other" / "prod.pem")
        reason = _reason(resp)
        self.assertIn("`!other/prod.pem`", reason)
        self._write_local("!other/prod.pem\n")
        self.assertEqual(_decision(self._write(self.root / "other" / "prod.pem")), "allow")
        self.assertEqual(_decision(self._write(self.root / "config" / "prod.pem")), "deny")


# -- Bash --------------------------------------------------------------------


class TestBashHandlerPathRule(BaseProjectScoped):
    def _bash(self, cmd: str, cwd: str | None = None, mode: str = "default") -> dict:
        return bash_handler.handle({
            "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "t"},
            "cwd": cwd or str(self.root),
            "permission_mode": mode,
        })

    def test_excluded_path_is_allowed_and_siblings_stay_denied(self):
        self._write_local("!config/prod.pem\n")
        self._file("config/prod.pem")
        self._file("other/prod.pem")
        self.assertEqual(_decision(self._bash("cat config/prod.pem")), "allow")
        self.assertEqual(_decision(self._bash("cat other/prod.pem")), "deny")
        self.assertEqual(_decision(self._bash("cat prod.pem")), "deny")
        # 絶対 path / サブディレクトリからの相対 path でも同じ 1 ファイルに解決
        self.assertEqual(
            _decision(self._bash(f"cat {self.root / 'config' / 'prod.pem'}")), "allow"
        )
        self.assertEqual(
            _decision(self._bash("cat prod.pem", cwd=str(self.root / "config"))),
            "allow",
        )
        self.assertEqual(
            _decision(self._bash("cat ../config/prod.pem", cwd=str(self.root / "other"))),
            "allow",
        )

    def test_redirect_target_honours_path_rule(self):
        # metadata-only + 書込み redirect (0.14.0 の deny 経路) も同じ root で判定
        self._write_local("!config/prod.pem\n")
        self.assertEqual(_decision(self._bash("ls > config/prod.pem")), "allow")
        self.assertEqual(_decision(self._bash("ls > other/prod.pem")), "deny")

    def test_deny_reason_offers_path_form_for_literal_operand(self):
        resp = self._bash("cat other/prod.pem")
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!other/prod.pem` (この 1 ファイルだけ)", reason)
        self.assertIn("同名ファイルをすべて外したい場合だけ `!prod.pem`", reason)
        self.assertNotIn(str(self.root), reason)

    def test_deny_reason_resolves_relpath_from_subdirectory_cwd(self):
        resp = self._bash("cat prod.pem", cwd=str(self.root / "other"))
        self.assertEqual(_decision(resp), "deny")
        self.assertIn("`!other/prod.pem`", _reason(resp))

    def test_glob_operand_keeps_basename_form(self):
        # glob はユーザーが書いた形をそのまま案内する (escape も path 形もしない)
        resp = self._bash("cat .env*")
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!.env*`", reason)
        self.assertNotIn("この 1 ファイルだけ", reason)

    def test_vcs_pathspec_keeps_basename_form(self):
        # HEAD:.env は git の解釈 (repo root 相対) と cwd 結合が一致しない
        # 場合があるので path 形は案内しない
        resp = self._bash("git show HEAD:sub/.env")
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!.env`", reason)
        self.assertNotIn("この 1 ファイルだけ", reason)

    def test_path_rule_applies_to_vcs_pathspec_piece(self):
        # 判定側はコロン分割した各片を root 相対で見るので、rule は効く
        self._write_local("!sub/.env\n")
        self.assertEqual(_decision(self._bash("git show HEAD:sub/.env")), "allow")
        self.assertEqual(_decision(self._bash("git show HEAD:.env")), "deny")

    def test_operand_outside_root_keeps_basename_form(self):
        outside = Path(self.tmp) / "elsewhere" / ".env"
        outside.parent.mkdir(parents=True)
        outside.write_text("KEY=v\n")
        resp = self._bash(f"cat {outside}")
        self.assertEqual(_decision(resp), "deny")
        reason = _reason(resp)
        self.assertIn("`!.env`", reason)
        self.assertNotIn("この 1 ファイルだけ", reason)
        self.assertNotIn(str(outside), reason.split("suggestion:")[-1])

    def test_rule_is_inert_without_project_root(self):
        self._write_local("!config/prod.pem\n")
        os.environ.pop("CLAUDE_PROJECT_DIR")
        (self.home / _LOCAL_REL).write_text("!config/prod.pem\n")
        self.assertEqual(_decision(self._bash("cat config/prod.pem")), "deny")

    def test_sed_expression_is_not_hit_by_anchored_path_rule(self):
        # 0.22.0 で parts を外した理由 (式が合成パスになる) は path 形にも当てはまる
        # が、途中に / を含む rule は root アンカーなので s/secrets/x/ には効かない
        self._write_local("secrets/*.yaml\n")  # include の path 形
        self.assertEqual(
            _decision(self._bash("sed -n 's/secrets/x/p' README.md")), "allow"
        )
        self._file("secrets/a.yaml", "k: v\n")
        self.assertEqual(_decision(self._bash("cat secrets/a.yaml")), "deny")


class TestOperandRelpath(unittest.TestCase):
    """``_operand_relpath`` は判定に影響しない補助値。条件は狭い方に倒す。"""

    def test_literal_under_root(self):
        self.assertEqual(
            bash_handler._operand_relpath("sub/.env", "/r", "/r"), "sub/.env"
        )
        self.assertEqual(
            bash_handler._operand_relpath("/r/sub/.env", "/elsewhere", "/r"),
            "sub/.env",
        )
        self.assertEqual(
            bash_handler._operand_relpath("../a/.env", "/r/sub", "/r"), "a/.env"
        )

    def test_empty_when_not_resolvable(self):
        self.assertEqual(bash_handler._operand_relpath("sub/.env", "/r", None), "")
        self.assertEqual(bash_handler._operand_relpath("", "/r", "/r"), "")
        self.assertEqual(bash_handler._operand_relpath("../.env", "/r", "/r"), "")
        self.assertEqual(bash_handler._operand_relpath("/x/.env", "/r", "/r"), "")
        # glob / VCS pathspec / URI は basename 形のみ
        self.assertEqual(bash_handler._operand_relpath(".env*", "/r", "/r"), "")
        self.assertEqual(bash_handler._operand_relpath("HEAD:.env", "/r", "/r"), "")
        self.assertEqual(bash_handler._operand_relpath("file://.env", "/r", "/r"), "")


if __name__ == "__main__":
    unittest.main()
