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
    cost. _MAX_CANDIDATE_FILES bounds it, mirroring the existing
    _MAX_DEP_FILES/_MAX_PUBSPEC_SCAN caps elsewhere -- deliberately at the
    cost of missing a real cluster placed after enough non-contributing
    candidates."""

    def test_real_cluster_past_the_candidate_cap_is_missed(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                f"src/models/dud{i:02d}.ts": "export interface Props {}\n"
                for i in range(25)  # stop-name only: 0 qualifying names each
            }
            files["src/models/zzz_real.ts"] = DomainTypesExclusionTest._OTHER_MODULE
            ctx = _ctx(tmp, files)
            # Sorted tracked-file order puts zzz_real.ts at position 26,
            # past the 20-file cap -- this is the documented trade-off, not
            # a bug: confirms the cap actually bounds the scan.
            self.assertIsNone(DomainTypesCollector().collect(ctx))

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


if __name__ == "__main__":
    unittest.main()
