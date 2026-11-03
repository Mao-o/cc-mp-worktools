"""hook の時間切れでコマンドが素通しになる経路を、期限で閉じる。

読み取りは制限時間つきの子プロセスで行い、時間切れ・異常は止める。読み取り・repo の確認・設定の読み・ゲートは hook 全体で 1 つの予算を分け合う。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from _testutil import commit_all, make_marketplace, sh, write, write_json

import config
import reading
from command import _READ_LIMIT, Invocation, find_invocations, unresolved_reason
from reading import CHILD_FROM, READ_SECONDS, read_command
from runner import HOOK_BUDGET, Deadline

_PKG = Path(__file__).resolve().parent.parent
_SLOW = "gh pr create; " + "a=$(" * 255  # 空白で埋めると上限ちょうどで数秒かかる (2 部構成の最悪の形)
_SLOW += " " * (_READ_LIMIT - len(_SLOW))
_MARGIN = 5  # 子の起動・kill と揺れの余裕


def _padded(head: str, size: int = CHILD_FROM) -> str:
    filler = size - len(head) - len("; echo ''")
    return f"{head}; echo '{'x' * filler}'"


def _load_entry():
    spec = importlib.util.spec_from_file_location("vpr_deadline_main", _PKG / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ConstantsTest(unittest.TestCase):
    """README に書いた値と、hook の時間切れとの関係。"""

    def test_documented_values(self):
        self.assertEqual(_READ_LIMIT, 32768)
        self.assertEqual(CHILD_FROM, 4096)
        self.assertEqual(READ_SECONDS, 15)
        self.assertEqual(HOOK_BUDGET, 105)
        self.assertEqual(config.DEFAULT_TIMEOUT, 90)
        self.assertEqual(config.MAX_TIMEOUT, 110)

    def test_budget_ends_well_before_the_hook_timeout(self):
        hooks = json.loads((_PKG.parent / "hooks.json").read_text(encoding="utf-8"))
        timeout = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
        self.assertEqual(timeout, 120)
        # 時間切れの後の後始末 (Windows の taskkill は 1 回 10 秒まで) と起動・出力の余裕
        self.assertGreaterEqual(timeout - HOOK_BUDGET, 15)

    def test_budget_holds_the_read_and_the_default_gate(self):
        self.assertGreaterEqual(HOOK_BUDGET, READ_SECONDS + config.DEFAULT_TIMEOUT)

    def test_deadline_sub_never_outlives_the_parent(self):
        parent = Deadline(1)
        self.assertLessEqual(parent.sub(100).remaining(), 1)
        self.assertLessEqual(parent.sub(0.5).remaining(), 0.5)


class WhereToReadTest(unittest.TestCase):
    def _spawned(self, cmd: str) -> bool:
        with mock.patch.object(reading.subprocess, "run", wraps=subprocess.run) as run:
            read_command(cmd, Deadline(60))
        return run.called

    def test_small_commands_stay_in_process(self):
        self.assertFalse(self._spawned("gh pr create --fill"))
        self.assertFalse(self._spawned(_padded("gh pr create --fill", CHILD_FROM - 1)))

    def test_commands_without_the_letters_stay_in_process(self):
        self.assertFalse(self._spawned("x" * (CHILD_FROM + 1)))
        self.assertFalse(self._spawned("echo pr " + "x" * CHILD_FROM))
        self.assertFalse(self._spawned("echo gh " + "x" * CHILD_FROM))

    def test_commands_above_the_limit_are_not_read(self):
        self.assertFalse(self._spawned(_padded("gh pr create", _READ_LIMIT + 1)))

    def test_from_the_threshold_to_the_limit_go_to_the_child(self):
        self.assertTrue(self._spawned(_padded("gh pr create", CHILD_FROM)))
        self.assertTrue(self._spawned(_padded("gh pr create", _READ_LIMIT)))
        self.assertTrue(self._spawned(_padded("g\\\nh pr create", CHILD_FROM)))


class ChildResultTest(unittest.TestCase):
    def test_child_returns_what_the_same_process_reads(self):
        for head in (
            "gh pr create --fill",
            "cd /tmp/a && cd b && gh pr create --base dev --draft --head w -R o/r",
            'cd "$X" && gh pr ready 3',
            "git status",
            "gh pr create --fill && gh pr ready https://example.com/o/r/pull/1",
            "bash -c 'gh pr create'",
            "(gh pr create)",
            "echo あ😀; gh pr create -t あ😀",
            "echo '\ud800'; gh pr create",
        ):
            cmd = _padded(head, CHILD_FROM + 10)
            with self.subTest(head=head):
                found, reason = read_command(cmd, Deadline(60))
                expected = find_invocations(cmd)
                self.assertEqual(found, expected)
                self.assertTrue(all(isinstance(inv, Invocation) and isinstance(inv.cd_steps, tuple) for inv in found))
                self.assertEqual(reason, unresolved_reason(cmd, expected))

    def test_every_invocation_field_crosses_the_process_boundary(self):
        cmd = _padded("cd /a && gh pr create --base dev --draft --head w -R o/r", CHILD_FROM)
        (inv,), _ = read_command(cmd, Deadline(60))
        self.assertEqual(
            (inv.kind, inv.base, inv.draft, inv.head, inv.repo, inv.cd_steps), ("create", "dev", True, "w", "o/r", ("/a",))
        )


class ReadDeadlineTest(unittest.TestCase):
    def test_timeout_stops_within_the_deadline(self):
        t = time.monotonic()
        found, reason = read_command(_SLOW, Deadline(0.2))
        elapsed = time.monotonic() - t
        self.assertLess(elapsed, 0.2 + _MARGIN)
        self.assertIn("制限時間", reason or "")
        self.assertIn("--body-file", reason or "")
        self.assertEqual(found, [])

    def test_worst_form_at_the_limit_ends_by_the_read_deadline(self):
        # 読み終えるか止まるかは速度しだいだが、期限 + 余裕の中で必ず決まる
        bound = READ_SECONDS + _MARGIN
        best = float("inf")
        for _ in range(2):
            t = time.monotonic()
            read_command(_SLOW, Deadline(HOOK_BUDGET))
            best = min(best, time.monotonic() - t)
            if best < bound:
                break
        self.assertLess(best, bound)

    def test_read_is_capped_by_read_seconds_not_only_by_the_budget(self):
        with mock.patch.object(reading, "READ_SECONDS", 0.2):
            t = time.monotonic()
            _, reason = read_command(_SLOW, Deadline(HOOK_BUDGET))
        self.assertLess(time.monotonic() - t, 0.2 + _MARGIN)
        self.assertIn("制限時間", reason or "")

    def test_exhausted_budget_stops_without_starting_the_child(self):
        spent = Deadline(0)
        with mock.patch.object(reading.subprocess, "run") as run:
            _, reason = read_command(_padded("gh pr create"), spent)
        run.assert_not_called()
        self.assertIn("--body-file", reason or "")

    def test_abnormal_child_stops(self):
        cmd = _padded("gh pr create")
        for name, result in (
            ("exit 3", subprocess.CompletedProcess([], 3, b'{"found": [], "reason": null}', b"")),
            ("killed", subprocess.CompletedProcess([], -9, b"", b"")),
            ("empty", subprocess.CompletedProcess([], 0, b"", b"")),
            ("junk", subprocess.CompletedProcess([], 0, b"junk", b"")),
            ("no keys", subprocess.CompletedProcess([], 0, b"{}", b"")),
            ("unknown field", subprocess.CompletedProcess([], 0, b'{"found": [{"x": 1, "cd_steps": []}], "reason": null}', b"")),
            ("no kind", subprocess.CompletedProcess([], 0, b'{"found": [{"cd_steps": []}], "reason": null}', b"")),
        ):
            with self.subTest(name=name), mock.patch.object(reading.subprocess, "run", return_value=result):
                _, reason = read_command(cmd, Deadline(60))
                self.assertIn("--body-file", reason or "")

    def test_child_that_cannot_start_stops(self):
        with mock.patch.object(reading.subprocess, "run", side_effect=OSError("no python")):
            _, reason = read_command(_padded("gh pr create"), Deadline(60))
        self.assertIn("--body-file", reason or "")

    def test_child_really_crashing_stops(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "crash.py"
            script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
            with mock.patch.object(reading, "__file__", str(script)):
                _, reason = read_command(_padded("gh pr create"), Deadline(60))
        self.assertIn("exit 3", reason or "")

    def test_child_ends_itself_when_the_parent_is_gone(self):
        # 正規表現の途中でも見張りが終わらせる (親が kill できなくても残らない)
        t = time.monotonic()
        r = subprocess.run(
            [sys.executable, str(_PKG / "reading.py"), "0.2"],
            input=json.dumps(_SLOW).encode("ascii"),
            capture_output=True,
            timeout=60,
        )
        self.assertLess(time.monotonic() - t, 0.2 + _MARGIN)
        self.assertNotEqual(r.returncode, 0)


class RealHookTest(unittest.TestCase):
    """別プロセスの hook 全体で確かめる。期限だけ短くして測る。"""

    def _hook(self, cmd: str, seconds: float) -> tuple[dict | None, float]:
        script = (
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(_PKG)!r})\n"
            "import reading\n"
            f"reading.READ_SECONDS = {seconds}\n"
            f"runpy.run_path({str(_PKG / '__main__.py')!r}, run_name='__main__')\n"
        )
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(_PKG)})
        env = {k: v for k, v in os.environ.items() if k != "VERIFY_PLUGIN_RELEASE_MODE"}
        t = time.monotonic()
        r = subprocess.run([sys.executable, "-c", script], input=payload.encode("ascii"), capture_output=True, timeout=60, env=env)
        elapsed = time.monotonic() - t
        self.assertEqual(r.returncode, 0, r.stderr)
        return (json.loads(r.stdout) if r.stdout.strip() else None), elapsed

    def test_slow_form_is_denied_by_the_real_hook(self):
        out, elapsed = self._hook(_SLOW, 0.2)
        decision = (out or {}).get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        self.assertIn("制限時間", decision.get("permissionDecisionReason", ""))
        self.assertLess(elapsed, 0.2 + _MARGIN)


class HookBudgetTest(unittest.TestCase):
    """読み取り・repo の確認・設定の読み・ゲートは 1 つの予算を分け合う。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_marketplace(Path(self._tmp.name) / "repo", ["alpha"])
        write_json(
            self.root,
            ".claude/verify-plugin-release.json",
            {"fetch": False, "timeout_seconds": 110, "test_command": [sys.executable, "-c", "import time; time.sleep(10)"]},
        )
        commit_all(self.root, "config")
        sh(self.root, "switch", "-q", "-c", "feat")
        write(self.root, "plugins/alpha/x.txt", "x\n")
        commit_all(self.root, "change")
        self.mod = _load_entry()
        self._env = mock.patch.dict(os.environ, _testutil.hook_process_env(), clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _evaluate(self, cmd: str, budget: float) -> tuple[dict | None, float]:
        with mock.patch.object(self.mod, "HOOK_BUDGET", budget):
            t = time.monotonic()
            out = self.mod.evaluate(cmd, str(self.root))
        return out, time.monotonic() - t

    def test_gate_is_cut_at_the_budget_and_says_timeout_seconds_shrank(self):
        out, elapsed = self._evaluate("gh pr create --fill", 3)
        decision = (out or {}).get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        reason = decision.get("permissionDecisionReason", "")
        self.assertIn("110 秒", reason)
        self.assertIn("縮めて", reason)
        self.assertLess(elapsed, 9)  # テストの 10 秒を待っていない

    def test_operations_in_one_command_share_the_budget(self):
        # draft の検査が予算を使い切ると、次の PR 操作は検査できないので止まる
        out, elapsed = self._evaluate("gh pr create --draft --fill && gh pr create --fill", 3)
        decision = (out or {}).get("hookSpecificOutput", {})
        self.assertEqual(decision.get("permissionDecision"), "deny")
        self.assertIn("使い切った", decision.get("permissionDecisionReason", ""))
        self.assertLess(elapsed, 9)  # テストの 10 秒を待っていない

    def test_repo_lookup_and_config_read_are_cut_by_the_budget(self):
        seen = []
        real_git = self.mod.git

        def spy(args, cwd, dl, cap=None):
            seen.append(dl.remaining())
            return real_git(args, cwd, dl, cap)

        with mock.patch.object(self.mod, "git", spy):
            self._evaluate("gh pr create --fill", 1)
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), 1)


if __name__ == "__main__":
    unittest.main()
