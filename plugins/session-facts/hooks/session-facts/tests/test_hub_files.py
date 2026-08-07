"""Hub Files: 被参照数ランキング (import 解決 + Counter 集計) のユニットテスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path 整備)

from collectors.hub_files import HubFilesCollector
from core.context import AnalysisConfig, RepoContext


def _ctx(tmp, files, max_hub_files=None):
    root = Path(tmp)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    cfg = AnalysisConfig(include_hub_files=True)
    if max_hub_files is not None:
        cfg.max_hub_files = max_hub_files
    ctx = RepoContext(root=root, config=cfg)
    ctx.tracked_files = list(files.keys())
    return ctx


class HubFilesCollectorTest(unittest.TestCase):
    def test_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = RepoContext(root=Path(tmp), config=AnalysisConfig())
            self.assertFalse(HubFilesCollector().should_run(ctx))

    def test_file_referenced_by_two_importers_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/utils/logger.ts": "export const log = () => {}\n",
                "src/a.ts": "import { log } from './utils/logger'\n",
                "src/b.ts": "import { log } from './utils/logger'\n",
            })
            out = HubFilesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("src/utils/logger.ts", out)
            self.assertIn("referenced by 2 files", out)

    def test_singleton_reference_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/utils/logger.ts": "export const log = () => {}\n",
                "src/a.ts": "import { log } from './utils/logger'\n",
            })
            self.assertIsNone(HubFilesCollector().collect(ctx))

    def test_python_relative_import_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "app/models/user.py": "class User:\n    pass\n",
                "app/services/order.py": "from ..models.user import User\n",
                "app/services/invoice.py": "from ..models.user import User\n",
            })
            out = HubFilesCollector().collect(ctx)
            self.assertIsNotNone(out)
            self.assertIn("app/models/user.py", out)
            self.assertIn("referenced by 2 files", out)

    def test_bare_package_imports_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/a.ts": "import React from 'react'\n",
                "src/b.ts": "import React from 'react'\n",
            })
            # 'react' isn't a tracked file, so it can never resolve -> no section.
            self.assertIsNone(HubFilesCollector().collect(ctx))

    def test_self_reference_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _ctx(tmp, {
                "src/a.ts": "import './a'\n",
                "src/b.ts": "import './a'\n",
            })
            # a.ts's self-import is excluded, leaving only b.ts -> 1 referrer,
            # below the min-refs=2 gate, so no section shows.
            self.assertIsNone(HubFilesCollector().collect(ctx))

    def test_max_hub_files_caps_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                "src/x.ts": "export const x = 1\n",
                "src/y.ts": "export const y = 1\n",
            }
            for i in range(4):
                files[f"src/importer{i}.ts"] = (
                    "import { x } from './x'\nimport { y } from './y'\n"
                )
            ctx = _ctx(tmp, files, max_hub_files=1)
            out = HubFilesCollector().collect(ctx)
            self.assertIsNotNone(out)
            ref_lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
            self.assertEqual(len(ref_lines), 1)


if __name__ == "__main__":
    unittest.main()
