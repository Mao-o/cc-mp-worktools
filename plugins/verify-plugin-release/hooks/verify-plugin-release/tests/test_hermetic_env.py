"""テストが作る git repo (push 先の bare repo を含む) と、ゲートが起動する git で、自動 gc /
maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に tempdir の後始末が走ると、tearDown が
`Directory not empty` で落ちる。この flaky は直接は検出できない (object の hash 次第で偶発的)
ので、原因の側に 2 通りの床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、repo を作る helper
  の中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける (テストが helper を使わずに repo を作る場合は、この床の対象外)。helper を迂回した git を
  作るために `GIT_CONFIG_*` を外すときは、開発者の global / system の config も空にする
  (`isolate_git_config`): そこに `maintenance.auto=false` があると、迂回しても起動せず床が黙って通る
- **設定の出どころ別**: 止める経路は env (`GIT_CONFIG_COUNT`) と global の fixture の 2 本で、
  同じ値を持つ。有効値だけを見ると片方が欠けてももう片方が埋めて通ってしまうので、1 本ずつ
  単独で見る。env は定数の中身 (1 項目ずつ) と、helper が実際に渡す経路 (fixture を外して挙動で)、
  fixture は内容の完全一致 (5 設定だけを持つこと)

ゲートが起動する git のうち、in-process で動かす側 (`GateTest` / `ReadyRefsTest`) はここの
`TestGateLaunchedGitInheritsTheSettings`、hook を subprocess で起動する側は
`test_main.HookLaunchEnvTest` が見る。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config` は未設定 / 読めないとき exit 1 などになる
ので、`check=True` を使わず、「無い」は例外ではなく値の不一致 (assertion) として出す。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil
from _testutil import HermeticGitTestCase

import gate
from config import Config
from runner import Deadline
from runner import git as gate_git

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
# push の受け側 (`receive-pack`) にも効く設定を足したもの (global の fixture と bare repo 自身の config)
EXPECTED_WITH_RECEIVE = {**EXPECTED, "receive.autogc": "false"}


def configured(repo: Path, key: str, *, local: bool = False) -> str | None:
    """`_testutil.sh` 経由で git が repo で見ている `key` の値。未設定なら None。

    `local=True` なら repo 自身の config ファイルだけを読む (env や global の設定を含めない)。
    """
    scope = ["--local"] if local else []
    try:
        return _testutil.sh(repo, "config", *scope, "--get", key).strip()
    except subprocess.CalledProcessError:
        return None


def empty_config_file(directory: str) -> str:
    """空の config file を作ってそのパスを返す (`GIT_CONFIG_GLOBAL` を「設定なし」に向けるため)。

    `os.devnull` ではなく実在する空 file にするのは、この suite が Windows の CI でも走るため
    (global の config を空 file で固定する形は、この suite が以前から使っている)。
    """
    path = os.path.join(directory, "empty.gitconfig")
    with open(path, "w", encoding="utf-8"):
        pass
    return path


def empty_global_config(home: str) -> None:
    """git が読む global / system の config を空にする (`HOME` / `XDG_CONFIG_HOME` を空の `home` に、
    system を無効に)。

    `mock.patch.dict(os.environ)` の中で呼ぶこと (環境を戻すため)。`GIT_CONFIG_*` を外すだけだと、
    helper を迂回した git (や、env の patch が外れたゲートの git) は開発者の `~/.gitconfig` と
    system config を読む。そこに `maintenance.auto=false` (2.55 では `gc.auto=0` でも同じ) があると、
    迂回しても自動 maintenance が起動せず、有効値も揃って、床が黙って通ってしまう。
    """
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_NOSYSTEM": "1"})


def isolate_git_config(home: str) -> None:
    """`GIT_CONFIG_*` を外し、global / system の config も空にする (「patch していない」状態を作る)。"""
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
        del os.environ[name]
    empty_global_config(home)


def trace_events(path: str) -> list[dict]:
    """`GIT_TRACE2_EVENT` が書いた JSON 行を読む。trace が取れていなければ空。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def command_names(events: list[dict]) -> set[str | None]:
    """trace に載った git プロセスのコマンド名 (`commit` / `receive-pack` / `fetch` など)。"""
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

    「patch していない」状態は、`GIT_CONFIG_*` を外し、global / system の config も空にして作る
    (`isolate_git_config`)。他のテストの patch 漏れや、開発者の shell / `~/.gitconfig` の値に
    左右されないため。

    見るのは**起動された git の挙動** (maintenance / gc の子が 0 件) で、ヘルパーが後から問い合わせた
    設定値ではない。`make_marketplace` の commit だけが env を持たずに起動されても、問い合わせ
    (`_testutil.sh` 経由) は env を足し直すので、値を見るテストでは気付けない。
    """

    def _spawned_while_making_a_marketplace(self, *, without_the_global_fixture: bool) -> list[list[str]]:
        """`make_marketplace` の間に起動された maintenance / gc の argv。

        `without_the_global_fixture` なら、この呼び出しの間だけ `HERMETIC_GIT_ENV` の
        `GIT_CONFIG_GLOBAL` を空の config file に上書きする: fixture を外して、helper が渡す
        `GIT_CONFIG_COUNT` だけで止まるかを見るため。

        上書きが helper の git に届いたことも前提として確かめる。helper が `HERMETIC_GIT_ENV` を
        呼び出しのたびに読まない形 (初回に固めたコピーを使うなど) に変わると、上書きが届かず
        env 経路の床が黙って空になるため。`all` ではなく `any` で見るのは、commit だけが helper を
        迂回する変異でも前提は満たしたまま、maintenance の起動の assertion で落とすため。
        """
        passed: list[dict] = []
        real_run = subprocess.run

        def spy(argv, *args, **kwargs):
            if argv[:1] == ["git"]:
                passed.append(dict(kwargs.get("env") or os.environ))
            return real_run(argv, *args, **kwargs)

        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            overrides = {"GIT_CONFIG_GLOBAL": empty_config_file(tmp)} if without_the_global_fixture else {}
            with mock.patch.dict(_testutil.HERMETIC_GIT_ENV, overrides):
                trace = os.path.join(tmp, "trace2.jsonl")
                os.environ["GIT_TRACE2_EVENT"] = trace
                with mock.patch.object(subprocess, "run", side_effect=spy):
                    _testutil.make_marketplace(Path(tmp) / "repo", ["alpha"])
                events = trace_events(trace)
                want = dict(_testutil.HERMETIC_GIT_ENV)
        self.assertTrue(
            any(all(env.get(k) == v for k, v in want.items()) for env in passed),
            "前提: 上書きした HERMETIC_GIT_ENV が helper の git に届いている",
        )
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        return spawned_maintenance(events)

    def test_repo_made_without_any_env_patch(self):
        self.assertEqual(self._spawned_while_making_a_marketplace(without_the_global_fixture=False), [])

    def test_the_env_path_alone_stops_it(self):
        """global の fixture を外し、helper が渡す `GIT_CONFIG_COUNT` だけで止まること。

        fixture が同じ設定を持つので、上のテストだけでは、helper が `GIT_CONFIG_COUNT` を渡し損ねても
        fixture が埋めて通ってしまう。
        """
        self.assertEqual(self._spawned_while_making_a_marketplace(without_the_global_fixture=True), [])


class TestEachSourceOfTheSettingsOnItsOwn(unittest.TestCase):
    """設定を止める経路 (env の `GIT_CONFIG_COUNT` / global の fixture) を 1 本ずつ単独で見る。

    2 本は同じ値を持つので、有効値だけを見るテストでは、片方の項目が欠けても、もう片方が埋めて
    通ってしまう。env は repo 自身の config より優先され、fixture は `receive-pack` にも届くため、
    どちらも外さない (`_testutil.HERMETIC_GIT_ENV` のコメント)。helper が env を実際に渡す経路は、
    上のクラスの `test_the_env_path_alone_stops_it` が挙動で見る。ここは定数の中身を見る。
    """

    def test_the_env_alone_has_the_four_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, **_testutil.HERMETIC_GIT_ENV, "GIT_CONFIG_GLOBAL": empty_config_file(tmp)}
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

    def test_the_global_fixture_holds_only_the_five_settings(self):
        """fixture が自動 maintenance を止める 5 設定だけを持つこと (完全一致)。

        キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
        将来足された `diff.noprefix` など、テストの前提を変えるもの) が増えても通る。fixture は
        git が読む global なので、増えると製品の git を含む全テストに効く。`git config --list` は
        キーを小文字で出す。`GIT_CONFIG_GLOBAL` が外れたときに開発者の `~/.gitconfig` を読んで通らない
        よう、`HOME` などは空にしてから見る。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            empty_global_config(tmp)
            res = subprocess.run(
                ["git", "config", "--global", "--list"],
                cwd=tmp,
                env={**os.environ, **_testutil.HERMETIC_GIT_ENV},
                capture_output=True,
                text=True,
            )
        expected = sorted(f"{key.lower()}={value}" for key, value in EXPECTED_WITH_RECEIVE.items())
        self.assertEqual((res.returncode, sorted(res.stdout.splitlines())), (0, expected))


class TestPlainBareOriginStartsNoMaintenance(unittest.TestCase):
    """`init_bare_origin` を通らずに作った bare repo (repo 自身の config に設定が無い) へ push しても、
    受け側 (`receive-pack`) が自動 maintenance を起動しないこと。

    `git push` がローカルの path へ送るとき、受け側は repo 用の env (`GIT_CONFIG_COUNT` など) を
    外されて起動する。env の設定だけだと、`git init --bare` を直接呼んだ bare repo では maintenance が
    起動する (実測)。外されない `GIT_CONFIG_GLOBAL` の fixture が止めていることを、起動された
    子プロセスで見る。helper を迂回した git が開発者の `~/.gitconfig` を読まないよう、global / system
    の config は空にする (`isolate_git_config`)。
    """

    def test_push_into_a_plain_bare_repo(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            root = _testutil.make_marketplace(Path(tmp) / "repo", ["alpha"])
            plain = Path(tmp) / "plain.git"
            _testutil.sh(Path(tmp), "init", "--bare", "-q", plain.name)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            _testutil.sh(root, "push", "-q", str(plain), "HEAD:refs/heads/main")
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
            bare = _testutil.init_bare_origin(Path(tmp))
            for key, expected in EXPECTED_WITH_RECEIVE.items():
                with self.subTest(key=key):
                    self.assertEqual(configured(bare, key, local=True), expected)


class TestGateLaunchedGitInheritsTheSettings(HermeticGitTestCase):
    """ゲート (製品コード) を in-process で動かすテストで、ゲートが起動する git にも同じ設定が届くこと。

    `runner.run` は env を渡さず `os.environ` を継ぐので、基底クラスが patch した env がそのまま
    見える。ここが外れると、ゲートが起動する git だけ自動 maintenance が復活する。基底クラスの patch
    から `HERMETIC_GIT_ENV` が外れたとき、ゲートの git が開発者の global / system の config から
    設定を拾って通らないよう、`HOME` などは空にしてから見る (`empty_global_config`。patch が効いて
    いれば `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_COUNT` が優先されるので結果は変わらない)。
    """

    def test_git_launched_by_the_gate_sees_the_settings(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            empty_global_config(tmp)
            for key, expected in EXPECTED.items():
                with self.subTest(key=key):
                    res = gate_git(["config", "--get", key], Path(tmp), Deadline(30))
                    self.assertEqual((res.returncode, res.stdout.strip()), (0, expected))

    def test_fetch_started_by_the_gate_starts_no_maintenance(self):
        """ゲートが自分で起動する git のうち、自動 maintenance の起点になりうるのは `fetch` だけ。

        上のテストは設定値を問い合わせるだけなので、ゲートの `fetch` が起動する子を `GIT_TRACE2_EVENT`
        で数える (挙動の床)。この suite の他のテストは設定ファイルで `fetch` を無効にしてゲートを
        動かすので、ゲートの `fetch` を実際に走らせるのはここだけ。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            empty_global_config(tmp)
            root = _testutil.make_marketplace(Path(tmp) / "repo", ["alpha"])
            origin = _testutil.init_bare_origin(Path(tmp))
            _testutil.sh(root, "remote", "add", "origin", str(origin))
            _testutil.sh(root, "push", "-q", "origin", "main")
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            base = gate.resolve_base(root, "main", Config(fetch=True), Deadline(60), gate.Report())
            events = trace_events(trace)
        self.assertEqual(base, "origin/main")
        self.assertIn("fetch", command_names(events), "前提: ゲートが fetch を起動している (空の床にしない)")
        self.assertEqual(spawned_maintenance(events), [])


if __name__ == "__main__":
    unittest.main()
