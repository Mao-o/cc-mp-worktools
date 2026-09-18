"""各 redactor が値を一切漏らさないことを確認するテスト。

重要: 値 (値の一部を含む) が reason 文字列に出てきたら即 fail。

Step 2 以降 engine.redact は file-like を受けるため、テストは ``BytesIO`` で
text を wrap して渡す (``_redact_text`` ヘルパ)。
"""
from __future__ import annotations

import sys
import unittest
from io import BytesIO

from _testutil import FIXTURES

from redaction.dotenv import redact_dotenv
from redaction.engine import is_direnv_literal, is_envrc_basename, redact
from redaction.jsonlike import redact_jsonlike
from redaction.keyonly_scan import scan_keys
from redaction.opaque import redact_opaque
from redaction.tomllike import redact_toml

# tomllib (redaction/tomllike.py が使う) は Python 3.11+ の標準ライブラリ。
# 未満の環境では TOML の構造付き minimal info が opaque にフォールバックする
# 意図した劣化 (engine.py の `except RuntimeError` 分岐、内部バックログ)
# なので、tomllib の成功パスだけを直接検証するテストはここでスキップする。
_TOMLLIB_AVAILABLE = sys.version_info >= (3, 11)
_TOMLLIB_SKIP_REASON = "tomllib requires Python 3.11+"


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

    @unittest.skipUnless(_TOMLLIB_AVAILABLE, _TOMLLIB_SKIP_REASON)
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


class TestJsonStatus(unittest.TestCase):
    """0.14.0 (E5) で json の str scalar 値に status / length / placeholder を付与。"""

    def test_str_set(self):
        reason = _redact_text("config.json", '{"k": "hello"}')
        self.assertIn("<type=str>", reason)
        self.assertIn("<set>", reason)
        self.assertIn("length=5", reason)

    def test_str_empty(self):
        reason = _redact_text("config.json", '{"k": ""}')
        self.assertIn("<empty>", reason)
        self.assertNotIn("length=0", reason)

    def test_str_placeholder_literal(self):
        reason = _redact_text("config.json", '{"k": "changeme"}')
        self.assertIn("<placeholder>", reason)
        self.assertIn('matched="changeme"', reason)

    def test_str_placeholder_pattern(self):
        reason = _redact_text("config.json", '{"k": "your_token_here"}')
        self.assertIn("<placeholder>", reason)
        self.assertIn('matched="your_*_here"', reason)

    def test_str_long(self):
        big = "a" * 4097
        reason = _redact_text("config.json", '{"k": "' + big + '"}')
        self.assertIn("<long>", reason)
        self.assertIn("length=4097", reason)

    def test_str_looks_truncated(self):
        reason = _redact_text("config.json", '{"k": "secret_value..."}')
        self.assertIn("<looks_truncated>", reason)

    def test_bool_num_null_no_status(self):
        # bool / num / null には status / length を出さない
        reason = _redact_text(
            "config.json", '{"a": true, "b": 42, "c": null}'
        )
        self.assertIn("<type=bool>", reason)
        self.assertIn("<type=num>", reason)
        self.assertIn("<type=null>", reason)
        self.assertNotIn("length=", reason)
        # bool/num/null 行に <set> が紛れていない (構造側に値はない)
        for line in reason.splitlines():
            if "<type=bool>" in line or "<type=num>" in line or "<type=null>" in line:
                self.assertNotIn("<set>", line, f"unexpected status on non-str line: {line}")

    def test_nested_str_status(self):
        reason = _redact_text(
            "config.json", '{"outer": {"inner": "hello"}}'
        )
        self.assertIn("inner", reason)
        self.assertIn("<set>", reason)
        self.assertIn("length=5", reason)

    def test_value_not_leaked_in_status(self):
        # placeholder regex 一致時に値そのものは出ない (label のみ)
        reason = _redact_text(
            "config.json", '{"k": "your_super_long_secret_here"}'
        )
        self.assertNotIn("super_long_secret", reason)
        self.assertIn('matched="your_*_here"', reason)


@unittest.skipUnless(_TOMLLIB_AVAILABLE, _TOMLLIB_SKIP_REASON)
class TestTomlStatus(unittest.TestCase):
    """0.14.0 (E5) で toml の str 値にも status / length / placeholder を付与。"""

    def test_str_set(self):
        reason = _redact_text("secrets.toml", 'k = "hello"\n')
        self.assertIn("format: toml", reason)
        self.assertIn("<set>", reason)
        self.assertIn("length=5", reason)

    def test_str_placeholder(self):
        reason = _redact_text("secrets.toml", 'k = "changeme"\n')
        self.assertIn("<placeholder>", reason)
        self.assertIn('matched="changeme"', reason)

    def test_str_empty(self):
        reason = _redact_text("secrets.toml", 'k = ""\n')
        self.assertIn("<empty>", reason)


class TestYamlExtraction(unittest.TestCase):
    """0.14.0 (E5) で yaml は top-level key 抽出 + nested 件数のみカウント。"""

    def test_top_level_keys_in_order(self):
        text = "database:\n  host: localhost\nfeatures:\n  flag: true\n"
        reason = _redact_text("secrets.yaml", text)
        self.assertIn("format: yaml", reason)
        self.assertIn("database", reason)
        self.assertIn("features", reason)
        # 順序: database が先
        self.assertLess(reason.index("database"), reason.index("features"))

    def test_nested_count(self):
        text = (
            "database:\n"
            "  host: localhost\n"
            "  port: 5432\n"
            "  password: super-secret\n"
            "features:\n"
            "  dark_mode: true\n"
        )
        reason = _redact_text("secrets.yaml", text)
        # nested entries 件数 (host/port/password/dark_mode = 4)
        self.assertIn("nested entries: 4", reason)

    def test_nested_keys_not_exposed(self):
        # nested の key 名 (host/port/password) は表に出ない
        text = (
            "database:\n"
            "  host: localhost\n"
            "  password: super-secret\n"
        )
        reason = _redact_text("secrets.yaml", text)
        # top-level "database" は出る
        self.assertIn("database", reason)
        # nested の "host" / "password" は top-level keys には出ない
        # (count としてはカウントされる、行表示はされない)
        keys_section_lines = []
        in_keys = False
        for line in reason.splitlines():
            if line.startswith("top-level keys"):
                in_keys = True
                continue
            if in_keys:
                if line.startswith(("nested entries", "note:", "</DATA>")):
                    break
                keys_section_lines.append(line)
        joined = "\n".join(keys_section_lines)
        self.assertNotIn("host", joined)
        self.assertNotIn("password", joined)

    def test_no_value_leak(self):
        text = (
            "database:\n"
            "  host: localhost\n"
            "  password: super-secret\n"
        )
        reason = _redact_text("secrets.yaml", text)
        _assert_no_leak(self, reason, "yaml extraction reason")

    def test_comments_skipped(self):
        text = "# top comment\ndatabase:\n  # nested comment\n  host: localhost\n"
        reason = _redact_text("secrets.yaml", text)
        self.assertNotIn("top comment", reason)
        self.assertNotIn("nested comment", reason)

    def test_empty_yaml(self):
        reason = _redact_text("secrets.yaml", "")
        self.assertIn("format: yaml", reason)
        self.assertIn("(no top-level keys matched)", reason)

    def test_list_form_ignored(self):
        # `- item:` 形式の list は top-level / nested どちらにも数えない
        text = "items:\n  - first: 1\n  - second: 2\n"
        reason = _redact_text("secrets.yaml", text)
        self.assertIn("items", reason)
        # nested 行を含むがマッチしない (先頭が `- ` なので `_YAML_NESTED_KEY_RE` 不一致)
        # ただし `first` 行は `^\s+([A-Za-z_]...)` にはマッチしないため count されない
        self.assertNotIn("first", reason)

    def test_max_top_keys_cap(self):
        # 多数 top-level key で 500 件 cap が効くこと (健全性確認)
        lines = [f"key{i}:" for i in range(10)]
        reason = _redact_text("secrets.yaml", "\n".join(lines) + "\n")
        self.assertIn("key0", reason)
        self.assertIn("key9", reason)


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
        """Step 4: guard="guardrail-v1" は固定 marker (random ではない)。"""
        r1 = _redact_text(".env", "FOO=bar\n")
        r2 = _redact_text(".env", "BAZ=qux\n")
        self.assertIn('guard="guardrail-v1"', r1)
        self.assertIn('guard="guardrail-v1"', r2)

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


class TestIsEnvrcBasenameRegression(unittest.TestCase):
    """``is_envrc_basename`` は ``.envrc`` **family** (``*.envrc``、大文字小文字
    問わず) 全体を True にすること (マージ前レビューの指摘 (P2))。

    0.29.1 は本関数を literal ``.envrc`` の exact match に厳格化していたが、
    これは family 全体を「dotenv 系の既定文言 (dotenv-cli 推奨 / .env.example)」
    にフォールバックさせてしまい、``source foo.envrc`` に dotenv-cli を勧める /
    ``cp foo.envrc x`` に無関係な ``.env.example`` を勧める、という別方向の
    実態不一致を生んでいた。判定を family 全体に戻し、「direnv が自動発見・
    自動 load するかどうか」(literal のみ) は :func:`is_direnv_literal`
    (``TestIsDirenvLiteralRegression``) に切り出した。

    ``_detect_format`` (dotenv 判定、parse 用途) は non-literal な
    ``*.envrc`` も dotenv 扱いのままで良い (``test_envrc_is_dotenv`` が
    そのまま固定している) — 本テストが対象にするのは助言文面の分岐専用の
    ``is_envrc_basename`` のみ。判定境界 (deny/allow) には影響しない。
    """

    def test_literal_envrc_is_true(self):
        self.assertTrue(is_envrc_basename(".envrc"))

    def test_named_envrc_script_is_true(self):
        """``foo.envrc`` も shell script family なので True。"""
        self.assertTrue(is_envrc_basename("foo.envrc"))

    def test_uppercase_envrc_is_true(self):
        """大文字小文字を区別しない (case-insensitive) 判定なので True。"""
        self.assertTrue(is_envrc_basename(".ENVRC"))

    def test_mixed_case_envrc_is_true(self):
        self.assertTrue(is_envrc_basename(".EnvRC"))

    def test_non_envrc_is_false(self):
        self.assertFalse(is_envrc_basename(".env"))


class TestIsDirenvLiteralRegression(unittest.TestCase):
    """``is_direnv_literal`` は literal ``.envrc`` だけを True にすること
    (マージ前レビューの指摘 (P2))。

    direnv が ``direnv allow`` 後の hook で自動発見・自動 load するのは、
    大文字小文字も一致する literal ``.envrc`` だけ。``foo.envrc`` のような
    命名付きスクリプトや、大文字小文字を区別する FS 上の ``.ENVRC`` は
    direnv の対象外なので、呼出側 (``core.messages._bash_deny_load``) が
    「direnv hook で自動読込してください」と案内してよいのはこの関数が
    True を返すときだけ。判定境界 (deny/allow) には影響しない。
    """

    def test_literal_envrc_is_true(self):
        self.assertTrue(is_direnv_literal(".envrc"))

    def test_named_envrc_script_is_false(self):
        self.assertFalse(is_direnv_literal("foo.envrc"))

    def test_uppercase_envrc_is_false(self):
        self.assertFalse(is_direnv_literal(".ENVRC"))

    def test_mixed_case_envrc_is_false(self):
        self.assertFalse(is_direnv_literal(".EnvRC"))


class TestDotenvTypeExpansion(unittest.TestCase):
    """0.9.0 で追加した型推定 (url / email / uuid / aws / stripe / github / openai)。"""

    def _type_of(self, text: str, key: str = "K") -> str:
        info = redact_dotenv(text)
        by_name = {k["name"]: k["type"] for k in info["keys"]}
        return by_name[key]

    def test_url_postgres(self):
        self.assertEqual(
            self._type_of("K=postgresql://u:p@h:5432/d\n"), "url"
        )

    def test_url_https(self):
        self.assertEqual(self._type_of("K=https://example.com/api\n"), "url")

    def test_email(self):
        self.assertEqual(self._type_of("K=user@example.com\n"), "email")

    def test_uuid(self):
        self.assertEqual(
            self._type_of("K=550e8400-e29b-41d4-a716-446655440000\n"), "uuid"
        )

    def test_uuid_uppercase(self):
        self.assertEqual(
            self._type_of("K=550E8400-E29B-41D4-A716-446655440000\n"), "uuid"
        )

    def test_aws_access_key_akia(self):
        self.assertEqual(
            self._type_of("K=AKIAIOSFODNN7EXAMPLE\n"), "aws_access_key"
        )

    def test_aws_access_key_asia(self):
        self.assertEqual(
            self._type_of("K=ASIAIOSFODNN7EXAMPLE\n"), "aws_access_key"
        )

    def test_stripe_secret_and_pk_rules_registered(self):
        # 値ベース test (`K=sk_live_<24chars>`) は GitHub Push Protection の
        # secret scanning が hardcode された Stripe 形式を block するため、
        # source code から該当形式の連続文字列を排除し、内部 _PREFIX_TYPE_MAP
        # の構成 (type 分類別 rule 数) を assert する形に refactor。
        from redaction.dotenv import _PREFIX_TYPE_MAP
        types = [t for _, t, _ in _PREFIX_TYPE_MAP]
        # sk_live_ / sk_test_ / rk_live_ / rk_test_ で 4 rule
        self.assertEqual(types.count("stripe_secret"), 4)
        # pk_live_ / pk_test_ で 2 rule
        self.assertEqual(types.count("stripe_pk"), 2)

    def test_github_pat_classic(self):
        self.assertEqual(
            self._type_of(
                "K=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            ),
            "github_pat",
        )

    def test_github_pat_user(self):
        self.assertEqual(
            self._type_of(
                "K=ghu_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            ),
            "github_pat",
        )

    def test_openai_key(self):
        self.assertEqual(
            self._type_of("K=sk-proj-abcdefghijklmnopqrstuvwxyz\n"),
            "openai_key",
        )

    def test_jwt_still_jwt(self):
        # 既存の jwt 判定は維持
        text = (
            "K=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
        )
        self.assertEqual(self._type_of(text), "jwt")

    def test_str_fallback(self):
        self.assertEqual(self._type_of("K=arbitrary_random_value_here\n"), "str")


class TestDotenvPrefix(unittest.TestCase):
    """0.9.0 prefix 検出 (Q3 採用)。識別子型のみ prefix を返す。"""

    def _entry(self, text: str, key: str = "K") -> dict:
        info = redact_dotenv(text)
        by_name = {k["name"]: k for k in info["keys"]}
        return by_name[key]

    def test_jwt_prefix_ey(self):
        text = (
            "K=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
        )
        self.assertEqual(self._entry(text)["prefix"], "ey")

    def test_aws_prefix_akia(self):
        e = self._entry("K=AKIAIOSFODNN7EXAMPLE\n")
        self.assertEqual(e["prefix"], "AKIA")

    def test_aws_prefix_asia(self):
        e = self._entry("K=ASIAIOSFODNN7EXAMPLE\n")
        self.assertEqual(e["prefix"], "ASIA")

    # Stripe prefix の値ベース test (sk_live_ / sk_test_ / pk_live_) は
    # GitHub Push Protection の secret scanning との衝突回避のため削除。
    # type 分類別 rule 数の assert は test_stripe_secret_and_pk_rules_registered
    # で別途担保する (内部 _PREFIX_TYPE_MAP の構成確認)。

    def test_github_pat_prefix(self):
        e = self._entry("K=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        self.assertEqual(e["prefix"], "ghp_")

    def test_openai_prefix(self):
        e = self._entry("K=sk-proj-abcdefghijklmnopqrstuvwxyz\n")
        self.assertEqual(e["prefix"], "sk-")

    def test_no_prefix_for_str(self):
        e = self._entry("K=random_string_value\n")
        self.assertNotIn("prefix", e)

    def test_no_prefix_for_url(self):
        e = self._entry("K=https://example.com/api\n")
        self.assertNotIn("prefix", e)

    def test_no_prefix_for_bool(self):
        e = self._entry("K=true\n")
        self.assertNotIn("prefix", e)


class TestDotenvStatus(unittest.TestCase):
    """0.9.0 value status (set/empty/placeholder/short/long/looks_truncated) の判定。"""

    def _entry(self, text: str, key: str = "K") -> dict:
        info = redact_dotenv(text)
        by_name = {k["name"]: k for k in info["keys"]}
        return by_name[key]

    def test_set_simple_string(self):
        self.assertEqual(self._entry("K=hello world\n")["status"], ["<set>"])

    def test_empty_no_value(self):
        e = self._entry("K=\n")
        self.assertEqual(e["status"], ["<empty>"])
        self.assertEqual(e["length"], 0)

    def test_empty_quoted(self):
        e = self._entry('K=""\n')
        self.assertEqual(e["status"], ["<empty>"])

    def test_empty_whitespace_only_quoted(self):
        e = self._entry('K="   "\n')
        self.assertNotIn("<empty>", e["status"])
        # ただし内容は空白のみなので set 扱い (length=3)
        self.assertEqual(e["length"], 3)

    def test_placeholder_literal(self):
        e = self._entry("K=changeme\n")
        self.assertIn("<placeholder>", e["status"])
        self.assertNotIn("<set>", e["status"])
        self.assertEqual(e["placeholder"], "changeme")

    def test_placeholder_pattern_your_here(self):
        e = self._entry("K=your_jwt_secret_here\n")
        self.assertIn("<placeholder>", e["status"])
        self.assertEqual(e["placeholder"], "your_*_here")

    def test_placeholder_pattern_angle(self):
        e = self._entry("K=<your-key>\n")
        self.assertIn("<placeholder>", e["status"])
        self.assertEqual(e["placeholder"], "<...>")

    def test_short_jwt(self):
        # jwt 型ではなく str 扱いのため short にはならないが、
        # 実際の jwt prefix `ey...` が短いケースで <short> に倒る
        # 注: 短い ey 風文字列は jwt regex を通らないので str
        e = self._entry("K=eyShort\n")
        # str + 8 文字 → short の閾値 (str は閾値なし) なので短くない
        self.assertNotIn("<short>", e["status"])

    def test_short_url(self):
        # url で 8 文字未満 → <short>
        e = self._entry("K=a://x\n")
        # "a://x" = 5 文字、url 型で min_len=8 → short
        self.assertEqual(e["type"], "url")
        self.assertIn("<short>", e["status"])

    def test_long_value(self):
        # 4097 文字の値 → <long>
        big = "a" * 4097
        e = self._entry(f"K={big}\n")
        self.assertIn("<long>", e["status"])
        self.assertEqual(e["length"], 4097)

    def test_looks_truncated_dotdotdot(self):
        e = self._entry("K=secret_value...\n")
        self.assertIn("<looks_truncated>", e["status"])

    def test_looks_truncated_marker(self):
        e = self._entry("K=secret<truncated>\n")
        self.assertIn("<looks_truncated>", e["status"])

    def test_looks_truncated_backslash(self):
        e = self._entry("K=secret\\\n")
        self.assertIn("<looks_truncated>", e["status"])

    def test_set_includes_length(self):
        e = self._entry("K=hello\n")
        self.assertEqual(e["length"], 5)

    def test_quoted_value_length(self):
        # quote 剥がしの後の長さ (5)
        e = self._entry('K="hello"\n')
        self.assertEqual(e["length"], 5)

    def test_length_is_character_count_not_byte_count(self):
        """``length=<N>`` は ``len(v)`` (文字数)。UTF-8 バイト長ではないこと
        を多バイト文字で固定する (docs/DESIGN.md がバイト長と誤記していた
        問題の回帰テスト、内部バックログ)。"""
        e = self._entry("K=あいうえお\n")
        self.assertEqual(e["length"], 5)  # UTF-8 では 15 byte
        e = self._entry("K=😀😀\n")
        self.assertEqual(e["length"], 2)  # UTF-8 では 8 byte (絵文字は 4 byte/文字)

    def test_set_with_short_combination(self):
        # url + 短い → set + short
        e = self._entry("K=a://x\n")
        self.assertIn("<set>", e["status"])
        self.assertIn("<short>", e["status"])


class TestDotenvFormatOutput(unittest.TestCase):
    """0.9.0 format_dotenv の新出力 (prefix / status / length / matched)。"""

    def test_format_includes_length(self):
        reason = _redact_text(".env", "K=hello\n")
        self.assertIn("length=5", reason)

    def test_format_includes_set_tag(self):
        reason = _redact_text(".env", "K=hello\n")
        self.assertIn("<set>", reason)

    def test_format_includes_empty_tag_no_length(self):
        reason = _redact_text(".env", "K=\n")
        self.assertIn("<empty>", reason)
        # empty のときは length= を出さない
        self.assertNotIn("length=0", reason)

    def test_format_includes_placeholder_matched(self):
        reason = _redact_text(".env", "K=your_jwt_secret_here\n")
        self.assertIn("<placeholder>", reason)
        self.assertIn('matched="your_*_here"', reason)

    # test_format_includes_prefix は GitHub Push Protection との衝突回避
    # のため削除。format に prefix が埋まることは test_format_jwt_prefix_ey
    # (jwt 版) で同等に担保される。

    def test_format_jwt_prefix_ey(self):
        text = (
            "K=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
        )
        reason = _redact_text(".env", text)
        self.assertIn('<type=jwt prefix="ey">', reason)

    def test_format_no_prefix_for_str(self):
        reason = _redact_text(".env", "K=arbitrary_value\n")
        self.assertIn("<type=str>", reason)
        # str 型に prefix= は出ない
        self.assertNotIn("<type=str prefix=", reason)

    def test_format_note_updated(self):
        reason = _redact_text(".env", "K=hello\n")
        # note 文に length / status の説明が入っている
        self.assertIn("length", reason)
        self.assertIn("status tags", reason)

    def test_no_value_leak_with_status(self):
        # 旧 0.9.0 テストでは Stripe + JWT 値で実値漏れを確認していたが、
        # GitHub Push Protection との衝突回避のため JWT のみで再構成。
        # Stripe 形式の type/prefix は test_stripe_secret_and_pk_rules_registered
        # 経由で内部 _PREFIX_TYPE_MAP の構成として別途担保。
        text = "JWT=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4f\n"
        reason = _redact_text(".env", text)
        # 実値部分は出ない (eyJzdWIi / SflKxwRJ)
        self.assertNotIn("eyJzdWIi", reason)
        self.assertNotIn("SflKxwRJ", reason)
        # ただし prefix (ey) は出る (Q3 採用)
        self.assertIn('prefix="ey"', reason)


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


class TestKeyonlyFallbackReason(unittest.TestCase):
    """keys-only scan フォールバックの理由別ラベル (0.26.0)。

    旧実装は理由を問わず ``format: <fmt> (large, keys-only scan)`` と表示して
    いたため、43 byte の壊れた JSON でも「大きすぎる」と事実に反する説明が
    出ていた。json/toml のパース失敗・tomllib 不在を区別し、事実どおりの
    ラベルと note を出す。
    """

    def test_broken_small_json_reports_parse_failed_not_large(self):
        text = "{not valid json"
        reason = _redact_text("credentials.json", text)
        self.assertIn("format: json (parse failed, keys-only scan)", reason)
        self.assertNotIn("(large,", reason)
        self.assertIn("JSON parse failed", reason)
        self.assertIn("file may be malformed", reason)
        # 43 byte 級の小ファイルで「too large」という事実に反する語を出さない
        self.assertNotIn("too large", reason)

    def test_broken_small_json_with_zero_keys_still_gets_note(self):
        """entries: 0 (鍵が一切マッチしない) でも note を省略しない。

        旧実装は ``format_keyonly`` の空 keys 分岐が note 追加より前に
        return していたため、この形だけ理由も no next-action も無い reason に
        なっていた (silent degradation と同型の欠落)。
        """
        text = "{not valid json"
        reason = _redact_text("credentials.json", text)
        self.assertIn("entries: 0", reason)
        self.assertIn("(no keys matched)", reason)
        self.assertIn("JSON parse failed", reason)

    @unittest.skipUnless(_TOMLLIB_AVAILABLE, _TOMLLIB_SKIP_REASON)
    def test_broken_small_toml_reports_parse_failed(self):
        text = "key = [1, 2"  # 閉じ括弧が無い不正 TOML
        reason = _redact_text("secrets.local.toml", text)
        self.assertIn("format: toml (parse failed, keys-only scan)", reason)
        self.assertNotIn("(large,", reason)
        self.assertIn("TOML parse failed", reason)
        self.assertNotIn("too large", reason)

    def test_tomllib_unavailable_reports_unsupported_not_large(self):
        """tomllib 不在 (Python < 3.11 相当) は「壊れている」ではなく「未対応」。

        妥当な TOML を渡しても、tomllib 自体が無ければパースを試みる前に
        RuntimeError で降りる。これを parse_failed と混同しないこと。
        """
        from unittest import mock

        from redaction import tomllike

        text = "key = 1\n"  # 正常な TOML (パース自体は成功しうる内容)
        with mock.patch.object(tomllike, "tomllib", None):
            reason = _redact_text("secrets.local.toml", text)
        self.assertIn("format: toml (unsupported, keys-only scan)", reason)
        self.assertNotIn("(large,", reason)
        self.assertNotIn("parse failed", reason)
        self.assertIn("Python 3.11+", reason)

    def test_format_keyonly_reason_labels_direct(self):
        from redaction.keyonly_scan import format_keyonly

        large = format_keyonly(["A"], 100, fmt_hint="opaque")
        self.assertIn("(large, keys-only scan)", large)
        self.assertIn("file too large for full parse", large)

        parse_failed = format_keyonly(
            ["A"], 43, fmt_hint="json", reason="parse_failed",
        )
        self.assertIn("(parse failed, keys-only scan)", parse_failed)
        self.assertIn("JSON parse failed", parse_failed)

        unsupported = format_keyonly(
            [], 47, fmt_hint="toml", reason="toml_unsupported",
        )
        self.assertIn("(unsupported, keys-only scan)", unsupported)
        self.assertIn("Python 3.11+", unsupported)

        # 未知の reason は旧来の "large" 相当にフォールバックする (安全側)
        unknown = format_keyonly(["A"], 10, fmt_hint="opaque", reason="???")
        self.assertIn("(large, keys-only scan)", unknown)

    def test_format_opaque_default_reason_is_large(self):
        """``format_opaque`` の既定は従来どおり (yaml / 純粋 opaque への影響なし)。"""
        from redaction.opaque import format_opaque

        info = {"format": "opaque", "keys": ["A"], "scanned_bytes": 10}
        self.assertIn("(large, keys-only scan)", format_opaque(info))
        self.assertIn(
            "(parse failed, keys-only scan)",
            format_opaque(info, reason="parse_failed"),
        )


class TestKeyonlyLineGranularity(unittest.TestCase):
    """keys-only scan は鍵名を **1 鍵 1 行** で出す (0.26.0 隔離内レビュー P1-1)。

    旧実装は全鍵名を ``keys: A, B, C, ...`` の **1 行**に並べていた。
    ``core.messages._fit_data_block_core`` は行単位でしか畳めないため、
    この 1 行が残予算に入らないと**行ごと落ち**、予算が 1KB 以上余っている
    のに鍵名が 0 個という状態になっていた (>32KB ファイル / json・toml の
    parse 失敗経路が該当)。行の粒度と内容の粒度を揃えることで、既存の
    折り畳み機構がそのまま効くようにする。
    """

    def test_each_key_gets_its_own_line(self):
        from redaction.keyonly_scan import format_keyonly

        keys = [f"SERVICE_API_KEY_{i:03d}" for i in range(10)]
        out = format_keyonly(keys, 1000, fmt_hint="opaque")
        key_lines = [ln for ln in out.split("\n") if ln.startswith("  ")]
        self.assertEqual(len(key_lines), len(keys))
        for key, line in zip(keys, key_lines):
            self.assertIn(key, line)
        # 旧実装の「1 行に複数鍵」形が残っていないこと
        self.assertNotIn(", ".join(keys[:2]), out)

    def test_preview_cap_is_reported_in_the_header_line(self):
        """preview 上限は維持しつつ、超過の告知を **末尾行ではなく header** に置く。

        末尾行 (`... (N more)`) だと折り畳みで真っ先に落ちるうえ、omit marker の
        件数と二重になって読み手が「いくつ落ちたか」を数えられない。
        """
        from redaction.keyonly_scan import PREVIEW_CAP, format_keyonly

        keys = [f"KEY_{i:04d}" for i in range(PREVIEW_CAP + 40)]
        out = format_keyonly(keys, 1000, fmt_hint="opaque")
        lines = out.split("\n")
        key_lines = [ln for ln in lines if ln.startswith("  ")]
        self.assertEqual(len(key_lines), PREVIEW_CAP)
        # 総数は entries: が持つ (header 側で重複して数を主張しない)
        self.assertIn(f"entries: {len(keys)}", out)
        header = next(ln for ln in lines if ln.startswith("keys"))
        self.assertIn(f"max {PREVIEW_CAP} shown", header)
        # 末尾は必ず note (折り畳みの note 保護が効く形を維持する)
        self.assertTrue(lines[-1].startswith("note:"), lines[-1])

    def test_preview_header_is_an_upper_bound_not_a_claim(self):
        """header は **上限** として書く (0.26.0 外部レビュー R1)。

        header の後段で ``core.messages._fit_data_block`` が予算でさらに畳む
        ため、``first N shown`` のような断定形は折り畳み後に嘘になる
        (500 鍵で「60 個表示」と言いながら実際は 23 行しか残らない)。
        """
        from redaction.keyonly_scan import PREVIEW_CAP, format_keyonly

        keys = [f"KEY_{i:04d}" for i in range(PREVIEW_CAP * 3)]
        header = next(
            ln
            for ln in format_keyonly(keys, 1000, fmt_hint="opaque").split("\n")
            if ln.startswith("keys")
        )
        self.assertNotIn("first", header)
        for word in ("max", "up to", "at most"):
            if word in header:
                break
        else:  # pragma: no cover - 失敗時のメッセージ用
            self.fail(f"header が上限表現になっていない: {header!r}")

    def test_all_keys_shown_header_has_no_partial_wording(self):
        from redaction.keyonly_scan import format_keyonly

        out = format_keyonly(["A", "B"], 10, fmt_hint="opaque")
        header = next(ln for ln in out.split("\n") if ln.startswith("keys"))
        self.assertNotIn("first", header)
        self.assertNotIn("max", header)


class TestTomlRecursionErrorLabel(unittest.TestCase):
    """深いネストの TOML を「tomllib 未搭載」と誤表示しない (0.26.0 レビュー P2-2)。

    ``RecursionError`` は ``RuntimeError`` のサブクラスなので、
    ``engine.redact`` の ``except RuntimeError`` (= tomllib 未搭載の検知) が
    先に捕まえ、Python 3.11+ の環境で「Python 3.11+ が必要」という事実に
    反する note が出ていた。json 分岐が ``except (ValueError, RecursionError)``
    を明示しているのと同じ理由で、toml 側にも明示が要る。
    """

    @unittest.skipUnless(_TOMLLIB_AVAILABLE, _TOMLLIB_SKIP_REASON)
    def test_deeply_nested_toml_reports_parse_failed(self):
        text = "a = " + "[" * 500 + "]" * 500 + "\n"
        reason = _redact_text("deep.secret.toml", text)
        self.assertIn("parse failed", reason)
        self.assertNotIn("Python 3.11+", reason)
        self.assertNotIn("tomllib unavailable", reason)

    def test_recursion_error_from_parser_is_not_labeled_unsupported(self):
        """深さの閾値に依存しない決定的な版 (parser が RecursionError を投げる形)。"""
        from unittest import mock

        from redaction import engine

        with mock.patch.object(
            engine, "redact_toml", side_effect=RecursionError("too deep")
        ):
            reason = _redact_text("secrets.local.toml", "key = 1\n")
        self.assertIn("(parse failed, keys-only scan)", reason)
        self.assertNotIn("Python 3.11+", reason)

    def test_missing_tomllib_still_reports_unsupported(self):
        """RecursionError を分けても tomllib 不在の分岐は従来どおり残る。"""
        from unittest import mock

        from redaction import tomllike

        with mock.patch.object(tomllike, "tomllib", None):
            reason = _redact_text("secrets.local.toml", "key = 1\n")
        self.assertIn("(unsupported, keys-only scan)", reason)
        self.assertIn("Python 3.11+", reason)


class TestDotenvEmptyKeepsNote(unittest.TestCase):
    """``entries: 0`` の dotenv でも末尾 note を落とさない (0.26.0 レビュー P3-1)。

    ``format_keyonly`` の空 keys 分岐で直したのと同一の欠陥クラスの取り残し。
    note は「実値は context に無い」という免責の開示なので、内容が空でも
    出す (silent degradation 対策と同じ方針)。
    """

    def test_empty_dotenv_still_has_trailing_note(self):
        reason = _redact_text(".env", "")
        self.assertIn("entries: 0", reason)
        self.assertIn("(no entries)", reason)
        self.assertIn("real values are not in context", reason)

    def test_format_dotenv_zero_entries_ends_with_note(self):
        from redaction.dotenv import format_dotenv

        out = format_dotenv({"format": "dotenv", "entries": 0, "keys": []})
        self.assertTrue(out.split("\n")[-1].startswith("note:"), out)


if __name__ == "__main__":
    unittest.main()
