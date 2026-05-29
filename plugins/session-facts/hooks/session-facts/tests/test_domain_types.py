"""Domain Types 検出のパス緩和・infra suffix 除外・>=5 ゲート (#10)。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.domain_types import DomainTypesCollector, _is_infra_name
from core.context import AnalysisConfig, RepoContext


def _ctx(tmp, files):
    root = Path(tmp)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    cfg = AnalysisConfig(include_domain_types=True)
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
            self.assertIsNotNone(out)
            self.assertIn("Case", out)
            self.assertIn("Draft", out)
            self.assertIn("Patent", out)
            # infra class filtered out
            self.assertNotIn("CaseRepository", out)

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


if __name__ == "__main__":
    unittest.main()
