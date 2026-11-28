"""値を静的に読めない前置きの代入は、ディレクトリ単位の固定の上書きに数える (v0.30.3)。

期待値が未登録の aws / gcloud / firebase は、hook の env (`AWS_PROFILE` など) で固定されていれば止めない
(v0.19.0)。行頭のインライン env で identity env を変えた target は固定とみなさない
(`AWS_PROFILE=prod aws ...`) が、0.30.2 までは値を読めない代入 (`AWS_PROFILE=$(echo prod) aws ...` /
`AWS_PROFILE=$P aws ...`) を上書きと見ず、hook の env の値を固定として通していた。実際の aws は代入した
値で動く。値は読めないが上書きしたことは確かなので、静的な上書きと同じく固定とみなさない
(通常の未登録の判定に戻す)。
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

from core.command_parser import unread_env_candidates  # noqa: E402
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


class UnreadKeysTests(unittest.TestCase):
    """`unread_env_candidates` の表 (候補 → 値を読めなかった前置きの代入のキー)。"""

    def _unread(self, command):
        try:
            return {c: set(k) for c, k in unread_env_candidates(command).items()}
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_unreadable_values_are_marked(self):
        for value in (
            "$(echo prod)", "`echo prod`", "$P", '"$P"', "${P}", "$((1))", "${P:-a b}",
            "dev$(echo x)", "'$P'",
        ):
            command = f"AWS_PROFILE={value} {AWS}"
            with self.subTest(command=command):
                # `${P:-a b}` は 0.29.9 の読みの候補 (`b} aws ...`) も足される。aws の候補だけを見る
                unread = self._unread(command)
                self.assertIn(AWS, unread)
                self.assertEqual(unread[AWS], {"AWS_PROFILE"})

    def test_non_plain_assignments_are_marked(self):
        for command in (f"AWS_PROFILE+=x {AWS}", f"AWS_PROFILE[0]=x {AWS}", f"AWS_PROFILE=(a b) {AWS}"):
            with self.subTest(command=command):
                self.assertEqual(self._unread(command), {AWS: {"AWS_PROFILE"}})

    def test_the_last_assignment_of_a_key_decides(self):
        for command, want in (
            # 後ろの読めない値が勝つ (収集 env には前の dev が残るが、印は付ける)
            (f"AWS_PROFILE=dev AWS_PROFILE=$(echo q) {AWS}", {AWS: {"AWS_PROFILE"}}),
            (f"AWS_PROFILE=dev env AWS_PROFILE=$(echo q) {AWS}", {AWS: {"AWS_PROFILE"}}),
            # 後ろの静的な値が勝つ
            (f"AWS_PROFILE=$(echo q) AWS_PROFILE=dev {AWS}", {}),
            (f"AWS_PROFILE=$(echo q) env AWS_PROFILE=dev {AWS}", {}),
            # sudo は前置きの env を捨てる (-E は残す)
            (f"AWS_PROFILE=$(echo q) sudo {AWS}", {}),
            (f"AWS_PROFILE=$(echo q) sudo -E {AWS}", {AWS: {"AWS_PROFILE"}}),
            (f"AWS_PROFILE=$(echo q) X=$P {AWS}", {AWS: {"AWS_PROFILE", "X"}}),
        ):
            with self.subTest(command=command):
                self.assertEqual(self._unread(command), want)

    def test_unmarked_forms(self):
        for command, want in (
            (AWS, {}),
            (f"AWS_PROFILE=prod {AWS}", {}),
            (f"AWS_PROFILE=~/x {AWS}", {}),
            # 別のコマンドの代入 (代入はそのコマンドだけ)
            (f"AWS_PROFILE=$(echo q) true; {AWS}", {"true": {"AWS_PROFILE"}}),
            # 代入だけのセグメント。先頭の代入から後ろを候補にし、剥がした代入が無いので印は付かない (v0.36.8。
            # 0.36.7 までは最初に値を飛ばした代入から後ろの断片を候補にし、その代入より前の印を付けていた)。
            # 後ろのコマンドへの効き目は `core/workdir.py` の `segment_env_changes` が、置換の中の空白 (`$(echo q)`)
            # や前に並ぶ静的な代入によらず数える (v0.36.8。tests/test_assignment_only_pin.py。0.36.7 までは
            # 数えておらず、固定のある世界では aws を固定で通していた)
            (f"AWS_PROFILE=$(echo q) X=1; {AWS}", {}),
            (f"Y=$P AWS_PROFILE=$(echo q) X=1; {AWS}", {}),
        ):
            with self.subTest(command=command):
                self.assertEqual(self._unread(command), want)


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


# hook の env の固定 (AWS_PROFILE=dev / CLOUDSDK_CORE_ACCOUNT=a@example.com) を、値を読めない代入で上書きする形
OVERRIDES = [
    f"AWS_PROFILE=$(echo prod) {AWS}",
    f"AWS_PROFILE=$P {AWS}",
    f'AWS_PROFILE="$P" {AWS}',
    f"AWS_PROFILE=`echo prod` {AWS}",
    f"AWS_PROFILE=$((1)) {AWS}",
    f"AWS_PROFILE+=x {AWS}",
    f"AWS_PROFILE=dev AWS_PROFILE=$(echo prod) {AWS}",
    f"AWS_PROFILE=$(echo prod) sudo -E {AWS}",
    f"AWS_PROFILE=$(echo prod) env {AWS}",
    f"HOME=$(echo /h) {AWS}",
    f"AWS_CONFIG_FILE=$P {AWS}",
    f"AWS_PROFILE=$(echo prod) {AWS} && aws s3 rm s3://b/other",
    f"true && AWS_PROFILE=$(echo prod) {AWS}",
    f"AWS_PROFILE=$(echo prod) watch 'true && {AWS}'",
    "CLOUDSDK_CORE_ACCOUNT=$(echo b@example.com) gcloud run deploy",
    "CLOUDSDK_CORE_ACCOUNT=$P gcloud run deploy",
    "CLOUDSDK_CONFIG=$P gcloud run deploy",
]
# 変わらない形 (固定のまま通す)
KEPT = [
    AWS,
    f"AWS_PROFILE=dev {AWS}",
    f"X=$(echo 1) {AWS}",
    f"AWS_PROFILE=$(echo q) AWS_PROFILE=dev {AWS}",
    f"AWS_PROFILE=$(echo q) env AWS_PROFILE=dev {AWS}",
    f"AWS_PROFILE=$(echo prod) true; {AWS}",
    # heredoc の行の代入は先頭の段だけ (`test_heredoc_line_env.py`)
    f"AWS_PROFILE=$(echo prod) cat <<EOF | aws s3 cp - s3://b/k\nx\nEOF",
    f"AWS_PROFILE=$P cat <<'EOF' && {AWS}\nx\nEOF",
    "X=$(echo 1) gcloud run deploy",
]


class PinnedOverrideTests(_Base):
    def test_unreadable_override_is_not_pinned(self):
        self._write_accounts({"github": "me"})
        for command in OVERRIDES:
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)
                self.assertIn("キーがありません", reason)

    def test_unreadable_override_is_not_pinned_without_any_accounts_file(self):
        for command in OVERRIDES:
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)
                self.assertIn("未設定です", reason)

    def test_other_forms_stay_pinned(self):
        self._write_accounts({"github": "me"})
        for command in KEPT:
            with self.subTest(command=command):
                self.assertEqual(self._decision(command), ("allow", ""))
        for command in KEPT:
            with self.subTest(command=command, accounts=None):
                (self.project_dir / ".claude" / "cloud-account-guard" / "accounts.local.json").unlink(
                    missing_ok=True
                )
                self.assertEqual(self._decision(command), ("allow", ""))

    def test_query_tier_only_warns(self):
        """リモート read だけのコマンドは、固定でなくなっても止めない (未登録の QUERY の扱い)。"""
        self._write_accounts({"github": "me"})
        decision, reason = self._decision("AWS_PROFILE=$(echo prod) aws s3 ls s3://b")
        self.assertEqual(decision, "warn", reason)
        self.assertIn("キーがありません", reason)
        self.assertEqual(self._decision("aws s3 ls s3://b"), ("allow", ""))

    def test_marked_query_does_not_unpin_an_unmarked_write(self):
        """印の付いた QUERY と、印の無い (固定で走る) WRITE は別の target に分ける。

        1 つに畳むと tier は WRITE、固定の判定は印の和を上書きに数え、固定で走る WRITE まで未登録の deny に
        なる。静的な上書きの対照 (inline env が違うので別の target) と同じく警告に留める。
        """
        for accounts in ({"github": "me"}, None):
            path = self.project_dir / ".claude" / "cloud-account-guard" / "accounts.local.json"
            if accounts is None:
                path.unlink(missing_ok=True)
            else:
                self._write_accounts(accounts)
            for command in (
                f"AWS_PROFILE=$(echo prod) aws s3 ls s3://b && {AWS}",
                f"{AWS} && AWS_PROFILE=$(echo prod) aws s3 ls s3://b",
                f"AWS_PROFILE=$P aws s3 ls s3://b; {AWS}",
                "CLOUDSDK_CORE_ACCOUNT=$(echo b@example.com) gcloud compute instances list"
                " && gcloud run deploy x",
                # 静的な上書きの対照
                f"AWS_PROFILE=prod aws s3 ls s3://b && {AWS}",
            ):
                with self.subTest(command=command, accounts=accounts):
                    decision, reason = self._decision(command)
                    self.assertEqual(decision, "warn", reason)
            # 印の付いた候補自身が WRITE なら、これまでどおり deny
            with self.subTest(accounts=accounts, write=True):
                decision, reason = self._decision(f"AWS_PROFILE=$(echo prod) {AWS} && aws s3 ls s3://b")
                self.assertEqual(decision, "deny", reason)

    def test_registered_value_is_still_checked_with_the_hook_env(self):
        """期待値を登録した service の照合は変えない (値を読めない代入は hook の env で照合する。0.29.10)。"""
        self._write_accounts({"aws": "123456789012"})
        seen = []

        def fake(expected, project_dir, env=None, context=None):
            seen.append((os.environ if env is None else env).get("AWS_PROFILE"))
            return None

        with mock.patch("services.aws.verify", side_effect=fake):
            self.assertEqual(self._decision(f"AWS_PROFILE=$(echo prod) {AWS}"), ("allow", ""))
        self.assertEqual(seen, ["dev"])


class FirebaseOverrideTests(_Base):
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
        self.assertEqual(self._decision("firebase deploy"), ("allow", ""))
        self.assertEqual(self._decision("X=$(echo 1) firebase deploy"), ("allow", ""))

    def test_unreadable_config_home_is_not_pinned(self):
        for command in ("XDG_CONFIG_HOME=$(echo /x) firebase deploy", "XDG_CONFIG_HOME=$P firebase deploy"):
            with self.subTest(command=command):
                decision, reason = self._decision(command)
                self.assertEqual(decision, "deny", reason)


if __name__ == "__main__":
    unittest.main()
