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

from cli import (
    _enforce_output_budget,
    _has_relevant_project_markers,
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
        # Tier 2: common project roots with no detector in this plugin yet.
        ("CMakeLists.txt", False),
        ("Package.swift", False),
        ("mix.exs", False),
        ("build.sbt", False),
        ("Cargo.lock", False),
        ("Gemfile.lock", False),
        ("App.csproj", False),  # *.csproj glob marker
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

