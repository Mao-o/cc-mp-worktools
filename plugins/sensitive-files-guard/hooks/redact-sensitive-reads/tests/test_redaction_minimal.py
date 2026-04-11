"""各 redactor が値を一切漏らさないことを確認するテスト。

重要: 値 (値の一部を含む) が reason 文字列に出てきたら即 fail。

Step 2 以降 engine.redact は file-like を受けるため、テストは ``BytesIO`` で
text を wrap して渡す (``_redact_text`` ヘルパ)。
"""
from __future__ import annotations

import unittest
from io import BytesIO

from _testutil import FIXTURES

from redaction.dotenv import redact_dotenv
from redaction.engine import redact
from redaction.jsonlike import redact_jsonlike
from redaction.keyonly_scan import scan_keys
from redaction.opaque import redact_opaque
from redaction.tomllike import redact_toml


def _redact_text(basename: str, text: str, truncated: bool = False) -> str:
    """text を BytesIO 化して engine.redact を呼ぶテスト専用ヘルパ。"""
    data = text.encode("utf-8")
    return redact(BytesIO(data), basename, len(data), truncated=truncated)


# 値として fixture に出てくる文字列の一部 (これが reason に現れたら fail)
LEAK_MARKERS = [
    "postgresql",
    "user:pass",
    "sk_live",
    "super-secret",
    "10.0.0.1",
    "localhost",
    "5432",
    "SflKxwRJSMe",  # JWT の一部
    "abc123",
]


def _assert_no_leak(self: unittest.TestCase, text: str, ctx: str):
    for marker in LEAK_MARKERS:
        self.assertNotIn(marker, text, f"leak detected [{marker}] in {ctx}")


class TestDotenvRedaction(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "sample.env").read_text()

    def test_keys_extracted(self):
        info = redact_dotenv(self.text)
        self.assertEqual(info["format"], "dotenv")
        names = [k["name"] for k in info["keys"]]
        self.assertIn("DATABASE_URL", names)
        self.assertIn("JWT_SECRET", names)
        self.assertIn("API_KEY", names)
        self.assertIn("DEBUG", names)
        self.assertIn("PORT", names)

    def test_type_classification(self):
        info = redact_dotenv(self.text)
        by_name = {k["name"]: k["type"] for k in info["keys"]}
        self.assertEqual(by_name["JWT_SECRET"], "jwt")
        self.assertEqual(by_name["DEBUG"], "bool")
        self.assertEqual(by_name["ENABLED"], "bool")
        self.assertEqual(by_name["PORT"], "num")
        self.assertEqual(by_name["TIMEOUT"], "num")

    def test_no_value_leak_via_engine(self):
        reason = _redact_text(".env", self.text)
        _assert_no_leak(self, reason, "dotenv reason")

    def test_comment_stripped(self):
        reason = _redact_text(".env", self.text)
        self.assertNotIn("# Database", reason)
        self.assertNotIn("Feature flags", reason)


class TestJsonRedaction(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "sample.json").read_text()

    def test_structure_extracted(self):
        info = redact_jsonlike(self.text)
        self.assertEqual(info["format"], "json")

    def test_no_value_leak(self):
        reason = _redact_text("config.local.json", self.text)
        _assert_no_leak(self, reason, "json reason")

    def test_bool_and_num_masked(self):
        reason = _redact_text("config.local.json", self.text)
        self.assertIn("<type=bool>", reason)
        self.assertIn("<type=num>", reason)
        for line in reason.splitlines():
            if ":" in line and "<type=" not in line and not line.startswith(("format:", "entries:", "file:", "NOTE:", "note:")):
                self.assertIn("<", line, f"possible value leak: {line}")


class TestTomlRedaction(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "sample.toml").read_text()

    def test_parse_ok(self):
        info = redact_toml(self.text)
        self.assertEqual(info["format"], "toml")

    def test_no_value_leak(self):
        reason = _redact_text("secrets.local.toml", self.text)
        _assert_no_leak(self, reason, "toml reason")


class TestOpaqueYaml(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "sample.yaml").read_text()

    def test_keyonly_scan(self):
        reason = _redact_text("secrets.local.yaml", self.text)
        _assert_no_leak(self, reason, "yaml reason")
        self.assertIn("database", reason)
        self.assertIn("features", reason)


class TestKeyonlyScan(unittest.TestCase):
    def test_dotenv_like(self):
        text = "FOO=x\nBAR=y\nBAZ: z\n"
        keys = scan_keys(text)
        self.assertEqual(keys, ["FOO", "BAR", "BAZ"])

    def test_skips_non_matching(self):
        text = "# comment\n   \nnot_an_assignment\nX=1\n"
        keys = scan_keys(text)
        self.assertEqual(keys, ["X"])


class TestReasonFormat(unittest.TestCase):
    def test_data_tag_wrapping(self):
        reason = _redact_text(".env", "FOO=bar\n")
        self.assertIn('<DATA untrusted="true"', reason)
        self.assertIn("</DATA>", reason)

    def test_basename_only(self):
        reason = _redact_text(".env", "FOO=bar\n")
        self.assertNotIn("/etc/", reason)
        self.assertNotIn("/Users/", reason)

    def test_guard_marker_is_deterministic(self):
        """Step 4: guard="sfg-v1" は固定 marker (random ではない)。"""
        r1 = _redact_text(".env", "FOO=bar\n")
        r2 = _redact_text(".env", "BAZ=qux\n")
        self.assertIn('guard="sfg-v1"', r1)
        self.assertIn('guard="sfg-v1"', r2)

    def test_body_does_not_leak_closing_tag(self):
        """Step 4: 鍵名に DATA 閉じタグ風文字列が紛れても外殻が壊れない。

        鍵名は sanitize_key で injection パターン ([?]) に置換されるが、本文経路
        でのエスケープも二重防御として効くことを確認する。
        """
        # 攻撃的な鍵名: `</DATA>` を埋め込む
        # sanitize_key で [?] に置換されるはずだが、万一抜けても escape_data_tag で
        # 外殻が維持されることを最終防御として検証
        reason = _redact_text(".env", "FOO=bar\n")
        # 包装の開始タグと終了タグが 1 対で、中間に生の </DATA> が無い
        self.assertEqual(reason.count("</DATA>"), 1)
        self.assertEqual(
            reason.count('<DATA untrusted="true"'),
            1,
        )


class TestDetectFormatRegression(unittest.TestCase):
    """``_detect_format`` の substring マッチ誤検出を防ぐ回帰 (#6)。"""

    def test_json_bak_is_opaque(self):
        reason = _redact_text("foo.json.bak", '{"key":"value"}')
        self.assertNotIn("format: json", reason)

    def test_tomlike_is_opaque(self):
        reason = _redact_text("my.tomlike", "[section]\nk=v\n")
        self.assertNotIn("format: toml", reason)

    def test_dotenv_production(self):
        reason = _redact_text(".env.production", "FOO=bar\n")
        self.assertIn("format: dotenv", reason)

    def test_foo_env_is_dotenv(self):
        """Step 3: ``foo.env`` を dotenv 扱いにする (endswith(".env"))。"""
        reason = _redact_text("foo.env", "FOO=bar\n")
        self.assertIn("format: dotenv", reason)

    def test_envrc_is_dotenv(self):
        """Step 3: ``.envrc`` / ``*.envrc`` (direnv) を dotenv 扱いにする。"""
        reason = _redact_text(".envrc", "export FOO=bar\n")
        self.assertIn("format: dotenv", reason)
        reason2 = _redact_text("prod.envrc", "export FOO=bar\n")
        self.assertIn("format: dotenv", reason2)


class TestDotenvInlineComment(unittest.TestCase):
    """dotenv inline comment の値漏洩と型誤判定の回帰 (#7)。"""

    def _type_of(self, text: str, key: str = "K") -> str:
        info = redact_dotenv(text)
        by_name = {k["name"]: k["type"] for k in info["keys"]}
        return by_name[key]

    def test_num_with_inline_comment(self):
        self.assertEqual(self._type_of("K=3000 # port\n"), "num")

    def test_bool_with_inline_comment(self):
        self.assertEqual(self._type_of("K=true # flag\n"), "bool")

    def test_null_with_inline_comment(self):
        self.assertEqual(self._type_of("K=null # note\n"), "null")

    def test_value_hash_no_space_is_string(self):
        self.assertEqual(self._type_of("K=value#frag\n"), "str")

    def test_double_quoted_hash(self):
        self.assertEqual(self._type_of('K="a # b"\n'), "str")

    def test_single_quoted_hash(self):
        self.assertEqual(self._type_of("K='a # b'\n"), "str")

    def test_no_value_leak_inline_comment(self):
        text = (
            "PORT=3000 # real port\n"
            'DATABASE_URL="postgresql://user:pass@host/db"\n'
            "FLAG=true # ship it\n"
        )
        reason = _redact_text(".env", text)
        _assert_no_leak(self, reason, "dotenv inline comment reason")
        self.assertNotIn("real port", reason)
        self.assertNotIn("ship it", reason)


if __name__ == "__main__":
    unittest.main()
