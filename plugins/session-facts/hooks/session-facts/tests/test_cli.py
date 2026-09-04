"""cli レベルのテスト (v0.5): --emit subagent-json (.1)、--no-recent-commits (.2)、
purpose dirname fallback 省略 (.3)。

v0.7 で追加: `- more:` ヒントが `python3` 付きで実行可能なこと、detector/collector
の例外が隔離され他のセクションを道連れにしないこと、summarize_repo() 自体が
落ちても main() が最低限のヘッダーで exit 0 すること。"""
from __future__ import annotations

import io
import os
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

from cli import (
    _enforce_output_budget,
    _has_relevant_project_markers,
    build_parser,
    main,
    parse_args,
    summarize_repo,
)
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
                f"- more: this is the default view; run `python3 {fake_invoked_as} "
                f"--root {shlex.quote(str(root.resolve()))} --help` "
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
            root_arg = f"--root {shlex.quote(str(root.resolve()))}"
            self.assertIn(f"`python3 {shlex.quote(spaced)} {root_arg} --help`", out)
            self.assertNotIn(f"`python3 {spaced} {root_arg} --help`", out)

    def test_hint_command_names_analyzed_root_not_readers_cwd(self):
        # internal backlog: the printed `--help` hint used to omit --root,
        # so copying it (or extending it with another opt-in flag, e.g.
        # --include-domain-types) from a different cwd than the analyzed
        # directory silently analyzed Path.cwd() instead of anything named
        # in this same header. --root is passed directly here (cwd ==
        # root, no subdirectory) to isolate that concern from the
        # subdirectory-scoping one covered by
        # test_hint_command_names_analyzed_subdirectory_not_repo_root
        # below: the hint must always carry an explicit --root, never rely
        # on the reader's own process cwd.
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            fake_invoked_as = str(root / "hooks" / "session-facts")
            with mock.patch.object(sys, "argv", [fake_invoked_as]):
                out = _run_cli(["--root", str(root)])
            self.assertIn(f"- repo_root: {root.resolve()}", out)
            self.assertIn(
                f"--root {shlex.quote(str(root.resolve()))} --help", out
            )

    def test_hint_command_names_analyzed_subdirectory_not_repo_root(self):
        # Codex review round 2 (P2): when --root points at a subdirectory
        # of a git repo, ctx.root is the git top-level while ctx.cwd
        # retains the originally analyzed subdirectory. Rebuilding this
        # hint from ctx.root reran a copied (or --help-swapped-for-a-real-
        # flag) command against the whole repo, silently losing the
        # subdirectory scope -- and with it whatever cwd-scoped context
        # (## Subtree, test filtering, ...) the original run had. The
        # hint must echo the subdirectory that was actually analyzed
        # (RepoContext.rerun_root), not the wider repo_root.
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            subdir = root / "sub"
            subdir.mkdir()
            fake_invoked_as = str(root / "hooks" / "session-facts")
            with mock.patch.object(sys, "argv", [fake_invoked_as]):
                out = _run_cli(["--root", str(subdir)])
            # repo_root still names the git top-level -- unaffected by this fix.
            self.assertIn(f"- repo_root: {root.resolve()}", out)
            # ...but the rerun hint's --root must be the subdirectory, not
            # the repo root, so copying it preserves the original scope.
            self.assertIn(
                f"--root {shlex.quote(str(subdir.resolve()))} --help", out
            )
            self.assertNotIn(
                f"--root {shlex.quote(str(root.resolve()))} --help", out
            )

    def test_hint_command_names_directory_for_non_git_full_analysis(self):
        # Companion to the subdirectory case above: a non-git directory has
        # no repo-root/cwd split at all (root == cwd by construction in
        # main()), so RepoContext.rerun_root must resolve to that same
        # directory here too, not fall back to some other value.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name": "x"}')
            fake_invoked_as = str(root / "hooks" / "session-facts")
            with mock.patch.object(sys, "argv", [fake_invoked_as]):
                out = _run_cli(["--root", str(root)])
            self.assertIn(
                f"--root {shlex.quote(str(root.resolve()))} --help", out
            )

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

    def test_root_then_help_argument_order_is_accepted(self):
        # A string assertion that the hint *contains* "--root ... --help"
        # cannot catch parse_args() rejecting that argument order (or
        # --help's own position) -- this checks the parser itself accepts
        # exactly what the hint prints, independent of shell/subprocess cost.
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                parse_args(["--root", "/tmp", "--help"])
        self.assertEqual(cm.exception.code, 0)


class HintCommandIsActuallyExecutableTest(unittest.TestCase):
    """Runs the printed `- more:` hint as a real subprocess against the
    plugin's own hooks/session-facts directory -- the way an agent copying
    it verbatim would -- rather than only asserting on its string content.

    This hint's executability has been fixed twice before (a missing
    `python3 ` prefix, then an unquoted path); a string assertion pins the
    text but cannot catch a third such regression, since a typo in how the
    command is assembled could still produce a string that "looks right" but
    fails to run (wrong flag order, a stray quote, etc.)."""

    def test_help_hint_from_a_real_run_executes_successfully(self):
        plugin_dir = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            with mock.patch.object(sys, "argv", [plugin_dir]):
                out = _run_cli(["--root", str(root)])
            hint_line = next(ln for ln in out.splitlines() if ln.startswith("- more:"))
            command = hint_line.split("run `", 1)[1].split("`", 1)[0]
            result = subprocess.run(
                shlex.split(command), capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout.lower())

    def test_help_hint_from_a_subdirectory_reruns_the_same_subdirectory(self):
        # Codex review round 2 (P2): confirm -- by actually executing the
        # rerun, not just matching the hint's string -- that a hint built
        # from a subdirectory analysis reproduces that same subdirectory
        # scope. Swap the hint's --help for no extra flag (the default
        # action) so the rerun's own output reveals what it analyzed: a
        # `- cwd: sub (subdirectory of repo_root)` line only appears when
        # the rerun's --root is still the subdirectory, not the repo root
        # (cwd == root would omit that line entirely -- see
        # RepoContext.cwd_relative).
        plugin_dir = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            subdir = root / "sub"
            subdir.mkdir()
            with mock.patch.object(sys, "argv", [plugin_dir]):
                out = _run_cli(["--root", str(subdir)])
            hint_line = next(ln for ln in out.splitlines() if ln.startswith("- more:"))
            command = hint_line.split("run `", 1)[1].split("`", 1)[0]
            tokens = shlex.split(command)
            self.assertEqual(tokens[-1], "--help")
            rerun_tokens = tokens[:-1]  # drop --help to run the real analysis
            result = subprocess.run(
                rerun_tokens, capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("- cwd: sub (subdirectory of repo_root)", result.stdout)


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

    def test_exactly_at_max_chars_returned_unchanged(self):
        """internal backlog P3-7: test_under_budget_returned_unchanged's
        max_chars=1000 against a ~50-char input is comfortably, trivially
        under budget and would pass even if the `<=` boundary check in
        _enforce_output_budget's early return were subtly wrong (e.g. off
        by one). This exercises the boundary itself: max_chars set to
        exactly the joined length, not just far above it."""
        sections = ["## Test Snapshot\n- code_files: 3"]
        full = "\n\n".join([self.HEADER] + sections)
        result = _enforce_output_budget(self.HEADER, sections, max_chars=len(full))
        self.assertEqual(result, full)
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

    def test_hard_cut_safety_net_holds_even_below_header_length(self):
        # internal backlog P3-7: this test was previously named
        # test_never_silently_drops_protected_sections_hard_cuts_instead,
        # but max_chars=30 is smaller than self.HEADER alone (~40 chars),
        # so nothing -- not even the header, let alone a protected section
        # -- can actually "survive" here; the only real invariant a budget
        # this pathological can demonstrate is the max_chars contract
        # itself. See test_protected_sections_are_hard_cut_not_dropped
        # below for a properly-sized budget that lets a protected section
        # genuinely survive (truncated, not silently dropped whole).
        sections = [
            "## Scripts\n- test: jest",
            "## Test Snapshot\n- code_files: 3\n- test_files: 1",
            "## Service Entry Points\n- src/api.py",
            "## Likely Commands\n- npm test",
        ]
        result = _enforce_output_budget(self.HEADER, sections, max_chars=30)
        self.assertLessEqual(len(result), 30)

    def test_protected_sections_are_hard_cut_not_dropped(self):
        """internal backlog P3-7: with a budget sized to hold the header
        plus a little of the first protected section (but not all of it,
        once the expendable ## Scripts is dropped), the protected section
        must actually be present -- truncated mid-body, not vanished
        whole. This is the "hard-cuts instead" claim the old test's name
        made but never checked."""
        sections = [
            "## Scripts\n- test: jest",
            "## Test Snapshot\n- code_files: 3\n- test_files: 1",
            "## Service Entry Points\n- src/api.py",
            "## Likely Commands\n- npm test",
        ]
        max_chars = len(self.HEADER) + 20
        result = _enforce_output_budget(self.HEADER, sections, max_chars)
        self.assertLessEqual(len(result), max_chars)
        self.assertNotIn("## Scripts", result)  # expendable: fully dropped
        self.assertIn("## Test Snapshot", result)  # protected: at least started
        # Confirms this is a genuine hard cut mid-body, not a clean
        # whole-section drop that happened to leave the header behind.
        self.assertNotIn("test_files: 1", result)

    def test_result_never_exceeds_max_chars_even_for_pathological_budgets(self):
        # Including budgets smaller than the "... (truncated)" marker itself
        # -- finish() must skip the marker rather than overflow max_chars.
        for max_chars in (0, 1, 5, 10, 17, 25):
            with self.subTest(max_chars=max_chars):
                result = _enforce_output_budget(
                    self.HEADER, ["## Scripts\n- a: b"], max_chars
                )
                self.assertLessEqual(len(result), max_chars)

    def test_subtree_is_trimmed_when_structure_alone_is_not_enough(self):
        """internal backlog P2-2: in cwd-scoped mode, ``## Structure``
        self-shrinks to a small top-level overview and the real bulk moves
        to ``## Subtree`` -- the cascade used to only know how to shrink
        ``## Structure``, leaving Subtree (often the larger section)
        completely untouched by the deliberate trim mechanism."""
        structure = "## Structure (dirs only, depth=1)\n├── plugins/\n├── skills/"
        subtree = "\n".join(
            ["## Subtree (cwd: plugins/foo, dirs only, depth=3)"]
            + [f"├── mod{i}/" for i in range(40)]
        )
        scripts = "## Scripts\n- test: pytest"
        sections = [structure, subtree, scripts]
        full_len = len("\n\n".join([self.HEADER] + sections))
        max_chars = len(self.HEADER) + len(structure) + len(scripts) + 80
        self.assertLess(max_chars, full_len)

        result = _enforce_output_budget(self.HEADER, sections, max_chars)
        self.assertLessEqual(len(result), max_chars)
        # The small, valuable cross-module overview survives in full ...
        self.assertIn(structure, result)
        # ... while Subtree -- the actual bulk -- is the one shrunk.
        self.assertLess(result.count("├── mod"), 40)
        self.assertIn("## Scripts\n- test: pytest", result)

    def test_cascade_does_not_drop_more_sections_than_max_chars_requires(self):
        """internal backlog P3-5: every step used to target
        max_chars - len(marker) instead of max_chars itself, so the
        cascade could drop an *additional* whole section purely to reserve
        room for the "... (truncated)" marker, even though dropping just
        the first one already satisfied max_chars. Env Keys must survive
        here: dropping Scripts alone is already enough."""
        header = "H" * 10
        protected = "## Test Snapshot\n- " + "a" * 30
        scripts = "## Scripts\n- " + "s" * 20
        env_keys = "## Env Keys\n- " + "e" * 20
        sections = [protected, scripts, env_keys]
        # Big enough that dropping Scripts alone clears max_chars; small
        # enough that the old max_chars-17 target would also drop Env Keys.
        max_chars = len("\n\n".join([header, protected, env_keys]))

        result = _enforce_output_budget(header, sections, max_chars)
        self.assertLessEqual(len(result), max_chars)
        self.assertNotIn("## Scripts", result)
        self.assertIn("## Env Keys", result)  # must survive


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
            # internal backlog P2-4: the minimal header must also name the
            # analyzed directory and hint at the escape hatch (--force-walk)
            # -- without either, an agent stuck here has no way to tell
            # whether this was the right directory or how to get past it.
            # main() resolves --root (symlinks included, e.g. macOS's
            # /var -> /private/var) before it ever reaches summarize_repo(),
            # so the expected value must be resolved the same way.
            self.assertIn(f"- repo_root: {root.resolve()}", out)
            self.assertIn("- git_repo: false", out)
            self.assertIn("- no project markers found; facts skipped", out)
            self.assertIn("--force-walk", out)
            self.assertNotIn("## Structure", out)
            self.assertNotIn("## Test Snapshot", out)

    def test_minimal_header_hint_is_runnable_with_python3_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("x\n")
            fake_invoked_as = str(root / "hooks" / "session-facts")
            with mock.patch.object(sys, "argv", [fake_invoked_as]):
                out = _run_cli(["--root", str(root)])
            # PR #67 round 3 (Codex P2): --root を明示して別ディレクトリから
            # 起動された場合、ヒントが root を落とすと Path.cwd() が解析され、
            # ヘッダーの repo_root とは別のディレクトリの結果が返る。
            self.assertIn(
                f"- more: run `python3 {fake_invoked_as} "
                f"--root {shlex.quote(str(root.resolve()))} --force-walk` "
                "to force the full analysis anyway",
                out,
            )

    def test_minimal_header_hint_quotes_paths_containing_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("x\n")
            spaced = str(root / "plugin root" / "session-facts")
            with mock.patch.object(sys, "argv", [spaced]):
                out = _run_cli(["--root", str(root)])
            root_arg = f"--root {shlex.quote(str(root.resolve()))}"
            self.assertIn(
                f"`python3 {shlex.quote(spaced)} {root_arg} --force-walk`", out
            )
            self.assertNotIn(f"`python3 {spaced} {root_arg} --force-walk`", out)

    def test_minimal_header_fallback_wording_when_invoked_as_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("x\n")
            out = summarize_repo(root, AnalysisConfig(), is_git=False, invoked_as=None)
            self.assertIn(
                f"- more: pass `--root {shlex.quote(str(root))} --force-walk` "
                "to force the full analysis anyway",
                out,
            )
            self.assertNotIn("run `", out)

    def test_minimal_header_hint_quotes_analyzed_root_with_spaces(self):
        """PR #67 round 3 (Codex P2): ヒントに載せる root も quote する。
        空白を含むディレクトリでヒントをそのまま実行すると引数が割れる。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "my project"
            root.mkdir()
            (root / "notes.txt").write_text("x\n")
            out = summarize_repo(root, AnalysisConfig(), is_git=False, invoked_as=None)
            self.assertIn(shlex.quote(str(root)), out)
            self.assertNotIn(f"--root {root} --force-walk", out)

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


class ProjectMarkerHomeMiseExclusionTest(unittest.TestCase):
    """Isolated-review P2-a (PR #67): the marker gate checked
    ``.config/mise/config.toml`` via has_project_markers()'s plain exists()
    check, so a user's global XDG mise config at $HOME made the gate treat
    $HOME as "a project" -- exactly the environment this gate exists to
    protect. core.runtime.mise_config_path() already excludes that path at
    $HOME (see test_runtime.py's MiseConfigHomeExclusionTest); the gate must
    share that exception instead of applying a plain exists() check that
    knows nothing about it."""

    def test_xdg_global_mise_config_alone_at_home_still_gets_minimal_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg_dir = home / ".config" / "mise"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text('[tools]\nawscli = "latest"\n')
            with mock.patch("core.runtime.Path.home", return_value=home):
                out = summarize_repo(home, AnalysisConfig(), is_git=False, invoked_as=None)
            self.assertIn("no project markers found", out)

    def test_project_style_mise_toml_at_home_still_gets_full_analysis(self):
        # Only the XDG global path is excluded -- a project-style .mise.toml
        # placed directly at $HOME is still a deliberate marker (matches
        # mise_config_path()'s own test_dotmise_toml_still_honored_at_home).
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".mise.toml").write_text('[tools]\nnode = "20"\n')
            with mock.patch("core.runtime.Path.home", return_value=home):
                out = summarize_repo(home, AnalysisConfig(), is_git=False, invoked_as=None)
            self.assertNotIn("no project markers found", out)

    def test_xdg_global_mise_config_still_fires_gate_outside_home(self):
        # Same layout, but root is not the (mocked) home dir -- the
        # exclusion must not leak outside the one directory it targets.
        with tempfile.TemporaryDirectory() as project_tmp, \
                tempfile.TemporaryDirectory() as home_tmp:
            root = Path(project_tmp)
            home = Path(home_tmp)
            cfg_dir = root / ".config" / "mise"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.toml").write_text('[tools]\npython = "3.12"\n')
            with mock.patch("core.runtime.Path.home", return_value=home):
                out = summarize_repo(root, AnalysisConfig(), is_git=False, invoked_as=None)
            self.assertNotIn("no project markers found", out)


class MinimalHeaderOutputBudgetTest(unittest.TestCase):
    """Isolated-review P2-d (PR #67): the marker gate's early return
    (_minimal_header()) bypassed _enforce_output_budget() entirely, so
    --max-output-chars was not actually a ceiling on the whole output on
    this path -- only on the (never-reached) full-analysis path. A budget
    of 100 still let the minimal header (~150-200+ chars depending on how
    long --root/invoked_as are) through untouched."""

    def test_minimal_header_is_capped_by_max_output_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("x\n")
            out = summarize_repo(
                root, AnalysisConfig(max_output_chars=100), is_git=False, invoked_as=None,
            )
            self.assertLessEqual(len(out), 100)

    def test_minimal_header_under_budget_is_returned_unchanged(self):
        # Sanity check: a generous (default) budget must still yield the
        # full, untruncated minimal header -- this must not regress into
        # always hard-cutting regardless of budget.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("x\n")
            out = summarize_repo(root, AnalysisConfig(), is_git=False, invoked_as=None)
            self.assertIn("- no project markers found; facts skipped", out)
            self.assertIn("--force-walk", out)


class ProjectMarkerCoverageTest(unittest.TestCase):
    """internal backlog P2-1: PROJECT_MARKERS used to list only ~27 files,
    missing markers for stacks this plugin's own detectors/collectors
    already recognise (review evidence: Dockerfile-only, uv.lock-only,
    nx.json-only, ... repos were all silently SKIPPED by the marker gate
    even though the matching detector would have fired). Each case below
    reproduces one such previously-SKIPPED fixture as a single-marker,
    non-git directory and confirms the gate no longer skips it.

    Setup is a (path, is_dir) pair so the fixture can be either a file
    (most markers) or a bare directory (detectors/prisma.py's "prisma").
    """

    NEW_MARKER_FIXTURES = [
        ("Dockerfile", False),
        ("docker-compose.yml", False),
        (".mise.toml", False),
        (".tool-versions", False),
        ("next.config.js", False),
        ("tsconfig.json", False),
        ("prisma", True),
        ("uv.lock", False),
        ("poetry.lock", False),
        ("vite.config.ts", False),
        ("nx.json", False),
        ("pnpm-workspace.yaml", False),
        ("turbo.json", False),
        ("playwright.config.ts", False),
        ("cypress.config.ts", False),
        ("firebase.json", False),
        (".firebaserc", False),
        # Tier 2: common project roots. CMakeLists.txt/Package.swift/
        # mix.exs/build.sbt/*.csproj/*.sln now also have a matching detector
        # (cmake_stack/swift_stack/elixir_stack/scala_stack/dotnet_stack);
        # Cargo.lock/Gemfile.lock are lockfile-only fallbacks for
        # rust_stack.py/ruby_stack.py, and *.tf (Terraform) is the one
        # entry left with no detector at all in this plugin.
        ("CMakeLists.txt", False),
        ("Package.swift", False),
        ("mix.exs", False),
        ("build.sbt", False),
        ("Cargo.lock", False),
        ("Gemfile.lock", False),
        ("App.csproj", False),  # *.csproj glob marker
        ("App.sln", False),  # *.sln glob marker
        ("main.tf", False),  # *.tf glob marker
    ]

    def test_each_new_marker_alone_avoids_the_minimal_header(self):
        for name, is_dir in self.NEW_MARKER_FIXTURES:
            with self.subTest(marker=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    if is_dir:
                        (root / name).mkdir()
                    else:
                        (root / name).write_text("x\n")
                    out = _run_cli(["--root", str(root)])
                    self.assertNotIn(
                        "no project markers found", out, f"marker {name!r} was not recognised"
                    )
                    self.assertIn("## Project Facts", out)


class RequirementsVariantMarkerTest(unittest.TestCase):
    """Isolated-review P2-b (PR #67): the `requirements.txt` PROJECT_MARKERS
    entry was a literal, so a non-git Python project whose only manifest is
    a common variant (requirements-dev.txt, requirements-prod.txt, ...) fell
    through the gate to the minimal header even though
    collectors/dependencies.py's _tracked_requirements() already recognises
    every ``requirements*.txt`` basename. The marker must mirror that same
    glob so the two definitions do not drift apart again."""

    REQUIREMENTS_VARIANTS = [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements_dev.txt",
        "requirements-prod.txt",
        "requirements-test.txt",
    ]

    def test_requirements_variant_alone_avoids_the_minimal_header(self):
        for name in self.REQUIREMENTS_VARIANTS:
            with self.subTest(marker=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / name).write_text("flask==3.0\n")
                    out = _run_cli(["--root", str(root)])
                    self.assertNotIn(
                        "no project markers found", out, f"marker {name!r} was not recognised"
                    )
                    self.assertIn("## Project Facts", out)


if __name__ == "__main__":
    unittest.main()


class MaxOutputCharsValidationTest(unittest.TestCase):
    """PR #67 round 4 (Codex P2): 負値を通すと `result[:max_chars]` が
    Python の負インデックス slice になり「ほぼ全文」が返る。上限として
    機能しないままハーネスの注入上限を超えうるので引数解析で弾く。
    """

    def test_negative_budget_is_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(["--root", ".", "--max-output-chars", "-1"])

    def test_zero_budget_is_still_accepted(self):
        args = parse_args(["--root", ".", "--max-output-chars", "0"])
        self.assertEqual(args.max_output_chars, 0)


class FormatChoicesTest(unittest.TestCase):
    """internal backlog: --format は json/human が argparse の choices に
    定義されているだけで、値を参照する dispatch 経路が無く出力は常に
    markdown 固定だった (dead option)。json/human を choices から外し
    markdown のみ受理するよう縮小した。既存の hooks.json / codex-hooks.json /
    SKILL.md は元々 `--format markdown` のみを使っているため、この変更で
    壊れる呼び出しは無い。
    """

    def test_format_markdown_is_still_accepted(self):
        args = parse_args(["--root", ".", "--format", "markdown"])
        self.assertEqual(args.format, "markdown")

    def test_format_omitted_defaults_to_markdown(self):
        args = parse_args(["--root", "."])
        self.assertEqual(args.format, "markdown")

    def test_format_json_is_rejected_by_argparse(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--root", ".", "--format", "json"])

    def test_format_human_is_rejected_by_argparse(self):
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--root", ".", "--format", "human"])


class ExceptionFallbackBudgetTest(unittest.TestCase):
    """PR #67 round 6 (Codex P2): summarize_repo() が予期せず落ちたときの
    フォールバックが上限適用を通っておらず、`--max-output-chars` が出力全体の
    上限として成立していなかった (長い root ではハーネスの注入上限を超えうる)。
    """

    def test_fallback_output_respects_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ("d" * 120)
            root.mkdir()
            with mock.patch(
                "cli.summarize_repo", side_effect=RuntimeError("boom")
            ), mock.patch("sys.stderr", new=io.StringIO()):
                buf = io.StringIO()
                with mock.patch("sys.stdout", new=buf):
                    rc = main(["--root", str(root), "--max-output-chars", "10"])
            self.assertEqual(rc, 0)
            self.assertLessEqual(len(buf.getvalue().strip()), 10)


class PythonVersionMarkerTest(unittest.TestCase):
    """PR #67 round 6 (Codex P2): ランタイム固定ファイルだけを持つ非 git の
    Python プロジェクトが marker gate で落ち、facts が丸ごと消えていた。
    """

    def test_python_version_only_project_is_analyzed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".python-version").write_text("3.11.9\n")
            (root / "app.py").write_text("print('hi')\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found; facts skipped", out)
            # gate を通ったことの実証: ランタイム検出とファイル走査の両方が
            # 実際に走っている (最小ヘッダーではどちらも出ない)。
            self.assertIn("- runtime: python 3.11.9 (.python-version)", out)
            self.assertIn("app.py", out)


class SurrogateBudgetTest(unittest.TestCase):
    r"""PR #67 (Codex P2): 非 UTF-8 バイトを含むファイル名は surrogateescape の
    コードポイントで渡り、上限判定は 1 文字と数えるが、stdout の
    backslashreplace ハンドラが出力時に 6 文字 (\udcff の形) へ展開する。
    上限ちょうどに収めたつもりの出力が実際には大きく超えていた。
    """

    def test_budget_counts_the_escaped_representation(self):
        surrogate = chr(0xDCFF)
        header = "## Project Facts\n- repo_root: /x/" + surrogate * 20
        out = _enforce_output_budget(header, [], 100)
        emitted = out.encode("utf-8", "backslashreplace").decode("utf-8")
        self.assertLessEqual(len(emitted), 100)


class HomeRuntimePinMarkerTest(unittest.TestCase):
    """PR #67 (Codex P2): `$HOME` 直下のランタイム固定ファイルはユーザー全体の
    既定を意味し、そのディレクトリがプロジェクトであることを示さない。
    gate がこれを通すと、まさに守るべきホームで全走査が復活する。
    """

    def test_home_runtime_pin_does_not_pass_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            (home / ".tool-versions").write_text("python 3.11.9\n")
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertFalse(_has_relevant_project_markers(home))

    def test_same_pin_outside_home_still_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            proj = Path(tmp) / "proj"
            proj.mkdir()
            (proj / ".tool-versions").write_text("python 3.11.9\n")
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertTrue(_has_relevant_project_markers(proj))


class NestedWorkspaceMarkerTest(unittest.TestCase):
    """PR #67 (Codex P2): ルート直下にマニフェストを置かないワークスペースを
    「非プロジェクト」と誤判定すると facts が丸ごと消える。gate の目的は
    無関係な巨大ディレクトリの全走査回避なので、深さと件数を限定して探す。
    """

    def test_nested_manifests_pass_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text("{}\n")
            (root / "api").mkdir()
            (root / "api" / "pyproject.toml").write_text("[project]\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found; facts skipped", out)

    def test_directory_without_any_manifest_is_still_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Photos").mkdir()
            (root / "Photos" / "a.jpg").write_text("x")
            (root / "notes.txt").write_text("shopping list\n")
            out = _run_cli(["--root", str(root)])
            self.assertIn("no project markers found; facts skipped", out)

    def test_deeply_buried_manifest_is_not_searched(self):
        # 深さ上限を超える位置のマニフェストは探さない (gate が避けたい
        # 「無制限の探索」に戻らないことの固定)。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "a" / "b" / "c" / "d"
            deep.mkdir(parents=True)
            (deep / "package.json").write_text("{}\n")
            out = _run_cli(["--root", str(root)])
            self.assertIn("no project markers found; facts skipped", out)


class LockfileOnlyMarkerTest(unittest.TestCase):
    """PR #67 (Codex P2): lockfile だけを持つディレクトリでも package manager・
    構造・ソース・テストの収集は動くので、gate で落とすと facts が無意味に消える。
    """

    def test_package_lock_only_project_passes_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
            (root / "index.js").write_text("console.log(1)\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found; facts skipped", out)

    def test_every_lockfile_pm_detects_is_a_marker(self):
        # pm.py が認識する lockfile と gate のマーカー一覧がずれないことを固定する
        # (ずれると「検出はできるのに gate で落ちる」構成が生まれる)。
        import re
        from core.constants import PROJECT_MARKERS

        pm_src = (Path(__file__).resolve().parent.parent / "core" / "pm.py").read_text()
        detected = set(re.findall(r'root / "([^"]+\.lock[^"]*|[^"]*lock[^"]*)"', pm_src))
        missing = {name for name in detected if name not in PROJECT_MARKERS}
        self.assertEqual(missing, set(), f"gate に無い lockfile: {sorted(missing)}")


class StdoutNewlineBudgetTest(unittest.TestCase):
    """PR #67 (Codex P2): plain stdout 経路は print() が末尾に改行を 1 文字
    足すため、上限ちょうどを指定した呼び出しが必ず 1 文字超えていた。
    """

    def test_emitted_bytes_including_newline_fit_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}\n")
            buf = io.StringIO()
            with mock.patch("sys.stdout", new=buf):
                main(["--root", str(root), "--max-output-chars", "60"])
            self.assertLessEqual(len(buf.getvalue()), 60)


class HomeNestedDiscoveryTest(unittest.TestCase):
    """PR #67 (Codex P2): ホームディレクトリは「配下のどこかにプロジェクトが
    ある」のが常態なので、入れ子探索を許すと必ず 1 件見つかり gate が素通りする。
    この gate が存在する理由そのものなので、ホームでは探索しない。
    """

    def test_home_with_nested_projects_still_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "dev" / "proj").mkdir(parents=True)
            (home / "dev" / "proj" / "package.json").write_text("{}\n")
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertFalse(_has_relevant_project_markers(home))

    def test_non_home_with_nested_projects_still_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            ws = Path(tmp) / "workspace"
            (ws / "web").mkdir(parents=True)
            (ws / "web" / "package.json").write_text("{}\n")
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertTrue(_has_relevant_project_markers(ws))


class ZeroBudgetEmitsNothingTest(unittest.TestCase):
    """PR #67 (Codex P2): 上限 0 のとき print() が改行 1 文字を出し、
    「出力全体が上限以内」という約束を上限ちょうどで破っていた。
    """

    def test_zero_budget_writes_no_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{}\n")
            buf = io.StringIO()
            with mock.patch("sys.stdout", new=buf):
                rc = main(["--root", str(root), "--max-output-chars", "0"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")


class NestedDiscoveryBoundsTest(unittest.TestCase):
    """PR #67 (Codex P2): 入れ子マーカー探索の 2 つの穴。

    1. clone を 1 つ置いただけのディレクトリが「ワークスペース」と判定される。
       walk 側は clone を .git 境界で刈るため、無関係な兄弟だけを拾った
       誤解を招く facts が出る
    2. 打ち切り前にディレクトリ全体を列挙・ソートしており、子が大量にある
       ディレクトリで上限が意味を失う
    """

    def test_directory_holding_only_a_clone_is_still_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Desktop"
            clone = root / "repo"
            clone.mkdir(parents=True)
            (clone / ".git").mkdir()
            (clone / "package.json").write_text("{}\n")
            out = _run_cli(["--root", str(root)])
            self.assertIn("no project markers found; facts skipped", out)

    def test_enumeration_stops_at_the_visit_budget(self):
        from core.fs import has_nested_project_markers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(200):
                (root / f"d{i:03d}").mkdir()
            seen = []
            real_scandir = os.scandir

            def counting_scandir(path):
                it = real_scandir(path)

                class Counting:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return it.__exit__(*exc)

                    def __iter__(self_inner):
                        for entry in it:
                            seen.append(entry)
                            yield entry

                return Counting()

            with mock.patch.object(os, "scandir", counting_scandir):
                self.assertFalse(
                    has_nested_project_markers(
                        root, ("package.json",), (), max_depth=2, max_dirs=8
                    )
                )
            # 200 件すべてを列挙してから打ち切るのではなく、予算ぶんで止まる。
            self.assertLess(len(seen), 200)

    def test_stat_calls_are_bounded_on_a_file_heavy_directory(self):
        """候補ディレクトリが 1 つも無い巨大ディレクトリでも stat が全件に
        及ばないこと。実コストは列挙 + is_dir/is_symlink/.git の stat なので、
        打ち切りは候補件数ではなく列挙件数にかける必要がある。
        """
        from core.fs import MAX_NESTED_SCAN_ENTRIES, has_nested_project_markers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            total = MAX_NESTED_SCAN_ENTRIES + 300
            for i in range(total):
                (root / f"f{i:05d}.txt").write_text("x")
            seen = []
            real_scandir = os.scandir

            def counting_scandir(path):
                it = real_scandir(path)

                class Counting:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return it.__exit__(*exc)

                    def __iter__(self_inner):
                        for entry in it:
                            seen.append(entry)
                            yield entry

                return Counting()

            with mock.patch.object(os, "scandir", counting_scandir):
                self.assertFalse(
                    has_nested_project_markers(root, ("package.json",), ())
                )
            # 列挙自体が上限で止まる (scandir はストリーミングなので、
            # ここで止めれば巨大ディレクトリを丸ごと読むコストが発生しない)。
            self.assertLessEqual(len(seen), MAX_NESTED_SCAN_ENTRIES)
            self.assertLess(len(seen), total)


class DropWithoutMarkerHeadroomTest(unittest.TestCase):
    """PR #67 (Codex P2): セクションを丸ごと落として上限に収まったが、
    marker を置く余地だけが無く、削れる木構造の末尾も残っていない場合、
    marker 無しで返していた。セクションが消えているのに完全な出力に見える。
    """

    def test_marker_survives_when_a_section_was_dropped(self):
        header = "## Project Facts\n- repo_root: /example"
        sections = ["## Scripts\n- build: x", "## Test Snapshot\n- tests: 1"]
        full = "\n\n".join([header] + sections)
        # Scripts を落とすとちょうど収まるが marker のぶんは残らない幅にする。
        without_scripts = "\n\n".join([header, sections[1]])
        budget = len(without_scripts) + 3
        self.assertLess(budget, len(full))

        result = _enforce_output_budget(header, sections, budget)
        self.assertLessEqual(len(result), budget)
        self.assertNotIn("## Scripts", result)
        self.assertIn("truncated", result)


class LocalVenvMarkerTest(unittest.TestCase):
    """PR #67 (Codex P2): ローカル venv だけを持つ非 git の Python
    プロジェクトが gate で落ち、ランタイム情報もソースも出なくなっていた。
    """

    def test_local_venv_project_passes_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".venv").mkdir()
            (root / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
            (root / "app.py").write_text("print(1)\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found; facts skipped", out)

    def test_home_local_venv_does_not_pass_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".venv").mkdir(parents=True)
            (home / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertFalse(_has_relevant_project_markers(home))


class GlobMarkerScanBoundTest(unittest.TestCase):
    """PR #67 (Codex P2): glob マーカーを 1 パターンずつ `glob()` で回すと、
    マッチしないときにパターン数ぶんルートを全列挙する。マーカー無しの巨大な
    フラットディレクトリ (この gate が抑止したい対象そのもの) で実行時間上限
    を食い潰しうる。
    """

    def test_glob_markers_scan_the_root_once_with_a_bound(self):
        from core.fs import MAX_NESTED_SCAN_ENTRIES, has_project_markers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            total = MAX_NESTED_SCAN_ENTRIES + 400
            for i in range(total):
                (root / f"f{i:05d}.txt").write_text("x")
            seen = []
            real_scandir = os.scandir

            def counting_scandir(path):
                it = real_scandir(path)

                class Counting:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return it.__exit__(*exc)

                    def __iter__(self_inner):
                        for entry in it:
                            seen.append(entry)
                            yield entry

                return Counting()

            with mock.patch.object(os, "scandir", counting_scandir):
                self.assertFalse(
                    has_project_markers(
                        root, ("requirements*.txt", "*.csproj", "*.tf")
                    )
                )
            # パターン数ぶんの全列挙 (3 * total) にならず、1 パス・上限つき。
            self.assertLessEqual(len(seen), MAX_NESTED_SCAN_ENTRIES)


class CandidateTupleMarkerParityTest(unittest.TestCase):
    """`core/constants.py` の `*_CANDIDATES` 系タプル (収集側が root 直下で
    探すファイル名の一覧) が、すべて gate のマーカーに入っていることを固定する。

    このクラスの穴 -- 「検出はできるのに gate で落ちる」= facts が丸ごと
    消える -- は本 PR のレビューで lockfile・ランタイム固定・ローカル venv・
    env テンプレートの 4 回出た。個別のモジュールごとに閉じても次の
    モジュールから出てくるので、候補タプルという単位でまとめて固定する。
    """

    def test_all_candidate_tuples_are_markers(self):
        import core.constants as constants
        from core.constants import PROJECT_MARKERS

        missing = {}
        for name in dir(constants):
            if not name.endswith("_CANDIDATES"):
                continue
            values = getattr(constants, name)
            if not isinstance(values, (tuple, list)):
                continue
            gaps = [v for v in values if v not in PROJECT_MARKERS]
            if gaps:
                missing[name] = gaps
        self.assertEqual(missing, {}, f"gate に無い候補: {missing}")


class RuntimeMarkerParityTest(unittest.TestCase):
    """検出側が根拠にするファイル名と gate のマーカー一覧がずれないことを固定する。

    ずれると「検出はできるのに gate で落ちる」= facts が丸ごと消える構成が
    生まれる。本 PR のレビューでは lockfile・ランタイム固定・ローカル venv の
    3 クラスで実際にこの穴が見つかった。lockfile だけを固定したときは venv の
    漏れを防げなかったので、ランタイム検出側も対象に含める。
    """

    def test_runtime_detected_files_are_markers(self):
        import re
        from core.constants import PROJECT_MARKERS

        src = (Path(__file__).resolve().parent.parent / "core" / "runtime.py").read_text()
        # runtime.py が根拠にするファイル名リテラル (root / "..." の形)。
        detected = set(re.findall(r'root / "([^"]+)"', src))
        # ディレクトリ判定に使うだけのものは除外する。
        detected = {n for n in detected if "." in n or "/" in n}
        missing = sorted(
            n for n in detected
            if n not in PROJECT_MARKERS and not any(
                m.startswith(n + "/") for m in PROJECT_MARKERS
            )
        )
        self.assertEqual(missing, [], f"gate に無い検出ファイル: {missing}")


class EnvTemplateMarkerTest(unittest.TestCase):
    """PR #67 (Codex P2): env テンプレートだけを持つ非 git プロジェクトが
    gate で落ち、env keys もソース由来の facts も出なくなっていた。
    """

    def test_env_example_only_project_passes_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("API_KEY=\n")
            (root / "app.py").write_text("print(1)\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found; facts skipped", out)

    def test_real_env_file_is_not_a_marker(self):
        # 実体の .env は機密。テンプレートと違い gate 通過の根拠にしない。
        from core.constants import PROJECT_MARKERS

        self.assertNotIn(".env", PROJECT_MARKERS)


class CappedMarkerScanTest(unittest.TestCase):
    """PR #67 (Codex P2): ルート直下の列挙を件数上限で打ち切った場合、
    「マーカーが無い」とは断定できない。断定して skip すると、ルートの
    ファイル数が多い実在のプロジェクトで facts が丸ごと消える。
    """

    def test_glob_marker_after_the_cap_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            from core.fs import MAX_NESTED_SCAN_ENTRIES

            root = Path(tmp)
            for i in range(MAX_NESTED_SCAN_ENTRIES + 200):
                (root / f"f{i:05d}.txt").write_text("x")
            # 上限より後ろに glob マーカーが来る配置。
            (root / "zz_main.tf").write_text("resource {}\n")
            out = _run_cli(["--root", str(root)])
            self.assertNotIn("no project markers found; facts skipped", out)

    def test_capped_scan_is_reported_as_incomplete(self):
        from core.fs import MAX_NESTED_SCAN_ENTRIES, scan_project_markers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(MAX_NESTED_SCAN_ENTRIES + 50):
                (root / f"f{i:05d}.txt").write_text("x")
            found, complete = scan_project_markers(root, ("*.tf",))
            self.assertFalse(found)
            self.assertFalse(complete)

    def test_small_root_scan_is_reported_as_complete(self):
        from core.fs import scan_project_markers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("x")
            found, complete = scan_project_markers(root, ("*.tf",))
            self.assertFalse(found)
            self.assertTrue(complete)

    def test_home_is_not_rescued_by_an_incomplete_scan(self):
        # ホームは gate の存在理由そのものなので救済の対象外。
        from core.fs import MAX_NESTED_SCAN_ENTRIES

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            for i in range(MAX_NESTED_SCAN_ENTRIES + 50):
                (home / f"f{i:05d}.txt").write_text("x")
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                self.assertFalse(_has_relevant_project_markers(home))


def _cli_options_table_text(readme_text):
    """Return just the CLI options table's rows: the pipe-delimited lines
    starting at the "| オプション | ..." header, up to the first line after
    it that is not itself a table row.

    A section- or file-wide substring search lets a flag's *other* mentions
    stand in for its table row: --root also appears in this section's
    non-project-directory sample output and in a "非 git かつ `--root`
    直下に..." prose sentence, so a naive ``flag not in readme_text`` (or
    even a check scoped to the whole "## CLI オプション" section) stays
    green after the table row documenting --root is deleted. Scoping to the
    table's own pipe-delimited rows closes that gap.
    """
    after_heading = readme_text.split("## CLI オプション", 1)[1]
    table_lines = []
    in_table = False
    for line in after_heading.splitlines():
        if line.startswith("| オプション"):
            in_table = True
        if in_table:
            if line.startswith("|"):
                table_lines.append(line)
            else:
                break
    return "\n".join(table_lines)


class HelpFlagsDocumentedInReadmeTest(unittest.TestCase):
    """internal backlog: README.md's CLI options table drifted out of sync
    with the actual flag list (missing --include-hub-files/--max-hub-files
    at the time this was filed). Introspects the real parser via
    build_parser() -- not a hand-maintained flag list here that could itself
    drift -- so a new flag added to cli.py without a README update fails
    this test instead of silently going undocumented."""

    def test_every_flag_appears_in_the_cli_options_table(self):
        parser = build_parser()
        flags = set()
        for action in parser._actions:
            flags.update(action.option_strings)
        flags.discard("-h")
        flags.discard("--help")
        self.assertTrue(flags, "sanity check: parser reported no flags at all")

        readme_path = Path(__file__).resolve().parents[3] / "README.md"
        readme_text = readme_path.read_text(encoding="utf-8")
        table = _cli_options_table_text(readme_text)
        # Matched with surrounding backticks (the table's own formatting),
        # not a bare substring: see _cli_options_table_text's docstring for
        # why a bare substring match is not enough.
        missing = sorted(flag for flag in flags if f"`{flag}`" not in table)
        self.assertEqual(
            missing,
            [],
            f"flags missing from the CLI options table in {readme_path}: {missing}",
        )

