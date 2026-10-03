"""テストが作る git repo (push 先の bare repo を含む) と、hook が起動する git で、自動 gc /
maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に tempdir の後始末が走ると、tearDown が
`Directory not empty` で落ちる。この flaky は直接は検出できない (object の hash 次第で偶発的)
ので、原因の側に 2 通りの床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、`init_repo` の
  中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける (テストが helper を使わずに repo を作る場合は、この床の対象外)
- **設定の出どころ別**: 止める経路は env (`GIT_CONFIG_COUNT`) と global の fixture の 2 本で、
  同じ値を持つ。有効値だけを見ると片方が欠けてももう片方が埋めて通ってしまうので、1 本ずつ
  単独で見る

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config --get` は未設定のキーで exit 1 になる
ので、「無い」は例外ではなく値の不一致 (assertion) として出す。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import _testutil
from _testutil import HookTestCase

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
# push の受け側 (`receive-pack`) にも効く設定を足したもの (global の fixture と bare repo 自身の config)
EXPECTED_WITH_RECEIVE = {**EXPECTED, "receive.autogc": "false"}


def configured(cwd: str, key: str, *, scope: str | None = None) -> str | None:
    """`_testutil.git` 経由で git が `cwd` で見ている `key` の値。未設定なら None。

    `scope` に "local" / "global" を渡すと、その config ファイルだけを読む (env で渡した設定を
    含めない)。"global" は `GIT_CONFIG_GLOBAL` が指す file (`HERMETIC_GIT_ENV` の fixture)。
    どちらも読むだけで、書かない。
    """
    option = [f"--{scope}"] if scope else []
    try:
        return _testutil.git(cwd, "config", *option, "--get", key).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def trace_events(path: str) -> list[dict]:
    """`GIT_TRACE2_EVENT` が書いた JSON 行を読む。trace が取れていなければ空。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def command_names(events: list[dict]) -> set[str | None]:
    """trace に載った git プロセスのコマンド名 (`commit` / `receive-pack` など)。"""
    return {e.get("name") for e in events if e.get("event") == "cmd_name"}


def spawned_maintenance(events: list[dict]) -> list[list[str]]:
    """起動された `git maintenance ...` / `git gc ...` の argv。自動 maintenance が走った証拠。"""
    return [
        e["argv"]
        for e in events
        if e.get("event") == "child_start" and (e.get("argv") or [])[1:2] in (["maintenance"], ["gc"])
    ]


class TestHelpersStopBackgroundMaintenance(unittest.TestCase):
    """テストクラスが env を patch していなくても、repo を作るヘルパー自身が止める。

    「patch していない」状態は、`GIT_CONFIG_*` をこのテストの中で明示的に外して作る。
    他のテストの patch 漏れや、開発者の shell が export している値に左右されないため。

    見るのは**起動された git の挙動** (maintenance / gc の子が 0 件) で、ヘルパーが後から問い合わせた
    設定値ではない。`init_repo` の commit だけが env を持たずに起動されても、問い合わせ
    (`_testutil.git` 経由) は env を足し直すので、値を見るテストでは気付けない。
    """

    def test_repo_made_without_any_env_patch(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
                del os.environ[name]
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            _testutil.init_repo(os.path.join(tmp, "repo"))
            events = trace_events(trace)
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        self.assertEqual(spawned_maintenance(events), [])


class TestEachSourceOfTheSettingsOnItsOwn(unittest.TestCase):
    """設定を止める経路 (env の `GIT_CONFIG_COUNT` / global の fixture) を 1 本ずつ単独で見る。

    2 本は同じ値を持つので、有効値だけを見るテストでは、片方の項目が欠けても、もう片方が埋めて
    通ってしまう。env は repo 自身の config より優先され、fixture は `receive-pack` にも届くため、
    どちらも外さない (`_testutil.HERMETIC_GIT_ENV` のコメント)。
    """

    def test_the_env_alone_has_the_four_settings(self):
        env = {**os.environ, **_testutil.HERMETIC_GIT_ENV, "GIT_CONFIG_GLOBAL": os.devnull}
        with tempfile.TemporaryDirectory() as tmp:
            for key, expected in EXPECTED.items():
                with self.subTest(key=key):
                    res = subprocess.run(
                        ["git", "config", "--get", key],
                        cwd=tmp,
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual((res.returncode, res.stdout.strip()), (0, expected))

    def test_the_global_fixture_has_the_five_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            for key, expected in EXPECTED_WITH_RECEIVE.items():
                with self.subTest(key=key):
                    self.assertEqual(configured(tmp, key, scope="global"), expected)


class TestPlainBareOriginStartsNoMaintenance(unittest.TestCase):
    """`init_bare_origin` を通らずに作った bare repo (repo 自身の config に設定が無い) へ push しても、
    受け側 (`receive-pack`) が自動 maintenance を起動しないこと。

    `git push` がローカルの path へ送るとき、受け側は repo 用の env (`GIT_CONFIG_COUNT` など) を
    外されて起動する。env の設定だけだと、`git init --bare` を直接呼んだ bare repo では maintenance が
    起動する (実測)。外されない `GIT_CONFIG_GLOBAL` の fixture が止めていることを、起動された
    子プロセスで見る。
    """

    def test_push_into_a_plain_bare_repo(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            repo = _testutil.init_repo(os.path.join(tmp, "repo"))
            _testutil.git(tmp, "init", "--bare", "-q", "plain.git")
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            _testutil.git(repo, "push", "-q", os.path.join(tmp, "plain.git"), "HEAD:refs/heads/main")
            events = trace_events(trace)
        self.assertIn(
            "receive-pack", command_names(events), "前提: 受け側まで trace に載っている (空の床にしない)"
        )
        self.assertEqual(spawned_maintenance(events), [])


class TestBareOriginKeepsTheSettingsInItsOwnConfig(unittest.TestCase):
    """`init_bare_origin` で作った bare repo は、global の fixture に加えて **repo 自身の config** にも
    設定を持つこと (二重の備え)。

    受け側の `receive-pack` に届くのは global の fixture だけで (上のクラス)、その指定が外れても、
    この helper で作った bare repo は止まるようにしてある。`--local` で config ファイルだけを見る
    (env や global の設定を含めない)。
    """

    def test_bare_origin_has_the_settings_in_its_own_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            bare = _testutil.init_bare_origin(tmp)
            for key, expected in EXPECTED_WITH_RECEIVE.items():
                with self.subTest(key=key):
                    self.assertEqual(configured(bare, key, scope="local"), expected)


class TestHookLaunchedGitInheritsTheSettings(HookTestCase):
    """hook (製品コード) が起動する git にも、同じ設定が届くこと。

    `gitscan._git` は env を渡さず `os.environ` を継承するので、基底クラスが patch した env が
    そのまま見える。ここが外れると hook の git だけ自動 maintenance が復活する。
    """

    def test_git_launched_by_the_hook_sees_the_settings(self):
        for key, expected in EXPECTED.items():
            with self.subTest(key=key):
                res = self.gitscan._git(self.repo, ["config", "--get", key])
                self.assertEqual(
                    (res.returncode, res.stdout.decode().strip()), (0, expected)
                )


if __name__ == "__main__":
    unittest.main()
