"""`${CLAUDE_PLUGIN_ROOT}` が cache コピーを指していても `hooks/_common` が解決すること。

本番では plugin 全体が `~/.claude/plugins/cache/<plugin>/` にコピーされ、各 hook は
`python3 <copy>/hooks/<hook>` で起動される。plugin root をまるごと別ディレクトリに
コピーして同じ起動形態で実行し、import エラーが出ないことを固定する。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _testutil
from _testutil import PLUGIN_ROOT

_HOOK_INVOCATIONS = (
    ("exitplan-review", []),
    ("post-implementation-review", ["--phase", "pre-tool"]),
    ("post-implementation-review", ["--phase", "post-tool"]),
    ("post-implementation-review", ["--phase", "stop"]),
    ("explore-parallel", ["--phase", "pre"]),
    ("explore-parallel", ["--phase", "post"]),
)


class TestCacheCopyResolvesCommon(unittest.TestCase):
    def test_each_hook_starts_from_a_copied_plugin_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "cache", "external-ai-assist")
            shutil.copytree(
                PLUGIN_ROOT,
                copy,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"),
            )
            tmpdir = os.path.join(tmp, "tmp")
            os.makedirs(tmpdir)
            # cursor / codex が PATH に無い状態で、早期 return 経路だけを通す
            env = {**os.environ, "TMPDIR": tmpdir, "PATH": "/usr/bin:/bin"}

            for hook, args in _HOOK_INVOCATIONS:
                with self.subTest(hook=hook, args=args):
                    proc = subprocess.run(
                        [sys.executable, os.path.join(copy, "hooks", hook), *args],
                        input='{"session_id": "s", "tool_name": "Read", "tool_input": {}}',
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=60,
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertNotIn("ModuleNotFoundError", proc.stderr)
                    self.assertEqual(proc.stdout, "")

    def test_hooks_dir_exposes_only_common(self):
        """hooks/ 直下に .py や import 可能な名前のディレクトリを置かない。

        sys.path に載せて見える名前を `_common` だけに保つ。hook ディレクトリはハイフン付き
        (`exitplan-review` 等) なので namespace package として import されることもない。
        """
        hooks_dir = PLUGIN_ROOT / "hooks"
        stray = sorted(
            p.name
            for p in hooks_dir.iterdir()
            if p.suffix == ".py"
            or (p.is_dir() and p.name.isidentifier() and p.name not in ("_common", "__pycache__"))
        )
        self.assertEqual(stray, [])


if __name__ == "__main__":
    unittest.main()
