"""is_sensitive の basename/parts/exclude/last-match-wins の確認。

Step 3 以降:
- case-insensitive 化: 既定で有効 (``SFG_CASE_SENSITIVE=1`` で旧挙動復帰)
- ``fnmatchcase`` 採用: OS 依存の挙動差を排除
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from _shared.matcher import is_sensitive


# 既定 patterns.txt 相当の rules (既定 → ローカルの順で last-match-wins 評価)。
DEFAULT_RULES: list[tuple[str, bool]] = [
    ("*.local.json", False),
    ("*.local.yaml", False),
    ("*.local.yml", False),
    ("*.local.toml", False),
    ("*.secret*", False),
    (".env", False),
    (".env.*", False),
    (".envrc", False),
    ("*.envrc", False),
    ("*.pem", False),
    ("*.key", False),
    ("*.p12", False),
    ("*.pfx", False),
    ("*.keystore", False),
    ("*.jks", False),
    ("id_rsa*", False),
    ("id_dsa*", False),
    ("id_ecdsa*", False),
    ("id_ed25519*", False),
    ("credentials*.json", False),
    ("service-account*.json", False),
    (".npmrc", False),
    (".pypirc", False),
    (".netrc", False),
    ("*.example", True),
    ("*.template", True),
    ("*.sample", True),
    ("*.dist", True),
    ("*.example.*", True),
    ("*.template.*", True),
    ("*.sample.*", True),
    ("*.dist.*", True),
    ("*.pub", True),
]


class TestMatcherDotenv(unittest.TestCase):
    def test_dotenv_basename(self):
        self.assertTrue(is_sensitive(".env", DEFAULT_RULES))
        self.assertTrue(is_sensitive("/foo/bar/.env", DEFAULT_RULES))
        self.assertTrue(is_sensitive(".env.production", DEFAULT_RULES))

    def test_exclude_wins_on_template(self):
        self.assertFalse(is_sensitive(".env.example", DEFAULT_RULES))
        self.assertFalse(is_sensitive("config.local.json.template", DEFAULT_RULES))


class TestMatcherBasic(unittest.TestCase):
    def test_non_sensitive(self):
        self.assertFalse(is_sensitive("README.md", DEFAULT_RULES))
        self.assertFalse(is_sensitive("src/main.py", DEFAULT_RULES))
        self.assertFalse(is_sensitive("package.json", DEFAULT_RULES))

    def test_secret_wildcard(self):
        self.assertTrue(is_sensitive("app.secret", DEFAULT_RULES))
        self.assertTrue(is_sensitive("app.secrets.yaml", DEFAULT_RULES))

    def test_local_suffix(self):
        self.assertTrue(is_sensitive("accounts.local.json", DEFAULT_RULES))
        self.assertFalse(is_sensitive("accounts.json", DEFAULT_RULES))

    def test_parts_match_parent_dir(self):
        # 親ディレクトリ名が機密パターンの場合 (symlink race 等の偽装対策)
        self.assertTrue(is_sensitive("/foo/.env/leak.txt", DEFAULT_RULES))

    def test_empty_rules(self):
        self.assertFalse(is_sensitive(".env", []))


class TestMatcherKeysAndCerts(unittest.TestCase):
    """鍵・証明書・クレデンシャル系の pattern 拡張 (#9) を確認。"""

    def test_keys_detected(self):
        self.assertTrue(is_sensitive("id_rsa", DEFAULT_RULES))
        self.assertTrue(is_sensitive("id_ed25519", DEFAULT_RULES))
        self.assertTrue(is_sensitive("id_ecdsa", DEFAULT_RULES))
        self.assertTrue(is_sensitive("id_dsa", DEFAULT_RULES))
        self.assertTrue(is_sensitive("foo.pem", DEFAULT_RULES))
        self.assertTrue(is_sensitive("foo.key", DEFAULT_RULES))
        self.assertTrue(is_sensitive("keystore.jks", DEFAULT_RULES))
        self.assertTrue(is_sensitive("release.keystore", DEFAULT_RULES))
        self.assertTrue(is_sensitive("cert.p12", DEFAULT_RULES))
        self.assertTrue(is_sensitive("cert.pfx", DEFAULT_RULES))

    def test_credentials_detected(self):
        self.assertTrue(is_sensitive("credentials.json", DEFAULT_RULES))
        self.assertTrue(is_sensitive("credentials-prod.json", DEFAULT_RULES))
        self.assertTrue(is_sensitive("service-account.json", DEFAULT_RULES))
        self.assertTrue(is_sensitive("service-account-abc.json", DEFAULT_RULES))
        self.assertTrue(is_sensitive(".npmrc", DEFAULT_RULES))
        self.assertTrue(is_sensitive(".pypirc", DEFAULT_RULES))
        self.assertTrue(is_sensitive(".netrc", DEFAULT_RULES))

    def test_public_keys_excluded(self):
        # last-match-wins: include (id_rsa*) → exclude (*.pub) → exclude 勝ち
        self.assertFalse(is_sensitive("id_rsa.pub", DEFAULT_RULES))
        self.assertFalse(is_sensitive("id_ed25519.pub", DEFAULT_RULES))

    def test_template_variants_excluded(self):
        # 複合拡張子テンプレート
        self.assertFalse(is_sensitive("credentials.example.json", DEFAULT_RULES))
        self.assertFalse(is_sensitive("service-account.sample.json", DEFAULT_RULES))
        self.assertFalse(is_sensitive("cert.pem.template", DEFAULT_RULES))
        self.assertFalse(is_sensitive(".env.example", DEFAULT_RULES))


class TestCaseInsensitive(unittest.TestCase):
    """Step 3: case-insensitive 化の既定挙動と opt-out の確認。"""

    def setUp(self):
        # 既定 (SFG_CASE_SENSITIVE 未設定) に戻す
        self._env_patcher = mock.patch.dict(os.environ, clear=False)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        os.environ.pop("SFG_CASE_SENSITIVE", None)

    def test_uppercase_env_detected(self):
        self.assertTrue(is_sensitive(".ENV", DEFAULT_RULES))
        self.assertTrue(is_sensitive(".Env", DEFAULT_RULES))

    def test_uppercase_key_detected(self):
        self.assertTrue(is_sensitive("ID_RSA", DEFAULT_RULES))
        self.assertTrue(is_sensitive("ID_ED25519", DEFAULT_RULES))

    def test_uppercase_credentials_detected(self):
        self.assertTrue(is_sensitive("CREDENTIALS.JSON", DEFAULT_RULES))
        self.assertTrue(is_sensitive("Credentials.json", DEFAULT_RULES))

    def test_case_sensitive_opt_out(self):
        os.environ["SFG_CASE_SENSITIVE"] = "1"
        # 旧挙動: 大文字小文字を区別 → .ENV は小文字 pattern ".env" に一致しない
        self.assertFalse(is_sensitive(".ENV", DEFAULT_RULES))
        self.assertFalse(is_sensitive("ID_RSA", DEFAULT_RULES))
        # 小文字の正規入力は変わらず一致
        self.assertTrue(is_sensitive(".env", DEFAULT_RULES))
        self.assertTrue(is_sensitive("id_rsa", DEFAULT_RULES))


class TestFnmatchCharClassRegression(unittest.TestCase):
    """patterns.txt に文字クラス (``[...]``) が無いことを固定する。

    fnmatch の ``[abc]`` は文字クラスとして解釈されるため、将来誤って追加すると
    予期せぬ誤検出が起きる。
    """

    def test_no_char_class_in_defaults(self):
        patterns_file = (
            FIXTURES.parent.parent.parent
            / "check-sensitive-files"
            / "patterns.txt"
        )
        text = patterns_file.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertNotIn(
                "[", stripped,
                f"character class '[' detected in pattern: {stripped}",
            )
            self.assertNotIn(
                "]", stripped,
                f"character class ']' detected in pattern: {stripped}",
            )


class TestLastMatchWins(unittest.TestCase):
    """既定 exclude をローカル include で打ち消せること (#10)。"""

    def test_local_overrides_default_exclude(self):
        # 既定には !*.pub (exclude) が入っている
        self.assertFalse(is_sensitive("test.pub", DEFAULT_RULES))
        # ユーザーが patterns.local.txt で *.pub (include) を足すと override
        rules = DEFAULT_RULES + [("*.pub", False)]
        self.assertTrue(is_sensitive("test.pub", rules))
        self.assertTrue(is_sensitive("id_rsa.pub", rules))

    def test_local_adds_extra_exclude(self):
        # ローカルで追加の exclude を重ねる
        rules = DEFAULT_RULES + [("foo.pem", True)]
        self.assertFalse(is_sensitive("foo.pem", rules))
        # 他の .pem は引き続き sensitive
        self.assertTrue(is_sensitive("other.pem", rules))

    def test_exclude_then_include_toggles_back(self):
        rules: list[tuple[str, bool]] = [
            ("*.x", False),
            ("*.x", True),
            ("*.x", False),
        ]
        self.assertTrue(is_sensitive("foo.x", rules))

    def test_no_match_returns_false(self):
        rules: list[tuple[str, bool]] = [("*.pem", False)]
        self.assertFalse(is_sensitive("README.md", rules))


if __name__ == "__main__":
    unittest.main()
