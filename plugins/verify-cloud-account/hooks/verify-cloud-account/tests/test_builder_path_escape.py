"""builder / pin_env が出すパスは、制御文字をエスケープして 1 行に収める (v0.21.2)。

builder は skill 経由で Claude が実行し、出力はモデルが読む。途中のディレクトリ名はリポジトリ側が
決められる (clone しただけのリポジトリでも、改行や端末の制御シーケンスを含む名前を置ける) ので、
パスをそのまま出すと、出力の外に偽の行 (指示のように見える行) を差し込める。hook の deny 文面は
0.21.0 / 0.21.1 で直したが、builder と pin_env の出力が残っていた。

fixture は、ディレクトリ名に改行 + 偽の行 + ESC を含める。各 case は、出力全体で
(1) 偽の行が行頭に現れない (2) エスケープした形 (`\\n` / `\\x1b`) で示される (3) ESC を含まない、
を確かめる。コマンドの形で案内する行 (`rm` / `--path`) は、制御文字を含まないパス (空白や `;` を含む)
では今まで通りシェルの規則で 1 語に戻ることを、`shlex.split` で往復して確かめる。
"""
from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import paths, shell_word  # noqa: E402
from scripts import accounts_builder as builder  # noqa: E402
from scripts import pin_env  # noqa: E402

_FAKE = "FAKE_LINE_9f3"
# 改行の後に偽の行、さらに端末の制御シーケンス (ESC) を含むディレクトリ名。
_EVIL = f"evil\n{_FAKE}\x1b[31m"
_ESCAPED = f"evil\\n{_FAKE}\\x1b[31m"
_ACCOUNTS_REL = Path(".claude") / "verify-cloud-account" / "accounts.local.json"
_DEPRECATED_REL = Path(".claude") / "accounts.local.json"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = Path(self.tmp)
        isolation = _testutil.start_isolation(self.root / "cliconfig", self.root / "iso_home")
        self.addCleanup(isolation.stop)
        self.project = self.root / _EVIL
        self.project.mkdir()
        # グローバル既定のパスも、制御文字を含むホームの下にする。
        self.home = self.root / f"home\n{_FAKE}\x1b[32m"
        (self.home / ".claude" / "verify-cloud-account").mkdir(parents=True)
        patcher = mock.patch.object(Path, "home", staticmethod(lambda: self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        self._launch(self.project)

    def _launch(self, project: Path) -> None:
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(project)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, base: Path, rel: Path, data) -> Path:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = builder.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def _assert_one_line(self, text: str, what: str = "", *, escaped: str = _ESCAPED) -> None:
        """偽の行が行頭に出ず、エスケープした形で示され、ESC が残らない。"""
        for line in text.splitlines():
            self.assertFalse(
                line.lstrip().startswith(_FAKE),
                f"{what}: 偽の行が差し込まれた: {line!r}\n---\n{text}",
            )
        self.assertIn(escaped, text, what)
        self.assertNotIn("\x1b", text, what)

    def _check(self, argv: list[str], *, code: int | None = None, escaped: str = _ESCAPED) -> tuple[str, str]:
        got, out, err = self._run(argv)
        if code is not None:
            self.assertEqual(got, code, f"{argv}\n{out}\n{err}")
        self._assert_one_line(out + err, " ".join(argv), escaped=escaped)
        return out, err


class TestTargetAndWriteOutput(_Base):
    """対象の注記・変更の見出し・書込の報告 (init / set / remove / migrate / auto-switch)。"""

    def test_fresh_target_note_and_written_for_every_write_command(self):
        for argv in (
            ["init", "--service", "aws", "--value", "111", "--commit"],
            ["set", "--service", "aws", "--value", "111", "--commit"],
            ["auto-switch", "--enable", "--commit"],
        ):
            with self.subTest(argv=argv[0]):
                self._write(self.project, _ACCOUNTS_REL, {"github": "u"}) if argv[0] == "auto-switch" else None
                out, _err = self._check(argv, code=0)
                self.assertIn("changes to", out)
                self.assertIn("written:", out)
                shutil.rmtree(self.project / ".claude", ignore_errors=True)

    def test_remove_commit_reports_changes_and_written(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u", "aws": "111"})
        out, _err = self._check(["remove", "--service", "aws", "--commit"], code=0)
        self.assertIn("changes to", out)
        self.assertIn("written:", out)

    def test_remove_of_a_missing_key(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        self._check(["remove", "--service", "aws", "--commit"], code=0)

    def test_existing_project_file_is_the_plain_target(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        self._check(["show"], code=0)

    def test_dry_run_changes_header_for_each_command(self):
        for argv in (
            ["init", "--service", "aws", "--value", "111", "--dry-run"],
            ["set", "--service", "aws", "--value", "111", "--dry-run"],
        ):
            with self.subTest(argv=argv[0]):
                self._check(argv, code=0)

    def test_gitignore_update_and_skipped_claude_md(self):
        (self.project / ".gitignore").write_text("node_modules\n", encoding="utf-8")
        signpost_dir = self.project / ".claude" / "verify-cloud-account"
        signpost_dir.mkdir(parents=True)
        (signpost_dir / "CLAUDE.md").write_text("mine\n", encoding="utf-8")
        out, _err = self._check(["init", "--service", "aws", "--value", "111", "--commit"], code=0)
        self.assertIn("updated:", out)
        self.assertIn("skipped:", out)

    def test_created_claude_md(self):
        out, _err = self._check(["init", "--service", "aws", "--value", "111", "--commit"], code=0)
        self.assertIn("created:", out)

    def test_claude_md_write_failure_names_the_path_and_the_os_error(self):
        signpost = (self.project / ".claude" / "verify-cloud-account" / "CLAUDE.md").resolve()
        real_write_text = Path.write_text

        def write_text(path, *args, **kwargs):
            if path == signpost:
                raise OSError(_OS_MESSAGE)
            return real_write_text(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", write_text):
            out, _err = self._check(["init", "--service", "aws", "--value", "111", "--commit"], code=0)
        self.assertIn("の書き込みに失敗しました", out)
        self.assertIn(_OS_ESCAPED, out)

    def test_claude_md_template_failure_carries_the_os_error(self):
        missing = mock.Mock()
        missing.read_text.side_effect = OSError(_OS_MESSAGE)
        with mock.patch.object(builder, "_PROJECT_CLAUDE_MD_TEMPLATE", missing):
            out, _err = self._check(
                ["init", "--service", "aws", "--value", "111", "--commit"],
                code=0,
            )
        self.assertIn("template の読み込みに失敗しました", out)
        self.assertIn(_OS_ESCAPED, out)

    def test_gitignore_failure_carries_the_os_error(self):
        gitignore = self.project / ".gitignore"
        gitignore.write_text("node_modules\n", encoding="utf-8")
        (self.project / ".claude").mkdir()
        real_write_text = Path.write_text

        def write_text(path, *args, **kwargs):
            if path == gitignore.resolve():
                raise OSError(_OS_MESSAGE)
            return real_write_text(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", write_text):
            out, _err = self._check(["init", "--service", "aws", "--value", "111", "--commit"], code=0)
        self.assertIn(".gitignore の更新に失敗しました", out)
        self.assertIn(_OS_ESCAPED, out)

    def test_write_failure_carries_the_os_error(self):
        for argv in (
            ["init", "--service", "aws", "--value", "111", "--commit"],
            ["set", "--service", "aws", "--value", "111", "--commit"],
            ["auto-switch", "--enable", "--commit"],
        ):
            with self.subTest(argv=argv[0]):
                target = self.project / _ACCOUNTS_REL
                self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
                with mock.patch.object(builder, "_write_json", side_effect=_raise_for(target)):
                    _out, err = self._check(argv, code=1)
                self.assertIn("書き込みに失敗しました", err)
                self.assertIn(_OS_ESCAPED, err)
                shutil.rmtree(self.project / ".claude", ignore_errors=True)

    def test_remove_write_failure_carries_the_os_error(self):
        target = self._write(self.project, _ACCOUNTS_REL, {"github": "u", "aws": "111"})
        with mock.patch.object(builder, "_write_json", side_effect=_raise_for(target)):
            _out, err = self._check(["remove", "--service", "aws", "--commit"], code=1)
        self.assertIn("書き込みに失敗しました", err)
        self.assertIn(_OS_ESCAPED, err)


_OS_MESSAGE = f"denied\n{_FAKE}_os"
_OS_ESCAPED = f"denied\\n{_FAKE}_os"


def _raise_for(_path: Path):
    """生の改行を含むメッセージの OSError。

    errno / filename 付きの OSError は `str()` が filename を repr で出す (改行は `\\n`) ので、
    メッセージを素通しにしても気付けない。メッセージだけの OSError なら、素通しは偽の行になる。
    """

    def fail(*_args, **_kwargs):
        raise OSError(_OS_MESSAGE)

    return fail


class TestAncestorAndExplicitPath(_Base):
    def setUp(self):
        super().setUp()
        self.sub = self.project / "sub"
        self.sub.mkdir()

    def test_ancestor_note_names_both_paths(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        self._launch(self.sub)
        self._check(["show"], code=0)

    def test_init_refuses_an_inherited_target(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        self._launch(self.sub)
        _out, err = self._check(["init", "--service", "aws", "--value", "111", "--commit"], code=2)
        self.assertIn("継承しています", err)
        self.assertIn("継承中の", err)

    def test_explicit_path_note_and_the_file_the_hook_reads_instead(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        elsewhere = self.root / f"else\n{_FAKE}"
        out, _err = self._check(
            ["show", "--path", str(elsewhere / _ACCOUNTS_REL)], code=0
        )
        self.assertIn("hook が現在読むのは", out)

    def test_path_outside_the_layout_is_refused_with_the_escaped_path(self):
        _out, err = self._check(
            ["init", "--service", "aws", "--value", "111", "--path", str(self.project / "x.json")],
            code=2,
        )
        self.assertIn("dispatcher が読む配置を指してください", err)

    def test_legacy_path_for_a_write_is_refused_with_the_escaped_path(self):
        _out, err = self._check(
            ["init", "--service", "aws", "--value", "111", "--path", str(self.project / _DEPRECATED_REL)],
            code=2,
        )
        self.assertIn("新パスに", err)


class TestLoadExisting(_Base):
    """既存ファイルを読めないときの理由は、パスをエスケープして示す (init / set / remove / show ...)。"""

    _CASES = {
        "malformed json": (b"{not json", "JSON が不正です"),
        "not utf-8": (b'{"github": "\xff"}', "UTF-8 で保存した JSON"),
        "not an object": (b"[]", "JSON オブジェクト"),
        "too deep": (("{\"a\": " * 5000 + "1" + "}" * 5000).encode(), "深"),
        "deeper than the limit": (
            ('{"github": "u", "pad": ' + "[" * 40 + "]" * 40 + "}").encode(),
            "入れ子が 32 段より深い",
        ),
    }

    def test_each_failure_names_the_escaped_path(self):
        for name, (data, reason) in self._CASES.items():
            self._write(self.project, _ACCOUNTS_REL, data)
            for argv in (
                ["set", "--service", "aws", "--value", "111", "--commit"],
                ["show"],
            ):
                with self.subTest(case=name, command=argv[0]):
                    _out, err = self._check(argv)
                    self.assertIn(reason, err)

    def test_unreadable_file_carries_the_os_error(self):
        path = self._write(self.project, _ACCOUNTS_REL, {"github": "u"}).resolve()
        real_read_text = Path.read_text

        def read_text(p, *args, **kwargs):
            if p == path:
                raise OSError(_OS_MESSAGE)
            return real_read_text(p, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_text):
            _out, err = self._check(["set", "--service", "aws", "--value", "111", "--commit"], code=1)
        self.assertIn("読み込みに失敗しました", err)
        self.assertIn(_OS_ESCAPED, err)

    def test_unstattable_file_names_the_escaped_path(self):
        path = self.project / _ACCOUNTS_REL
        path.parent.mkdir(parents=True)
        os.symlink(path.name, path)  # 自分自身を指す symlink (stat が ELOOP)
        self.assertIsNotNone(paths.stat_failure(path))
        _out, err = self._check(["show", "--path", str(path)], code=1)
        self.assertIn("確かめられません", err)


class TestConflictsAndMigrate(_Base):
    def test_legacy_file_refuses_a_write_and_lists_it(self):
        self._write(self.project, _DEPRECATED_REL, {"github": "u"})
        _out, err = self._check(["init", "--service", "aws", "--value", "111", "--commit"], code=1)
        self.assertIn("旧パスに", err)

    def test_show_lists_conflicting_files(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        self._write(self.project, _DEPRECATED_REL, {"github": "u"})
        _out, err = self._check(["show"], code=1)
        self.assertIn("複数のパス", err)

    def test_show_of_a_legacy_file_names_it(self):
        self._write(self.project, _DEPRECATED_REL, {"github": "u"})
        self._check(["show"], code=0)

    def test_show_without_a_file(self):
        out, _err = self._check(["show"], code=0)
        self.assertIn("no accounts.local.json found at", out)

    def test_migrate_of_only_the_new_path(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        out, _err = self._check(["migrate"], code=0)
        self.assertIn("nothing to migrate", out)

    def test_migrate_dry_run_and_commit(self):
        self._write(self.project, _DEPRECATED_REL, {"github": "u"})
        out, _err = self._check(["migrate"], code=0)
        self.assertIn("=== migrate to", out)
        out, _err = self._check(["migrate", "--commit"], code=0)
        self.assertIn("written:", out)

    def test_migrate_does_not_offer_rm_in_a_command_form_for_a_control_path(self):
        self._write(self.project, _DEPRECATED_REL, {"github": "u"})
        out, _err = self._check(["migrate", "--commit"], code=0)
        self.assertIn("手で削除してください", out)
        self.assertIn(shell_word.NOT_COMMAND_FORM_REMOVE, out)
        for line in out.splitlines():
            self.assertFalse(line.lstrip().startswith("rm "), line)
        self.assertNotIn("rm '", out)

    def test_migrate_rm_hint_for_a_clean_path_still_round_trips(self):
        # 空白と `;` を含むが制御文字は無いパスは、今まで通りコマンドの形で案内する。
        project = self.root / "my project; echo x"
        project.mkdir()
        self._launch(project)
        legacy = self._write(project, _DEPRECATED_REL, {"github": "u"})
        code, out, err = self._run(["migrate", "--commit"])
        self.assertEqual(code, 0, out + err)
        (line,) = [ln for ln in out.splitlines() if ln.startswith("  rm ")]
        self.assertEqual(shlex.split(line, comments=True), ["rm", str(legacy.resolve())])
        self.assertNotIn("手で削除", out)

    def test_migrate_write_failure_carries_the_os_error(self):
        self._write(self.project, _DEPRECATED_REL, {"github": "u"})
        with mock.patch.object(builder, "_write_json", side_effect=_raise_for(self.project)):
            _out, err = self._check(["migrate", "--commit"], code=1)
        self.assertIn(_OS_ESCAPED, err)

    def test_pin_env_lists_conflicting_files(self):
        self._write(self.project, _ACCOUNTS_REL, {"aws": {"profile": "p"}})
        self._write(self.project, _DEPRECATED_REL, {"aws": {"profile": "p"}})
        _out, err = self._check(["pin-env"], code=1)
        self.assertIn("複数のパス", err)

    def test_migrate_conflict_does_not_write(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "a"})
        self._write(self.project, _DEPRECATED_REL, {"github": "b"})
        self._check(["migrate", "--commit"], code=1, escaped="")


class TestGlobalDefault(_Base):
    def _global(self, data) -> Path:
        return self._write(self.home, _ACCOUNTS_REL, data)

    def test_fresh_target_warns_with_the_escaped_global_path_and_keys(self):
        self._global({f"k\n{_FAKE}": "x"})
        out, _err = self._check(
            ["init", "--service", "github", "--value", "u", "--dry-run"],
            code=0,
            escaped="home\\n" + _FAKE,
        )
        self.assertIn("継承されません", out)
        self.assertIn(f"k\\n{_FAKE}", out)
        self.assertIn("手で指定してください", out)

    def test_show_points_at_the_global_default(self):
        self._global({"github": "u"})
        out, _err = self._check(["show"], code=0, escaped="home\\n" + _FAKE)
        self.assertIn("グローバル既定", out)
        self.assertIn("手で指定してください", out)

    def test_auto_switch_without_a_file_points_at_the_global_default(self):
        self._global({"github": "u"})
        _out, err = self._check(["auto-switch", "--enable", "--commit"], code=1)
        self.assertIn("がありません", err)
        self.assertIn("手で指定してください", err)

    def test_pin_env_without_a_file_points_at_the_global_default(self):
        self._global({"aws": {"profile": "p"}})
        _out, err = self._check(["pin-env"], code=1)
        self.assertIn("手で指定してください", err)


class TestKeyNames(_Base):
    """期待値ファイルのキー名もリポジトリ側が決められる。パスと同じくエスケープして 1 行に収める。

    キーは値ではない (値は `--show-values` を待って出す) ので、show / migrate の見出しにそのまま出る。
    判定 (`--service` との照合など) は生のキーで行い、表示だけを変える。
    """

    _KFAKE = "FAKE_KEY_LINE_7c1"
    _KEY = f"zz\n{_KFAKE}\x1b[1m"
    _KEY_ESCAPED = f"zz\\n{_KFAKE}\\x1b[1m"

    def _assert_key_line(self, text: str, what: str) -> None:
        for line in text.splitlines():
            self.assertFalse(
                line.lstrip().startswith(self._KFAKE),
                f"{what}: キー名から偽の行が差し込まれた: {line!r}\n---\n{text}",
            )
        self.assertIn(self._KEY_ESCAPED, text, what)
        self.assertNotIn("\x1b[1m", text, what)

    def test_show_escapes_the_key(self):
        self._write(self.project, _ACCOUNTS_REL, {self._KEY: "x", "github": "u"})
        _code, out, err = self._run(["show"])
        self._assert_key_line(out + err, "show")
        self.assertIn("[unknown service]", out)

    def test_show_still_filters_by_the_raw_service_key(self):
        # 表示だけを変える: `--service` は生のキーと照合する (エスケープ後の文字列では引けない)。
        self._write(self.project, _ACCOUNTS_REL, {self._KEY: "x", "github": "u"})
        _code, out, _err = self._run(["show", "--service", "github"])
        self.assertNotIn(self._KEY_ESCAPED, out)

    def test_migrate_dry_run_escapes_the_merged_key(self):
        self._write(self.project, _DEPRECATED_REL, {self._KEY: "x"})
        code, out, err = self._run(["migrate"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("+ merged from deprecated:", out)
        self._assert_key_line(out + err, "migrate (merged)")

    def test_migrate_with_show_values_escapes_the_key(self):
        # `--show-values` は 1 行にキーと値を並べる別の分岐を通る。
        self._write(self.project, _DEPRECATED_REL, {self._KEY: "x"})
        code, out, err = self._run(["migrate", "--show-values"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(" -> ", out)
        self._assert_key_line(out + err, "migrate (show-values)")

    def test_migrate_escapes_an_unchanged_key(self):
        self._write(self.project, _ACCOUNTS_REL, {self._KEY: "x"})
        self._write(self.project, _DEPRECATED_REL, {"github": "u"})
        code, out, err = self._run(["migrate"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("= unchanged:", out)
        self._assert_key_line(out + err, "migrate (unchanged)")

    def test_migrate_conflict_escapes_the_key(self):
        self._write(self.project, _ACCOUNTS_REL, {self._KEY: "a"})
        self._write(self.project, _DEPRECATED_REL, {self._KEY: "b"})
        code, out, err = self._run(["migrate"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("new=", err)
        self._assert_key_line(out + err, "migrate (conflict)")

    def test_object_value_key_in_a_rejected_shape_is_escaped(self):
        # 値の中のキー (host / alias 名) も期待値ファイルが決める。形が不正な理由に名前が出る。
        self._write(self.project, _DEPRECATED_REL, {"github": {self._KEY: ""}})
        code, out, err = self._run(["migrate"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("空文字・空白のみ", err)
        self._assert_key_line(out + err, "migrate (invalid shape)")


    def test_entry_shape_reasons_escape_the_object_key(self):
        # 値の形の検証 (set --value / migrate の取り込み) が理由文に出すキー名。分岐ごとに 1 件。
        github = builder._SERVICE_BY_KEY["github"]
        gcloud = builder._SERVICE_BY_KEY["gcloud"]
        cases = {
            "unsupported key (strict)": (gcloud, {self._KEY: "x"}, True, "は未対応です"),
            "empty value": (github, {self._KEY: ""}, False, "空文字・空白のみ"),
            "non-string value (strict)": (github, {self._KEY: 1}, True, "空でない文字列で"),
            "non-string value (lenient)": (github, {self._KEY: 1}, False, "現在: int"),
        }
        for label, (svc, value, strict, marker) in cases.items():
            with self.subTest(case=label):
                reason = builder._validate_entry_shape(svc, value, strict_keys=strict)
                self.assertIsNotNone(reason)
                self.assertIn(marker, reason)
                self._assert_key_line(reason, label)


class TestPathOption(unittest.TestCase):
    """案内文に埋め込む `--path <file>` は、制御文字を含まないパスだけコマンドの形にする。"""

    def test_clean_paths_round_trip_through_the_shell(self):
        for raw in ("/tmp/a b/x.json", "/tmp/semi;colon/x.json", "/tmp/it's/x.json", "/tmp/日本語/x.json"):
            with self.subTest(path=raw):
                self.assertEqual(
                    shlex.split(builder._path_option(Path(raw)), comments=True), ["--path", raw]
                )

    def test_control_paths_are_not_offered_as_a_command(self):
        for raw in (f"/tmp/{_EVIL}/x.json", "/tmp/a\rb", "/tmp/a\x1b[31mb", "/tmp/a\u202eb"):
            with self.subTest(path=repr(raw)):
                out = builder._path_option(Path(raw))
                self.assertNotIn("\n", out)
                self.assertNotIn("\r", out)
                self.assertNotIn("\x1b", out)
                self.assertNotIn("'", out)
                self.assertIn("手で指定してください", out)


class TestCliErrorText(_Base):
    def test_cli_error_text_in_show_is_one_line(self):
        self._write(self.project, _ACCOUNTS_REL, {"github": "u"})
        svc = builder._SERVICE_BY_KEY["github"]
        with mock.patch.object(svc, "get_active_account", side_effect=RuntimeError(f"boom\n{_FAKE}")):
            out, _err = self._check(["show"], code=0, escaped=f"boom\\n{_FAKE}")
        self.assertIn("CLI error:", out)


class TestPinEnv(_Base):
    def _git_repo(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, stdin=subprocess.DEVNULL)

    def _esc_repo(self) -> Path:
        """改行を含まない (git の出力が行に割れない) が、ESC を含むリポジトリ。"""
        repo = self.root / "esc\x1b[31m_repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, stdin=subprocess.DEVNULL)
        return repo

    def test_pin_env_output_is_one_line(self):
        self._git_repo()
        self._write(self.project, _ACCOUNTS_REL, {"aws": {"profile": "p"}})
        out, _err = self._check(["pin-env"], code=0)
        self.assertIn("書き込み先:", out)

    def test_foreign_owner_names_the_escaped_path(self):
        repo = self._esc_repo()
        with mock.patch.object(pin_env.os, "getuid", return_value=os.getuid() + 1):
            target, note = pin_env.settings_local_target(str(repo))
        self.assertIsNone(target)
        self._assert_one_line(note, "owner", escaped="esc\\x1b[31m_repo")
        self.assertIn("所有者が自分ではない", note)

    def test_unreadable_state_names_the_escaped_path(self):
        repo = self._esc_repo()
        with mock.patch.object(Path, "exists", return_value=True), mock.patch.object(
            Path, "stat", side_effect=OSError("denied")
        ):
            target, note = pin_env.settings_local_target(str(repo))
        self.assertIsNone(target)
        self._assert_one_line(note, "stat", escaped="esc\\x1b[31m_repo")
        self.assertIn("状態を読めない", note)

    def test_render_target_line_is_one_line(self):
        lines = pin_env.render(
            [], Path(f"/tmp/{_EVIL}/.claude/settings.local.json"), "note", {}, {}, None, show_values=False
        )
        self._assert_one_line("\n".join(lines), "render")


if __name__ == "__main__":
    unittest.main()
