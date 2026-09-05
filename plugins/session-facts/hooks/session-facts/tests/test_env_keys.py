"""collectors/env_keys.py had zero test coverage (internal backlog)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.env_keys import EnvKeysCollector, _collect_env_keys
from core.context import AnalysisConfig, RepoContext


class EnvKeysShouldRunTest(unittest.TestCase):
    def test_always_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RepoContext(root=Path(tmp), config=AnalysisConfig())
            self.assertTrue(EnvKeysCollector().should_run(ctx))


class CollectEnvKeysTest(unittest.TestCase):
    def test_no_env_files_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_collect_env_keys(Path(tmp), max_items=40), [])

    def test_keys_from_env_example_are_extracted_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("API_KEY=\nDATABASE_URL=postgres://x\n")
            self.assertEqual(
                _collect_env_keys(root, max_items=40), ["API_KEY", "DATABASE_URL"]
            )

    def test_lowercase_and_no_equals_lines_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text(
                "# a comment\napi_key=lowercase_ignored\nJUST_A_LINE_NO_EQUALS\nFOO=bar\n"
            )
            self.assertEqual(_collect_env_keys(root, max_items=40), ["FOO"])

    def test_single_char_key_does_not_match(self):
        # The pattern requires at least 2 chars ([A-Z][A-Z0-9_]+).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("A=1\nAB=2\n")
            self.assertEqual(_collect_env_keys(root, max_items=40), ["AB"])

    def test_duplicate_keys_across_files_are_deduped_keeping_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("SHARED_KEY=1\n")
            (root / ".env.local.example").write_text("SHARED_KEY=2\nOTHER_KEY=3\n")
            keys = _collect_env_keys(root, max_items=40)
            self.assertEqual(keys.count("SHARED_KEY"), 1)
            self.assertIn("OTHER_KEY", keys)

    def test_dynamically_discovered_env_variant_is_included(self):
        # Any ".env*example"/".env*sample" file not in the static
        # ENV_FILE_CANDIDATES list is still picked up via safe_iterdir().
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.staging.example").write_text("STAGING_KEY=1\n")
            self.assertEqual(_collect_env_keys(root, max_items=40), ["STAGING_KEY"])

    def test_real_env_file_is_not_scanned(self):
        # The real .env (not .example/.sample) is never a candidate --
        # it may hold secret values, not just key names.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("REAL_SECRET=shh\n")
            self.assertEqual(_collect_env_keys(root, max_items=40), [])

    def test_max_items_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = "\n".join(f"KEY_{i}=" for i in range(10))
            (root / ".env.example").write_text(lines + "\n")
            self.assertEqual(len(_collect_env_keys(root, max_items=3)), 3)


class EnvKeysCollectTest(unittest.TestCase):
    def test_collect_returns_none_when_no_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RepoContext(root=Path(tmp), config=AnalysisConfig())
            self.assertIsNone(EnvKeysCollector().collect(ctx))

    def test_collect_renders_section_with_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("API_KEY=\n")
            ctx = RepoContext(root=root, config=AnalysisConfig())
            out = EnvKeysCollector().collect(ctx)
            self.assertEqual(out, "## Env Keys\n- API_KEY")


if __name__ == "__main__":
    unittest.main()
