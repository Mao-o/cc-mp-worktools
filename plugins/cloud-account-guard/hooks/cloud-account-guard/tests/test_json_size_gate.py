"""信頼できない JSON を parse する前の大きさの上限 (`cli_config.MAX_FILE_BYTES`)。

Python 3.9 の `json.loads` は大きな整数の parse が桁数の二乗の時間で、hook の timeout を超えると
出力が捨てられて検証が素通しになる。3.9 の実機が無い環境でも固定できるよう、「上限を超える
ファイルでは `json.loads` が大きな入力で呼ばれない」ことを見る (parse より先に弾く)。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401

from core import cache, cli_config  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from services import firebase  # noqa: E402

_LIMIT = cli_config.MAX_FILE_BYTES
# 上限を超える、桁の多すぎる整数を含む JSON (parse に入れば 3.9 で遅く、3.11 以上で ValueError)
_BIG_JSON = '{"projects": {"default": "p"}, "n": ' + "9" * (_LIMIT + 10) + "}"
# 上限を超えるが、普通に parse できる JSON (空白の埋め草)
_PADDED = '{"projects": {"default": "p"}}' + " " * (_LIMIT + 10)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = Path(self.tmp)
        self.big_parses: list[int] = []
        real = json.loads

        def spy(text, *args, **kwargs):
            if len(text) > _LIMIT:
                self.big_parses.append(len(text))
            return real(text, *args, **kwargs)

        patcher = mock.patch.object(json, "loads", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, fn, *args):
        """例外が漏れる退行を ERROR ではなく FAIL (検出) にする。hook では例外が検証のスキップになる。"""
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.fail(f"{fn.__name__} から {type(e).__name__} が抜けた (hook は検証をスキップする)")

    def assertNoBigParse(self):
        self.assertEqual(self.big_parses, [], "上限を超える入力が json.loads に渡された")


class TestReadBoundedText(unittest.TestCase):
    def test_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            ok, over = Path(d) / "ok", Path(d) / "over"
            ok.write_bytes(b"a" * _LIMIT)
            over.write_bytes(b"a" * (_LIMIT + 1))
            try:
                text = cli_config.read_bounded_text(ok)
            except cli_config.FileTooLargeError:
                self.fail("上限ちょうどのファイルは読める")
            self.assertEqual(len(text), _LIMIT)
            with self.assertRaises(cli_config.FileTooLargeError):
                cli_config.read_bounded_text(over)


class TestFirebasercSize(_Base):
    def _rc(self, text: str) -> str:
        (self.root / ".firebaserc").write_text(text, encoding="utf-8")
        return str(self.root)

    def test_over_limit_is_unconfirmed(self):
        for name, text in {"bigint": _BIG_JSON, "padded": _PADDED}.items():
            with self.subTest(name):
                self.assertIsNone(self.call(firebase._read_firebaserc, self._rc(text)))
        self.assertNoBigParse()

    def test_at_limit_is_read(self):
        head = '{"projects": {"default": "p"}}'
        self.assertEqual(
            self.call(firebase._read_firebaserc, self._rc(head + " " * (_LIMIT - len(head)))),
            {"default": "p"},
        )

    def test_over_limit_denies_project_flag_before_parse(self):
        """上限を超える .firebaserc では、現在値を解決せず「確かめられない」で止まる。"""
        self._rc(_BIG_JSON)
        root = str(self.root)
        self.assertFalse(self.call(firebase.firebaserc_reads_like_cli, root))
        self.assertEqual(self.call(firebase._from_local, root), "")
        self.assertNoBigParse()


class TestFirebaseJsonSize(_Base):
    def test_over_limit_blocks_local_resolution(self):
        for name, text in {"bigint": '{"n": ' + "9" * (_LIMIT + 10) + "}", "padded": "{}" + " " * (_LIMIT + 10)}.items():
            with self.subTest(name):
                path = self.root / "firebase.json"
                path.write_text(text, encoding="utf-8")
                self.assertTrue(self.call(firebase._firebase_json_blocks_local, str(path)))
        self.assertNoBigParse()


    def test_empty_file_is_not_blocking(self):
        """空 (0 バイト) の firebase.json は、旧形式キーが無いものとして止めない。"""
        path = self.root / "firebase.json"
        path.write_bytes(b"")
        self.assertFalse(self.call(firebase._firebase_json_blocks_local, str(path)))
        path.write_text("{}", encoding="utf-8")
        self.assertFalse(self.call(firebase._firebase_json_blocks_local, str(path)))
        path.write_text('{"firebase": "x"}', encoding="utf-8")
        self.assertTrue(self.call(firebase._firebase_json_blocks_local, str(path)))


class TestConfigstoreSize(_Base):
    def test_over_limit_is_not_resolved(self):
        store = self.root / "configstore" / "firebase-tools.json"
        store.parent.mkdir()
        proj = str(self.root / "proj")
        for name, text in {
            "bigint": '{"activeProjects": {"%s": "x"}, "n": ' % proj + "9" * (_LIMIT + 10) + "}",
            "padded": json.dumps({"activeProjects": {proj: "x"}}) + " " * (_LIMIT + 10),
        }.items():
            with self.subTest(name):
                store.write_text(text, encoding="utf-8")
                env = {"XDG_CONFIG_HOME": str(self.root)}
                self.assertEqual(self.call(firebase._from_configstore, proj, env), "")
        self.assertNoBigParse()


class TestCacheStateSize(_Base):
    def test_state_file_over_limit_is_unreadable(self):
        for name, text in {"bigint": '{"epoch": ' + "9" * (_LIMIT + 10) + "}", "padded": '{"epoch": 5}' + " " * (_LIMIT + 10)}.items():
            with self.subTest(name):
                path = self.root / f"{name}.json"
                path.write_text(text, encoding="utf-8")
                self.assertIsNone(self.call(cache.read_state, path))
        self.assertNoBigParse()

    def test_epoch_over_limit_is_zero(self):
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}):
            cache.invalidate("svc")
            path = cache._epoch_path("svc")
            self.assertGreater(cache._read_epoch("svc")[0], 0)
            path.write_text(
                '{"epoch": 7, "at_ns": 1}' + " " * (_LIMIT + 10), encoding="utf-8"
            )
            self.assertEqual(self.call(cache._read_epoch, "svc"), (0, 0))
        self.assertNoBigParse()

    def test_success_entry_over_limit_is_miss(self):
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.root)}):
            cache.set_success("svc", "/p", "exp", 1.0)
            self.assertTrue(cache.get_success("svc", "/p", "exp", 1.0))
            files = [
                p for p in cache._cache_dir().iterdir() if p.name.startswith("svc-")
            ]
            self.assertEqual(len(files), 1)
            files[0].write_text(
                files[0].read_text(encoding="utf-8") + " " * (_LIMIT + 10), encoding="utf-8"
            )
            self.assertFalse(self.call(cache.get_success, "svc", "/p", "exp", 1.0))
        self.assertNoBigParse()


class TestAccountsFileSize(_Base):
    def test_over_limit_denies_before_parse(self):
        project = self.root / "proj"
        (project / ".claude" / "cloud-account-guard").mkdir(parents=True)
        cache_tmp = self.root / "cache_tmp"
        cache_tmp.mkdir()
        isolation = _testutil.start_isolation(self.root / "cliconfig", self.root / "home")
        self.addCleanup(isolation.stop)
        env = mock.patch.dict(
            os.environ, {"CLAUDE_PROJECT_DIR": str(project), "TMPDIR": str(cache_tmp)}
        )
        env.start()
        self.addCleanup(env.stop)
        path = project / ".claude" / "cloud-account-guard" / "accounts.local.json"
        for name, text in {
            "bigint": '{"github": "x", "n": ' + "9" * (_LIMIT + 10) + "}",
            "padded": '{"github": "x"}' + " " * (_LIMIT + 10),
        }.items():
            with self.subTest(name):
                path.write_text(text, encoding="utf-8")
                result = self.call(dispatch, "gh pr create", str(project))
                self.assertIsNotNone(result)
                reason = result["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("大きすぎ", reason)
        self.assertNoBigParse()


if __name__ == "__main__":
    unittest.main()
