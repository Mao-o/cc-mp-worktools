"""collectors/nextjs_facts.py had zero test coverage (internal backlog)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.nextjs_facts import NextjsFactsCollector
from core.context import AnalysisConfig, RepoContext


def _ctx(root: Path, stack=("nextjs",)) -> RepoContext:
    ctx = RepoContext(root=root, config=AnalysisConfig())
    ctx.stack = list(stack)
    return ctx


class NextjsFactsShouldRunTest(unittest.TestCase):
    def test_runs_only_when_nextjs_in_stack(self):
        collector = NextjsFactsCollector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(collector.should_run(_ctx(root, stack=["nextjs"])))
            self.assertFalse(collector.should_run(_ctx(root, stack=["node"])))


class NextjsFactsCollectTest(unittest.TestCase):
    def test_no_signal_at_all_returns_none(self):
        # nextjs in stack but no package.json / router dirs / config file --
        # collect() has nothing to add beyond the (empty) section title.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(NextjsFactsCollector().collect(_ctx(root)))

    def test_next_version_from_package_json_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                '{"dependencies": {"next": "^14.2.3"}}'
            )
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- next_version: 14.2", out)

    def test_app_router_directory_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- app_router: yes", out)

    def test_src_app_router_directory_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "app").mkdir(parents=True)
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- app_router: yes", out)

    def test_pages_router_directory_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pages").mkdir()
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- pages_router: yes", out)

    def test_both_routers_present_reports_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "pages").mkdir()
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- app_router: yes", out)
            self.assertIn("- pages_router: yes", out)

    def test_config_file_is_named_and_scanned_for_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "next.config.ts").write_text(
                "export default {\n  typedRoutes: true,\n  trailingSlash: true,\n}\n"
            )
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- config_file: next.config.ts", out)
            self.assertIn("- config_hint: typedRoutes enabled", out)
            self.assertIn("- config_hint: trailingSlash enabled", out)

    def test_output_standalone_hint_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "next.config.js").write_text(
                'module.exports = { output: "standalone" }\n'
            )
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- config_hint: output standalone", out)

    def test_config_hint_count_is_capped_by_max_config_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "next.config.js").write_text(
                "module.exports = {\n"
                "  typedRoutes: true,\n"
                '  output: "standalone",\n'
                "  trailingSlash: true,\n"
                "  images: {},\n"
                "  experimental: {},\n"
                "}\n"
            )
            ctx = _ctx(root)
            ctx.config.max_config_hints = 2
            out = NextjsFactsCollector().collect(ctx)
            self.assertEqual(out.count("- config_hint:"), 2)

    def test_first_matching_config_candidate_wins(self):
        # NEXT_CONFIG_CANDIDATES order is next.config.js, .mjs, .ts -- the
        # first one found on disk is the one named, the rest are ignored.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "next.config.js").write_text("module.exports = {}\n")
            (root / "next.config.ts").write_text("export default {}\n")
            out = NextjsFactsCollector().collect(_ctx(root))
            self.assertIn("- config_file: next.config.js", out)
            self.assertNotIn("next.config.ts", out)


if __name__ == "__main__":
    unittest.main()
