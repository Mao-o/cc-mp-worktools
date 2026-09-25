"""config.load と layout (plugin の発見・変更ファイルの振り分け) のテスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _testutil  # noqa: F401
from _testutil import add_plugin, write, write_json

import config
import layout


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_gives_defaults(self):
        cfg = config.load(self.root)
        self.assertFalse(cfg.single_plugin_per_pr)
        self.assertEqual(cfg.timeout_seconds, config.DEFAULT_TIMEOUT)

    def test_values(self):
        write_json(
            self.root,
            ".claude/verify-plugin-release.json",
            {"single_plugin_per_pr": True, "test_command": ["make", "test"], "timeout_seconds": 999},
        )
        cfg = config.load(self.root)
        self.assertTrue(cfg.single_plugin_per_pr)
        self.assertEqual(cfg.test_command, ["make", "test"])
        self.assertEqual(cfg.timeout_seconds, config.MAX_TIMEOUT)

    def test_broken_config_raises(self):
        for body in ("{", "[]", '{"unknown": 1}', '{"fetch": "yes"}', '{"test_command": "make"}'):
            with self.subTest(body=body):
                write(self.root, ".claude/verify-plugin-release.json", body)
                with self.assertRaises(config.ConfigError):
                    config.load(self.root)


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_marketplace_and_unlisted_plugins(self):
        write_json(
            self.root,
            ".claude-plugin/marketplace.json",
            {"name": "m", "owner": {"name": "o"}, "plugins": [
                {"name": "a", "source": "./plugins/a"},
                {"name": "remote", "source": {"source": "github", "repo": "o/r"}},
            ]},
        )
        add_plugin(self.root, "a")
        add_plugin(self.root, "b")  # marketplace 未登録
        found = {p.dir: p for p in layout.discover(self.root)}
        self.assertEqual(set(found), {"plugins/a", "plugins/b"})
        self.assertTrue(found["plugins/a"].in_marketplace)
        self.assertFalse(found["plugins/b"].in_marketplace)

    def test_plugin_root_prefix(self):
        write_json(
            self.root,
            ".claude-plugin/marketplace.json",
            {"name": "m", "owner": {"name": "o"}, "metadata": {"pluginRoot": "./plugins"},
             "plugins": [{"name": "a", "source": "a"}]},
        )
        add_plugin(self.root, "a")
        self.assertEqual([p.dir for p in layout.discover(self.root)], ["plugins/a"])

    def test_single_plugin_repo(self):
        write_json(self.root, ".claude-plugin/plugin.json", {"name": "solo"})
        plugins = layout.discover(self.root)
        self.assertEqual([(p.name, p.dir) for p in plugins], [("solo", "")])
        self.assertEqual(layout.owner("hooks/x.py", plugins).name, "solo")

    def test_owner_and_doc_only(self):
        plugins = [layout.Plugin("a", "plugins/a", True), layout.Plugin("ab", "plugins/ab", True)]
        self.assertEqual(layout.owner("plugins/ab/x.py", plugins).name, "ab")
        self.assertIsNone(layout.owner("README.md", plugins))
        a = plugins[0]
        self.assertTrue(layout.is_doc_only("plugins/a/README.md", a))
        self.assertTrue(layout.is_doc_only("plugins/a/docs/guide.md", a))
        self.assertFalse(layout.is_doc_only("plugins/a/skills/x/SKILL.md", a))


if __name__ == "__main__":
    unittest.main()
