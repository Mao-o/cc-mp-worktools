"""`core/auto_switch.py` (不一致 deny を hook の切替で置き換える opt-in) のテスト。

service は偽物 (`_FakeService`) を使い、設定の解釈・並行セッションのガード・
`attempt()` の手順 (どの段で止まり、何を呼ばないか) だけを固定する。gh 固有の
切替は `test_github_switch.py`、dispatcher との組み合わせは
`test_dispatcher_auto_switch.py`。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import auto_switch, budget, cache  # noqa: E402
from services import ALL as ALL_SERVICES  # noqa: E402

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


def _fake_service(name="fake", plan=None, apply=None, verify=None, supports=True):
    """plan_switch / apply_switch / verify を差し込める service モジュールの偽物。"""
    svc = types.ModuleType(f"services.{name}")
    svc.ACCOUNT_KEY = name
    svc.calls = []

    def _plan(entry, project_dir, env=None):
        svc.calls.append("plan")
        return plan if plan is not None else ([("host", "old", "new")], None)

    def _apply(steps, env=None):
        svc.calls.append("apply")
        return apply if apply is not None else (list(steps), None)

    def _verify(entry, project_dir, env=None, context=None):
        svc.calls.append("verify")
        return verify

    if supports:
        svc.plan_switch = _plan
        svc.apply_switch = _apply
    svc.verify = _verify
    return svc


class _TmpCacheDir(unittest.TestCase):
    """ガード記録は成功 cache と同じ `$TMPDIR` 配下に置かれるので、テストごとに分ける。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"TMPDIR": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)
        budget.clear()
        self.addCleanup(budget.clear)


class TestResolution(unittest.TestCase):
    """env → `"$auto_switch"` → 無効 の解決と、不正な値の扱い。"""

    def _env(self, value):
        return {auto_switch.ENV_VAR: value}

    def test_default_is_disabled(self):
        enabled, notes = auto_switch.resolve({}, ALL_SERVICES, env={})
        self.assertEqual(enabled, frozenset())
        self.assertEqual(notes, [])

    def test_env_enables_github(self):
        for value in ("github", "GitHub", " github , ", "github,github"):
            with self.subTest(value=value):
                enabled, notes = auto_switch.resolve({}, ALL_SERVICES, env=self._env(value))
                self.assertEqual(enabled, frozenset({"github"}))
                self.assertEqual(notes, [])

    def test_empty_env_falls_through_to_file(self):
        enabled, _notes = auto_switch.resolve(
            {"$auto_switch": ["github"]}, ALL_SERVICES, env=self._env("  ")
        )
        self.assertEqual(enabled, frozenset({"github"}))

    def test_env_off_disables_even_when_file_enables(self):
        enabled, notes = auto_switch.resolve(
            {"$auto_switch": ["github"]}, ALL_SERVICES, env=self._env("OFF")
        )
        self.assertEqual(enabled, frozenset())
        self.assertEqual(notes, [])

    def test_env_typo_disables_instead_of_falling_back_to_file(self):
        """env の綴り間違いでファイル側の指定が生きると「止めたつもりが切り替わる」。"""
        enabled, notes = auto_switch.resolve(
            {"$auto_switch": ["github"]}, ALL_SERVICES, env=self._env("gihtub")
        )
        self.assertEqual(enabled, frozenset())
        self.assertEqual(len(notes), 1)
        self.assertIn("gihtub", notes[0])
        self.assertIn("対応していません", notes[0])

    def test_unsupported_service_is_dropped_with_note(self):
        """対応していない service (gcloud 等) は落として、残りは有効にする。"""
        enabled, notes = auto_switch.resolve({}, ALL_SERVICES, env=self._env("github,gcloud"))
        self.assertEqual(enabled, frozenset({"github"}))
        self.assertEqual(len(notes), 1)
        self.assertIn("gcloud", notes[0])
        self.assertIn("github", notes[0], "対応している service を案内していない")

    def test_file_list_and_single_string(self):
        for raw in (["github"], "github", ["GitHub"]):
            with self.subTest(raw=raw):
                enabled, notes = auto_switch.resolve(
                    {"$auto_switch": raw}, ALL_SERVICES, env={}
                )
                self.assertEqual(enabled, frozenset({"github"}))
                self.assertEqual(notes, [])

    def test_file_off_and_empty_list_are_disabled_without_note(self):
        for raw in ("off", []):
            with self.subTest(raw=raw):
                enabled, notes = auto_switch.resolve(
                    {"$auto_switch": raw}, ALL_SERVICES, env={}
                )
                self.assertEqual(enabled, frozenset())
                self.assertEqual(notes, [])

    def test_file_bulk_true_is_rejected(self):
        """`true` の一括指定は受け付けない (対応 service が増えたとき黙って広がるため)。"""
        for raw in (True, 1, None, {"github": True}, ["github", 1]):
            with self.subTest(raw=raw):
                enabled, notes = auto_switch.resolve(
                    {"$auto_switch": raw}, ALL_SERVICES, env={}
                )
                self.assertEqual(enabled, frozenset())
                self.assertEqual(len(notes), 1)
                self.assertIn("配列", notes[0])

    def test_env_wins_over_file(self):
        enabled, _ = auto_switch.resolve(
            {"$auto_switch": "off"}, ALL_SERVICES, env=self._env("github")
        )
        self.assertEqual(enabled, frozenset({"github"}))

    def test_uses_process_env_by_default(self):
        with mock.patch.dict(os.environ, {auto_switch.ENV_VAR: "github"}):
            enabled, _ = auto_switch.resolve({}, ALL_SERVICES)
        self.assertEqual(enabled, frozenset({"github"}))

    def test_only_github_supports_auto_switch(self):
        """対応 service の一覧 (README の記述と一致させる)。増やすときは README も直す。"""
        supported = sorted(
            svc.ACCOUNT_KEY for svc in ALL_SERVICES if auto_switch.supports(svc)
        )
        self.assertEqual(supported, ["github"])


class TestGuard(_TmpCacheDir):
    """並行セッションのガード: 直前の「別の値への自動切替」とぶつかったら見送る。"""

    STEPS = [("github.com", "other", "Mao-o")]

    def test_no_record_no_conflict(self):
        self.assertIsNone(auto_switch.conflicting_switch("github", self.STEPS))

    def test_recent_switch_to_another_value_conflicts(self):
        auto_switch.record_switch("github", [("github.com", "Mao-o", "work-user")], "/other")
        note = auto_switch.conflicting_switch("github", self.STEPS)
        self.assertIsNotNone(note)
        self.assertIn("work-user", note)
        self.assertIn("/other", note)
        self.assertIn("ユーザーに確認", note)

    def test_same_value_does_not_conflict(self):
        """同じ期待値 (同じ repo の worktree 同士など) は見送る理由が無い。"""
        auto_switch.record_switch("github", [("github.com", "x", "Mao-o")], "/worktree")
        self.assertIsNone(auto_switch.conflicting_switch("github", self.STEPS))

    def test_other_target_does_not_conflict(self):
        auto_switch.record_switch("github", [("ghe.example.com", "x", "corp")], "/other")
        self.assertIsNone(auto_switch.conflicting_switch("github", self.STEPS))

    def test_old_record_does_not_conflict(self):
        now = time.time_ns()
        auto_switch.record_switch(
            "github", [("github.com", "Mao-o", "work-user")], "/other",
            now_ns=now - (auto_switch.GUARD_SEC + 1) * 1_000_000_000,
        )
        self.assertIsNone(auto_switch.conflicting_switch("github", self.STEPS, now_ns=now))

    def test_record_just_inside_window_conflicts(self):
        now = time.time_ns()
        auto_switch.record_switch(
            "github", [("github.com", "Mao-o", "work-user")], "/other",
            now_ns=now - (auto_switch.GUARD_SEC - 1) * 1_000_000_000,
        )
        self.assertIsNotNone(auto_switch.conflicting_switch("github", self.STEPS, now_ns=now))

    def test_corrupted_record_is_ignored(self):
        path = cache.service_state_path("github", ".autoswitch.json")
        for text in ("{", "[]", json.dumps({"github.com": "x"}),
                     json.dumps({"github.com": {"value": 1, "at_ns": "x"}})):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                self.assertIsNone(auto_switch.conflicting_switch("github", self.STEPS))

    def test_record_is_not_removed_by_cache_invalidation(self):
        """記録は成功 cache の破棄 (`invalidate`) で消えてはいけない (切替の直前に呼ぶため)。"""
        auto_switch.record_switch("github", [("github.com", "Mao-o", "work-user")], "/other")
        cache.invalidate("github")
        self.assertIsNotNone(auto_switch.conflicting_switch("github", self.STEPS))

    def test_record_keeps_other_targets(self):
        auto_switch.record_switch("github", [("ghe.example.com", "x", "corp")], "/a")
        auto_switch.record_switch("github", [("github.com", "x", "work-user")], "/b")
        note = auto_switch.conflicting_switch(
            "github", [("ghe.example.com", "corp", "other-corp")]
        )
        self.assertIsNotNone(note, "別 target の記録で上書きされて消えている")

    def test_state_suffix_must_not_collide_with_cache_glob(self):
        with self.assertRaises(ValueError):
            cache.service_state_path("github", "-x.json")


class TestAttempt(_TmpCacheDir):
    """`attempt()` の手順: どの段で止まり、止まったら何を呼ばないか。"""

    def test_switching_command_is_left_alone(self):
        svc = _fake_service()
        out = auto_switch.attempt(svc, "Mao-o", "/p", switching_here=True)
        self.assertFalse(out.resolved)
        self.assertEqual(out.note, auto_switch.NOTE_SWITCHING_HERE)
        self.assertEqual(svc.calls, [])

    def test_expired_budget_starts_nothing(self):
        svc = _fake_service()
        budget.start(0.0)
        out = auto_switch.attempt(svc, "Mao-o", "/p")
        self.assertEqual(out.note, auto_switch.NOTE_BUDGET)
        self.assertEqual(svc.calls, [])

    def test_unswitchable_plan_does_not_apply(self):
        svc = _fake_service(plan=(None, "ログインしていません"))
        with mock.patch("core.cache.invalidate") as invalidate:
            out = auto_switch.attempt(svc, "Mao-o", "/p")
        self.assertFalse(out.resolved)
        self.assertIn("ログインしていません", out.note)
        self.assertEqual(svc.calls, ["plan"])
        self.assertFalse(invalidate.called, "切り替えないのに成功 cache を捨てている")

    def test_already_matching_plan_only_reverifies(self):
        """plan の時点で一致していた (並行して誰かが切り替えた) なら切り替えずに通す。"""
        svc = _fake_service(plan=([], None), verify=None)
        out = auto_switch.attempt(svc, "Mao-o", "/p")
        self.assertTrue(out.resolved)
        self.assertEqual(out.switched, ())
        self.assertEqual(svc.calls, ["plan", "verify"])

    def test_happy_path_invalidates_before_switching_and_records(self):
        events = []
        svc = _fake_service(verify=None)
        original_apply = svc.apply_switch

        def apply(steps, env=None):
            events.append("apply")
            return original_apply(steps, env=env)

        svc.apply_switch = apply
        with mock.patch(
            "core.cache.invalidate", side_effect=lambda name: events.append(f"invalidate:{name}")
        ):
            out = auto_switch.attempt(svc, "new", "/p")
        self.assertTrue(out.resolved)
        self.assertEqual(out.switched, (("host", "old", "new"),))
        self.assertEqual(events, ["invalidate:fake", "apply"], "切替の前に cache を捨てていない")
        self.assertIsNotNone(
            auto_switch.conflicting_switch("fake", [("host", "new", "other")]),
            "切り替えた記録が残っていない",
        )

    def test_conflict_stops_before_touching_anything(self):
        auto_switch.record_switch("fake", [("host", "x", "someone-else")], "/other")
        svc = _fake_service()
        with mock.patch("core.cache.invalidate") as invalidate:
            out = auto_switch.attempt(svc, "new", "/p")
        self.assertFalse(out.resolved)
        self.assertIn("someone-else", out.note)
        self.assertEqual(svc.calls, ["plan"])
        self.assertFalse(invalidate.called)

    def test_budget_expiring_after_plan_stops_before_switch(self):
        """CLI を起動する前ごとに予算を確かめる (plan の後で切れたら切り替えない)。"""
        svc = _fake_service()
        original_plan = svc.plan_switch

        def plan(entry, project_dir, env=None):
            result = original_plan(entry, project_dir, env=env)
            budget.start(0.0)
            return result

        svc.plan_switch = plan
        out = auto_switch.attempt(svc, "new", "/p")
        self.assertEqual(out.note, auto_switch.NOTE_BUDGET)
        self.assertEqual(svc.calls, ["plan"])

    def test_budget_expiring_after_switch_skips_reverify(self):
        svc = _fake_service()
        original_apply = svc.apply_switch

        def apply(steps, env=None):
            result = original_apply(steps, env=env)
            budget.start(0.0)
            return result

        svc.apply_switch = apply
        out = auto_switch.attempt(svc, "new", "/p")
        self.assertFalse(out.resolved)
        self.assertEqual(out.switched, (("host", "old", "new"),))
        self.assertIn("予算", out.error)
        self.assertNotIn("verify", svc.calls)

    def test_failed_switch_without_changes_keeps_original_error(self):
        """何も切り替わらなかったら再検証しない (切替前の verify() の文面が正しいまま)。"""
        svc = _fake_service(apply=([], "github.com の切替に失敗しました (boom)。"))
        out = auto_switch.attempt(svc, "new", "/p")
        self.assertFalse(out.resolved)
        self.assertIsNone(out.error)
        self.assertIn("boom", out.note)
        self.assertEqual(svc.calls, ["plan", "apply"])
        self.assertIsNone(
            auto_switch.conflicting_switch("fake", [("host", "new", "other")]),
            "切り替えていないのに記録している",
        )

    def test_partial_switch_reverifies_and_reports(self):
        svc = _fake_service(
            plan=([("a", "x", "new"), ("b", "y", "new")], None),
            apply=([("a", "x", "new")], "b の切替に失敗しました (boom)。"),
            verify="GitHub [b] アカウント不一致",
        )
        out = auto_switch.attempt(svc, "new", "/p")
        self.assertFalse(out.resolved)
        self.assertEqual(out.switched, (("a", "x", "new"),))
        self.assertEqual(out.error, "GitHub [b] アカウント不一致")
        self.assertIn("boom", out.note)
        self.assertEqual(svc.calls, ["plan", "apply", "verify"])

    def test_still_mismatching_after_switch(self):
        svc = _fake_service(verify="アカウント不一致: 現在=old, 期待=new")
        out = auto_switch.attempt(svc, "new", "/p")
        self.assertFalse(out.resolved)
        self.assertEqual(out.error, "アカウント不一致: 現在=old, 期待=new")
        self.assertIn("一致しませんでした", out.note)

    def test_notes_do_not_contain_cli_command_forms(self):
        """注記に切替コマンドの実形を書かない (deny 文面の案内は verify() の分だけにする)。"""
        import re

        cli = re.compile(
            r"(?<![\w/.-])(?:gh|firebase|aws|gcloud|kubectl)(?:\s+[A-Za-z0-9_.:/=<>@-]+)+"
        )
        auto_switch.record_switch("fake", [("host", "x", "someone-else")], "/other")
        texts = [
            auto_switch.NOTE_SWITCHING_HERE,
            auto_switch.NOTE_BUDGET,
            auto_switch.conflicting_switch("fake", [("host", "x", "new")]),
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(cli.search(text), text)


class TestNotice(unittest.TestCase):
    def test_uses_service_description(self):
        svc = types.ModuleType("services.fake")
        svc.describe_switch = lambda switched: f"described {len(switched)}"
        text = auto_switch.notice(svc, [("h", "a", "b")])
        self.assertTrue(text.startswith("[verify-cloud-account]"))
        self.assertIn("described 1", text)

    def test_generic_fallback(self):
        svc = types.ModuleType("services.fake")
        text = auto_switch.notice(svc, [("h", "a", "b")])
        self.assertIn("h: a → b", text)


if __name__ == "__main__":
    unittest.main()
