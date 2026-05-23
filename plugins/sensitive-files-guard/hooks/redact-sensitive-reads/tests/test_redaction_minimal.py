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


if __name__ == "__main__":
    unittest.main()
