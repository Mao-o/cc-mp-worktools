"""cli レベルのテスト (v0.5): --emit subagent-json (.1)、--no-recent-commits (.2)、
purpose dirname fallback 省略 (.3)。

v0.7 で追加: `- more:` ヒントが `python3` 付きで実行可能なこと、detector/collector
の例外が隔離され他のセクションを道連れにしないこと、summarize_repo() 自体が
落ちても main() が最低限のヘッダーで exit 0 すること。"""
from __future__ import annotations

import io
import json
import subprocess
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401  (sys.path 整備)

from cli import _enforce_output_budget, main, summarize_repo
from core.context import AnalysisConfig


def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True,
    )


def _make_repo(tmp) -> Path:
    root = Path(tmp)
    _git(["init", "-b", "main"], root)
    (root / "a.txt").write_text("1\n")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "first commit"], root)
    return root


def _run_cli(argv) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    assert rc == 0
    return buf.getvalue()


class EmitSubagentJsonTest(unittest.TestCase):
    def test_wraps_output_in_hook_specific_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root), "--emit", "subagent-json"])
            payload = json.loads(out)
            hso = payload["hookSpecificOutput"]
            self.assertEqual(hso["hookEventName"], "SubagentStart")
            self.assertIn("## Project Facts", hso["additionalContext"])

    def test_default_emit_is_plain_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root)])
            self.assertTrue(out.startswith("## Project Facts"))


class NoRecentCommitsFlagTest(unittest.TestCase):
    def test_flag_suppresses_recent_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root), "--no-recent-commits"])
            self.assertNotIn("recent_commits", out)

    def test_default_keeps_recent_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = _run_cli(["--root", str(root)])
            self.assertIn("- recent_commits:", out)
            self.assertIn("first commit", out)


class PurposeFallbackTest(unittest.TestCase):
    def test_dirname_fallback_omits_purpose_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n")
            # A project marker keeps this exercising _infer_purpose()'s own
            # fallback logic -- without one, the non-git/no-marker gate
            # (internal backlog) would short-circuit to the minimal header
            # before purpose inference even runs, passing this assertion
            # for an unrelated reason.
            (root / "Makefile").write_text("test:\n\techo hi\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("- purpose:", out)

    def test_package_json_description_still_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"name": "x", "description": "Does something useful"})
            )
            out = _run_cli(["--root", str(root)])
            self.assertIn("- purpose: Does something useful", out)


class MoreHintInvokedAsTest(unittest.TestCase):
    """`- more:` names a directory (real hook invocation is `python3 <dir>`),
    so the printed follow-up command must keep the `python3 ` prefix or it is
    unrunnable as-is ('permission denied': it names a directory, not a
    program)."""

    def test_hint_command_is_runnable_with_python3_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            fake_invoked_as = str(root / "hooks" / "session-facts")
            with mock.patch.object(sys, "argv", [fake_invoked_as]):
                out = _run_cli(["--root", str(root)])
            self.assertIn(
                f"- more: this is the default view; run `python3 {fake_invoked_as} --help` "
                "for additional opt-in analyses",
                out,
            )

    def test_hint_command_quotes_paths_containing_spaces(self):
        # The plugin can be installed under a directory with spaces; an
        # unquoted path makes the interpreter open only the first segment,
        # so the printed command is not runnable as promised.
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            spaced = str(root / "plugin root" / "session-facts")
            with mock.patch.object(sys, "argv", [spaced]):
                out = _run_cli(["--root", str(root)])
            self.assertIn(f"`python3 {shlex.quote(spaced)} --help`", out)
            self.assertNotIn(f"`python3 {spaced} --help`", out)

    def test_fallback_wording_when_invoked_as_is_none(self):
        # summarize_repo() called as a library (invoked_as=None, e.g. not
        # via the CLI's sys.argv[0]) keeps the skill-pointer wording instead
        # of rendering a command with nothing to name.
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out = summarize_repo(root, AnalysisConfig(), is_git=True, cwd=root, invoked_as=None)
            self.assertIn(
                "- more: this is the default view; the session-facts skill "
                "has additional opt-in analyses (see --help)",
                out,
            )
            self.assertNotIn("run `", out)


class ExceptionIsolationTest(unittest.TestCase):
    """A detector/collector that raises must not blank out the rest of the
    output (internal backlog: collector/detector 例外が隔離されず出力ゼロ)."""

    @staticmethod
    def _fake_discover_plugins(pkg_dir, base_package):
        class _GoodDetector:
            name = "good_detector"
            priority = 1

            def detect(self, ctx):
                return ["good_stack_marker"]

        class _BadDetector:
            name = "bad_detector"
            priority = 2

            def detect(self, ctx):
                raise RuntimeError("detector boom")

        class _GoodCollector:
            name = "good_collector"
            section_title = "## Good Section"
            priority = 1

            def should_run(self, ctx):
                return True

            def collect(self, ctx):
                return "## Good Section\n- ok"

        class _BadCollector:
            name = "bad_collector"
            section_title = "## Bad Section"
            priority = 2

            def should_run(self, ctx):
                return True

            def collect(self, ctx):
                raise RuntimeError("collector boom")

        if base_package == "detectors":
            return [_GoodDetector(), _BadDetector()]
        if base_package == "collectors":
            return [_GoodCollector(), _BadCollector()]
        return []

    def test_bad_detector_and_collector_do_not_suppress_good_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with mock.patch("cli.discover_plugins", side_effect=self._fake_discover_plugins):
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = main(["--root", str(root)])
            self.assertEqual(rc, 0)
            out = out_buf.getvalue()
            err = err_buf.getvalue()
            # Header always renders.
            self.assertIn("## Project Facts", out)
            # The good detector's stack entry and the good collector's
            # section both survive despite their "bad" siblings raising.
            self.assertIn("good_stack_marker", out)
            self.assertIn("## Good Section", out)
            self.assertIn("- ok", out)
            # The failing ones are skipped, not silently retried or crashed.
            self.assertNotIn("## Bad Section", out)
            # Failures are surfaced on stderr with the plugin's name, not
            # swallowed entirely.
            self.assertIn("[session-facts] WARNING: detector bad_detector failed", err)
            self.assertIn("[session-facts] WARNING: collector bad_collector failed", err)


class SummarizeRepoFailureFallbackTest(unittest.TestCase):
    """If summarize_repo() itself raises (any failure not already isolated
    per-detector/collector), main() must still exit 0 with a minimal header
    rather than exit 1 with a traceback and no output at all."""

    def test_exit_0_with_minimal_header_on_unexpected_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with mock.patch("cli.summarize_repo", side_effect=RuntimeError("total meltdown")):
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = main(["--root", str(root)])
            self.assertEqual(rc, 0)
            self.assertIn("## Project Facts", out_buf.getvalue())
            self.assertIn("[session-facts] WARNING", err_buf.getvalue())
            self.assertIn("total meltdown", err_buf.getvalue())

    def test_subagent_json_envelope_still_wraps_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with mock.patch("cli.summarize_repo", side_effect=RuntimeError("total meltdown")):
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = main(["--root", str(root), "--emit", "subagent-json"])
            self.assertEqual(rc, 0)
            payload = json.loads(out_buf.getvalue())
            hso = payload["hookSpecificOutput"]
            self.assertEqual(hso["hookEventName"], "SubagentStart")
            self.assertIn("## Project Facts", hso["additionalContext"])


class OutputBudgetTest(unittest.TestCase):
    """internal backlog: no cap on total output size risked exceeding the
    harness's own additionalContext/plain-stdout injection ceiling."""

    HEADER = "## Project Facts\n- repo_root: /example"

    def test_under_budget_returned_unchanged(self):
        sections = ["## Test Snapshot\n- code_files: 3"]
        result = _enforce_output_budget(self.HEADER, sections, max_chars=1000)
        self.assertEqual(result, "\n\n".join([self.HEADER] + sections))
        self.assertNotIn("truncated", result)

    def test_structure_tail_is_trimmed_first_leaving_other_sections_intact(self):
        structure = "\n".join(
            ["## Structure (dirs only, depth=3)"] + [f"├── dir{i}/" for i in range(60)]
        )
        scripts = "## Scripts\n- test: jest --watch"
        sections = [structure, scripts]
        full_len = len("\n\n".join([self.HEADER] + sections))
        # Budget well below the full length but comfortably above header +
        # scripts alone -- only Structure needs to shrink, nothing else.
        max_chars = len(self.HEADER) + len(scripts) + 60
        self.assertLess(max_chars, full_len)

        result = _enforce_output_budget(self.HEADER, sections, max_chars)
        self.assertLessEqual(len(result), max_chars)
        self.assertIn("## Scripts\n- test: jest --watch", result)  # untouched
        self.assertIn("... (truncated)", result)
        self.assertIn("## Structure", result)  # shrunk, not dropped
        self.assertLess(result.count("├──"), 60)

    def test_drops_scripts_then_env_keys_then_notes_before_protected_sections(self):
        sections = [
            "## Structure (dirs only, depth=1)\n├── src/",
            "## Scripts\n- test: jest",
            "## Env Keys\n- API_KEY",
            "## Repo-Specific Notes\n- note: something",
            "## Test Snapshot\n- code_files: 3",
        ]
        # Small enough that Structure's tiny body can't save it, but big
        # enough to keep Test Snapshot -- Scripts/Env Keys/Notes must go.
        max_chars = len(self.HEADER) + len("## Test Snapshot\n- code_files: 3") + 40
        result = _enforce_output_budget(self.HEADER, sections, max_chars)
        self.assertLessEqual(len(result), max_chars)
        self.assertNotIn("## Scripts", result)
        self.assertNotIn("## Env Keys", result)
        self.assertNotIn("## Repo-Specific Notes", result)
        self.assertIn("## Test Snapshot", result)  # protected, survives

    def test_never_silently_drops_protected_sections_hard_cuts_instead(self):
        # Budget so tight even the protected sections alone don't fit -- the
        # cascade must not drop them; it hard-cuts the joined string instead
        # (the max_chars contract holds either way).
        sections = [
            "## Scripts\n- test: jest",
            "## Test Snapshot\n- code_files: 3\n- test_files: 1",
            "## Service Entry Points\n- src/api.py",
            "## Likely Commands\n- npm test",
        ]
        result = _enforce_output_budget(self.HEADER, sections, max_chars=30)
        self.assertLessEqual(len(result), 30)

    def test_result_never_exceeds_max_chars_even_for_pathological_budgets(self):
        # Including budgets smaller than the "... (truncated)" marker itself
        # -- finish() must skip the marker rather than overflow max_chars.
        for max_chars in (0, 1, 5, 10, 17, 25):
            with self.subTest(max_chars=max_chars):
                result = _enforce_output_budget(
                    self.HEADER, ["## Scripts\n- a: b"], max_chars
                )
                self.assertLessEqual(len(result), max_chars)


class HugePackageJsonWithinBudgetTest(unittest.TestCase):
    """Ticket acceptance case: a package.json with many scripts must still
    produce output within max_output_chars, not an unbounded dump."""

    def test_many_long_scripts_stay_within_default_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            scripts = {
                f"script{i}": "run-something --with-a-fairly-long-flag-list " * 4
                for i in range(80)
            }
            (root / "package.json").write_text(json.dumps({"scripts": scripts}))
            out = _run_cli(["--root", str(root)])
            self.assertLessEqual(len(out), AnalysisConfig().max_output_chars)

    def test_custom_max_output_chars_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            scripts = {f"script{i}": "echo hi" for i in range(80)}
            (root / "package.json").write_text(json.dumps({"scripts": scripts}))
            out = _run_cli(["--root", str(root), "--max-output-chars", "500"])
            self.assertLessEqual(len(out), 500)


class ProjectMarkerGateTest(unittest.TestCase):
    """internal backlog: running from a non-project, non-git directory
    (e.g. $HOME, Desktop) used to unconditionally filesystem-walk and
    produce 100+ lines of noise built from whatever files happen to be
    there."""

    def test_non_git_no_markers_gets_minimal_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("shopping list\n")
            (root / "Photos").mkdir()
            out = _run_cli(["--root", str(root)])
            self.assertEqual(
                out.strip(),
                "## Project Facts\n"
                "- git_repo: false\n"
                "- no project markers found; facts skipped",
            )
            self.assertNotIn("## Structure", out)
            self.assertNotIn("## Test Snapshot", out)

    def test_non_git_with_marker_gets_full_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name": "x"}')
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found", out)
            self.assertIn("## Project Facts", out)

    def test_force_walk_restores_full_analysis_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("shopping list\n")
            out = _run_cli(["--root", str(root), "--force-walk"])
            self.assertNotIn("no project markers found", out)

    def test_git_repo_without_markers_is_unaffected(self):
        # is_git bypasses the gate entirely regardless of project markers.
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)  # git-init'd, only a.txt tracked
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found", out)
            self.assertIn("## Project Facts", out)

    def test_subagent_json_envelope_wraps_the_minimal_header_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("x\n")
            out = _run_cli(["--root", str(root), "--emit", "subagent-json"])
            payload = json.loads(out)
            self.assertIn(
                "no project markers found",
                payload["hookSpecificOutput"]["additionalContext"],
            )


if __name__ == "__main__":
    unittest.main()
