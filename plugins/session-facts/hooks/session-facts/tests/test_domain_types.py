"""Domain Types 検出のパス緩和・infra suffix 除外・>=5 ゲート (#10)。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.domain_types import DomainTypesCollector, _is_infra_name
from core.context import AnalysisConfig, RepoContext


def _ctx(tmp, files, max_domain_types=None):
    root = Path(tmp)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    cfg = AnalysisConfig(include_domain_types=True)
    if max_domain_types is not None:
        cfg.max_domain_types = max_domain_types
    ctx = RepoContext(root=root, config=cfg)
    ctx.tracked_files = list(files.keys())
    return ctx


class InfraNameTest(unittest.TestCase):
    def test_infra_suffixes_detected(self):
        for name in ("CaseRepository", "UserService", "AuthController", "FooFactory"):
            self.assertTrue(_is_infra_name(name), name)

    def test_plain_domain_names_pass(self):
        for name in ("Case", "Draft", "Patent", "Applicant"):
            self.assertFalse(_is_infra_name(name), name)

    def test_exact_suffix_word_not_treated_as_infra(self):
        # The bare word "Service" is in _STOP_NAMES; _is_infra_name should not
        # also flag it via the endswith check (avoids double-handling).
        self.assertFalse(_is_infra_name("Service"))


class DomainTypesCollectorTest(unittest.TestCase):
    def test_repositories_dir_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/repositories/case.ts": (
                    "export interface Case {}\n"
                    "export type Draft = {}\n"
                    "export interface Applicant {}\n"
                    "export interface Patent {}\n"
                    "export enum PatentStatus { A, B }\n"
                    "export class CaseRepository {}\n"
                ),
            })
            out = DomainTypesCollector().collect(ctx)
            # Per-file display cap (internal backlog: representativeness)
            # keeps only the first 3 qualifying names from this one file --
            # all 5 still count toward the >= 5 cluster gate (see the
            # DomainTypesGateVsDisplayTest class below), and the infra class
            # is filtered out regardless of the cap.
            self.assertEqual(
                out,
                "## Domain Types\n- src/repositories/case.ts: Case, Draft, Applicant",
            )

    def test_services_and_schemas_dirs_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "app/services/order.py": "class Order:\n    pass\nclass Invoice:\n    pass\n",
                "app/schemas/user.py": "class User:\n    pass\nclass Profile:\n    pass\nclass Address:\n    pass\n",
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            for name in ("Order", "Invoice", "User", "Profile", "Address"):
                self.assertIn(name, out)

    def test_below_five_types_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/models/a.ts": "export interface One {}\nexport interface Two {}\n",
                "src/models/b.ts": "export type Three = {}\n",
            })
            # Only 3 unique types -> below the 5-type threshold -> no section.
            self.assertIsNone(DomainTypesCollector().collect(ctx))

    def test_low_cap_with_cluster_shows_truncated(self):
        # Codex P2 regression: --max-domain-types below the cluster gate must
        # still surface types when the repo genuinely has >= 5 of them.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/models/types.ts": (
                    "export interface Case {}\n"
                    "export interface Draft {}\n"
                    "export interface Patent {}\n"
                    "export interface Applicant {}\n"
                    "export interface Inventor {}\n"
                    "export interface Claim {}\n"
                ),
            }, max_domain_types=3)
            out = DomainTypesCollector().collect(ctx)
            self.assertEqual(
                out,
                "## Domain Types\n- src/models/types.ts: Case, Draft, Patent",
            )

    def test_low_cap_without_cluster_suppressed(self):
        # Only 3 types exist (< gate); a low cap must not lower the cluster bar.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/models/types.ts": (
                    "export interface One {}\n"
                    "export interface Two {}\n"
                    "export interface Three {}\n"
                ),
            }, max_domain_types=3)
            self.assertIsNone(DomainTypesCollector().collect(ctx))

    def test_non_domain_repo_not_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/utils/helpers.ts": "export function foo() {}\n",
                "src/components/Button.tsx": "export const Button = () => null\n",
            })
            self.assertIsNone(DomainTypesCollector().collect(ctx))

    def test_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RepoContext(root=root, config=AnalysisConfig())  # include_domain_types=False
            self.assertFalse(DomainTypesCollector().should_run(ctx))


class DomainTypesDistributionTest(unittest.TestCase):
    """internal backlog: a single outsized candidate file used to crowd out
    every other module (real-world case: 10/10 shown all came from one
    entities file). Round-robin across candidate files + a per-file display
    cap fix this; the >= 5 cluster gate is unaffected (see
    DomainTypesGateVsDisplayTest)."""

    def test_large_file_does_not_crowd_out_other_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = "\n".join(f"export interface Big{i} {{}}" for i in range(10))
            ctx = _ctx(tmp, {
                "src/entities/big.ts": big,
                "src/models/a.ts": "export interface Alpha {}\n",
                "src/models/b.ts": "export interface Beta {}\n",
                "src/models/c.ts": "export interface Gamma {}\n",
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertEqual(
                out,
                "## Domain Types\n"
                "- src/entities/big.ts: Big0, Big1, Big2\n"
                "- src/models/a.ts: Alpha\n"
                "- src/models/b.ts: Beta\n"
                "- src/models/c.ts: Gamma",
            )

    def test_single_file_never_exceeds_the_per_file_display_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = "\n".join(f"export interface Big{i} {{}}" for i in range(10))
            ctx = _ctx(tmp, {"src/entities/big.ts": big}, max_domain_types=10)
            out = DomainTypesCollector().collect(ctx)
            self.assertEqual(
                out, "## Domain Types\n- src/entities/big.ts: Big0, Big1, Big2"
            )


class DomainTypesGateVsDisplayTest(unittest.TestCase):
    """The per-file display cap must not tighten the >= 5 cluster gate: a
    single file with a genuine cluster of 5+ domain types is still a real
    cluster, even though only 3 of them are shown."""

    def test_single_file_with_five_types_still_clears_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/models/types.ts": (
                    "export interface One {}\n"
                    "export interface Two {}\n"
                    "export interface Three {}\n"
                    "export interface Four {}\n"
                    "export interface Five {}\n"
                ),
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("One, Two, Three", out)

    def test_single_file_with_four_types_does_not_clear_the_gate(self):
        # One below the >= 5 gate: must stay suppressed, not get rescued by
        # anything related to the display cap.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/models/types.ts": (
                    "export interface One {}\n"
                    "export interface Two {}\n"
                    "export interface Three {}\n"
                    "export interface Four {}\n"
                ),
            })
            self.assertIsNone(DomainTypesCollector().collect(ctx))


class DomainTypesExclusionTest(unittest.TestCase):
    """internal backlog: 7 of 10 slots in one real-world repo were a vendored
    ``*.d.ts`` type declaration file under src/types/. .d.ts, vendor/,
    generated/, *.generated.*, and *.pb.* must never be candidates."""

    _OTHER_MODULE = (
        "export interface Alpha {}\n"
        "export interface Beta {}\n"
        "export interface Gamma {}\n"
        "export interface Delta {}\n"
        "export interface Epsilon {}\n"
    )

    def test_dts_vendor_type_declaration_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/types/woff.d.ts": (
                    'declare module "*.woff" { const x: string; export default x; }\n'
                    "export interface FontFace {}\n"
                ),
                "src/models/a.ts": self._OTHER_MODULE,
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertNotIn("FontFace", out)
            self.assertNotIn("woff.d.ts", out)

    def test_vendor_directory_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/vendor/models/thirdparty.ts": "export interface ThirdPartyThing {}\n",
                "src/models/a.ts": self._OTHER_MODULE,
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertNotIn("ThirdPartyThing", out)

    def test_generated_directory_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/generated/models/auto.ts": "export interface AutoThing {}\n",
                "src/models/a.ts": self._OTHER_MODULE,
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertNotIn("AutoThing", out)

    def test_dot_generated_filename_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/models/schema.generated.ts": "export interface GenThing {}\n",
                "src/models/a.ts": self._OTHER_MODULE,
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertNotIn("GenThing", out)

    def test_dot_pb_filename_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/models/service.pb.go": "type PbThing struct {}\n",
                "src/models/a.ts": self._OTHER_MODULE,
            })
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertNotIn("PbThing", out)


class DomainTypesCandidateFileCapTest(unittest.TestCase):
    """internal backlog: this collector runs on every SessionStart/
    SubagentStart (hooks.json passes --include-domain-types unconditionally),
    so unbounded candidate scanning is a per-hook-call cost, not opt-in
    cost. _MAX_CANDIDATE_FILES bounds *scan cost* once the >= 5 cluster gate
    has already been satisfied -- it must never silently drop a genuine
    cluster that happens to sit past the cap in tracked-file order.
    Monorepos commonly have their real domain types under a directory that
    sorts after 20+ barrel/index files (e.g. packages/ after apps/), so an
    earlier version that truncated the candidate list itself before opening
    any file made the whole section disappear for exactly the repos this
    collector exists to help with; that regression is what the tests below
    guard against."""

    def test_real_cluster_past_the_candidate_cap_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                f"src/models/dud{i:02d}.ts": "export interface Props {}\n"
                for i in range(25)  # stop-name only: 0 qualifying names each
            }
            files["src/models/zzz_real.ts"] = DomainTypesExclusionTest._OTHER_MODULE
            ctx = _ctx(tmp, files)
            # Tracked-file order puts zzz_real.ts at position 26, past the
            # 20-file cap. The cap only bounds scan cost once the gate is
            # already satisfied, and 25 stop-name-only duds never satisfy
            # it, so the scan must continue past the cap and find the
            # cluster -- unlike the old hard-truncation behavior this
            # replaced.
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("Alpha", out)

    def test_candidate_count_one_past_the_cap_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                f"src/models/dud{i:02d}.ts": "export interface Props {}\n"
                for i in range(20)  # exactly _MAX_CANDIDATE_FILES duds
            }
            files["src/models/zzz_real.ts"] = DomainTypesExclusionTest._OTHER_MODULE
            ctx = _ctx(tmp, files)
            # 21 total candidates, real cluster at position 21 -- the exact
            # boundary the soft-cap fix targets (20 candidates was already
            # fine; 21 is where the old hard truncation first dropped the
            # section entirely).
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("Alpha", out)

    def test_candidate_count_exactly_at_the_cap_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                f"src/models/dud{i:02d}.ts": "export interface Props {}\n"
                for i in range(19)
            }
            files["src/models/zzz_real.ts"] = DomainTypesExclusionTest._OTHER_MODULE
            ctx = _ctx(tmp, files)
            # Exactly 20 candidates (19 duds + 1 real): already worked
            # before the soft-cap fix, and must keep working identically.
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("Alpha", out)

    def test_cluster_within_the_candidate_cap_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                f"src/models/aaa_dud{i:02d}.ts": "export interface Props {}\n"
                for i in range(10)
            }
            files["src/models/aab_real.ts"] = DomainTypesExclusionTest._OTHER_MODULE
            ctx = _ctx(tmp, files)
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("Alpha", out)

    def test_break_only_fires_once_the_gate_is_already_satisfied(self):
        # None of the other tests in this class pin the soft-cap break
        # itself: they all have gate_total stuck at 0 for the whole
        # 0..19 stretch (every one of the first 20 candidates is a
        # stop-name-only dud), so "gate_total >= _MIN_DOMAIN_TYPES" is
        # never true when index 20 is reached and the break's own
        # condition never decides the outcome -- commenting the break out
        # entirely still leaves every other test in this file green.
        #
        # This fixture puts the gate_total in between: 6 files under the
        # cap each contribute exactly one distinct type name (gate_total
        # reaches 5, then 6, within the first 20 candidates), and index 20
        # is a 21st file with its own distinct type. With the break,
        # gate_total (6) already clears _MIN_DOMAIN_TYPES (5) by the time
        # index 20 is reached, so that file is never opened and its type
        # must not appear. Without the break, it would be opened (6 is
        # still under the default max_items=10 collect_target) and its
        # type would appear.
        with tempfile.TemporaryDirectory() as tmp:
            files = {}
            for i, name in enumerate(
                ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Theta")
            ):
                files[f"src/models/real{i:02d}.ts"] = f"export interface {name} {{}}\n"
            for i in range(14):  # pad to exactly 20 candidates (6 + 14)
                files[f"src/models/dud{i:02d}.ts"] = "export interface Props {}\n"
            files["src/models/zzz_past_cap.ts"] = "export interface Zeta {}\n"
            ctx = _ctx(tmp, files)
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("Alpha", out)
            self.assertNotIn("Zeta", out)


class DomainTypesScannedFileHardCapTest(unittest.TestCase):
    """internal backlog: the soft cap (_MAX_CANDIDATE_FILES) only stops
    opening new candidate files once the >= 5 cluster gate is already
    satisfied. A repo whose domain-path matches never cluster to >= 5
    qualifying names never satisfies that condition, so the soft cap alone
    left this walk unbounded -- every matching candidate got opened on
    every SessionStart/SubagentStart hook call. _MAX_SCANNED_FILES adds a
    second, gate-independent cap on total files opened; the tests below
    confirm it closes that gap without narrowing the soft-cap fix it sits
    alongside (a genuine cluster between the two caps must still be
    found)."""

    def test_cluster_past_the_scanned_file_hard_cap_is_missed(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {}
            for i in range(229):
                files[f"src/models/dud{i:03d}.ts"] = "export interface Props {}\n"
            files["src/models/mid_real.ts"] = DomainTypesExclusionTest._OTHER_MODULE
            for i in range(229, 250):
                files[f"src/models/dud{i:03d}.ts"] = "export interface Props {}\n"
            ctx = _ctx(tmp, files)
            # 250 stop-name-only duds + 1 real (5-type) file. Dict/tracked-
            # file insertion order puts the real file at index 229 --
            # position 230 -- past the 200-file hard cap
            # (_MAX_SCANNED_FILES). None of the 229 duds before it ever
            # satisfy the >= 5 gate (Props is a stop name, contributing 0
            # qualifying names each), so unlike the soft-cap-only version
            # this scan never gets a chance to open the real file: the
            # hard cap stops opening new candidates at file 200, well
            # short of index 229.
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNone(out)

    def test_cluster_between_the_soft_and_hard_cap_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {}
            for i in range(149):
                files[f"src/models/dud{i:03d}.ts"] = "export interface Props {}\n"
            files["src/models/mid_real.ts"] = DomainTypesExclusionTest._OTHER_MODULE
            ctx = _ctx(tmp, files)
            # 149 stop-name-only duds + 1 real (5-type) file at index 149 --
            # position 150. Past the 20-file soft cap
            # (_MAX_CANDIDATE_FILES) but well short of the 200-file hard
            # cap (_MAX_SCANNED_FILES): the gate is never satisfied by the
            # duds alone, so the soft cap's own gate-satisfied condition
            # never fires and scanning must continue past both index 20
            # and index 149 to find this cluster.
            out = DomainTypesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("Alpha", out)


if __name__ == "__main__":
    unittest.main()
