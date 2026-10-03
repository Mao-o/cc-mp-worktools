"""案内するコマンドへの値の埋め込み (`core/shell_word.py` と各 service の deny 文面)。

deny 文面の切替案内 (`kubectl config use-context <期待値>` 等) は、Claude がそのまま
実行しがちな「次に打つコマンド」。値は accounts.local.json の期待値・CLI の設定
(AWS config の profile 名、gh の host 名) から来て、期待値のファイルはリポジトリに
置かれうる。0.17.0 までは値をそのまま埋め込んでいたので、`;` / `$()` / 改行や先頭の
`-` を含む値で、案内どおりに打ったコマンドが別のコマンドや option として走りえた。

見ること:
- 許容形の値は、文面が 0.17.0 と同じ (quote が付かない) で、コマンドの 1 引数になる
- 外れた値は、コマンドの形で案内せず「手で確認してください」の文になる
- どちらも deny のまま (判定は変えない)
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401

from core import shell_word  # noqa: E402
from services import aws, firebase, gcloud, github, kubectl  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    """実環境の gh / gcloud / AWS の設定を読ませない (CLI は mock する)。"""
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


# 許容形 (WORD / NAME) から外れる値。どれも案内コマンドに出ない。シェルの構文・option・
# クォートの要る文字を含む値のほか、シェル上は無害な値 (`.hidden` / `a=b` / `ｄｅｖ` は
# 引数の位置ならクォートしなくても 1 語のまま) も含む — 許容形は広げず、案内しない側に
# 倒している (core/shell_word.py の UNSAFE が「危険な文字を含む」と言わない理由)。
HOSTILE = (
    "x; touch pwned",
    "$(touch pwned)",
    "`touch pwned`",
    "a b",
    "dev\nrm -rf ~",
    "dev\n",
    "it's",
    '"q"',
    "a|b",
    "a&b",
    "a>b",
    "a*b",
    "-P",
    "--kubeconfig=/tmp/x",
    ".hidden",
    "~root",
    "a=b",
    "ｄｅｖ",
)
# 実在する形の名前 (WORD の許容形)。EKS / kubeadm の context 名、メールアドレス、
# ドメイン付きの project ID を含む。
PLAIN = (
    "prod-ctx",
    "arn:aws:eks:us-east-1:123456789012:cluster/my-cluster",
    "kubernetes-admin@kubernetes",
    "me+ci@example.com",
    "example.com:my-project",
    "A_1.v2",
)
# firebase の alias / project ID の許容形 (NAME)。
PLAIN_NAME = ("proj-dev", "A_1.v2")

# 外れた値の代わりに置く文の目印。
CHECK_BY_HAND = "手で確認してください"


def _fake(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _run_returning(stdout: str = "", stderr: str = "", returncode: int = 0):
    return mock.patch("subprocess.run", return_value=_fake(stdout, stderr, returncode))


def _flag_with_value(flag: str, reason: str) -> bool:
    """`<flag> <値>` の形 (flag を直す案内) が文面にあるか。

    「`--context を外してください`」のように flag の後ろが日本語の文なら案内ではない。
    検出したコマンド自身の指定を示す「コマンド指定 <flag> <値>,」も数えない (テストの
    コマンドは `other` を指定する)。
    """
    shown_removed = re.sub(r"コマンド指定 [^,]*,", "", reason)
    return re.search(rf"{re.escape(flag)} +[^\sを、(]", shown_removed) is not None


class TestArg(unittest.TestCase):
    def test_plain_values_are_one_word_without_quotes(self):
        """許容形の値は quote が付かない (0.17.0 の文面と self-remediation の照合が変わらない)。"""
        for value in PLAIN:
            with self.subTest(value=value):
                self.assertEqual(shell_word.arg(value), value)
                self.assertEqual(shlex.split(shell_word.arg(value)), [value])
        for value in PLAIN_NAME:
            with self.subTest(value=value):
                self.assertEqual(shell_word.arg(value, shell_word.NAME), value)

    def test_hostile_values_are_refused(self):
        for value in HOSTILE:
            with self.subTest(value=value):
                self.assertIsNone(shell_word.arg(value))
                self.assertIsNone(shell_word.arg(value, shell_word.NAME))

    def test_non_strings_are_refused(self):
        for value in (None, 123, ["a"], {"a": "b"}):
            with self.subTest(value=value):
                self.assertIsNone(shell_word.arg(value))

    def test_name_is_narrower_than_word(self):
        for value in ("a:b", "a/b", "a@b", "a+b"):
            with self.subTest(value=value):
                self.assertIsNone(shell_word.arg(value, shell_word.NAME))
                self.assertEqual(shell_word.arg(value), value)

    def test_every_allowed_character_needs_no_quote(self):
        """許容形の文字は shlex.quote が quote を付けない範囲 (普通の値の文面は不変)。"""
        allowed = set("0123456789._:/@+-") | {chr(c) for c in range(ord("a"), ord("z") + 1)}
        allowed |= {c.upper() for c in allowed}
        for ch in sorted(allowed):
            with self.subTest(ch=ch):
                self.assertIsNotNone(shell_word.WORD.fullmatch(f"a{ch}"))
                self.assertEqual(shlex.quote(f"a{ch}"), f"a{ch}")

    def test_quoting_still_holds_if_the_pattern_is_loosened(self):
        """許容形の検証と quote は二重化: 検証を緩めても、シェルの構文は 1 引数に収まる。"""
        loose = re.compile(r".+", re.DOTALL)
        for value in HOSTILE:
            with self.subTest(value=value):
                word = shell_word.arg(value, loose)
                try:
                    parts = shlex.split(word)
                except ValueError:
                    self.fail(f"シェルとして閉じていない形を出した: {word!r}")
                self.assertEqual(parts, [value])


class TestKubectlGuidance(unittest.TestCase):
    PROJECT_DIR = "/nonexistent-project"

    def _reasons(self, expected: str) -> dict[str, str]:
        with _run_returning("dev-ctx\n"):
            mismatch = kubectl.verify(expected, self.PROJECT_DIR)
            override = kubectl.verify(expected, self.PROJECT_DIR, context={"context": "other"})
        with _run_returning("", "error: current-context is not set\n", 1):
            unset = kubectl.verify(expected, self.PROJECT_DIR)
        return {"mismatch": mismatch, "unset": unset, "override": override}

    def test_plain_expected_is_guided_as_one_argument(self):
        for value in PLAIN:
            reasons = self._reasons(value)
            for case in ("mismatch", "unset"):
                with self.subTest(value=value, case=case):
                    self.assertIn(f"kubectl config use-context {value}", reasons[case])
            with self.subTest(value=value, case="override"):
                self.assertIn(f"--context {value} を指定してください", reasons["override"])

    def test_hostile_expected_is_not_guided_as_a_command(self):
        for value in HOSTILE:
            reasons = self._reasons(value)
            for case, reason in reasons.items():
                with self.subTest(value=value, case=case):
                    self.assertIsNotNone(reason)
                    self.assertNotIn("kubectl config use-context", reason)
                    self.assertFalse(_flag_with_value("--context", reason), reason)
                    self.assertIn(CHECK_BY_HAND, reason)

    def test_command_override_is_shown_quoted(self):
        """検出したコマンド自身の `--context` は、検証せず quote して示す。"""
        with _run_returning("dev-ctx\n"):
            reason = kubectl.verify("prod-ctx", self.PROJECT_DIR, context={"context": "a; b"})
        self.assertIn("コマンド指定 --context='a; b',", reason)

    def test_harmless_value_outside_the_form_is_not_said_to_be_shell_syntax(self):
        """外れた値の文面は「案内に使える形ではない」とだけ言う。シェル上は無害な値も
        外れるので、「シェルの構文や option として解釈されうる文字を含む」は事実と
        合わない (マージ前レビューの指摘)。"""
        for value in ("_local", "a,b", "開発クラスタ"):
            with self.subTest(value=value):
                reason = self._reasons(value)["mismatch"]
                self.assertIn("案内に使える形", reason)
                self.assertNotIn("シェルの構文", reason)
                self.assertIn(CHECK_BY_HAND, reason)


class TestGcloudGuidance(unittest.TestCase):
    PROJECT_DIR = "/nonexistent-project"

    @staticmethod
    def _run(project: str | None, account: str | None):
        def run(argv, **_kw):
            value = project if argv[-1] == "project" else account
            return _fake("(unset)\n" if value is None else f"{value}\n")

        return mock.patch("subprocess.run", side_effect=run)

    def _reasons(self, value: str) -> dict[str, str]:
        reasons = {}
        with self._run("other-proj", "other@example.com"):
            reasons["project mismatch"] = gcloud.verify(value, self.PROJECT_DIR)
            reasons["account mismatch"] = gcloud.verify(
                {"project": "other-proj", "account": value}, self.PROJECT_DIR
            )
            reasons["--project"] = gcloud.verify(
                value, self.PROJECT_DIR, context={"project": "other"}
            )
            reasons["--account"] = gcloud.verify(
                {"account": value}, self.PROJECT_DIR, context={"account": "other@example.com"}
            )
        with self._run(None, None):
            reasons["project unset"] = gcloud.verify(value, self.PROJECT_DIR)
            reasons["account unset"] = gcloud.verify({"account": value}, self.PROJECT_DIR)
        return reasons

    def test_plain_expected_is_guided_as_one_argument(self):
        for value in PLAIN:
            reasons = self._reasons(value)
            expected_text = {
                "project mismatch": f"gcloud config set project {value}",
                "project unset": f"gcloud config set project {value} を実行",
                "account mismatch": f"gcloud config set account {value}",
                "account unset": f"gcloud config set account {value} を実行",
                "--project": f"--project {value} を指定してください",
                "--account": f"--account {value} を指定してください",
            }
            for case, text in expected_text.items():
                with self.subTest(value=value, case=case):
                    self.assertIn(text, reasons[case])

    def test_hostile_expected_is_not_guided_as_a_command(self):
        for value in HOSTILE:
            for case, reason in self._reasons(value).items():
                with self.subTest(value=value, case=case):
                    self.assertIsNotNone(reason)
                    self.assertNotIn("gcloud config set", reason)
                    self.assertFalse(_flag_with_value("--project", reason), reason)
                    self.assertFalse(_flag_with_value("--account", reason), reason)
                    self.assertIn(CHECK_BY_HAND, reason)


class TestGithubGuidance(unittest.TestCase):
    def _reasons(self, value: str) -> dict[str, str]:
        return {
            "scalar expected": github._verify_against({"github.com": "other"}, value),
            "active host": github._verify_against({value: "other"}, "Mao-o"),
            "dict expected": github._verify_against({"github.com": "other"}, {"github.com": value}),
            "dict host": github._verify_against({value: "other"}, {value: "Mao-o"}),
            "host not logged in": github._verify_against(
                {"github.com": "Mao-o"}, {"github.com": "Mao-o", value: "corp"}
            ),
        }

    def test_plain_values_are_guided_as_one_argument(self):
        for value in PLAIN:
            reasons = self._reasons(value)
            expected_text = {
                "scalar expected": f"gh auth switch --hostname github.com --user {value}",
                "active host": f"gh auth switch --hostname {value} --user Mao-o",
                "dict expected": f"gh auth switch --hostname github.com --user {value}",
                "dict host": f"gh auth switch --hostname {value} --user Mao-o",
                "host not logged in": f"gh auth login --hostname {value} --skip-ssh-key",
            }
            for case, text in expected_text.items():
                with self.subTest(value=value, case=case):
                    self.assertIn(text, reasons[case])

    def test_hostile_values_are_not_guided_as_a_command(self):
        for value in HOSTILE:
            for case, reason in self._reasons(value).items():
                with self.subTest(value=value, case=case):
                    self.assertIsNotNone(reason)
                    self.assertNotIn("gh auth switch", reason)
                    self.assertNotIn("gh auth login", reason)
                    self.assertIn(CHECK_BY_HAND, reason)

    def test_host_is_displayed_only_in_the_allowed_form(self):
        """`GitHub [<host>]` の host は許容形のときだけ出す。期待値の型の誤りの deny は切替を
        案内しないので、host が切替コマンドの形だと表示だけで dispatcher の注記が付いていた
        (マージ前レビューの指摘)。ほかの文面の host も同じ規則で出す。"""
        host_cases = ("active host", "dict host", "host not logged in", "non-string expected")
        for value in PLAIN + HOSTILE:
            reasons = self._reasons(value)
            reasons["non-string expected"] = github._verify_against(
                {"github.com": "me"}, {"github.com": "me", value: 1}
            )
            shown = value if value in PLAIN else "表示しない host"
            for case in host_cases:
                with self.subTest(value=value, case=case):
                    self.assertIn(f"GitHub [{shown}]", reasons[case])
                    if value not in PLAIN:
                        self.assertNotIn(f"[{value}]", reasons[case])


class TestFirebaseGuidance(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "firebase.json").write_text("{}", encoding="utf-8")
        which = mock.patch("services.firebase.shutil.which", return_value="/usr/bin/firebase")
        which.start()
        self.addCleanup(which.stop)

    def _reasons(self, expected) -> dict[str, str]:
        project_dir = str(self.root)
        with _run_returning("proj-other\n"):
            mismatch = firebase.verify(expected, project_dir)
            override = firebase.verify(expected, project_dir, context={"project": "other"})
        with _run_returning("", "Error: not logged in\n", 1):
            unresolved = firebase.verify(expected, project_dir)
        return {"mismatch": mismatch, "unresolved": unresolved, "--project": override}

    def test_plain_values_are_guided_as_one_argument(self):
        for value in PLAIN_NAME:
            scalar = self._reasons(value)
            alias = self._reasons({value: "proj-a"})
            with self.subTest(value=value):
                self.assertIn(f"切り替え: firebase use {value}", scalar["mismatch"])
                self.assertIn(f"firebase login && firebase use {value} を実行", scalar["unresolved"])
                self.assertIn(f"--project {value} を指定してください", scalar["--project"])
                for case in ("mismatch", "unresolved"):
                    self.assertIn(f"  firebase use {value}  # → proj-a", alias[case])
                self.assertIn(f"  --project {value}  # → proj-a", alias["--project"])

    def test_hostile_values_are_not_guided_as_a_command(self):
        """scalar の期待値と dict の alias はコマンドの引数なので NAME、dict の project は
        `#` の後ろ (改行でコメントの外に出る) なので WORD から外れる値を案内しない。
        `example.com:my-project` は NAME から外れるが WORD には収まる (project としては
        案内する。下のテスト)。"""
        not_name = HOSTILE + ("example.com:my-project",)
        cases = [("scalar", value, value) for value in not_name]
        cases += [("alias", value, {value: "proj-a"}) for value in not_name]
        cases += [("project", value, {"default": value}) for value in HOSTILE]
        for shape, value, expected in cases:
            for case, reason in self._reasons(expected).items():
                with self.subTest(value=value, shape=shape, case=case):
                    self.assertIsNotNone(reason)
                    self.assertNotIn("firebase use", reason)
                    self.assertFalse(_flag_with_value("--project", reason), reason)
                    self.assertIn(CHECK_BY_HAND, reason)

    def test_domain_scoped_project_is_guided_after_the_comment_mark(self):
        """`#` の後ろの project はコメントなので WORD で見る。ドメイン付きの project ID でも
        alias の案内行を出す (NAME で見ていたときは案内を一切出さなかった。マージ前
        レビューの指摘)。"""
        reasons = self._reasons({"default": "example.com:my-project"})
        for case in ("mismatch", "unresolved"):
            with self.subTest(case=case):
                self.assertIn("  firebase use default  # → example.com:my-project", reasons[case])
                self.assertNotIn(CHECK_BY_HAND, reasons[case])
        with self.subTest(case="--project"):
            self.assertIn("  --project default  # → example.com:my-project", reasons["--project"])

    def test_dict_keeps_the_plain_entries_and_says_some_were_left_out(self):
        expected = {"$(touch pwned)": "proj-a", "default": "dev\nrm -rf ~", "prod": "proj-prod"}
        for case, reason in self._reasons(expected).items():
            command = "--project" if case == "--project" else "firebase use"
            # 期待値の表示 (「期待=... のいずれか」) より後ろが案内
            guidance = reason.split("のいずれか")[-1]
            with self.subTest(case=case):
                self.assertIn(f"  {command} prod  # → proj-prod", guidance)
                self.assertNotIn("pwned", reason)
                self.assertNotIn("rm -rf", guidance)
                self.assertIn("ほかの alias は", guidance)

    def test_command_project_is_shown_quoted(self):
        """検出したコマンド自身の `--project` は、検証せず quote して示す。"""
        (self.root / ".firebaserc").write_text(
            json.dumps({"projects": {"a b": "proj-x"}}), encoding="utf-8"
        )
        with _run_returning("proj-other\n"):
            reason = firebase.verify("proj-dev", str(self.root), context={"project": "a b"})
        self.assertIn("コマンド指定 --project 'a b' (→ proj-x),", reason)

    def test_resolved_project_is_displayed_only_in_the_allowed_form(self):
        """`--project <alias>` の行き先 (`.firebaserc` の値) は許容形のときだけ `(→ <project>)`
        に出す。この deny は flag を直す案内で切替は案内しないので、行き先が切替コマンドの形
        だと表示だけで dispatcher の注記が付いていた (マージ前レビューの指摘)。"""
        for value in PLAIN + HOSTILE:
            (self.root / ".firebaserc").write_text(
                json.dumps({"projects": {"prod": value}}), encoding="utf-8"
            )
            with _run_returning("proj-other\n"):
                reason = firebase.verify("proj-dev", str(self.root), context={"project": "prod"})
            shown = value if value in PLAIN else "表示しない値"
            with self.subTest(value=value):
                self.assertIn(f"コマンド指定 --project prod (→ {shown}),", reason)
                if value not in PLAIN:
                    self.assertNotIn(f"(→ {value})", reason)


class TestAwsGuidance(unittest.TestCase):
    ACCOUNT = "123456789012"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / "config"
        self.env = {"AWS_CONFIG_FILE": str(self.config)}

    def _write_profiles(self, *names: str) -> None:
        self.config.write_text(
            "".join(f"[profile {n}]\nsso_account_id = {self.ACCOUNT}\n" for n in names),
            encoding="utf-8",
        )

    def _reasons(self) -> dict[str, str]:
        with _run_returning("111111111111\n"):
            mismatch = aws.verify(self.ACCOUNT, str(self.tmp), self.env)
        with _run_returning("", "Error loading SSO Token\n", 255):
            no_credentials = aws.verify(self.ACCOUNT, str(self.tmp), self.env)
        return {"mismatch": mismatch, "no credentials": no_credentials}

    def test_plain_profile_is_guided_as_one_argument(self):
        for value in PLAIN:
            self._write_profiles(value)
            for case, reason in self._reasons().items():
                with self.subTest(value=value, case=case):
                    self.assertIn(f"  AWS_PROFILE={value} aws ...", reason)
                    self.assertIn(f"  aws sso login --profile {value}  #", reason)
                    self.assertIn(f"profile: {value})", reason)

    def test_hostile_profile_is_neither_guided_nor_listed(self):
        """profile 名はコマンドにも一覧にも出さない (一覧の名前も `<profile>` に当てはめて
        使われる)。section 見出しは 1 行なので改行を含む名前は作れない。"""
        for value in (v for v in HOSTILE if "\n" not in v):
            self._write_profiles(value)
            for case, reason in self._reasons().items():
                with self.subTest(value=value, case=case):
                    self.assertIn("  AWS_PROFILE=<profile> aws ...", reason)
                    self.assertIn("  aws sso login --profile <profile>  #", reason)
                    self.assertNotIn(f"AWS_PROFILE={value}", reason)
                    self.assertNotIn(f"--profile {value}", reason)
                    self.assertNotIn(f"profile: {value}", reason)
                    self.assertIn(CHECK_BY_HAND, reason)

    def test_plain_profiles_are_kept_when_some_are_left_out(self):
        self._write_profiles("x; touch pwned", "prod")
        for case, reason in self._reasons().items():
            with self.subTest(case=case):
                self.assertIn("  AWS_PROFILE=prod aws ...", reason)
                self.assertIn("profile: prod)", reason)
                self.assertNotIn("pwned", reason)
                self.assertIn("ほかの profile は", reason)

    def test_command_profile_is_shown_quoted(self):
        """検出したコマンド自身の `--profile` は、検証せず quote して示す。"""
        self._write_profiles("prod")
        with _run_returning("111111111111\n"):
            reason = aws.verify(self.ACCOUNT, str(self.tmp), self.env, context={"profile": "a; b"})
        self.assertIn("AWS アカウント不一致 (--profile 'a; b'):", reason)


if __name__ == "__main__":
    unittest.main()
