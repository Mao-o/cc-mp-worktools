"""README「守らない範囲」の wrapper と heredoc の行に関する 4 項目の例を、今の挙動のまま固定する (特性テスト)。

どれも検証しないと決めた形で、判定のコードは変えていない。形ごとに、検証される対照 (README が
「こう書けば検証される」と案内する形) と、検証されない例を並べる。候補になるように hook を直したら、
このテストと README の行を一緒に直す。

照合した値は、期待値を登録した service の `verify` を差し替えて取る (CLI は起動しない)。差し替えた
`verify` は常に不一致を返すので、検証されれば deny、検証されなければ allow になる。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

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
LAUNCH = "launch"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project_dir = self.tmp / "repo"
        (self.project_dir / ".git").mkdir(parents=True)
        cache = self.tmp / "cache"
        cache.mkdir()
        d = self.project_dir / ".claude" / "cloud-account-guard"
        d.mkdir(parents=True)
        (d / "accounts.local.json").write_text(
            json.dumps({"aws": "123456789012", "kubectl": "ctx", "github": "me"}), encoding="utf-8"
        )
        patcher = mock.patch.dict(
            os.environ,
            {"CLAUDE_PROJECT_DIR": str(self.project_dir), "TMPDIR": str(cache), "AWS_PROFILE": LAUNCH},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_hook(self, command):
        """(判定, 照合された (service, 照合に使った identity env の値) の一覧) を返す。"""
        seen = []

        def fake(name, key):
            def verify(expected, project_dir, env=None, context=None):
                seen.append((name, (os.environ if env is None else env).get(key)))
                return "MISMATCH"
            return verify

        try:
            with mock.patch("services.aws.verify", side_effect=fake("aws", "AWS_PROFILE")), \
                    mock.patch("services.kubectl.verify", side_effect=fake("kubectl", "KUBECONFIG")), \
                    mock.patch("services.github.verify", return_value=None), \
                    mock.patch("subprocess.run", side_effect=AssertionError("CLI は起動しない")):
                result = dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        if result is None:
            return "allow", seen
        return result["hookSpecificOutput"].get("permissionDecision", "warn"), seen

    def assertNotVerified(self, command):
        decision, seen = self.run_hook(command)
        self.assertEqual((decision, seen), ("allow", []), command)

    def assertVerified(self, command, want):
        decision, seen = self.run_hook(command)
        self.assertEqual((decision, seen), ("deny", want), command)


class QuotedWrapperContentsTests(_Base):
    """1. 引用符で包んだ wrapper の中身に、リダイレクトや heredoc の後ろの段が続く形。"""

    def test_trailing_words_hide_the_quoted_cli(self):
        for command in (
            f"watch 'kubectl delete po x' >/dev/null",
            f"sudo -s '{AWS}' 2>&1",
            f"sudo -i '{AWS}' <<EOF && true\nx\nEOF",
            f"watch '{AWS}' <<EOF && true\nx\nEOF",
            f'watch "{AWS}" >/dev/null',
            f"{{ watch '{AWS}' <<EOF; }}\nx\nEOF",
            f"( sudo -s '{AWS}' <<EOF )\nx\nEOF",
        ):
            with self.subTest(command=command):
                self.assertNotVerified(command)

    def test_whole_quoted_rest_and_plain_separators_are_verified(self):
        decision, seen = self.run_hook("watch 'kubectl delete po x'")
        self.assertEqual((decision, [s for s, _ in seen]), ("deny", ["kubectl"]))
        for command in (
            f"sudo -s '{AWS}'",
            f"sudo -i '{AWS}' <<EOF\nx\nEOF",
            f"watch '{AWS}' | cat",
            f"sudo -s '{AWS}' && true",
            f"sudo -s '{AWS}' ; true",
            # README の案内: リダイレクトを手前に置く / wrapper を使わず直接書く
            f">/dev/null watch '{AWS}'",
            f"{AWS} >/dev/null",
            # npx -c は同じ形でも検証する
            f"npx -c '{AWS}' 2>&1",
            f"npx -c '{AWS}' <<EOF && true\nx\nEOF",
        ):
            with self.subTest(command=command):
                self.assertVerified(command, [("aws", LAUNCH)])


class GhTokenHeredocLineTests(_Base):
    """2. heredoc の行の `gh` の `GH_TOKEN=$(...)` と、後ろの段の値を読めない代入。"""

    def test_later_stage_is_not_verified(self):
        for command in (
            f"GH_TOKEN=$(echo t) gh pr create <<EOF && X=$(echo y) {AWS}\nx\nEOF",
            f"GH_TOKEN=$(echo t) gh pr create <<EOF ; X=$(echo y) {AWS}\nx\nEOF",
            f"GH_TOKEN=$(echo t) gh pr create <<EOF | X=$(echo y) {AWS}\nx\nEOF",
        ):
            with self.subTest(command=command):
                self.assertNotVerified(command)

    def test_neighbouring_forms_are_verified(self):
        for command in (
            # 後ろの段に値を読めない代入が無い
            f"GH_TOKEN=$(echo t) gh pr create <<EOF && {AWS}\nx\nEOF",
            # gh の前置きが静的な値
            f"GH_TOKEN=abc gh pr create <<EOF && X=$(echo y) {AWS}\nx\nEOF",
            # gh の前置きが無い
            f"gh pr create <<EOF && X=$(echo y) {AWS}\nx\nEOF",
            # gh 以外の段に GH_TOKEN
            f"GH_TOKEN=$(echo t) cat <<EOF && X=$(echo y) {AWS}\nx\nEOF",
            # heredoc の行に後ろの段を置かない (README の案内)
            f"GH_TOKEN=$(echo t) gh pr create <<EOF\nx\nEOF\nX=$(echo y) {AWS}",
            f"GH_TOKEN=$(echo t) gh pr create && X=$(echo y) {AWS}",
        ):
            with self.subTest(command=command):
                self.assertVerified(command, [("aws", LAUNCH)])


class WrapperContentsEnvTests(_Base):
    """3. wrapper の中身の最初の段の前置きの代入は、中の全段に継ぐ (実際のシェルでは最初の段だけ)。"""

    def test_first_stage_assignment_is_carried_to_the_cli(self):
        for command in (
            f"watch 'AWS_PROFILE=dev true ; {AWS}'",
            f"watch 'AWS_PROFILE=dev true && {AWS}'",
            f"AWS_PROFILE=dev watch 'true ; {AWS}'",
            # 後ろの段の `;` の前でなく CLI の直前に置いた代入は、その段のもの (README の案内)
            f"watch 'true ; AWS_PROFILE=dev {AWS}'",
        ):
            with self.subTest(command=command):
                self.assertVerified(command, [("aws", "dev")])

    def test_no_assignment_uses_the_launch_value(self):
        self.assertVerified(f"watch 'true ; {AWS}'", [("aws", LAUNCH)])


class BraceHeredocLineTests(_Base):
    """4. 行が `; }` で終わる heredoc の行の `npx -c` の中身が複数の段のとき、外側の前置きの代入を継がない。"""

    def test_multi_stage_contents_lose_the_outer_assignment(self):
        for command in (
            f"{{ AWS_PROFILE=prod npx -c 'true && {AWS}' <<EOF; }}\nx\nEOF",
            f"{{ AWS_PROFILE=prod npx -c 'true ; {AWS}' <<EOF; }}\nx\nEOF",
            f"{{ AWS_PROFILE=prod npx -c 'true && true && {AWS}' <<EOF; }}\nx\nEOF",
        ):
            with self.subTest(command=command):
                self.assertVerified(command, [("aws", LAUNCH)])

    def test_other_shapes_carry_the_outer_assignment(self):
        for command in (
            # 中身が 1 段
            f"{{ AWS_PROFILE=prod npx -c '{AWS}' <<EOF; }}\nx\nEOF",
            # 行が `)` で終わる
            f"( AWS_PROFILE=prod npx -c 'true && {AWS}' <<EOF )\nx\nEOF",
            # `}` を別の行に置く (README の案内)
            f"{{ AWS_PROFILE=prod npx -c 'true && {AWS}' <<EOF\nx\nEOF\n}}",
            # 括弧や波括弧の無い heredoc の行
            f"AWS_PROFILE=prod npx -c 'true && {AWS}' <<EOF\nx\nEOF",
            # 代入を CLI の直前に置く (README の案内)
            f"{{ npx -c 'true && AWS_PROFILE=prod {AWS}' <<EOF; }}\nx\nEOF",
        ):
            with self.subTest(command=command):
                self.assertVerified(command, [("aws", "prod")])


if __name__ == "__main__":
    unittest.main()
