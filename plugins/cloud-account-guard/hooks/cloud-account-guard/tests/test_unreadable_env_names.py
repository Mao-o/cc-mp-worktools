"""前段で名前を読めない形で env を入れ直したコマンドを、ディレクトリ単位の固定とみなさない (v0.36.7)。

期待値が未登録の aws / gcloud / firebase は、hook の env (`AWS_PROFILE` など) で固定されていれば止めない
(v0.19.0)。同じコマンドの前段の `export` / `unset` / 代入で identity env を変えた後ろの CLI は固定とみなさないが、
0.36.6 までは `export` の引数を shlex で分けた語の `=` の前を名前として数えていたので、名前を読めない引数
(`export $(cat .env | xargs)` / `export $V` / `declare -x `...``) では `$(cat` などの断片を名前と読み、identity env を
変えていないとして固定で通していた。`read` / `printf -v` は env を変える形として数えていなかった。実際の CLI は
入れ直した値で走る (bash 3.2 / `zsh -f` で、偽の aws / gcloud が受け取った値を測った)。

0.36.7 は、代入の組み込みの引数に名前を読めない語があれば「どの env を変えたか決められない」とし、`read` の引数の
名前と `printf -v` の名前を変えた名前に数える。名前を読める `export` (`export FOO=$(cat f)`) の結果は変えない。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import workdir  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

_ISOLATION = None
_ISOLATION_ROOT = None


def setUpModule():
    global _ISOLATION, _ISOLATION_ROOT
    _ISOLATION_ROOT = tempfile.mkdtemp()
    _ISOLATION = _testutil.start_isolation(Path(_ISOLATION_ROOT))


def tearDownModule():
    if _ISOLATION is not None:
        _ISOLATION.stop()
    if _ISOLATION_ROOT is not None:
        shutil.rmtree(_ISOLATION_ROOT, ignore_errors=True)


AWS = "aws s3 rm s3://b/k"
GCLOUD = "gcloud run deploy"
ANY = workdir.ANY_ENV

# 名前を読めない引数のある代入の組み込み。どれも、実シェルでは展開した名前 (.env の AWS_PROFILE など) に値が入る
UNREADABLE_ASSIGN = [
    "export $(cat .env | xargs)",
    "export $(grep -v '^#' .env | xargs)",
    "export $(xargs < .env)",
    'export "$(head -1 .env)"',
    "export `head -1 .env`",
    "declare -x $(cat .env)",
    "typeset -x $(cat .env)",
    "declare $(head -1 .env)",
    "readonly $(head -1 .env)",
    "export $V",
    "export ${N}=prod",
    "export $N=prod",
    'export "$K=prod"',
    # brace 展開と glob (cwd に `AWS_PROFILE=prod` という名前のファイルがあれば一致する)
    "export {AWS_PROFILE,X}=prod",
    "export [A]WS_PROFILE=prod",
    "export AWS_*",
    "export AWS_PROFIL?=prod",
    # 名前を読める語の後ろ・タブの後ろ・リダイレクトの後ろ・`--` の後ろ
    "export FOO=1 $(cat .env)",
    "export FOO=1\t$(cat .env)",
    "export FOO=1 > /dev/null $(cat .env)",
    "export -- $(cat .env)",
]

# 名前を読める (0.36.6 と同じ結果の) 代入の組み込みと、identity env を変えない read / printf
READABLE = [
    "export FOO=bar",
    "export FOO=$(cat f)",
    "export FOO=$(cat $X)",
    'export FOO="a $X"',
    'export "FOO=$X"',
    "export FOO=1 >$LOG",
    "export FOO=1 2> $LOG",
    "export FOO=1 2>$LOG",
    "declare -a arr=(1 $(cat f))",
    "declare arr[1]=x",
    "declare arr[$i]=x",
    "declare -x FOO=1",
    "export -p",
    "read -r X < f",
    "printf -v X x",
    "printf '%s' -v AWS_PROFILE",
    "printf -- -v AWS_PROFILE",
    "printf -v",
]

# read / printf -v が値を入れる identity env の名前
READ_PRINTF = [
    ("read -r AWS_PROFILE < f", "AWS_PROFILE"),
    ("read AWS_PROFILE", "AWS_PROFILE"),
    ("read -r X AWS_PROFILE < .env", "AWS_PROFILE"),
    # zsh の `名前?プロンプト` (bash は名前でないとして失敗する)
    ("read 'AWS_PROFILE?p: ' < f", "AWS_PROFILE"),
    ("read -a AWS_PROFILE < f", "AWS_PROFILE"),
    ("read -r CLOUDSDK_CORE_ACCOUNT < f", "CLOUDSDK_CORE_ACCOUNT"),
    ("printf -v AWS_PROFILE prod", "AWS_PROFILE"),
    ("printf -v AWS_PROFILE '%s' \"$(cat f)\"", "AWS_PROFILE"),
    ("printf -vAWS_PROFILE prod", "AWS_PROFILE"),
    ("printf -v 'AWS_PROFILE[0]' prod", "AWS_PROFILE"),
]

# 固定のある世界 (AWS_PROFILE=dev / CLOUDSDK_CORE_ACCOUNT=a@example.com) で、固定とみなさなくなる形
OVERRIDES = (
    [f"{p}; {AWS}" for p in UNREADABLE_ASSIGN]
    + [f"{p}; {AWS}" for p, name in READ_PRINTF if name == "AWS_PROFILE"]
    + [
        f"export $(cat .env | xargs) && {GCLOUD}",
        f"read -r CLOUDSDK_CORE_ACCOUNT < f && {GCLOUD}",
        f"while IFS='=' read -r k v; do export \"$k=$v\"; done < .env; {AWS}",
        f"IFS= read -r AWS_PROFILE < f; {AWS}",
        # bash ではパイプの中の read は後ろに効かないが、zsh では最後の段がシェル自身で走るので効く
        f"cat f | read AWS_PROFILE; {AWS}",
        f'N=AWS_PROFILE; printf -v "$N" prod; {AWS}',
        f"{{ export $(cat .env); {AWS}; }}",
        f"command export $(cat .env); {AWS}",
    ]
)
# 固定のまま通す形 (0.36.6 と同じ)
KEPT = [f"{p}; {AWS}" for p in READABLE] + [
    f"{AWS}; export $(cat .env | xargs)",
    f"export FOO=$(cat f); {GCLOUD}",
    f"read -r X < f && {GCLOUD}",
]


class EnvNamesTests(unittest.TestCase):
    """`workdir._env_names_changed` の表 (セグメント → 変えた env の名前)。"""

    def _names(self, segment):
        try:
            return workdir._env_names_changed(segment)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_unreadable_names_are_any(self):
        for segment in UNREADABLE_ASSIGN + ["declare -a arr=(1 $(cat .env)"]:
            with self.subTest(segment=segment):
                self.assertEqual(self._names(segment), {ANY})

    def test_readable_forms_do_not_change_identity(self):
        for segment in READABLE:
            with self.subTest(segment=segment):
                names = self._names(segment)
                self.assertNotIn(ANY, names)
                self.assertFalse(
                    [n for n in names if n.startswith(("AWS_", "CLOUDSDK_"))], names
                )

    def test_read_and_printf_names(self):
        for segment, name in READ_PRINTF:
            with self.subTest(segment=segment):
                names = self._names(segment)
                self.assertNotIn(ANY, names)
                self.assertIn(name, names)

    def test_printf_unreadable_name_is_any(self):
        for segment in ('printf -v "$N" prod', "printf -v `echo N` prod", 'printf -v"$N" prod'):
            with self.subTest(segment=segment):
                self.assertEqual(self._names(segment), {ANY})

    def test_long_segments_are_read_in_linear_time(self):
        """語を 4,000 並べた形 (16〜40 KB) も 2 秒に届かない (手元で数 ms〜十数 ms)。

        閉じない配列代入の語が並ぶ形 (`declare a=( a=( ...`) は、語ごとに閉じを末尾まで探すと語の数の 2 乗になる
        (手元で 4.6 秒)。閉じない配列代入を一度見たら、後ろの語の配列代入の値は読まない。
        """
        for head, unit in (
            ("declare ", "a=( "),
            ("declare ", "arr=(1 2) "),
            ("export ", "A=$(b c) "),
            ("export ", '"a b" '),
            ("export ", "> f "),
        ):
            segment = head + unit * 4000
            with self.subTest(unit=unit):
                started = time.perf_counter()
                self._names(segment)
                self.assertLess(time.perf_counter() - started, 2.0)

    def test_readable_names_keep_the_0_36_6_reading(self):
        """名前を読める `export` は、0.36.6 と同じく shlex で分けた語の `=` の前を数える (結果を変えない)。"""
        for segment, want in (
            ("export AWS_PROFILE=prod", {"AWS_PROFILE"}),
            ("export AWS_PROFILE=$(cat f)", {"AWS_PROFILE", "f)"}),
            ("export FOO=$(echo AWS_X)", {"FOO", "AWS_X)"}),
            ('export "AWS_PROFILE=prod" X', {"AWS_PROFILE", "X"}),
            ("declare -x AWS_PROFILE", {"AWS_PROFILE"}),
        ):
            with self.subTest(segment=segment):
                self.assertEqual(self._names(segment), want)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project_dir = self.tmp / "repo"
        (self.project_dir / ".git").mkdir(parents=True)
        cache_tmp = self.tmp / "cache"
        cache_tmp.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PROJECT_DIR": str(self.project_dir),
                "TMPDIR": str(cache_tmp),
                "AWS_PROFILE": "dev",
                "CLOUDSDK_CORE_ACCOUNT": "a@example.com",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_accounts(self, data: dict) -> None:
        d = self.project_dir / ".claude" / "cloud-account-guard"
        d.mkdir(parents=True, exist_ok=True)
        (d / "accounts.local.json").write_text(json.dumps(data), encoding="utf-8")

    def _decision(self, command):
        try:
            with mock.patch("subprocess.run", side_effect=AssertionError("CLI は起動しない")):
                result = dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        if result is None:
            return "allow", ""
        out = result["hookSpecificOutput"]
        if "permissionDecision" not in out:
            return "warn", out.get("additionalContext", "")
        return out["permissionDecision"], out.get("permissionDecisionReason", "")


class PinnedTests(_Base):
    def test_overrides_are_not_pinned(self):
        self._write_accounts({"github": "me"})
        for command in OVERRIDES:
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)
                self.assertIn("キーがありません", reason)

    def test_overrides_are_not_pinned_without_any_accounts_file(self):
        for command in OVERRIDES:
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)
                self.assertIn("未設定です", reason)

    def test_other_forms_stay_pinned(self):
        path = self.project_dir / ".claude" / "cloud-account-guard" / "accounts.local.json"
        for accounts in ({"github": "me"}, None):
            if accounts is None:
                path.unlink(missing_ok=True)
            else:
                self._write_accounts(accounts)
            for command in KEPT:
                with self.subTest(command=command, accounts=accounts):
                    self.assertEqual(self._decision(command), ("allow", ""))

    def test_query_tier_only_warns(self):
        self._write_accounts({"github": "me"})
        decision, reason = self._decision("export $(cat .env | xargs); aws s3 ls s3://b")
        self.assertEqual(decision, "warn", reason)
        self.assertIn("キーがありません", reason)

    def test_name_value_export_of_the_identity_env_is_unchanged(self):
        """名前を読める `export` で identity env を書く形は、0.36.6 と同じく固定とみなさない。

        前段の `export` の自己参照 (`export AWS_PROFILE=${AWS_PROFILE:-dev}`。値は起動時の値のまま) も 0.36.6 と同じく
        上書きに数える (行頭の代入の自己参照 (v0.36.5) の読みを前段の `export` には当てない)。
        """
        self._write_accounts({"github": "me"})
        for command in (
            f"export AWS_PROFILE=prod; {AWS}",
            f"export AWS_PROFILE=$(cat f); {AWS}",
            f"export AWS_PROFILE=${{AWS_PROFILE:-dev}}; {AWS}",
            f"export AWS_PROFILE=$AWS_PROFILE; {AWS}",
        ):
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)

    def test_registered_value_is_still_checked_with_the_hook_env(self):
        """期待値を登録した service の照合は変えない (前段の env の変更は照合の env に重ねない)。"""
        self._write_accounts({"aws": "123456789012"})
        seen = []

        def fake(expected, project_dir, env=None, context=None):
            seen.append((os.environ if env is None else env).get("AWS_PROFILE"))
            return None

        with mock.patch("services.aws.verify", side_effect=fake):
            self.assertEqual(self._decision(f"export $(cat .env | xargs); {AWS}"), ("allow", ""))
        self.assertEqual(seen, ["dev"])


class FirebaseTests(_Base):
    """firebase の固定 (`firebase use` の記録) は configstore の場所 (`XDG_CONFIG_HOME`) で決まる。"""

    def setUp(self):
        super().setUp()
        self._write_accounts({"github": "me"})
        store = Path(os.environ["XDG_CONFIG_HOME"]) / "configstore"
        store.mkdir(parents=True, exist_ok=True)
        (store / "firebase-tools.json").write_text(
            json.dumps({"activeProjects": {str(self.project_dir.resolve()): "my-proj"}}),
            encoding="utf-8",
        )

    def test_pinned_by_the_record(self):
        self.assertEqual(self._decision("export FOO=$(cat f); firebase deploy"), ("allow", ""))

    def test_unreadable_rewrite_is_not_pinned(self):
        for command in (
            "export $(cat .env | xargs); firebase deploy",
            "read -r XDG_CONFIG_HOME < f; firebase deploy",
            "printf -v FIREBASE_TOKEN x; firebase deploy",
        ):
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)


if __name__ == "__main__":
    unittest.main()
