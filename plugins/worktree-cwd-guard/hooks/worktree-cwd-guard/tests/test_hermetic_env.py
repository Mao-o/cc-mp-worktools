"""テストが作る git repo と、hook が起動する git で、自動 gc / maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に tempdir の後始末が走ると、tearDown が
`Directory not empty` で落ちる。この flaky は直接は検出できない (object の hash 次第で偶発的)
ので、原因の側に床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、`make_repo` の
  中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける。前提として、commit / receive-pack が trace に載っていること (空の床にしない) と、上書き
  した env が helper の git に届いていること (spy) も確かめる。陽性対照として、止める設定が無い
  commit では起動が trace に見えることも確かめる (見えない git の版では「0 件」は何も見ていない)
- **設定の出どころ別** (`_HermeticConfigChecks`): 止める経路は env の `GIT_CONFIG_COUNT` と global の
  fixture の 2 本で、同じ値を持つので、有効値だけを見ると片方が欠けてももう片方が埋めて通ってしまう。
  加えて system の config は `GIT_CONFIG_NOSYSTEM` で読ませない。そこで 3 つを 1 本ずつ別の検査で見る。
  起動の仕方 (定数だけ / helper / hook の git) ごとに同じ 3 本を流すので、env を当てる各点 (定数・helper・
  基底クラス) で、`COUNT` だけ・`NOSYSTEM` 抜き・`GLOBAL` 抜きのどれが起きても、どれかが assertion で
  落ちる。外側の env には止めない側の値を置き、当てる側がそれに勝つことも見る
  - `GIT_CONFIG_COUNT`: repo 自身の config に反対の値を置き、git が見る値が止める側であること
    (env は repo 自身の config より優先される。fixture は負ける)
  - `GIT_CONFIG_GLOBAL`: `git config --global --list` が fixture の 5 設定と完全一致すること。
    キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
    将来足された `core.hooksPath` など、テストの前提を変えるもの) が増えても通る
  - `GIT_CONFIG_NOSYSTEM`: system の config の代わりに置いた目印が読まれないこと

「patch していない」状態は `isolate_git_config` で作る。`GIT_CONFIG_*` を外し、global を空にし、
system の config を目印の file に向けるが、**`GIT_CONFIG_NOSYSTEM` は床の側で立てない**。床の側で
立てると、helper・基底クラス・定数が `GIT_CONFIG_NOSYSTEM` を当て損ねても (部分適用)、床が埋めて
通る。目印は `NOSYSTEM` が効いていなければ読めるので、当て損ねが見える。
床が当てる側の値を持たないこと自体も、当てる側が当てる前の env (床の env) だけで起動した git で
確かめる (`test_the_floor_alone_stops_nothing`)。床が当てる側の値を持つ形に戻ると、当て損ねを床が
埋めて、上の 3 本が黙って通るため。
開発者の本物の system / global の config (そこに `maintenance.auto=false` があると、迂回した git も
maintenance を起動せず、床が黙って通る) は、目印と空の HOME で置き換わるので読まれない。
ただし Apple の git が読む Xcode / Command Line Tools 同梱の config (`--show-scope` で unknown) は
`GIT_CONFIG_SYSTEM` では置き換わらず、`GIT_CONFIG_NOSYSTEM` でだけ外れる。中身はこの床の検査に効かず、
maintenance を止める設定が入れば陽性対照が落ちる。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config` は未設定 / 読めないとき exit 1 などになる
ので、`check=True` を使わず、「無い」は例外ではなく値の不一致 (assertion) として出す。

この suite の hook とテスト本体は push しない。push の床 (`TestPlainBareOriginStartsNoMaintenance`) は、
global の fixture が `receive-pack` に届くことを、同じ作りの他の suite と揃えて固定するためのもの。
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

import family

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
# push の受け側 (`receive-pack`) にも効く設定を足したもの (global の fixture)
EXPECTED_WITH_RECEIVE = {**EXPECTED, "receive.autogc": "false"}
# `git config --list` はキーを小文字で出す
FIXTURE_LINES = sorted(f"{key.lower()}={value}" for key, value in EXPECTED_WITH_RECEIVE.items())
# repo 自身の config に置く、止めない側の値 (env が効いていれば、これには負けない)
OPPOSITE = {
    "maintenance.auto": "true",
    "maintenance.autoDetach": "true",
    "gc.auto": "6700",
    "gc.autoDetach": "true",
}
# system の config の代わりに置く目印。`GIT_CONFIG_NOSYSTEM` が効いていなければ読める
SYSTEM_MARKER = "[hermetic]\n\tsystem = read\n"


def empty_file(directory: str) -> str:
    """空の config file を作ってパスを返す (global の fixture の代わりに指す。何も設定しない)。

    `os.devnull` ではなく実体のある空 file にするのは、Windows の `nul` を git が config として
    読めるかが git の版に依存しうるため (この suite は Windows の CI でも流れる)。
    """
    path = os.path.join(directory, "empty.gitconfig")
    Path(path).write_text("", encoding="utf-8")
    return path


def isolate_git_config(home: str) -> None:
    """「patch していない」状態を作る。`mock.patch.dict(os.environ)` の中で呼ぶこと (環境を戻すため)。

    `GIT_CONFIG_*` (`GIT_CONFIG_NOSYSTEM` を含む) を外し、`HOME` / `XDG_CONFIG_HOME` を空の `home` に
    向け、`GIT_CONFIG_SYSTEM` を目印 (`hermetic.system=read`) の file に向ける。`GIT_CONFIG_NOSYSTEM` を
    立てないのが要点 (モジュールの docstring)。`GIT_CONFIG_SYSTEM` は git 2.32 以上。
    """
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
        del os.environ[name]
    system = os.path.join(home, "system.gitconfig")
    Path(system).write_text(SYSTEM_MARKER, encoding="utf-8")
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_SYSTEM": system})


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
    (`HERMETIC_GIT_ENV` の全項目が helper の git に届いている) で拾える。

    見るのは**起動された git の挙動** (maintenance / gc の子が 0 件) で、ヘルパーが後から問い合わせた
    設定値ではない。`make_repo` の commit だけが env を持たずに起動されても、問い合わせ
    (`_testutil.sh` 経由) は env を足し直すので、値を見るテストでは気付けない。
    """

    def _spawned_by_make_repo(self, *, with_fixture: bool) -> list[list[str]]:
        """`make_repo` の間に起動された maintenance / gc の argv。

        `with_fixture=False` なら、この呼び出しの間だけ `HERMETIC_GIT_ENV` の `GIT_CONFIG_GLOBAL` を
        空の config file に上書きする。helper が渡す設定のうち fixture を外して、残りの経路 (env) だけで
        止まるかを見るために使う。

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
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            isolate_git_config(home)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            overrides = {} if with_fixture else {"GIT_CONFIG_GLOBAL": empty_file(tmp)}
            with mock.patch.dict(_testutil.HERMETIC_GIT_ENV, overrides):
                with mock.patch.object(subprocess, "run", side_effect=spy):
                    _testutil.make_repo(Path(tmp) / "work")
                want = dict(_testutil.HERMETIC_GIT_ENV)
            events = trace_events(trace)
        self.assertTrue(
            any(all(env.get(k) == v for k, v in want.items()) for env in passed),
            "前提: 上書きした HERMETIC_GIT_ENV が helper の git に届いている",
        )
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        return spawned_maintenance(events)

    def test_the_trace_sees_maintenance_when_nothing_stops_it(self):
        """陽性対照: 止める設定が無い commit では、maintenance の起動が trace に見えること。

        これが成り立たない環境 (git の版で起動の形が変わった等) では、この file の「0 件」は何も
        見ていない。後始末と重ならないよう、背景へ切り離さない設定 (`autoDetach=false`) だけは渡す
        (起動は残り、`--no-detach` になる)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            isolate_git_config(home)
            os.environ.update(
                {
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": "maintenance.autoDetach",
                    "GIT_CONFIG_VALUE_0": "false",
                    "GIT_CONFIG_KEY_1": "gc.autoDetach",
                    "GIT_CONFIG_VALUE_1": "false",
                }
            )
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            for args in (
                ["init", "-q", "-b", "main"],
                ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                 "commit", "--allow-empty", "-qm", "x"],
            ):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            events = trace_events(trace)
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        self.assertNotEqual(spawned_maintenance(events), [])

    def test_repo_made_without_any_env_patch(self):
        self.assertEqual(self._spawned_by_make_repo(with_fixture=True), [])

    def test_the_env_path_alone_stops_it(self):
        """global の fixture を外し、helper が渡す `GIT_CONFIG_COUNT` だけで止まること。

        fixture が同じ設定を持つので、上のテストだけでは、helper が `GIT_CONFIG_COUNT` を渡し損ねても
        fixture が埋めて通ってしまう。
        """
        self.assertEqual(self._spawned_by_make_repo(with_fixture=False), [])


class TestPlainBareOriginStartsNoMaintenance(unittest.TestCase):
    """repo 自身の config に設定が無い bare repo (`git init --bare` を直接呼んだもの) へ push しても、
    受け側 (`receive-pack`) が自動 maintenance を起動しないこと。

    `git push` がローカルの path へ送るとき、受け側は repo 用の env (`GIT_CONFIG_COUNT` など) を
    外されて起動する。env の設定だけだと、設定の無い bare repo では maintenance が起動する。外されない
    `GIT_CONFIG_GLOBAL` の fixture が止めていることを、起動された子プロセスで見る。helper を迂回した
    git が開発者の `~/.gitconfig` を読まないよう、global の config は空に、system の config は目印の
    file に向ける (`isolate_git_config`)。
    """

    def test_push_into_a_plain_bare_repo(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            isolate_git_config(home)
            main, _, _ = _testutil.make_repo(Path(tmp) / "work")
            plain = os.path.join(tmp, "plain.git")
            _testutil.sh(Path(tmp), "init", "--bare", "-q", plain)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            _testutil.sh(main, "push", "-q", plain, "HEAD:refs/heads/main")
            events = trace_events(trace)
        self.assertIn(
            "receive-pack", command_names(events), "前提: 受け側まで trace に載っている (空の床にしない)"
        )
        self.assertEqual(spawned_maintenance(events), [])


class _HermeticConfigChecks:
    """git が見る設定を、出どころ (env / global の fixture / system) ごとに 1 本ずつ確かめる共通の検査。

    起動の仕方 (`query`) は継承先が決める。`setUp` は「patch していない」状態を先に作ってから
    (`isolate_git_config`)、`super().setUp()` で基底クラスがあれば env を当てさせる。床が当てる側の値
    (`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の `GIT_CONFIG_COUNT`) を持つと、
    基底クラスの当て損ねを床が埋めてしまうので、床は当てる側の値を持たない
    (`test_the_floor_alone_stops_nothing` で確かめる)。外側の env (開発者の shell など) に
    `GIT_CONFIG_NOSYSTEM` があっても、`isolate_git_config` が先に外すので、当て損ねは隠れない。

    その間に、外側の env として止めない側の値 (`GIT_CONFIG_GLOBAL` = 空の file、`GIT_CONFIG_COUNT` で
    `maintenance.auto=true`) を置く。当てる側 (定数 / helper / 基底クラス) は外側の env に勝つこと。
    外側の env が空のままだと、helper が外側の env を後から混ぜる向き
    (`{**HERMETIC_GIT_ENV, **os.environ}`) に変わっても、混ぜる値が無いので気付けない。
    """

    def query(self, args: list[str]) -> tuple[int, str]:
        """継承先が使う起動の仕方で git を起動して `(returncode, stdout)` を返す。cwd は `self.repo`。"""
        raise NotImplementedError

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        isolate_git_config(self.tmp)
        os.environ.update(
            {
                "GIT_CONFIG_GLOBAL": empty_file(self.tmp),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "maintenance.auto",
                "GIT_CONFIG_VALUE_0": "true",
            }
        )
        # 当てる側が当てる前の env。床が当てる側の値を持たないことの確認に使う
        self.floor_env = dict(os.environ)
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _testutil.sh(Path(self.repo), "init", "-q", "-b", "main")
        for key, value in OPPOSITE.items():
            _testutil.sh(Path(self.repo), "config", key, value)

    def test_the_floor_alone_stops_nothing(self):
        """床の env だけで起動した git では、下の 3 本が見るもののどれにも当てる側の値が見えないこと。

        床が当てる側の値 (`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の
        `GIT_CONFIG_COUNT`) を持つ形に戻ると、当てる側の当て損ねを床が埋めて、下の 3 本が黙って通る。
        """

        def floor_query(args: list[str]) -> tuple[int, str]:
            res = subprocess.run(
                ["git", *args], cwd=self.repo, env=self.floor_env, capture_output=True, text=True
            )
            return res.returncode, res.stdout.strip()

        for key, value in OPPOSITE.items():
            with self.subTest(key=key):
                self.assertEqual(floor_query(["config", "--get", key]), (0, value))
        self.assertEqual(floor_query(["config", "--global", "--list"]), (0, ""))
        self.assertEqual(floor_query(["config", "--get", "hermetic.system"]), (0, "read"))

    def test_env_beats_the_repos_own_config(self):
        """`GIT_CONFIG_COUNT` が効いていること。

        repo 自身の config に止めない側の値を置いてある。env は repo 自身の config より優先され、
        global の fixture は負けるので、止める側の値が見えるのは env が効いているときだけ。
        """
        for key, expected in EXPECTED.items():
            with self.subTest(key=key):
                rc, out = self.query(["config", "--get", key])
                self.assertEqual((rc, out.strip()), (0, expected))

    def test_global_is_the_fixture_only(self):
        """global として fixture を読み、それだけを読むこと (`--global --list` が 5 設定の完全一致)。

        fixture は git が読む global なので、余計な設定が増えると git を起動する全テストに効く。
        """
        rc, out = self.query(["config", "--global", "--list"])
        self.assertEqual((rc, sorted(out.splitlines())), (0, FIXTURE_LINES))

    def test_system_config_is_not_read(self):
        """system の config を読まないこと (`GIT_CONFIG_SYSTEM` が指す目印が読まれない)。"""
        # 前提: `GIT_CONFIG_NOSYSTEM` が無ければ目印は読める。これが成り立たない環境 (git が古い等) では
        # 下の「読まれない」は何も見ていない。ここで外すのは確認用の env だけで、起動の仕方には触らない
        env = {k: v for k, v in os.environ.items() if k != "GIT_CONFIG_NOSYSTEM"}
        control = subprocess.run(
            ["git", "config", "--get", "hermetic.system"],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            (control.returncode, control.stdout.strip()),
            (0, "read"),
            "前提: GIT_CONFIG_NOSYSTEM が無ければ system の目印は読める (空の床にしない)",
        )
        self.assertEqual(self.query(["config", "--get", "hermetic.system"]), (1, ""))


class TestConstantAlone(_HermeticConfigChecks, unittest.TestCase):
    """`HERMETIC_GIT_ENV` の定数だけで git を起動したとき (helper も基底クラスも通さない)。"""

    def query(self, args: list[str]) -> tuple[int, str]:
        res = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env={**os.environ, **_testutil.HERMETIC_GIT_ENV},
            capture_output=True,
            text=True,
        )
        return res.returncode, res.stdout


class TestHelperLaunchedGit(_HermeticConfigChecks, unittest.TestCase):
    """repo を作る helper (`_testutil.sh`) が起動する git。helper が毎回 env を足すので、
    env を patch していない状態でも、定数と同じ設定が見えること。"""

    def query(self, args: list[str]) -> tuple[int, str]:
        try:
            return 0, _testutil.sh(Path(self.repo), *args)
        except subprocess.CalledProcessError as e:  # 設定の問い合わせは非ゼロ終了を値として見る
            return e.returncode, e.stdout


class TestHookLaunchedGit(_HermeticConfigChecks, HermeticGitTestCase):
    """hook (製品コード) が起動する git。基底クラス `HermeticGitTestCase` が当てた env を継承すること。

    `family._git` は env を渡さず `os.environ` を継承するので、基底クラスが patch した env がそのまま
    見える。ここが外れると、hook の git だけ、開発者の global / system の config と自動 maintenance の
    既定に戻る。hook の git は読み取りだけ (`rev-parse` / `worktree list`) で maintenance を起動しない
    ので、ここで守るのは、開発者の設定に左右されないことと、helper と同じ設定が届くこと。

    `family._git` は非ゼロ終了も、起動できなかった・timeout も `None` にして握りつぶす。期待値が
    `None` の検査 (system を読まない) は、git が失敗しても通ってしまうので、`subprocess.run` の結果を
    捕まえて returncode を見る。起動できなかったときは前提の assertion で落とす。

    外側の環境に `GIT_CONFIG_NOSYSTEM` があっても、`setUp` が先に外してから基底クラスに当てさせるので、
    当て損ねは隠れない。
    """

    def query(self, args: list[str]) -> tuple[int, str]:
        runs: list[subprocess.CompletedProcess] = []
        real_run = subprocess.run

        def spy(argv, *a, **kw):
            res = real_run(argv, *a, **kw)
            runs.append(res)
            return res

        with mock.patch.object(subprocess, "run", side_effect=spy):
            family._git(args, Path(self.repo))
        self.assertEqual(len(runs), 1, "前提: hook の git を起動できた")
        return runs[0].returncode, runs[0].stdout


if __name__ == "__main__":
    unittest.main()
