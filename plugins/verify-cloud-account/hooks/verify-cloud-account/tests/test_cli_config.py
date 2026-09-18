"""`core/cli_config.py` の最小 YAML / INI パーサのテスト。

fixture は実機の形 (gh 2.9x の `hosts.yml` / gcloud の `configurations/config_<name>`)
から起こしている。**想定外の形では必ず None を返す** (呼び出し側が CLI 実行へ
落ちる) ことを固定するのがこのモジュールの主眼。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401

from core import cli_config  # noqa: E402

# gh 2.9x が書く hosts.yml の形 (実測: インデント 4、host 直下に `users:` と
# `user:` が並ぶ)。`users:` はそのホストに紐付く**全**アカウントで、アクティブな
# のは `user:` だけ。
GH_HOSTS_YML = (
    "github.com:\n"
    "    git_protocol: https\n"
    "    users:\n"
    "        active-user:\n"
    "        other-user:\n"
    "    user: active-user\n"
)

GH_HOSTS_YML_MULTI = (
    "github.com:\n"
    "    users:\n"
    "        active-user:\n"
    "    user: active-user\n"
    "ghe.example.com:\n"
    "    users:\n"
    "        corp-user:\n"
    "    user: corp-user\n"
)

GCLOUD_CONFIG_INI = "[core]\naccount = someone@example.com\nproject = my-project\n"


class TestParseNestedScalarMap(unittest.TestCase):
    def test_reads_active_user_from_real_shape(self):
        parsed = cli_config.parse_nested_scalar_map(GH_HOSTS_YML)
        self.assertEqual(
            parsed,
            {"github.com": {"git_protocol": "https", "user": "active-user"}},
        )

    def test_nested_users_block_is_not_read(self):
        """`users:` の中身を直下と混同しない (混同すると非アクティブな
        アカウントをアクティブと誤認し、期待値と一致する非アクティブ
        アカウントで allow する最悪の誤りになる)。"""
        parsed = cli_config.parse_nested_scalar_map(GH_HOSTS_YML)
        host = parsed["github.com"]
        self.assertNotIn("other-user", host)
        self.assertNotIn("active-user", host)
        self.assertEqual(host.get("user"), "active-user")

    def test_multiple_hosts(self):
        parsed = cli_config.parse_nested_scalar_map(GH_HOSTS_YML_MULTI)
        self.assertEqual(
            {host: props["user"] for host, props in parsed.items()},
            {"github.com": "active-user", "ghe.example.com": "corp-user"},
        )

    def test_comments_and_blank_lines_are_skipped(self):
        parsed = cli_config.parse_nested_scalar_map(
            "# comment\n\ngithub.com:\n    # inner\n    user: u\n"
        )
        self.assertEqual(parsed, {"github.com": {"user": "u"}})

    def test_quoted_key_and_value(self):
        parsed = cli_config.parse_nested_scalar_map(
            '"github.com":\n    user: "quoted-user"\n'
        )
        self.assertEqual(parsed, {"github.com": {"user": "quoted-user"}})

    def test_two_space_indent(self):
        parsed = cli_config.parse_nested_scalar_map(
            "github.com:\n  users:\n    u:\n  user: u\n"
        )
        self.assertEqual(parsed, {"github.com": {"user": "u"}})

    def test_empty_text_is_empty_map(self):
        self.assertEqual(cli_config.parse_nested_scalar_map(""), {})

    def test_unsupported_shapes_return_none(self):
        cases = {
            "tab indent": "github.com:\n\tuser: u\n",
            "sequence": "hosts:\n    - github.com\n",
            "top-level scalar": "version: 2\ngithub.com:\n    user: u\n",
            "dedent inside block": "github.com:\n    users:\n  user: u\n",
            "no colon": "github.com\n    user: u\n",
            "child before block": "    user: u\n",
            "unterminated quote": 'github.com:\n    user: "u\n',
            "block scalar": "github.com:\n    user: |\n        u\n",
            "anchor": "github.com:\n    user: &a u\n",
            "alias": "github.com:\n    user: *a\n",
            "flow map": "github.com:\n    user: {a: b}\n",
            "document start": "---\ngithub.com:\n    user: u\n",
        }
        for label, text in cases.items():
            with self.subTest(shape=label):
                self.assertIsNone(
                    cli_config.parse_nested_scalar_map(text),
                    "解釈できない形は None を返して CLI 実行に落とすこと",
                )


class TestParseIniSections(unittest.TestCase):
    def test_reads_core_section(self):
        parsed = cli_config.parse_ini_sections(GCLOUD_CONFIG_INI)
        self.assertEqual(
            parsed,
            {"core": {"account": "someone@example.com", "project": "my-project"}},
        )

    def test_percent_in_value_is_kept(self):
        """補間 (`%(x)s`) を行わない RawConfigParser を使っていること。"""
        parsed = cli_config.parse_ini_sections("[core]\nproject = a%b\n")
        self.assertEqual(parsed, {"core": {"project": "a%b"}})

    def test_multiple_sections(self):
        parsed = cli_config.parse_ini_sections(
            "[core]\nproject = p\n[compute]\nregion = asia-northeast1\n"
        )
        self.assertEqual(parsed["compute"], {"region": "asia-northeast1"})

    def test_broken_ini_returns_none(self):
        cases = {
            "duplicate key": "[core]\nproject = a\nproject = b\n",
            "missing section header": "project = p\n",
            "garbage line": "[core]\nproject\n",
        }
        for label, text in cases.items():
            with self.subTest(shape=label):
                self.assertIsNone(cli_config.parse_ini_sections(text))


class TestReadText(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_reads_utf8(self):
        path = self.tmp / "f.yml"
        path.write_text("github.com:\n    user: u\n", encoding="utf-8")
        self.assertIn("user", cli_config.read_text(path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(cli_config.read_text(self.tmp / "absent.yml"))

    def test_directory_returns_none(self):
        self.assertIsNone(cli_config.read_text(self.tmp))

    def test_too_large_returns_none(self):
        path = self.tmp / "big.yml"
        path.write_bytes(b"x" * (cli_config.MAX_FILE_BYTES + 1))
        self.assertIsNone(cli_config.read_text(path))

    def test_non_utf8_returns_none(self):
        path = self.tmp / "bin.yml"
        path.write_bytes(b"\xff\xfe\x00")
        self.assertIsNone(cli_config.read_text(path))


if __name__ == "__main__":
    unittest.main()
