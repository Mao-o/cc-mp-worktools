"""exclusion.py の判定規則 (既定 glob・語 / 追加 glob / `!` 否定 / CODE_ONLY / 大文字小文字 / サブディレクトリ)。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401  (sys.path 整備)

import exclusion

DEFAULTS = exclusion.load_policy({})


def reason(policy: exclusion.Policy, *rels: str | None) -> str | None:
    return policy.reason(rels)


class TestDefaultGlobs(unittest.TestCase):
    def test_secret_like_names_are_excluded(self):
        for rel in (
            ".env",
            ".env.local",
            ".env.production",
            "prod.env",
            ".envrc",
            "server.pem",
            "certs/server.key",
            "keystore.p12",
            "app.pfx",
            "AuthKey_ABC123.p8",
            "release.jks",
            "id_rsa",
            "id_ed25519.pub",
            ".ssh/id_ecdsa",
            "config/secrets.yaml",
            "infra/secrets/prod.yaml",
            "client_secret.json",
            "aws_credentials",
            "credentials.json",
            "gcp/service-account.json",
            "my_service_account_key.json",
            "kubeconfig",
            ".kube/kubeconfig-prod",
            "vpn/client.ovpn",
            "terraform.tfstate",
            "terraform.tfstate.backup",
            "prod.tfvars",
            "terraform.tfvars.json",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            "vault.kdbx",
            "backup.gpg",
        ):
            with self.subTest(rel=rel):
                self.assertIsNotNone(reason(DEFAULTS, rel))

    def test_ordinary_code_is_not_excluded(self):
        for rel in (
            "src/app.py",
            "id_generator.py",  # `id_*` を既定にすると巻き込まれる (SSH 鍵の実名に限定している)
            "environment.py",
            "keyboard.ts",
            "tokenizer.py",
            "password_validator.py",
            "src/secretary.py",  # `*secret*` glob だと巻き込まれる (語境界で判定している)
            "lib/secretsanta.ts",
            "nosecret/x.py",
            "service_account_manager.py",  # 既定は *service_account*.json のみ
            "kube/config.yaml",
            "hooks/hooks.json",
            "README.md",
            "docs/meeting-notes.txt",
            "Makefile",
            ".gitignore",
            "sensitive-files-guardrail/README.md",
        ):
            with self.subTest(rel=rel):
                self.assertIsNone(reason(DEFAULTS, rel))

    def test_case_insensitive(self):
        for rel in (".ENV", "Secret.JSON", "Config/CREDENTIALS.txt", "ID_RSA", "Server.PEM"):
            with self.subTest(rel=rel):
                self.assertIsNotNone(reason(DEFAULTS, rel))

    def test_subdirectory_and_directory_names(self):
        self.assertIsNotNone(reason(DEFAULTS, "deep/nested/dir/.env"))
        self.assertIsNotNone(
            reason(DEFAULTS, "config/secrets/db.yaml"), "ディレクトリ名の secrets も拾う"
        )
        self.assertIsNotNone(reason(DEFAULTS, "infra/credentials/prod.yaml"))
        self.assertIsNotNone(
            reason(DEFAULTS, "credentials-service/main.go"),
            "ディレクトリ名が語に当たれば配下のコードも除外 (逃げ道は `!credentials-service/*`)",
        )

    def test_words_need_boundaries(self):
        """`secret` / `credential` は英数字以外で区切られた単語としてだけ当てる。"""
        for rel, expected in (
            ("my-secret.txt", True),
            ("app/credential_store.py", True),
            ("SECRETS/x", True),
            (".secrets", True),
            ("secret", True),
            ("docs/secret-santa.md", True),  # 語としては当たる (害は無い)
            ("secretary.py", False),
            ("secretsanta.ts", False),
            ("nosecret", False),
            ("credentialed.py", False),
            ("topsecret123", False),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(reason(DEFAULTS, rel) is not None, expected)

    def test_reason_names_the_pattern_not_the_content(self):
        self.assertEqual(reason(DEFAULTS, "certs/server.pem"), "既定除外: *.pem")
        self.assertEqual(reason(DEFAULTS, ".env.local"), "既定除外: .env.*")
        self.assertEqual(reason(DEFAULTS, "client_secret.json"), '既定除外: 語 "secret"')
        self.assertEqual(reason(DEFAULTS, "aws_credentials"), '既定除外: 語 "credentials"')

    def test_any_candidate_name_triggers_and_explain_names_the_hit(self):
        """symlink のリンク名と実体のどちらかが当たれば除外 (None / 空の候補は無視)。"""
        self.assertEqual(
            DEFAULTS.explain(("vault/c.json", "credentials.json")),
            ("credentials.json", '既定除外: 語 "credentials"'),
        )
        self.assertEqual(DEFAULTS.explain((".env", None)), (".env", "既定除外: .env"))
        self.assertIsNone(DEFAULTS.explain(("a.py", None, "")))
        self.assertIsNone(DEFAULTS.explain(()))
        self.assertIsNotNone(reason(DEFAULTS, "Vault/C.JSON", "CREDENTIALS.json"))


class TestEnvGlobs(unittest.TestCase):
    def test_extra_globs_are_additive(self):
        policy = exclusion.load_policy({exclusion.ENV_EXCLUDE: "docs/*, *.txt"})
        self.assertEqual(
            reason(policy, "docs/design.yaml"), f"{exclusion.ENV_EXCLUDE}: docs/*"
        )
        self.assertEqual(reason(policy, "notes.txt"), f"{exclusion.ENV_EXCLUDE}: *.txt")
        self.assertIsNotNone(reason(policy, ".env"), "既定除外はそのまま効く")
        self.assertIsNone(reason(policy, "src/app.py"))

    def test_parse_globs_normalizes(self):
        self.assertEqual(
            exclusion.parse_globs(
                " ./docs/ ,, *.csv , ,notes/*.md,*.csv, /abs/, !keep/*, ! ./keep2/ ,!"
            ),
            (("docs/*", "*.csv", "notes/*.md", "abs/*"), ("keep/*", "keep2/*")),
        )
        self.assertEqual(exclusion.parse_globs(None), ((), ()))
        self.assertEqual(exclusion.parse_globs(""), ((), ()))
        self.assertEqual(exclusion.parse_globs(" , ,"), ((), ()))

    def test_odd_patterns_do_not_raise(self):
        for raw in ("[", "[a", "[!", "**", "\\", "*.{py,js}", "日本語/*", "!", "!!x"):
            with self.subTest(raw=raw):
                policy = exclusion.load_policy({exclusion.ENV_EXCLUDE: raw})
                policy.reason(("src/app.py", "[weird]/x.py"))

    def test_star_matches_across_directories(self):
        """fnmatch の `*` は `/` にもマッチする: `docs/*` は深い階層も拾う。"""
        policy = exclusion.load_policy({exclusion.ENV_EXCLUDE: "docs/"})
        self.assertIsNotNone(reason(policy, "docs/a/b/c.txt"))
        self.assertIsNone(reason(policy, "src/docs.py"))

    def test_defaults_can_be_disabled(self):
        policy = exclusion.load_policy(
            {exclusion.ENV_EXCLUDE_DEFAULTS: "0", exclusion.ENV_EXCLUDE: "*.pem"}
        )
        self.assertIsNone(reason(policy, ".env"), "既定 glob が無効化されている")
        self.assertIsNone(reason(policy, "secret_rotation.py"), "既定の語も無効化されている")
        self.assertIsNotNone(reason(DEFAULTS, "secret_rotation.py"), "既定では語 secret で除外")
        self.assertIsNotNone(reason(policy, "server.pem"), "追加 glob は効く")

    def test_defaults_stay_on_for_other_values(self):
        for value in ("", "1", "true", "yes"):
            with self.subTest(value=value):
                policy = exclusion.load_policy({exclusion.ENV_EXCLUDE_DEFAULTS: value})
                self.assertIsNotNone(reason(policy, ".env"))


class TestNegation(unittest.TestCase):
    """`!glob` は必ず送る (既定 / 追加 glob / CODE_ONLY より優先)。"""

    def test_negation_overrides_default_word(self):
        policy = exclusion.load_policy({exclusion.ENV_EXCLUDE: "!credentials-service/*"})
        self.assertIsNone(reason(policy, "credentials-service/main.go"))
        self.assertIsNotNone(reason(policy, "credentials.json"), "他の既定除外はそのまま")
        self.assertIsNotNone(reason(DEFAULTS, "credentials-service/main.go"))

    def test_negation_overrides_extra_glob_and_code_only(self):
        policy = exclusion.load_policy(
            {exclusion.ENV_EXCLUDE: "docs/*, !docs/public/*, !*.md", exclusion.ENV_CODE_ONLY: "1"}
        )
        self.assertIsNotNone(reason(policy, "docs/internal/a.txt"))
        self.assertIsNone(reason(policy, "docs/public/a.txt"))
        self.assertIsNone(reason(policy, "README.md"), "CODE_ONLY より否定が優先")
        self.assertIsNotNone(reason(policy, "notes.txt"))

    def test_negation_applies_to_any_candidate_name(self):
        policy = exclusion.load_policy({exclusion.ENV_EXCLUDE: "!vault/*"})
        self.assertIsNone(policy.explain(("vault/c.json", "credentials.json")))


class TestCodeOnly(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(DEFAULTS.code_only)
        self.assertIsNone(reason(DEFAULTS, "docs/meeting-notes.txt"))

    def test_non_code_suffixes_are_excluded(self):
        policy = exclusion.load_policy({exclusion.ENV_CODE_ONLY: "1"})
        for rel in (
            "README.md",
            "docs/meeting-notes.txt",
            "NOTES.TXT",
            "data/rows.csv",
            "report.pdf",
            "img/logo.png",
            "mail/customer.eml",
            "export.jsonl",
            "server.log",
            "archive.tar.gz",
        ):
            with self.subTest(rel=rel):
                self.assertIsNotNone(reason(policy, rel))
        self.assertEqual(reason(policy, "docs/meeting-notes.txt"), "CODE_ONLY: .txt")

    def test_code_and_config_are_kept(self):
        policy = exclusion.load_policy({exclusion.ENV_CODE_ONLY: "true"})
        for rel in (
            "src/app.py",
            "web/index.ts",
            "hooks/hooks.json",
            "config.yaml",
            "pyproject.toml",
            "schema.xml",
            "page.html",
            "style.css",
            "Makefile",
            "Dockerfile",
            "LICENSE",
            "query.sql",
            "notebook.ipynb",
            "docs.md/index.py",  # ディレクトリ名の拡張子は見ない
        ):
            with self.subTest(rel=rel):
                self.assertIsNone(reason(policy, rel))

    def test_truthy_and_falsy_values(self):
        for value, expected in (
            ("1", True), ("true", True), ("on", True), ("YES", True),
            ("0", False), ("false", False), ("", False), ("maybe", False),
        ):
            with self.subTest(value=value):
                policy = exclusion.load_policy({exclusion.ENV_CODE_ONLY: value})
                self.assertEqual(policy.code_only, expected)

    def test_default_globs_take_precedence_in_reason(self):
        """`secrets.txt` は CODE_ONLY でも既定除外の理由で出る (どちらでも除外には変わりない)。"""
        policy = exclusion.load_policy({exclusion.ENV_CODE_ONLY: "1"})
        self.assertEqual(reason(policy, "secrets.txt"), '既定除外: 語 "secrets"')


if __name__ == "__main__":
    unittest.main()
