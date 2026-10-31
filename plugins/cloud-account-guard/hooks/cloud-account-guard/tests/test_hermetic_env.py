"""テストが作る git repo で、自動 gc / maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。背景へ切り離された repack の書き込みは、object の
hash 次第で偶発的にしか影響しないので、直接は検出できない。原因の側に床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件であること。
  「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、`init_repo` の中の commit だけが env を
  持たずに起動されても気付ける。前提として commit が trace に載っていること (空の床にしない) と、上書きした
  env が helper の git に**起動の引数として**届いていること (spy) も確かめる。陽性対照として、止める設定が無い
  commit では起動が trace に見えることも確かめる (見えない git の版では「0 件」は何も見ていない)。trace は
  maintenance を起動しない git (`init` / `config` / `add`) の迂回には気付けないので、helper が起動する git の
  全部が `HERMETIC_GIT_ENV` を持つことは、spy で全件記録して別に見る
- **設定の出どころ別** (`_HermeticConfigChecks`): 止める経路は env の `GIT_CONFIG_COUNT` と global の fixture の
  2 本で、同じ値を持つので、有効値だけを見ると片方が欠けてももう片方が埋めて通ってしまう。加えて system の
  config は `GIT_CONFIG_NOSYSTEM` で読ませない。そこで 3 つを 1 本ずつ別の検査で見る。起動の仕方 (定数だけ /
  helper) ごとに同じ検査を流す。外側の env には止めない側の値を置き、当てる側がそれに勝つことも見る
  - `GIT_CONFIG_COUNT`: repo 自身の config に反対の値を置き、git が見る値が止める側であること
  - `GIT_CONFIG_GLOBAL`: `git config --global --list` が fixture の 5 設定と完全一致すること
  - `GIT_CONFIG_NOSYSTEM`: system の config の代わりに置いた目印が読まれないこと。前提として、
    `GIT_CONFIG_NOSYSTEM` が無い env では目印が読めること (陽性対照)
- **外側の repo / config / template を指す変数**: 外側の env に `GIT_DIR` や `GIT_CONFIG_PARAMETERS`、
  `GIT_TEMPLATE_DIR` などがあっても (git の hook や `git -c` の配下から suite を流すと入る)、helper の git に
  届かないこと。外側の repo の中身が前後で一致することと、起動した git の env に変数が無いことで見る
- **床自身**: 床 (外側の env) が当てる側の値を持つと、当てる側が同じ値を落としても床が埋めて、上の検査が黙って
  通る。床を作る関数 (`isolate_git_config`) は、開発者の global の config に見立てた file も床を作る前に置いて、
  読まれないことを `TestTheIsolatedEnvStopsNothing` で見る。trace / spy の床は、当てる側の直前にも見る
  (`assert_the_floor_stops_nothing`)。床の側では `GIT_CONFIG_NOSYSTEM` を立てない (立てると、定数・helper が
  `GIT_CONFIG_NOSYSTEM` / `GIT_CONFIG_GLOBAL` を当て損ねる部分適用を床が埋める)
- **直接の起動**: test module が `subprocess` で git を直接起動していないこと。repo を作る git が helper を
  迂回すると、上の床は helper しか見ないので気付けない
- **測る道具の自己確認** (`TestMeasuringTools`): 起動を記録する spy は起動の**時点**の env を記録する。spy が
  記録に当てる側の値を足す形に変わると、env を渡さない helper も「届いた」と読めて前提が黙って通る。
  helper が `os.environ` の patch で env を届ける形に変わった場合も、床の検査が空になる

製品コード (`scripts/accounts_builder.py` の `check-ignore`、`scripts/pin_env.py` の `rev-parse`) が起動する git
は読み取りだけで、自動 maintenance を起動する操作 (commit / merge / fetch) ではない。そのため製品側の git の
床は持たない (全 suite を通した trace で maintenance / gc の起動が 0 件であることは、変更時に確かめた)。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から 1 項目消えても
期待値ごと消えて通ってしまう。`git config` は未設定 / 読めないとき exit 1 などになるので、`check=True` を
使わず、「無い」は例外ではなく値の不一致 (assertion) として出す。

`TestGitIgnoreAware` (`test_accounts_builder.py`) は global の config を自分で書く (`core.excludesFile` を
製品の `check-ignore` に読ませる) ので、その global は fixture ではなくテスト専用の file のまま。この class は
commit しない。
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _testutil

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
# 外側の env にあると、git が別の repo や別の config、別の template を見てしまう変数。
# `_testutil.OUTER_GIT_LEAK_ENV` とは別にリテラルで持つ
OUTER_REPO_ENV_NAMES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_TEMPLATE_DIR",
)


def outer_repo_env(directory: str) -> dict[str, str]:
    """外側の env に置く、`OUTER_REPO_ENV_NAMES` の変数。値は `directory` の中を指す (当てる側が外し損ねても、
    外側の本物の repo を触らない)。`GIT_CONFIG_PARAMETERS` は maintenance を止めない側の値。"""
    other = os.path.join(directory, "outer")
    return {
        "GIT_DIR": os.path.join(other, ".git"),
        "GIT_WORK_TREE": other,
        "GIT_INDEX_FILE": os.path.join(other, ".git", "index"),
        "GIT_COMMON_DIR": os.path.join(other, ".git"),
        "GIT_OBJECT_DIRECTORY": os.path.join(other, ".git", "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(other, ".git", "objects"),
        "GIT_NAMESPACE": "outer",
        "GIT_CONFIG": os.path.join(other, "legacy.gitconfig"),
        "GIT_CONFIG_PARAMETERS": "'maintenance.auto'='true'",
        "GIT_TEMPLATE_DIR": os.path.join(other, "template"),
    }


def tree_state(root: Path) -> dict[str, bytes]:
    """`root` 以下の全 file の中身 (相対 path -> bytes)。外側の repo が変わっていないことを見るため。"""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def git_in(env: dict, cwd, args: list) -> tuple:
    """`env` をそのまま渡して git を起動し、`(returncode, stdout)` を返す。床の env を見る検査が共通で使う。"""
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
    )
    return res.returncode, res.stdout


def empty_file(directory: str) -> str:
    """空の config file を作ってパスを返す (global の fixture の代わりに指す。何も設定しない)。"""
    path = os.path.join(directory, "empty.gitconfig")
    Path(path).write_text("", encoding="utf-8")
    return path


def isolate_git_config(home: str) -> None:
    """「patch していない」状態を作る。`mock.patch.dict(os.environ)` の中で呼ぶこと (環境を戻すため)。

    `GIT_CONFIG_*` (`GIT_CONFIG_NOSYSTEM` を含む) と、外側の repo / config / template を指す変数を外し、
    `HOME` / `XDG_CONFIG_HOME` を空の `home` に向け、`GIT_CONFIG_SYSTEM` を目印 (`hermetic.system=read`) の
    file に向ける。`home` には空の `.gitconfig` を置く: global の config が無いと `git config --global --list`
    は exit 128 になり、空の global を `(0, "")` で見られないため。`GIT_CONFIG_NOSYSTEM` を立てないのが要点
    (モジュール docstring)。`GIT_CONFIG_SYSTEM` は git 2.32 以上。
    """
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_") or n in OUTER_REPO_ENV_NAMES]:
        del os.environ[name]
    system = os.path.join(home, "system.gitconfig")
    Path(system).write_text(SYSTEM_MARKER, encoding="utf-8")
    Path(home, ".gitconfig").write_text("", encoding="utf-8")
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_SYSTEM": system})


def assert_the_floor_stops_nothing(case: unittest.TestCase, cwd: str) -> None:
    """当てる側を呼ぶ直前の床 (今の `os.environ`) が、当てる側の値を持たないこと。

    自動 maintenance を止める設定が無いこと、global が空であること、system の目印が読めること
    (`GIT_CONFIG_NOSYSTEM` が無い)。`GIT_TRACE2_EVENT` は除いて起動する (この確認の git を trace に載せない)。
    """
    floor = {k: v for k, v in os.environ.items() if k != "GIT_TRACE2_EVENT"}
    case.assertEqual(
        git_in(floor, cwd, ["config", "--get", "maintenance.auto"]), (1, ""), "前提: 床は自動 maintenance を止めない"
    )
    case.assertEqual(git_in(floor, cwd, ["config", "--global", "--list"]), (0, ""), "前提: 床の global は空")
    case.assertEqual(
        git_in(floor, cwd, ["config", "--get", "hermetic.system"]),
        (0, "read\n"),
        "前提: 床は GIT_CONFIG_NOSYSTEM を持たない",
    )


def trace_events(path: str) -> list[dict]:
    """`GIT_TRACE2_EVENT` が書いた JSON 行を読む。trace が取れていなければ空。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return [json.loads(line) for line in f if line.strip()]


def command_names(events: list[dict]) -> set:
    """trace に載った git プロセスのコマンド名 (`commit` など)。"""
    return {e.get("name") for e in events if e.get("event") == "cmd_name"}


def spawned_maintenance(events: list[dict]) -> list[list[str]]:
    """起動された `git maintenance ...` / `git gc ...` の argv。自動 maintenance が走った証拠。"""
    return [
        e["argv"]
        for e in events
        if e.get("event") == "child_start" and (e.get("argv") or [])[1:2] in (["maintenance"], ["gc"])
    ]


class GitLaunch:
    """spy が捕まえた git の起動 1 件。起動の**時点**の値だけを持つ。"""

    def __init__(self, explicit_env: dict | None, process_env: dict, argv: list | None = None):
        self.argv = list(argv or [])
        self.explicit_env = explicit_env  # 起動の引数 `env=` で渡された値 (渡さなければ None)
        self.process_env = process_env  # 起動の時点の `os.environ`

    @property
    def effective_env(self) -> dict:
        """子プロセスが実際に受け取る env。`env=` が無ければ `os.environ` を継承する。"""
        return self.explicit_env if self.explicit_env is not None else self.process_env


@contextlib.contextmanager
def record_git_launches():
    """`subprocess.run` で git を起動するたびに、その時点の env を記録する (起動は実際に行う)。"""
    launches: list[GitLaunch] = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        if list(argv)[:1] == ["git"]:
            env = kwargs.get("env")
            launches.append(GitLaunch(dict(env) if env is not None else None, dict(os.environ), list(argv)))
        return real_run(argv, *args, **kwargs)

    with mock.patch.object(subprocess, "run", side_effect=spy):
        yield launches


def helper_git(args: list[str], cwd):
    """`_testutil.git` を呼び、git の失敗 (外側の変数が漏れたときなど) は例外ではなく assertion の失敗にする
    (変異が「検出」として数えられるように)。"""
    try:
        return _testutil.git(args, cwd)
    except subprocess.CalledProcessError as e:
        raise AssertionError(f"helper の git が失敗した: {e.cmd} {e.stderr!r}") from e


def helper_init_repo(repo) -> None:
    """`_testutil.init_repo` を呼び、git の失敗は assertion の失敗にする。"""
    try:
        _testutil.init_repo(repo)
    except subprocess.CalledProcessError as e:
        raise AssertionError(f"helper の git が失敗した: {e.cmd} {e.stderr!r}") from e


def make_repo_with_one_commit(repo: str) -> None:
    """helper だけで repo を作り、commit まで進める (自動 maintenance が起動しうる最小の操作)。"""
    helper_init_repo(repo)
    Path(repo, "seed.txt").write_text("seed\n", encoding="utf-8")
    helper_git(["add", "seed.txt"], repo)
    helper_git(["commit", "-qm", "init"], repo)


class TestHelpersStopBackgroundMaintenance(unittest.TestCase):
    """テストクラスが env を patch していなくても、repo を作る helper 自身が止める。

    見るのは**起動された git の挙動** (maintenance / gc の子が 0 件) で、helper が後から問い合わせた設定値
    ではない。
    """

    def _spawned_by_helpers(self, *, with_fixture: bool) -> list[list[str]]:
        """helper が repo を作って commit する間に起動された maintenance / gc の argv。

        `with_fixture=False` なら、この呼び出しの間だけ `HERMETIC_GIT_ENV` の `GIT_CONFIG_GLOBAL` を空の
        config file に上書きし、残りの経路 (env) だけで止まるかを見る。上書きが helper の git に**起動の引数**
        (`explicit_env`) として届いたことも前提として確かめる (`os.environ` の patch で届ける形に変わると、
        外側の env が床の検査を埋めるため)。`any` で見るのは、commit だけが helper を迂回する変異でも前提は
        満たしたまま、maintenance の起動の assertion で落とすため
        (全件は `test_every_git_launched_by_the_helpers_carries_the_env`)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            overrides = {} if with_fixture else {"GIT_CONFIG_GLOBAL": empty_file(tmp)}
            with mock.patch.dict(_testutil.HERMETIC_GIT_ENV, overrides):
                repo = os.path.join(tmp, "repo")
                os.makedirs(repo)
                assert_the_floor_stops_nothing(self, tmp)
                with record_git_launches() as launches:
                    make_repo_with_one_commit(repo)
                want = dict(_testutil.HERMETIC_GIT_ENV)
            events = trace_events(trace)
        self.assertTrue(
            any(
                launch.explicit_env is not None
                and all(launch.explicit_env.get(k) == v for k, v in want.items())
                for launch in launches
            ),
            "前提: 上書きした HERMETIC_GIT_ENV が helper の git に起動の引数 (env=) として届いている",
        )
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        return spawned_maintenance(events)

    def test_the_trace_sees_maintenance_when_nothing_stops_it(self):
        """陽性対照: 止める設定が無い commit では、maintenance の起動が trace に見えること。

        これが成り立たない環境 (git の版で起動の形が変わった等) では、この file の「0 件」は何も見ていない。
        後始末と重ならないよう、背景へ切り離さない設定 (autoDetach=false) だけは渡す。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
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
            # 外側の repo / config / template を指す変数だけ外す。止める設定は渡したくないので
            # `hermetic_env()` ではなくここで外す
            env = {k: v for k, v in os.environ.items() if k not in OUTER_REPO_ENV_NAMES}
            for args in (
                ["init", "-q"],
                [
                    "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                    "commit", "--allow-empty", "-qm", "x",
                ],
            ):
                subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
            self.assertTrue(os.path.isdir(os.path.join(repo, ".git")), "前提: repo が自分の場所に作られている")
            events = trace_events(trace)
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        self.assertNotEqual(spawned_maintenance(events), [])

    def test_every_git_launched_by_the_helpers_carries_the_env(self):
        """repo を作る helper (`init_repo` / `git`) が起動する git の全部が、`HERMETIC_GIT_ENV` の全項目を
        起動の引数 (`env=`) で持つこと。

        trace の床は maintenance を起動する git (commit など) しか見ない。`init` / `config` / `add` を 1 つ
        だけ env を渡さずに起動する形は、maintenance の起動を数える床には差が出ない。そこで起動を全件記録して
        `all` で見る。前提として起動の件数と種類 (init / config 3 件 / add / commit の 6 件) を確かめる。
        床が `HERMETIC_GIT_ENV` のどの key も同じ値で持たないことも key ごとに見る。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            assert_the_floor_stops_nothing(self, tmp)
            for key, value in _testutil.HERMETIC_GIT_ENV.items():
                self.assertNotEqual(os.environ.get(key), value, f"前提: 床が {key} を当てる側と同じ値で持たない")
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with record_git_launches() as launches:
                make_repo_with_one_commit(repo)
            want = dict(_testutil.HERMETIC_GIT_ENV)
        self.assertEqual(
            [launch.argv[1] for launch in launches],
            ["init", "config", "config", "config", "add", "commit"],
            "前提: init / config x3 / add / commit の 6 件を記録できている",
        )
        for launch in launches:
            with self.subTest(argv=launch.argv[1:3]):
                self.assertIsNotNone(launch.explicit_env, "env= を起動の引数として渡している")
                env = launch.explicit_env or {}
                self.assertEqual({k: env.get(k) for k in want}, want)

    def test_repo_made_without_any_env_patch(self):
        self.assertEqual(self._spawned_by_helpers(with_fixture=True), [])

    def test_the_env_path_alone_stops_it(self):
        """global の fixture を外し、helper が渡す `GIT_CONFIG_COUNT` だけで止まること。

        fixture が同じ設定を持つので、上のテストだけでは、helper が `GIT_CONFIG_COUNT` を渡し損ねても
        fixture が埋めて通ってしまう。
        """
        self.assertEqual(self._spawned_by_helpers(with_fixture=False), [])

    def test_the_fixture_path_alone_stops_it(self):
        """env の `GIT_CONFIG_COUNT` を外し、global の fixture だけで止まること。

        対になる検査。helper が fixture を指し損ねても、`GIT_CONFIG_COUNT` が埋めて通ってしまうのを防ぐ。
        env の設定は `git_config_env` が作るので、ここでは件数ごと空にした env で helper を回す。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            no_count = {k: v for k, v in _testutil.HERMETIC_GIT_ENV.items() if not k.startswith("GIT_CONFIG_KEY")}
            no_count["GIT_CONFIG_COUNT"] = "0"
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with mock.patch.object(_testutil, "HERMETIC_GIT_ENV", no_count):
                assert_the_floor_stops_nothing(self, tmp)
                with record_git_launches() as launches:
                    make_repo_with_one_commit(repo)
            events = trace_events(trace)
        self.assertTrue(
            any(
                launch.explicit_env is not None
                and launch.explicit_env.get("GIT_CONFIG_COUNT") == "0"
                and launch.explicit_env.get("GIT_CONFIG_GLOBAL") == _testutil.HERMETIC_GIT_CONFIG
                for launch in launches
            ),
            "前提: COUNT を空にした env が helper の git に起動の引数として届いている",
        )
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        self.assertEqual(spawned_maintenance(events), [])


class TestPinEnvRepoHelperStopsMaintenance(unittest.TestCase):
    """`test_pin_env._init_repo` (repo を作って commit する実際の箇所) が、自動 maintenance を起動しないこと。

    `direct_git_launches` の AST 検出は、argv を変数に入れてから起動する形をすり抜ける。実際に commit する
    箇所の挙動を直接見る床。外側の env には、止めない側の値 (背景へ切り離さない autoDetach=false だけ) を
    置く。helper が env を渡し損ねると、commit が maintenance を起動して trace に見える。
    """

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
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "maintenance.autoDetach",
                "GIT_CONFIG_VALUE_0": "false",
                "GIT_CONFIG_KEY_1": "gc.autoDetach",
                "GIT_CONFIG_VALUE_1": "false",
            }
        )
        self.trace = os.path.join(self.tmp, "trace2.jsonl")
        os.environ["GIT_TRACE2_EVENT"] = self.trace

    def test_init_repo_of_test_pin_env_spawns_no_maintenance(self):
        import test_pin_env

        repo = Path(self.tmp) / "repo"
        try:
            test_pin_env._init_repo(repo)
        except subprocess.CalledProcessError as e:
            raise AssertionError(f"git が失敗した: {e.cmd} {e.stderr!r}") from e
        events = trace_events(self.trace)
        self.assertIn("commit", command_names(events), "前提: commit が trace に載っている (空の床にしない)")
        self.assertEqual(spawned_maintenance(events), [])


class TestTheIsolatedEnvStopsNothing(unittest.TestCase):
    """`isolate_git_config` だけで作った env (当てる側が何も当てていない床) が、当てる側の値を持たないこと。

    床を作る前の env に、外すべきもの (`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の
    `GIT_CONFIG_COUNT`、外側の repo / config / template を指す変数、開発者の global の config に見立てた 2 つの
    file) を置いてから `isolate_git_config` を呼び、床が組んだ env だけで起動した git で、どれも見えないこと
    を、他の床と同じ `git_in` で確かめる。開発者の config はこのテストが自分で置くので、実行者の HOME に左右
    されない。
    """

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        os.environ.update(outer_repo_env(self.tmp))
        os.environ.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": _testutil.HERMETIC_GIT_CONFIG,
                **_testutil.git_config_env(_testutil.NO_BACKGROUND_GIT_SETTINGS),
                **self.plant_a_developers_global_config(),
            }
        )
        self.outer = dict(os.environ)
        isolate_git_config(self.tmp)
        self.env = dict(os.environ)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        # repo は床の env のまま作る (helper が足す env を混ぜない)
        self.assertEqual(git_in(self.env, self.repo, ["init", "-q"]), (0, ""))

    def plant_a_developers_global_config(self) -> dict[str, str]:
        """開発者の global の config に見立てた file を作り、それを指す `HOME` / `XDG_CONFIG_HOME` を返す。

        `HOME` 側は `maintenance.auto`、`XDG_CONFIG_HOME` 側は `gc.auto` を書く (`~/.gitconfig` がある間、
        `--global --list` は XDG の config を読まないので、後者は 4 設定の問い合わせで見る)。
        """
        dev_home = os.path.join(self.tmp, "dev-home")
        dev_xdg = os.path.join(self.tmp, "dev-xdg")
        os.makedirs(dev_home)
        os.makedirs(os.path.join(dev_xdg, "git"))
        Path(dev_home, ".gitconfig").write_text("[maintenance]\n\tauto = false\n", encoding="utf-8")
        Path(dev_xdg, "git", "config").write_text("[gc]\n\tauto = 0\n", encoding="utf-8")
        return {"HOME": dev_home, "XDG_CONFIG_HOME": dev_xdg}

    def test_the_outer_values_were_there_before_isolating(self):
        """前提: 床を作る前の env に、外すべきものがある (置き損ねると、下の「外れている」は何も見ていない)。"""
        self.assertEqual(self.outer.get("GIT_CONFIG_NOSYSTEM"), "1")
        self.assertEqual(self.outer.get("GIT_CONFIG_GLOBAL"), _testutil.HERMETIC_GIT_CONFIG)
        self.assertEqual(self.outer.get("GIT_CONFIG_COUNT"), str(len(EXPECTED)))
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, self.outer)
        for key, parts in (("HOME", (".gitconfig",)), ("XDG_CONFIG_HOME", ("git", "config"))):
            with self.subTest(key=key):
                self.assertNotEqual(Path(self.outer[key], *parts).read_text(encoding="utf-8"), "")

    def test_every_placed_variable_is_in_the_name_list(self):
        """`outer_repo_env` が置く変数が、名前の一覧 (`OUTER_REPO_ENV_NAMES`) の全部であること。"""
        self.assertEqual(set(outer_repo_env(self.tmp)), set(OUTER_REPO_ENV_NAMES))

    def test_none_of_the_four_settings_is_set(self):
        for key in EXPECTED:
            with self.subTest(key=key):
                self.assertEqual(git_in(self.env, self.repo, ["config", "--get", key]), (1, ""))

    def test_global_is_empty(self):
        self.assertEqual(git_in(self.env, self.repo, ["config", "--global", "--list"]), (0, ""))

    def test_the_system_marker_is_readable(self):
        self.assertEqual(git_in(self.env, self.repo, ["config", "--get", "hermetic.system"]), (0, "read\n"))

    def test_no_outer_repo_or_config_variable_is_left(self):
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, self.env)


class TestMeasuringTools(unittest.TestCase):
    """床の測り方 (spy と helper の env の渡し方) 自体が空でないこと。"""

    def test_the_spy_records_the_env_at_launch_time(self):
        """env を渡さない起動は「引数なし」、渡す起動はその値、として記録されること。"""
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            passed = {**os.environ, "MARK": "1"}
            for key, value in _testutil.HERMETIC_GIT_ENV.items():
                self.assertNotEqual(passed.get(key), value, f"前提: 渡す env が {key} を当てる側と同じ値で持たない")
            with record_git_launches() as launches:
                subprocess.run(["git", "--version"], capture_output=True, check=True)
                subprocess.run(["git", "--version"], capture_output=True, check=True, env=passed)
            inherited = dict(os.environ)
        bare, explicit = launches
        self.assertIsNone(bare.explicit_env)
        self.assertEqual(bare.effective_env, inherited)
        self.assertEqual(explicit.explicit_env, passed)
        self.assertNotIn("GIT_CONFIG_COUNT", bare.effective_env)
        self.assertNotIn("GIT_CONFIG_GLOBAL", bare.effective_env)

    def test_the_helper_passes_the_env_as_an_argument_not_through_os_environ(self):
        """`_testutil.git` が env を起動の引数で渡し、`os.environ` を patch しないこと。"""
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            before = dict(os.environ)
            with record_git_launches() as launches:
                _testutil.git(["--version"], tmp)
            self.assertEqual(dict(os.environ), before)
        (launch,) = launches
        self.assertIsNotNone(launch.explicit_env, "helper は env= を起動の引数として渡す")
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
            with self.subTest(key=key):
                self.assertNotIn(key, launch.process_env)
                self.assertEqual(launch.explicit_env.get(key), _testutil.HERMETIC_GIT_ENV.get(key))


class _HermeticConfigChecks:
    """git が見る設定を、出どころ (env / global の fixture / system) ごとに 1 本ずつ確かめる共通の検査。

    起動の仕方 (`launch`) は継承先が決める。`setUp` は「patch していない」状態を先に作ってから
    (`isolate_git_config`)、外側の env として止めない側の値を置き、その env を `self.floor_env` に控える。
    床が当てる側の値を持つと当て損ねを床が埋めてしまうので、床は当てる側の値を持たない
    (`test_the_floor_alone_stops_nothing`)。外側の env には止めない側の値 (`GIT_CONFIG_GLOBAL` = 空の
    file、`GIT_CONFIG_COUNT` で `maintenance.auto=true`) を置く: 外側の env が空のままだと、helper が
    外側の env を後から混ぜる向き (`{**HERMETIC_GIT_ENV, **os.environ}`) に変わっても気付けない。
    `query` は `launch` の前後で、床を作った後に env が変わっていないことを確かめる。
    """

    outer_config_parameters = True

    def launch(self, args: list[str]) -> tuple[int, str]:
        """継承先が使う起動の仕方で git を起動して `(returncode, stdout)` を返す。cwd は `self.repo`。"""
        raise NotImplementedError

    def query(self, args: list[str]) -> tuple[int, str]:
        self.assertEqual(dict(os.environ), self.applied_env, "前提: 床を作った後に env が変わっていない")
        try:
            return self.launch(args)
        finally:
            self.assertEqual(dict(os.environ), self.applied_env, "前提: 起動の中で env を変えていない")

    def setUp(self) -> None:
        self.assertIs(type(self).query, _HermeticConfigChecks.query, "前提: 継承先が query を上書きしていない")
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
        if self.outer_config_parameters:
            os.environ["GIT_CONFIG_PARAMETERS"] = "'maintenance.auto'='true'"
        self.floor_env = dict(os.environ)
        self.applied_env = dict(os.environ)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        helper_init_repo(self.repo)
        for key, value in OPPOSITE.items():
            helper_git(["config", key, value], self.repo)

    def test_the_floor_alone_stops_nothing(self):
        """床の env (`self.floor_env`) だけで起動した git に、当てる側の値が見えないこと。床が置いたはずの
        止めない側の値は見えること。"""
        if self.outer_config_parameters:
            self.assertEqual(
                self.floor_env.get("GIT_CONFIG_PARAMETERS"),
                "'maintenance.auto'='true'",
                "前提: 床が外側の GIT_CONFIG_PARAMETERS (止めない側) を置いている",
            )
        # repo 自身の config に止めない側の値がある: 床の env が止める側の値を持てば、この値が変わる
        for key, value in OPPOSITE.items():
            with self.subTest(key=key):
                self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--get", key]), (0, value + "\n"))
        # repo の外では env の値だけが見える。床が置いた止めない側の値 (maintenance.auto=true) だけがある
        for key in EXPECTED:
            with self.subTest(outside_a_repo=key):
                want = (0, "true\n") if key == "maintenance.auto" else (1, "")
                self.assertEqual(git_in(self.floor_env, self.tmp, ["config", "--get", key]), want)
        # global は空 (fixture を指していない)。system の目印は読める (`GIT_CONFIG_NOSYSTEM` が無い)
        self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--global", "--list"]), (0, ""))
        self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--get", "hermetic.system"]), (0, "read\n"))

    def test_env_beats_the_repos_own_config(self):
        """`GIT_CONFIG_COUNT` が効いていること。

        repo 自身の config に止めない側の値を置いてある。env は repo 自身の config より優先され、global の
        fixture は負けるので、止める側の値が見えるのは env が効いているときだけ。外側の env にも止めない側の値
        があるので、当てる側が外側に勝たないと落ちる。
        """
        for key, expected in EXPECTED.items():
            with self.subTest(key=key):
                rc, out = self.query(["config", "--get", key])
                self.assertEqual((rc, out.strip()), (0, expected))

    def test_global_is_the_fixture_only(self):
        """global として fixture を読み、それだけを読むこと (`--global --list` が 5 設定の完全一致)。

        キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
        `core.hooksPath` など) が増えても通る。外側の env の `GIT_CONFIG_GLOBAL` (空の file) に勝つことも見る。
        """
        rc, out = self.query(["config", "--global", "--list"])
        self.assertEqual((rc, sorted(out.splitlines())), (0, FIXTURE_LINES))

    def test_system_config_is_not_read(self):
        """system の config を読まないこと (`GIT_CONFIG_SYSTEM` が指す目印が読まれない)。"""
        # 前提: 床の env (`GIT_CONFIG_NOSYSTEM` が無い) では目印が読める。これが成り立たない環境では、
        # 下の「読まれない」は何も見ていない
        self.assertEqual(
            git_in(self.floor_env, self.repo, ["config", "--get", "hermetic.system"]),
            (0, "read\n"),
            "前提: GIT_CONFIG_NOSYSTEM が無ければ system の目印は読める (空の床にしない)",
        )
        self.assertEqual(self.query(["config", "--get", "hermetic.system"]), (1, ""))


class TestConstantAlone(_HermeticConfigChecks, unittest.TestCase):
    """`HERMETIC_GIT_ENV` の定数だけで git を起動したとき (helper を通さない)。

    外側の env に `GIT_CONFIG_PARAMETERS` を置かない。それは `hermetic_env()` が外すもので、定数だけでは
    外せない。置くと `GIT_CONFIG_COUNT` に勝って、外す仕組みの無いこの起動が必ず落ちる。外す側の検査は
    `TestOuterRepoEnvAndTheHelpers` が持つ。
    """

    outer_config_parameters = False

    def launch(self, args: list[str]) -> tuple[int, str]:
        return git_in({**os.environ, **_testutil.HERMETIC_GIT_ENV}, self.repo, args)


class TestHelperLaunchedGit(_HermeticConfigChecks, unittest.TestCase):
    """repo を作る helper (`_testutil.git`) が起動する git。helper が毎回 env を足すので、env を patch して
    いない状態でも、定数と同じ設定が見えること。"""

    def launch(self, args: list[str]) -> tuple[int, str]:
        res = _testutil.git(args, self.repo, check=False)
        return res.returncode, res.stdout.decode("utf-8").replace("\r\n", "\n")


class TestOuterRepoEnvAndTheHelpers(unittest.TestCase):
    """外側の env に `GIT_DIR` などがあっても、helper の git に届かず、外側の repo に書き込まないこと。"""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        isolate_git_config(self.tmp)
        self.outer_repo = Path(self.tmp, "outer")
        self.outer_repo.mkdir()
        # 外側の repo は、変数を置く前に素の env で作る
        self.assertEqual(git_in(dict(os.environ), self.outer_repo, ["init", "-q"])[0], 0)
        os.environ.update(outer_repo_env(self.tmp))

    def test_the_outer_variables_were_placed(self):
        """前提: 外側の env に、一覧の変数が全部ある。"""
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, os.environ)

    def test_none_of_the_outer_variables_reaches_git(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        with record_git_launches() as launches:
            make_repo_with_one_commit(repo)
        self.assertEqual(len(launches), 6, "前提: helper の git を 6 件記録できている")
        for launch in launches:
            for name in OUTER_REPO_ENV_NAMES:
                with self.subTest(argv=launch.argv[1:3], name=name):
                    self.assertIsNotNone(launch.explicit_env)
                    self.assertNotIn(name, launch.explicit_env or {})

    def test_the_helpers_leave_the_outer_repo_alone(self):
        before = tree_state(self.outer_repo)
        self.assertNotEqual(before, {}, "前提: 外側の repo に中身がある")
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        make_repo_with_one_commit(repo)
        self.assertTrue(os.path.isdir(os.path.join(repo, ".git")), "前提: repo が自分の場所に作られている")
        self.assertEqual(tree_state(self.outer_repo), before)

    def test_the_outer_config_parameters_do_not_revive_maintenance(self):
        """外側の `GIT_CONFIG_PARAMETERS` (`maintenance.auto=true`) は `GIT_CONFIG_COUNT` に勝つので、
        helper が外さないと、helper の git が見る `maintenance.auto` が止めない側に戻る。"""
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        helper_init_repo(repo)
        res = _testutil.git(["config", "--get", "maintenance.auto"], repo, check=False)
        self.assertEqual((res.returncode, res.stdout.decode().strip()), (0, "false"))


_SUBPROCESS_LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def launches_git_literally(node: ast.AST) -> bool:
    """`subprocess.run(["git", ...])` のように、argv の先頭を literal の `"git"` で書いた git の起動か。

    見つけるのは、`subprocess` を名前で参照した呼び出しのうち、argv (位置引数の先頭か `args=`) が次のどれか
    の形だけ: 先頭が literal の `"git"` の list / tuple、その list / tuple を左辺に置いた連結
    (`["git"] + rest`)、先頭の語が `git` の文字列 (`shell=True` で渡す `"git commit ..."`)。argv を変数に入れて
    渡す形、f-string、別名 import、`os.system` での起動は見つけられない。
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_LAUNCHERS):
        return False
    if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
        return False
    argv = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
    if isinstance(argv, ast.BinOp) and isinstance(argv.op, ast.Add):
        argv = argv.left
    if isinstance(argv, ast.Constant) and isinstance(argv.value, str):
        return argv.value.split()[:1] == ["git"]
    if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
        return False
    first = argv.elts[0]
    return isinstance(first, ast.Constant) and first.value == "git"


def direct_git_launches(source: str, filename: str = "<source>") -> list[int]:
    """`source` の中で git を直接起動している行。メソッドの中のような入れ子の呼び出しも見る。"""
    tree = ast.parse(source, filename=filename)
    return [n.lineno for n in ast.walk(tree) if launches_git_literally(n)]


class TestNoTestLaunchesGitOutsideTheHelper(unittest.TestCase):
    """repo を作る / commit する git は `_testutil.git` を通すこと。

    helper を迂回した git には `HERMETIC_GIT_ENV` が足されず、自動 maintenance が起動する。上の床は
    helper の挙動しか見ないので、迂回した起動は別の検査で拾う。このファイル自身は、helper を通さない git の
    挙動を見るために直接起動するので対象に含めない。
    """

    def test_no_test_module_runs_git_directly(self):
        tests_dir = Path(__file__).resolve().parent
        scanned, offenders = [], []
        for path in sorted(tests_dir.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            scanned.append(path.name)
            source = path.read_text(encoding="utf-8")
            offenders += [f"{path.name}:{line}" for line in direct_git_launches(source, str(path))]
        self.assertLessEqual(
            {"test_pin_env.py", "test_accounts_builder.py", "test_builder_path_escape.py"},
            set(scanned),
            f"前提: repo を作る module を走査している (対象 0 件で黙って通らない): {scanned}",
        )
        self.assertEqual(offenders, [], "git の直接の起動は `_testutil.git` に置き換える (env を足すため)")

    def test_the_detector_sees_a_direct_launch(self):
        """検出の規則自体が空でないこと (見つけるべき形を 1 つずつ通す)。"""

        def call(source: str) -> ast.AST:
            return ast.parse(source).body[0].value

        nested = "class T:\n    def f(self):\n        subprocess.run(['git', 'init'], cwd=x)\n"
        self.assertEqual(direct_git_launches(nested), [3], "module の走査が入れ子の呼び出しを見る")
        for source in (
            'subprocess.run(["git", "init"], cwd=x)',
            'subprocess.check_call(("git", "init"))',
            'subprocess.Popen(["git", *args], cwd=x)',
            'subprocess.run(args=["git", "init"], cwd=x)',
            'subprocess.run(["git"] + rest, cwd=x)',
            'subprocess.run("git commit -qm x", shell=True, cwd=x)',
        ):
            with self.subTest(source=source):
                self.assertTrue(launches_git_literally(call(source)))
        for source in (
            "subprocess.run([sys.executable, '-c', code])",
            "subprocess.run(cmd)",
            "real_run(['git', 'init'])",
            "_git(['init'], cwd)",
            "subprocess.run('gitk', shell=True)",
        ):
            with self.subTest(source=source):
                self.assertFalse(launches_git_literally(call(source)))


if __name__ == "__main__":
    unittest.main()
