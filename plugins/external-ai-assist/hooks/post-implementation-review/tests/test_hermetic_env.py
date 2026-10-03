"""テストが作る git repo (push 先の bare repo を含む) と、hook が起動する git で、自動 gc /
maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に tempdir の後始末が走ると、tearDown が
`Directory not empty` で落ちる。この flaky は直接は検出できない (object の hash 次第で偶発的)
ので、原因の側に 2 通りの床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、`init_repo` の
  中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける (テストが helper を使わずに repo を作る場合は、この床の対象外)。helper を迂回した git を
  作るために `GIT_CONFIG_*` を外すときは、開発者の global の config を空に、system の config を
  目印の file に向ける (`isolate_git_config`): そこに `maintenance.auto=false` があると、迂回しても
  起動せず床が黙って通る。**床の側では `GIT_CONFIG_NOSYSTEM` を立てない**: 立てると、helper や
  基底クラスがそれを渡し損ねても、床が埋めて通ってしまう (system の目印は `GIT_CONFIG_NOSYSTEM`
  が効いていれば読まれないので、効いているかを値で見られる)。外側の env には、止めない側の値
  (`OUTER_NON_STOPPING_ENV`) を置く: helper が env を混ぜる向きを逆にして外側の値を勝たせる変異も、
  止まっていないことで拾える。「起動が 0 件」は検出器が何も拾えなくても成り立つので、何も止めない
  commit で起動が見えること (`TestTheDetectorSeesMaintenance`) を先に確かめる
- **設定の出どころ別**: 止める経路は env (`GIT_CONFIG_COUNT`) と global の fixture の 2 本で、
  同じ値を持つ。有効値だけを見ると片方が欠けてももう片方が埋めて通ってしまうので、1 本ずつ
  単独で見る。env は定数の中身 (1 項目ずつ) と、helper が実際に渡す経路 (fixture を外して挙動で)、
  fixture は内容の完全一致 (5 設定だけを持つこと)

hook (製品コード) の関数が起動する git は `_ProductGitChecks` が、基底クラスが張った env のまま
見る。`HERMETIC_GIT_ENV` を自前で張る基底クラスは 3 つ (`HookTestCase` / `GitScanTestCase` /
`ReviewSetTestCase`) あり、床は 3 つとも見る (基底クラスごとの具体クラス)。見るのは、4 設定が
`GIT_CONFIG_COUNT` から見えること (repo 自身の config に逆の値 `OPPOSITE` を置いて見る。env は repo 自身の
config に勝ち、fixture は負けるので、止める側の値は `GIT_CONFIG_COUNT` が届いているときだけ見える)、
global として fixture を読むこと (`GIT_CONFIG_GLOBAL` が届いている)、system の目印を読まないこと
(`GIT_CONFIG_NOSYSTEM` が届いている)。もう 1 つ、hook の未追跡の判定が、開発者の global の ignore を
読まないこと。`GIT_CONFIG_GLOBAL` を指しても git の既定の除外ファイルは外れないので、
`HERMETIC_GIT_ENV` が `XDG_CONFIG_HOME` を空の dir に向けている (定数の床と、hook が起動する git の床の
両方で、外側に置いた ignore が効かないことを見る。ignore は `$XDG_CONFIG_HOME/git/ignore` と、
`XDG_CONFIG_HOME` が空のときに git が読む `$HOME/.config/git/ignore` の両方に置く)。この床も、外側の
`GIT_CONFIG_*` を外した後に止めない側の値を置いてから基底クラスに当てさせ、床の env だけではどの経路も
止める側にならないこと (`test_the_floor_alone_stops_nothing`) を見る。

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
from unittest import mock

import _testutil
from _testutil import HookTestCase
from test_gitscan import GitScanTestCase
from test_review_set import ReviewSetTestCase

import gitscan

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
# push の受け側 (`receive-pack`) にも効く設定を足したもの (global の fixture と bare repo 自身の config)
EXPECTED_WITH_RECEIVE = {**EXPECTED, "receive.autogc": "false"}
# repo 自身の config に置く、止めない側の値。env (`GIT_CONFIG_COUNT`) は repo 自身の config に勝ち、
# global の fixture は負けるので、止める側の値が見えるのは `GIT_CONFIG_COUNT` が届いているときだけ
OPPOSITE = {
    "maintenance.auto": "true",
    "maintenance.autoDetach": "true",
    "gc.auto": "6700",
    "gc.autoDetach": "true",
}


def configured(repo: str, key: str, *, local: bool = False) -> str | None:
    """`_testutil.git` 経由で git が repo で見ている `key` の値。未設定なら None。

    `local=True` なら repo 自身の config ファイルだけを読む (env や global の設定を含めない)。
    """
    scope = ["--local"] if local else []
    try:
        return _testutil.git(repo, "config", *scope, "--get", key).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def empty_global_config(home: str) -> None:
    """git が読む global の config を空にし、system の config を目印の file に向ける。

    global 側は `HOME` / `XDG_CONFIG_HOME` を空の `home` に向ける。system 側は `GIT_CONFIG_SYSTEM` で
    `home` の中の目印 file (`hermetic.system = read` だけを持つ) に向ける: 開発者の system config は
    読まれず、しかも `GIT_CONFIG_NOSYSTEM` が効いていれば目印も読まれないので、効いているかを値
    (`system_marker`) で見られる。**`GIT_CONFIG_NOSYSTEM` は立てない。** 床の側が立てると、helper や
    基底クラスがそれを渡し損ねても、床が埋めて通ってしまう。

    `mock.patch.dict(os.environ)` の中で呼ぶこと (環境を戻すため)。`GIT_CONFIG_*` を外すだけだと、
    helper を迂回した git (や、`HERMETIC_GIT_ENV` が外れた hook の git) は開発者の `~/.gitconfig` と
    system config を読む。そこに自動 maintenance を止める設定 (`maintenance.auto=false` など) があると、
    迂回しても自動 maintenance が起動せず、有効値も揃って、床が黙って通ってしまう。
    """
    system = os.path.join(home, "system.gitconfig")
    with open(system, "w", encoding="utf-8") as f:
        f.write("[hermetic]\n\tsystem = read\n")
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_SYSTEM": system})


# 外側の env (開発者の shell など) が持ちうる「止めない側」の git の設定。global は空、env の設定は
# `maintenance.auto=true`。`isolate_git_config` が置き、helper や定数が env を混ぜる向きを床が見られるように
# する (次の項)。
OUTER_NON_STOPPING_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "maintenance.auto",
    "GIT_CONFIG_VALUE_0": "true",
}


def isolate_git_config(home: str) -> None:
    """`GIT_CONFIG_*` を外し、global の config も空に、system の config も目印の file に向け、そのうえで
    外側の「止めない側」の env (`OUTER_NON_STOPPING_ENV`) を置く (「patch していない」状態を作る)。
    `GIT_CONFIG_NOSYSTEM` は立てない。

    外側を全部外しただけだと、helper が env を混ぜる向きを逆にして (`{**HERMETIC_GIT_ENV, **os.environ}`)
    外側の値を勝たせても、床の側に外側の値が無いので気付けない。止めない側の値を置いておけば、helper の
    値が勝っていること (止まっていること) が、向きを逆にした変異で崩れる。
    """
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
        del os.environ[name]
    empty_global_config(home)
    os.environ.update(OUTER_NON_STOPPING_ENV)


IGNORED_NAME = "note.hermetic-ignored"


def plant_default_excludes(xdg_config_home: str) -> None:
    """開発者の global の ignore に見立てた file を、git の既定の除外ファイルの場所
    (`xdg_config_home/git/ignore`) に置く。`IGNORED_NAME` を除外する。"""
    path = os.path.join(xdg_config_home, "git", "ignore")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("*.hermetic-ignored\n")


def untracked_names(repo: str, env: dict[str, str]) -> list[str]:
    """`env` で起動した git が、`repo` で「未追跡で、ignore されていない」と見る名前 (ソート済み)。

    git の失敗は例外にする (`check=True`)。失敗を空の一覧として返すと、「ignore が効いて見えない」
    と区別がつかず、確認が黙って空になる。
    """
    res = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(res.stdout.split())


def query_git(cwd: str, env: dict[str, str], *args: str) -> tuple[int, str]:
    """`env` で起動した `git <args>` の (終了コード, 標準出力)。

    `git config --get` は未設定のとき exit 1 になるので、`check=True` は使わない (「無い」を例外では
    なく値の不一致として出す)。
    """
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    return res.returncode, res.stdout.strip()


def system_marker(cwd: str, env: dict[str, str]) -> tuple[int, str]:
    """`env` で起動した git が、`empty_global_config` の system の目印を読むか。

    読めば `(0, "read")`。読まなければ (`GIT_CONFIG_NOSYSTEM` が効いている) `(1, "")`: 未設定のとき
    `git config --get` は exit 1 になるので、`check=True` は使わない。
    """
    res = subprocess.run(
        ["git", "config", "--get", "hermetic.system"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    return res.returncode, res.stdout.strip()


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

    「patch していない」状態は、`GIT_CONFIG_*` を外し、global の config を空に、system の config を
    目印の file に向けて作る (`isolate_git_config`。`GIT_CONFIG_NOSYSTEM` は立てない)。他のテストの
    patch 漏れや、開発者の shell / `~/.gitconfig` の値に左右されないため。床が `GIT_CONFIG_NOSYSTEM`
    を立てないので、helper が渡す env から `GIT_CONFIG_NOSYSTEM` が抜けることも、下の前提
    (`HERMETIC_GIT_ENV` の全項目が helper の git に届いている) で拾える。外側には止めない側の値
    (`OUTER_NON_STOPPING_ENV`) を置くので、helper が env を混ぜる向きを逆にして外側の値を勝たせても、
    同じ前提で拾える。

    見るのは**起動された git の挙動** (maintenance / gc の子が 0 件) で、ヘルパーが後から問い合わせた
    設定値ではない。`init_repo` の commit だけが env を持たずに起動されても、問い合わせ
    (`_testutil.git` 経由) は env を足し直すので、値を見るテストでは気付けない。
    """

    def _spawned_by_init_repo(self, hermetic_overrides: dict[str, str]) -> list[list[str]]:
        """`init_repo` の間に起動された maintenance / gc の argv。

        `hermetic_overrides` は、この呼び出しの間だけ `HERMETIC_GIT_ENV` に上書きする値。helper が
        渡す設定の一部を外して、残りの経路だけで止まるかを見るために使う。

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

        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            _testutil.HERMETIC_GIT_ENV, hermetic_overrides
        ):
            isolate_git_config(tmp)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            with mock.patch.object(subprocess, "run", side_effect=spy):
                _testutil.init_repo(os.path.join(tmp, "repo"))
            events = trace_events(trace)
            want = dict(_testutil.HERMETIC_GIT_ENV)
        self.assertTrue(
            any(all(env.get(k) == v for k, v in want.items()) for env in passed),
            "前提: 上書きした HERMETIC_GIT_ENV が helper の git に届いている",
        )
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        return spawned_maintenance(events)

    def test_repo_made_without_any_env_patch(self):
        self.assertEqual(self._spawned_by_init_repo({}), [])

    def test_the_env_path_alone_stops_it(self):
        """global の fixture を外し、helper が渡す `GIT_CONFIG_COUNT` だけで止まること。

        fixture が同じ設定を持つので、上のテストだけでは、helper が `GIT_CONFIG_COUNT` を渡し損ねても
        fixture が埋めて通ってしまう。
        """
        self.assertEqual(self._spawned_by_init_repo({"GIT_CONFIG_GLOBAL": os.devnull}), [])


# 起動した maintenance を背景へ切り離さないだけの設定 (起動そのものは止めない)
FOREGROUND_ONLY_SETTINGS = (
    ("maintenance.autoDetach", "false"),
    ("gc.autoDetach", "false"),
)


class TestTheDetectorSeesMaintenance(unittest.TestCase):
    """陽性対照: 何も止めない commit では、`spawned_maintenance` が maintenance の起動を拾えること。

    上の床は「起動が 0 件」を見るので、検出器 (`trace_events` / `spawned_maintenance`) が何も拾えない
    状態でも通る (前提で見ているのは、`command_names` が `commit` を拾えることだけ)。止める設定を
    渡さない commit で、検出器が起動を 1 件以上拾うことを先に確かめる。

    `maintenance.autoDetach=false` / `gc.autoDetach=false` だけは渡す: 起動した maintenance が背景へ
    切り離されると、tempdir の後始末と競合して、この対照自身が `Directory not empty` で落ちうる
    (起動は止めず、前景で終わらせるだけ)。止める設定を足す helper (`_testutil.git`) は使わず、
    `isolate_git_config` で隔離した env のまま git を直接起動する。
    """

    def test_a_commit_that_stops_nothing_spawns_maintenance(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            os.environ.update(_testutil.git_config_env(FOREGROUND_ONLY_SETTINGS))
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            for args in (
                ["init", "-q"],
                ["config", "user.email", "test@example.com"],
                ["config", "user.name", "test"],
            ):
                subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
            _testutil.write(repo, "seed.txt", "alpha\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, capture_output=True, check=True)
            events = trace_events(trace)
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        self.assertNotEqual(
            spawned_maintenance(events), [], "何も止めない commit で、maintenance の起動が見えない"
        )


class TestEachSourceOfTheSettingsOnItsOwn(unittest.TestCase):
    """設定を止める経路 (env の `GIT_CONFIG_COUNT` / global の fixture) を 1 本ずつ単独で見る。

    2 本は同じ値を持つので、有効値だけを見るテストでは、片方の項目が欠けても、もう片方が埋めて
    通ってしまう。env は repo 自身の config より優先され、fixture は `receive-pack` にも届くため、
    どちらも外さない (`_testutil.HERMETIC_GIT_ENV` のコメント)。helper が env を実際に渡す経路は、
    上のクラスの `test_the_env_path_alone_stops_it` が挙動で見る。ここは定数の中身を見る (4 設定、
    global の fixture、system の config を読ませないこと)。
    """

    def test_the_env_alone_has_the_four_settings(self):
        """定数の `GIT_CONFIG_COUNT` だけで 4 設定が見えること (global は空の file に向ける)。

        外側の env の `GIT_CONFIG_*` (`GIT_CONFIG_PARAMETERS` など) が定数の欠けを埋めないよう、先に外す
        (`isolate_git_config`)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            env = {**os.environ, **_testutil.HERMETIC_GIT_ENV, "GIT_CONFIG_GLOBAL": os.devnull}
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
        よう、`HOME` などは空にしてから見る。外側の env の `GIT_CONFIG_*` が、定数の当て損ねを埋めない
        よう先に外す (`isolate_git_config`)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            res = subprocess.run(
                ["git", "config", "--global", "--list"],
                cwd=tmp,
                env={**os.environ, **_testutil.HERMETIC_GIT_ENV},
                capture_output=True,
                text=True,
            )
        expected = sorted(f"{key.lower()}={value}" for key, value in EXPECTED_WITH_RECEIVE.items())
        self.assertEqual((res.returncode, sorted(res.stdout.splitlines())), (0, expected))

    def test_the_constant_does_not_read_the_system_config(self):
        """`HERMETIC_GIT_ENV` が system の config を読ませないこと (`GIT_CONFIG_NOSYSTEM`)。

        system は目印の file に向けてあるので (`empty_global_config`)、定数が `GIT_CONFIG_NOSYSTEM` を
        持たなければ目印が読まれる (`hermetic.system` が `read` で見える)。目印が読めること自体は
        `TestTheSystemMarkerIsLive` が見る。外側の env に `GIT_CONFIG_NOSYSTEM` があると定数の当て損ねを
        埋めてしまうので、先に外す (`isolate_git_config`)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            result = system_marker(tmp, {**os.environ, **_testutil.HERMETIC_GIT_ENV})
        self.assertEqual(result, (1, ""))

    def test_the_constant_does_not_read_the_default_excludes_file(self):
        """`HERMETIC_GIT_ENV` が、開発者の global の ignore (`$XDG_CONFIG_HOME/git/ignore`) を読ませない
        こと。

        `GIT_CONFIG_GLOBAL` を指しても、git の既定の除外ファイルは外れない
        (`_testutil.HERMETIC_XDG_CONFIG_HOME` のコメント)。外側の env の `XDG_CONFIG_HOME` が指す先に
        ignore を置き (`plant_default_excludes`)、置いた ignore が外側の env では効くこと (前提: 空の床に
        しない) と、定数を重ねた env では効かないこと (未追跡の名前が見える) を、続けて見る。

        ignore は `HOME` の側 (`$HOME/.config/git/ignore`) にも置く。`XDG_CONFIG_HOME` が空なら git は
        そちらを読むので、定数の `XDG_CONFIG_HOME` が空になる変異も、HOME の側の ignore が効いて落ちる
        (そちらが効くことも前提として見る)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            plant_default_excludes(tmp)
            # XDG_CONFIG_HOME が空なら git は $HOME/.config/git/ignore を読む。定数の XDG が空になっても拾う
            plant_default_excludes(os.path.join(tmp, ".config"))
            repo = _testutil.init_repo(os.path.join(tmp, "repo"))
            _testutil.write(repo, IGNORED_NAME, "x\n")
            outer_view = untracked_names(repo, dict(os.environ))
            fallback_view = untracked_names(repo, {**os.environ, "XDG_CONFIG_HOME": ""})
            hermetic_view = untracked_names(repo, {**os.environ, **_testutil.HERMETIC_GIT_ENV})
        self.assertEqual((outer_view, fallback_view, hermetic_view), ([], [], [IGNORED_NAME]))


class TestTheSystemMarkerIsLive(unittest.TestCase):
    """system の目印が、`GIT_CONFIG_NOSYSTEM` を立てなければ読まれ、立てれば読まれないこと。

    「目印が読まれない (`hermetic.system` が未設定)」という確認は、目印がそもそも読めない状態でも
    成り立つ。`empty_global_config` が目印を置き損ねる / `GIT_CONFIG_SYSTEM` を向け損ねる / 床の側で
    `GIT_CONFIG_NOSYSTEM` を立てる、のどれでも、`GIT_CONFIG_NOSYSTEM` が届いているかを見る床が黙って
    空になるので、道具の側を先に確かめる。
    """

    def test_the_marker_is_read_unless_nosystem_is_set(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            readable = system_marker(tmp, dict(os.environ))
            skipped = system_marker(tmp, {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertEqual((readable, skipped), ((0, "read"), (1, "")))


class TestPlainBareOriginStartsNoMaintenance(unittest.TestCase):
    """`init_bare_origin` を通らずに作った bare repo (repo 自身の config に設定が無い) へ push しても、
    受け側 (`receive-pack`) が自動 maintenance を起動しないこと。

    `git push` がローカルの path へ送るとき、受け側は repo 用の env (`GIT_CONFIG_COUNT` など) を
    外されて起動する。env の設定だけだと、`git init --bare` を直接呼んだ bare repo では maintenance が
    起動する (実測)。外されない `GIT_CONFIG_GLOBAL` の fixture が止めていることを、起動された
    子プロセスで見る。helper を迂回した git が開発者の `~/.gitconfig` を読まないよう、global の
    config は空に、system の config は目印の file に向ける (`isolate_git_config`)。
    """

    def test_push_into_a_plain_bare_repo(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
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
                    self.assertEqual(configured(bare, key, local=True), expected)


class _ProductGitChecks:
    """hook (製品コード) の関数が起動する git にも、同じ設定が届き、global は fixture だけ、system は
    読まないこと。具体クラス (このファイルの末尾の 3 つ) が基底クラスを 1 つずつ組み、その基底クラスが
    張った env のまま見る。

    `gitscan._git` は env を渡さず `os.environ` を継承するので、基底クラスが patch した env が
    そのまま見える。ここが外れると、hook の git だけ、開発者の global / system の config と自動
    maintenance の既定に戻る。hook を起動するテストクラス (`HookTestCase`) だけでなく、hook の関数を
    直接呼ぶテストクラス (`GitScanTestCase` / `ReviewSetTestCase`) も同じ定数を自前で張るので、床は
    3 つとも見る。1 つしか見ていないと、残りの基底クラスが env を張らなくなっても、空の HOME では
    何も落ちない (`_testutil.git` のコメントを読んで「クラス側の patch は要らない」と消す退行がありうる)。
    基底クラスの patch から外れたものが、開発者の global / system の config から拾われて通らないよう、
    `HOME` などは空にしてから見る (`empty_global_config`)。見るものは 3 つ (それぞれ別のテスト):

    - 4 設定が見える (`--get`)。repo 自身の config に逆の値 (`OPPOSITE`) を書いてから見る。env
      (`GIT_CONFIG_COUNT`) は repo 自身の config に勝ち、fixture は負けるので、止める側の値は
      `GIT_CONFIG_COUNT` が届いているときだけ見える (逆の値が無いと、fixture が埋めて通る)
    - global として fixture を読む (`config --global --list` が 5 設定の完全一致)。基底クラスが
      `GIT_CONFIG_GLOBAL` を張っていること
    - system の config を読まない。`GIT_CONFIG_SYSTEM` に目印の file を指しておき、
      `GIT_CONFIG_NOSYSTEM` が効いていれば読まれない (`hermetic.system` が未設定のまま)

    床自身が `GIT_CONFIG_NOSYSTEM` を立てると、基底クラスの当て損ねを床が埋めてしまい、3 つ目が
    見えなくなる。そのため `empty_global_config` は `HOME` / `XDG_CONFIG_HOME` を空にして system の
    目印を指すだけで、`GIT_CONFIG_NOSYSTEM` / `GIT_CONFIG_GLOBAL` は基底クラスが張ったものをそのまま
    見る。`GIT_CONFIG_GLOBAL` が外れたときは、開発者の `~/.gitconfig` を読んで通ってしまわず、
    `--global --list` の不一致として落ちる。床がそうした止める側の値を持たないこと自体も、
    `test_the_floor_alone_stops_nothing` で見る。

    外側の env (開発者の shell や、mutation を流す道具) に `GIT_CONFIG_*` があると、基底クラスが
    当て損ねても、その値が残って確認が素通りする。そのため `setUp` で、外側の `GIT_CONFIG_*` を
    先に外してから基底クラスの patch を張る。外した後には、helper / 定数の床 (`isolate_git_config`) と
    同じく止めない側の値 (`OUTER_NON_STOPPING_ENV`) を置く: 外側を全部外しただけだと、基底クラスが env を
    混ぜる向きを逆にして外側の値を勝たせても、外側の値が無いので結果が変わらない。

    もう 1 つ、hook の未追跡の判定 (`gitscan.untracked_among`) が、開発者の global の ignore を
    読まないこと。`GIT_CONFIG_GLOBAL` を指しても既定の除外ファイルは外れないので、基底クラスが
    `XDG_CONFIG_HOME` を空の dir に向けている (`HERMETIC_GIT_ENV`)。ignore は、外側の env の
    `XDG_CONFIG_HOME` が指す先 (`$XDG_CONFIG_HOME/git/ignore`) と、`XDG_CONFIG_HOME` が空のときに git が
    読む `$HOME/.config/git/ignore` の両方に置き (外側の `HOME` もそこに向ける)、そのうえで基底クラスの
    patch を張って、hook の git でその ignore が効かないことを見る。定数の `XDG_CONFIG_HOME` が空になる
    変異は、HOME の側の ignore が効いて落ちる。
    """

    def setUp(self) -> None:
        outer = mock.patch.dict(os.environ)
        outer.start()
        self.addCleanup(outer.stop)
        for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
            del os.environ[name]
        # 基底クラスが張る値が、外側の止めない側の値に勝つことも見る (混ぜる向きの逆転を拾う)
        os.environ.update(OUTER_NON_STOPPING_ENV)
        planted = tempfile.TemporaryDirectory()
        self.addCleanup(planted.cleanup)
        self.planted_xdg_config_home = planted.name
        plant_default_excludes(self.planted_xdg_config_home)
        # XDG_CONFIG_HOME が空なら git は $HOME/.config/git/ignore を読む。HOME もそこに向けて置く
        plant_default_excludes(os.path.join(self.planted_xdg_config_home, ".config"))
        os.environ["XDG_CONFIG_HOME"] = self.planted_xdg_config_home
        os.environ["HOME"] = self.planted_xdg_config_home
        # 当てる側が当てる前の env。床が止める側の値を持たないことの確認に使う
        self.floor_env = dict(os.environ)
        super().setUp()

    def _hook_git(self, *args: str) -> tuple[int, str]:
        """hook の `gitscan._git` で `git <args>` を起動した (終了コード, 標準出力)。

        `HOME` などは空に、system の config は目印の file に向けてから起動する。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            empty_global_config(tmp)
            res = gitscan._git(self.repo, list(args))
        return res.returncode, res.stdout.decode().strip()

    def test_the_floor_alone_stops_nothing(self):
        """床の env だけで起動した git では、どの経路も止める側にならないこと。

        当てる側 (基底クラス) が当てる前の env (`self.floor_env`) に、`_hook_git` と同じ隔離
        (`empty_global_config`) を重ねて git を起動する。repo 自身の config に書いた逆の値 (`OPPOSITE`)
        がそのまま見え、global は空で、system の目印は読める (床が `GIT_CONFIG_NOSYSTEM` を立てていない)
        こと。床が止める側の値 (`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の
        `GIT_CONFIG_COUNT`) を持つ形に戻ると、当てる側の当て損ねを床が埋めて、他の床が黙って通る。
        """
        for key, value in OPPOSITE.items():
            _testutil.git(self.repo, "config", key, value)
        with mock.patch.dict(os.environ, self.floor_env, clear=True), tempfile.TemporaryDirectory() as tmp:
            empty_global_config(tmp)
            floor = dict(os.environ)
            settings = {key: query_git(self.repo, floor, "config", "--get", key) for key in OPPOSITE}
            global_list = query_git(self.repo, floor, "config", "--global", "--list")
            marker = system_marker(self.repo, floor)
        for key, value in OPPOSITE.items():
            with self.subTest(key=key):
                self.assertEqual(settings[key], (0, value))
        self.assertEqual(global_list, (0, ""), "床が global として何かを読ませている")
        self.assertEqual(marker, (0, "read"), "床が system の config を読ませない (GIT_CONFIG_NOSYSTEM)")

    def test_git_launched_by_the_hook_sees_the_settings(self):
        """4 設定が、基底クラスの `GIT_CONFIG_COUNT` から見えること (repo 自身の config の逆の値に勝つ)。"""
        for key, value in OPPOSITE.items():
            _testutil.git(self.repo, "config", key, value)
        for key, expected in EXPECTED.items():
            with self.subTest(key=key):
                self.assertEqual(self._hook_git("config", "--get", key), (0, expected))

    def test_git_launched_by_the_hook_reads_the_fixture_as_its_global_config(self):
        code, out = self._hook_git("config", "--global", "--list")
        expected = sorted(f"{key.lower()}={value}" for key, value in EXPECTED_WITH_RECEIVE.items())
        self.assertEqual((code, sorted(out.splitlines())), (0, expected))

    def test_git_launched_by_the_hook_does_not_read_the_system_config(self):
        self.assertEqual(self._hook_git("config", "--get", "hermetic.system"), (1, ""))

    def test_git_launched_by_the_hook_does_not_read_the_default_excludes_file(self):
        """前提 (空の床にしない): 外側に置いた ignore は、`XDG_CONFIG_HOME` がそこを向いていれば効き、
        `XDG_CONFIG_HOME` が空なら HOME の側に置いたものが効く。そのうえで、基底クラスが張った env の
        ままの hook の判定には効かない。"""
        _testutil.write(self.repo, IGNORED_NAME, "x\n")
        planted_view = untracked_names(
            self.repo, {**os.environ, "XDG_CONFIG_HOME": self.planted_xdg_config_home}
        )
        self.assertEqual(planted_view, [], "前提: 置いた ignore が効く (空の床にしない)")
        fallback_view = untracked_names(self.repo, {**os.environ, "XDG_CONFIG_HOME": ""})
        self.assertEqual(fallback_view, [], "前提: XDG が空なら HOME の側に置いた ignore が効く")
        self.assertEqual(
            gitscan.untracked_among(self.repo, [IGNORED_NAME]),
            {IGNORED_NAME},
            "hook の git が開発者の global の ignore を読んでいる",
        )


class TestHookLaunchedGitInheritsTheSettings(_ProductGitChecks, HookTestCase):
    """`HookTestCase` (hook を起動するテスト) が張る env で、hook の git に設定が届くこと。"""


class TestGitScanTestCaseHandsTheSettings(_ProductGitChecks, GitScanTestCase):
    """`GitScanTestCase` (test_gitscan。hook の関数を直接呼ぶ) が張る env で、同じこと。"""


class TestReviewSetTestCaseHandsTheSettings(_ProductGitChecks, ReviewSetTestCase):
    """`ReviewSetTestCase` (test_review_set) が張る env で、同じこと。"""


if __name__ == "__main__":
    unittest.main()
