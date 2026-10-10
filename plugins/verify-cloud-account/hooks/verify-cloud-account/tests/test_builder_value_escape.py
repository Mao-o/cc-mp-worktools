"""builder / pin_env が出す値は、制御文字をエスケープして 1 行に収める (v0.21.3)。

0.21.2 でパスとキー名を直したが、値の表示が残っていた。値は accounts.local.json・CLI の出力・
CLI の設定 (AWS の profile 名など)・env から来て、リポジトリ側が決められる。出力は skill 経由で
Claude が読むので、値に行を割る文字を入れると、出力の外に偽の行を差し込める。

`json.dumps` は U+0020 未満 (改行を含む) は直すが、DEL・C1 (`\\x85` / `\\x9b`)・行区切り (U+2028)・
書式文字 (U+202E など) はそのまま出す。fixture はそれらを含む値 (`_EVIL`) を使い、各 case は出力全体で
(1) 偽の行が行頭に現れない (2) 行を割る文字・ESC が生のまま残らない (3) エスケープした形で示される
を確かめる。

- 変更の差分 (`+ add` / `- current` など)・`show` の期待値と CLI 現在値は、値を置き換えずにエスケープ
  する (置き換えると何が変わるか分からなくなる)
- `pin-env` の表示 (候補・値・現在値) は hook の deny 文面と同じ `shell_word.shown` に通す (示せない値は
  「(表示しない値)」)。env に足す断片は貼り付けて使う値そのものなので、置き換えずに JSON の `\\uXXXX`
  で 1 行に収め、`json.loads` で元の値に戻ることを確かめる
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import shell_word  # noqa: E402
from scripts import accounts_builder as builder  # noqa: E402
from scripts import pin_env  # noqa: E402

_FAKE = "FAKE_LINE_9f3"
# 行区切り (U+2028) の後に偽の行、C1 (NEL / CSI)・双方向制御・DEL を含む値。`json.dumps` は
# これらをそのまま出す (改行は直す)。
_EVIL = f"v {_FAKE}\x85x\x9b[31m‮\x7f"
_ESCAPED = f"v\\u2028{_FAKE}\\x85x\\x9b[31m\\u202e\\x7f"
# 生のまま出力に残ってはいけない文字。
_RAW_BAD = (" ", " ", "\x85", "\x9b", "‮", "\x7f", "\x1b", "\r")
_ACCOUNT = "123456789012"
_ACCOUNTS_REL = Path(".claude") / "verify-cloud-account" / "accounts.local.json"
_DEPRECATED_REL = Path(".claude") / "accounts.local.json"


def _assert_one_line(test: unittest.TestCase, text: str, what: str, *, escaped: str | None = _ESCAPED) -> None:
    for line in text.splitlines():
        test.assertFalse(
            line.lstrip().startswith(_FAKE),
            f"{what}: 偽の行が差し込まれた: {line!r}\n---\n{text!r}",
        )
    for ch in _RAW_BAD:
        test.assertNotIn(ch, text, f"{what}: 生の {ch!r} が残った\n---\n{text!r}")
    if escaped is not None:
        test.assertIn(escaped, text, what)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = Path(self.tmp)
        isolation = _testutil.start_isolation(self.root / "cliconfig", self.root / "iso_home")
        self.addCleanup(isolation.stop)
        self.project = self.root / "proj"
        self.project.mkdir()
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.project)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, rel: Path, data) -> Path:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = builder.main(argv, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def _check(self, argv: list[str], *, code: int = 0, escaped: str | None = _ESCAPED) -> tuple[str, str]:
        got, out, err = self._run(argv)
        self.assertEqual(got, code, f"{argv}\n{out}\n{err}")
        _assert_one_line(self, out + err, " ".join(argv), escaped=escaped)
        return out, err


class TestChangeLines(_Base):
    """変更の差分 (`_print_change_line`) に出す値。"""

    def test_add_with_a_scalar_value(self):
        out, _ = self._check(["set", "--service", "aws", "--value", _EVIL, "--show-values"])
        self.assertIn(f'+ add: aws -> "{_ESCAPED}"', out)

    def test_add_with_a_dict_value(self):
        value = json.dumps({"github.com": _EVIL})
        out, _ = self._check(["set", "--service", "github", "--value", value, "--show-values"])
        self.assertIn(f'"github.com": "{_ESCAPED}"', out)

    def test_update_shows_current_and_new(self):
        self._write(_ACCOUNTS_REL, {"aws": _EVIL})
        out, _ = self._check(["set", "--service", "aws", "--value", "other", "--show-values"])
        self.assertIn(f'- current: aws -> "{_ESCAPED}"', out)

    def test_unchanged_value(self):
        self._write(_ACCOUNTS_REL, {"aws": _EVIL})
        out, _ = self._check(["set", "--service", "aws", "--value", _EVIL, "--show-values"])
        self.assertIn(f'= unchanged: aws -> "{_ESCAPED}"', out)

    def test_init_does_not_overwrite_and_shows_existing_and_proposed(self):
        self._write(_ACCOUNTS_REL, {"aws": _EVIL})
        out, _ = self._check(["init", "--service", "aws", "--value", f"{_EVIL}2", "--show-values"])
        self.assertIn(f'existing: aws -> "{_ESCAPED}"', out)
        self.assertIn(f'proposed: aws -> "{_ESCAPED}2"', out)

    def test_remove_of_a_whole_key(self):
        self._write(_ACCOUNTS_REL, {"aws": _EVIL})
        out, _ = self._check(["remove", "--service", "aws", "--show-values"])
        self.assertIn(f'- remove: aws -> "{_ESCAPED}"', out)

    def test_remove_of_one_host_among_several(self):
        self._write(_ACCOUNTS_REL, {"github": {"github.com": _EVIL, "ghe.example.com": "u"}})
        out, _ = self._check(
            ["remove", "--service", "github", "--host", "github.com", "--show-values"]
        )
        self.assertIn(f'- remove: github[github.com] -> "{_ESCAPED}"', out)

    def test_remove_of_the_last_host(self):
        self._write(_ACCOUNTS_REL, {"github": {"github.com": _EVIL}})
        out, _ = self._check(
            ["remove", "--service", "github", "--host", "github.com", "--show-values"]
        )
        self.assertIn(f'"github.com": "{_ESCAPED}"', out)

    def test_remove_that_drops_the_key_shows_the_reason_and_the_value(self):
        # 残る dict が使えない形になるので、理由つきの見出しでキーごと消す。
        self._write(_ACCOUNTS_REL, {"gcloud": {"project": _EVIL, "region": "x"}})
        out, _ = self._check(
            ["remove", "--service", "gcloud", "--host", "project", "--show-values"]
        )
        self.assertIn("キーごと削除します", out)
        self.assertIn(_ESCAPED, out)

    def test_remove_of_a_missing_host_names_it_escaped(self):
        self._write(_ACCOUNTS_REL, {"github": {"github.com": "u"}})
        out, _ = self._check(
            ["remove", "--service", "github", "--host", _EVIL, "--show-values"]
        )
        self.assertIn(f"host/alias '{_ESCAPED}' は存在しません", out)

    def test_value_is_hidden_without_show_values(self):
        out, _ = self._check(["set", "--service", "aws", "--value", _EVIL], escaped=None)
        self.assertIn(builder._VALUE_HIDDEN_MARK, out)
        self.assertNotIn(_FAKE, out)

    def test_status_heading_is_escaped_too(self):
        # status は固定の見出しのほか、値の形が不正な理由が入る見出しもある。
        out = io.StringIO()
        builder._print_change_line(f"- x {_FAKE}", "aws", "v", False, out)
        _assert_one_line(self, out.getvalue(), "status", escaped=f"- x\\u2028{_FAKE}")

    def test_shape_reason_names_the_key_escaped(self):
        # 理由に出すオブジェクトのキー (0.21.2 で通した) は、値の表示の変更後も 1 行に収まる。
        value = json.dumps({"project": "p", _EVIL: "x"})
        out, err = self._check(
            ["set", "--service", "gcloud", "--value", value, "--show-values"], code=1
        )
        self.assertIn("未対応", err)

    def test_shape_reason_for_each_kind_of_bad_value_names_the_key_escaped(self):
        # 理由はキー名と型名だけを含む (値そのものは含まない)。キー名は理由ごとに通す箇所が別。
        cases = {
            "空の値": ({_EVIL: ""}, "空文字・空白のみの"),
            "文字列でない値 (厳格)": ({_EVIL: 1}, "空でない文字列で"),
        }
        for label, (value, fragment) in cases.items():
            with self.subTest(label):
                _out, err = self._check(
                    ["set", "--service", "github", "--value", json.dumps(value)], code=1
                )
                self.assertIn(fragment, err)

    def test_shape_reason_when_migrate_takes_in_a_bad_old_value(self):
        # 緩い検証 (strict_keys=False) の「文字列である必要があります」の理由。
        self._write(_DEPRECATED_REL, {"github": {_EVIL: 1}})
        _code, out, err = self._run(["migrate"])
        _assert_one_line(self, out + err, "migrate reason")
        self.assertIn("文字列である必要が", err)

    def test_auto_switch_note_about_a_bad_expected_value_names_the_key_escaped(self):
        self._write(_ACCOUNTS_REL, {"github": {_EVIL: ""}})
        out, _ = self._check(["auto-switch", "--enable"])
        self.assertIn("期待値の形が不正です", out)

    def test_migrate_shows_unchanged_merged_and_conflict_values(self):
        self._write(_ACCOUNTS_REL, {"aws": _EVIL, "gcloud": "same"})
        self._write(_DEPRECATED_REL, {"aws": f"{_EVIL}old", "github": _EVIL})
        _code, out, err = self._run(["migrate", "--show-values"])
        _assert_one_line(self, out + err, "migrate conflict", escaped=_ESCAPED)
        self.assertIn(f"new=\"{_ESCAPED}\"", err)
        self.assertIn(f"deprecated=\"{_ESCAPED}old\"", err)
        # 衝突を解いた状態で、取り込み (`+ merged from`) と `= unchanged` の値も確かめる。
        self._write(_DEPRECATED_REL, {"aws": _EVIL, "github": _EVIL})
        out, _ = self._check(["migrate", "--show-values"])
        self.assertIn(f'= unchanged: aws -> "{_ESCAPED}"', out)
        self.assertIn(f'+ merged from deprecated: github -> "{_ESCAPED}"', out)

    def test_auto_switch_shows_the_reserved_key_value(self):
        self._write(_ACCOUNTS_REL, {"github": "u", "$auto_switch": _EVIL})
        out, _ = self._check(["auto-switch", "--enable"])
        self.assertIn(f'- current: $auto_switch -> "{_ESCAPED}"', out)


class TestShow(_Base):
    def test_expected_value_with_show_values(self):
        self._write(_ACCOUNTS_REL, {"aws": _EVIL})
        with mock.patch("services.aws.get_active_account", return_value=None):
            out, _ = self._check(["show", "--show-values"])
        self.assertIn(f'aws: "{_ESCAPED}"', out)

    def test_current_value_of_a_mismatch(self):
        self._write(_ACCOUNTS_REL, {"aws": "expected"})
        with mock.patch("services.aws.get_active_account", return_value=_EVIL):
            out, _ = self._check(["show", "--show-values"])
        self.assertIn(f'[mismatch] current="{_ESCAPED}"', out)

    def test_reserved_keys_are_shown_without_show_values(self):
        for key in ("$mode", "$readonly", "$auto_switch"):
            with self.subTest(key=key):
                self._write(_ACCOUNTS_REL, {key: _EVIL})
                out, _ = self._check(["show"])
                self.assertIn(f'{key}: "{_ESCAPED}"', out)


def _make_env(**overrides) -> dict:
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(overrides)
    return env


def _snippet(test: unittest.TestCase, lines: list[str]) -> dict:
    header = 'settings.local.json の "env" に足す内容 (既存のキーは残す):'
    body = "\n".join(lines[lines.index(header) + 1:])
    try:
        return json.loads("{" + body + "}")
    except ValueError as e:
        test.fail(f"env の断片が JSON として読めない ({e}): {body!r}")


class TestPinEnvRender(unittest.TestCase):
    def _render(self, plans, *, show_values=True, session=None, file_env=None):
        return pin_env.render(
            plans, None, "理由", session or {}, file_env or {}, None, show_values=show_values
        )

    def test_candidates_are_shown_through_shown_all(self):
        candidates = ("dev", _EVIL, f"{_EVIL}2")
        plans = [pin_env.Plan("aws", pins=(pin_env.Pin("AWS_PROFILE", None, candidates),))]
        lines = self._render(plans)
        text = "\n".join(lines)
        _assert_one_line(self, text, "candidates", escaped=None)
        self.assertIn(f"AWS_PROFILE: 候補 {shell_word.shown_all(candidates)} (1 つ選ぶ)", text)
        self.assertIn(shell_word.NOT_SHOWN, text)
        # 断片の目印 (placeholder) も同じ表示形で、JSON として読める。
        self.assertEqual(
            _snippet(self, lines), {"AWS_PROFILE": f"<{shell_word.NOT_SHOWN} / dev のどれか>"}
        )

    def test_single_value_line_is_shown_and_the_snippet_keeps_the_value(self):
        plans = [pin_env.Plan("aws", pins=(pin_env.Pin("AWS_PROFILE", _EVIL, (_EVIL,)),))]
        lines = self._render(plans)
        text = "\n".join(lines)
        _assert_one_line(self, text, "value", escaped=None)
        self.assertIn(f"AWS_PROFILE: {shell_word.NOT_SHOWN}", text)
        # 貼り付けて使う値は置き換えない。1 行の JSON で、読み直すと元の値に戻る。
        self.assertEqual(_snippet(self, lines), {"AWS_PROFILE": _EVIL})
        for escaped in ("\\u2028", "\\u0085", "\\u009b", "\\u202e", "\\u007f"):
            self.assertIn(escaped, text)

    def test_snippet_round_trips_characters_outside_the_bmp(self):
        # タグ文字 (U+E0041、書式文字) は代理対の `\\uXXXX` で出る。
        value = "p\U000e0041q"
        plans = [pin_env.Plan("gcloud", pins=(pin_env.Pin("CLOUDSDK_CORE_PROJECT", value, secret=True),))]
        lines = self._render(plans)
        _assert_one_line(self, "\n".join(lines), "non-bmp", escaped=None)
        self.assertNotIn("\U000e0041", "\n".join(lines))
        self.assertEqual(_snippet(self, lines), {"CLOUDSDK_CORE_PROJECT": value})

    def test_plain_values_and_japanese_are_unchanged(self):
        plans = [pin_env.Plan("aws", pins=(pin_env.Pin("AWS_PROFILE", "dev", ("dev",)),))]
        lines = self._render(plans)
        self.assertIn("  AWS_PROFILE: dev", lines)
        self.assertEqual(pin_env._env_member("K", "日本語 v"), '"K": "日本語 v"')

    def test_current_values_are_shown(self):
        plans = [pin_env.Plan("aws", pins=(pin_env.Pin("AWS_PROFILE", "dev", ("dev",)),))]
        text = "\n".join(
            self._render(
                plans,
                session={"AWS_PROFILE": _EVIL},
                file_env={"AWS_PROFILE": f"{_EVIL}2"},
            )
        )
        _assert_one_line(self, text, "current", escaped=None)
        self.assertIn(
            f"このセッション={shell_word.NOT_SHOWN} / 書き込み先={shell_word.NOT_SHOWN}", text
        )

    def test_secret_values_stay_hidden_without_show_values(self):
        plans = [pin_env.Plan("gcloud", pins=(pin_env.Pin("CLOUDSDK_CORE_PROJECT", _EVIL, secret=True),))]
        lines = self._render(plans, show_values=False, session={"CLOUDSDK_CORE_PROJECT": _EVIL})
        text = "\n".join(lines)
        _assert_one_line(self, text, "hidden", escaped=None)
        self.assertNotIn(shell_word.NOT_SHOWN, text)
        self.assertIn(pin_env.HIDDEN, text)


class TestPinEnvCommand(_Base):
    """CLI の設定 (AWS config の profile 名) と env から来る値を、コマンドで通す。"""

    def setUp(self):
        super().setUp()
        # profile 名に行を割る文字は置けない (config を行で読む) ので、CSI / 双方向制御 / DEL を使う。
        self.profile_a = f"a\x9b[31m{_FAKE}‮"
        self.profile_b = "b\x7f"
        config = self.root / "aws_config"
        config.write_text(
            f"[profile {self.profile_a}]\nsso_account_id = {_ACCOUNT}\n"
            f"[profile {self.profile_b}]\nsso_account_id = {_ACCOUNT}\n",
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            os.environ,
            {"AWS_CONFIG_FILE": str(config), "AWS_PROFILE": f"cur {_FAKE}"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._write(_ACCOUNTS_REL, {"aws": _ACCOUNT})

    def test_candidates_and_current_values_in_the_command_output(self):
        code, out, err = self._run(["pin-env", "--service", "aws"])
        self.assertEqual(code, 0, err)
        _assert_one_line(self, out + err, "pin-env", escaped=None)
        self.assertIn(f"候補 {shell_word.NOT_SHOWN} (1 つ選ぶ)", out)
        self.assertIn(f"このセッション={shell_word.NOT_SHOWN}", out)
        # 断片は、行を割る文字なしで JSON として読める。
        header = 'settings.local.json の "env" に足す内容 (既存のキーは残す):'
        body = out.split(header + "\n", 1)[1]
        self.assertEqual(
            json.loads("{" + body + "}"),
            {"AWS_PROFILE": f"<{shell_word.NOT_SHOWN} のどれか>"},
        )


if __name__ == "__main__":
    unittest.main()
