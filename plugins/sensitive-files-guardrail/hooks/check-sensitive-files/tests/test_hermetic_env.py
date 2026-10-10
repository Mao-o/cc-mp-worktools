"""テストが作る git repo と、hook が起動する git で、自動 gc / maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に `TemporaryDirectory` の後始末 (この file の床が使う) が走ると、
`Directory not empty` で落ちる。repo を作るテストの後始末は `rmtree(ignore_errors=True)` なので
落ちないが、tmp に残骸が残り、背景の git がテストより長く生きる。どちらも直接は検出できない
(object の hash 次第で偶発的) ので、原因の側に床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、`init_repo` の
  中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける。前提として、commit / receive-pack が trace に載っていること (空の床にしない) と、上書き
  した env が helper の git に**起動の引数として**届いていること (spy) も確かめる。陽性対照として、
  止める設定が無い commit では起動が trace に見えることも確かめる (見えない git の版では「0 件」は
  何も見ていない)。trace は maintenance を起動しない git (`init` / `config` / `add`) の迂回には
  気付けないので、helper が起動する git の全部が `HERMETIC_GIT_ENV` を持つことは、spy で全件記録して
  別に見る (`test_every_git_launched_by_the_helpers_carries_the_env`)
- **設定の出どころ別** (`_HermeticConfigChecks`): 止める経路は env の `GIT_CONFIG_COUNT` と global の
  fixture の 2 本で、同じ値を持つので、有効値だけを見ると片方が欠けてももう片方が埋めて通ってしまう。
  加えて system の config は `GIT_CONFIG_NOSYSTEM` で読ませない (止める経路には数えない)。そこで 3 つを
  1 本ずつ別の検査で見る。起動の仕方 (定数だけ / helper / hook の git) ごとに同じ検査を流すので、env を
  当てる各点 (定数・helper・基底クラス) で、`COUNT` だけ・`NOSYSTEM` 抜き・`GLOBAL` 抜きのどれが
  起きても、どれかが assertion で落ちる。外側の env には止めない側の値を置き、当てる側がそれに勝つことも
  見る
  - `GIT_CONFIG_COUNT`: repo 自身の config に反対の値を置き、git が見る値が止める側であること
    (env は repo 自身の config より優先される。fixture は負ける)
  - `GIT_CONFIG_GLOBAL`: `git config --global --list` が fixture の 5 設定と完全一致すること
  - `GIT_CONFIG_NOSYSTEM`: system の config の代わりに置いた目印が読まれないこと
- **外側の repo / config / template を指す変数** (`_OuterRepoEnvChecks`): 外側の env に `GIT_DIR` や
  `GIT_CONFIG_PARAMETERS`、`GIT_TEMPLATE_DIR` などがあっても (git の hook や `git -c` の配下から suite を
  流すと入る)、helper の git と基底クラスが起動する git に届かないこと。届くと helper の `init` / `config` /
  `commit` が外側の repo に書き込み、`GIT_CONFIG_PARAMETERS` は `GIT_CONFIG_COUNT` に勝って maintenance を
  黙って復活させ、外側の template の pre-commit が helper の commit で走る。外側の repo の中身が前後で
  一致することと、起動した git の env に変数が無いことで見る
- **床自身**: 床 (外側の env) が当てる側の値を持つと、当てる側が同じ値を落としても床が埋めて、上の検査が
  黙って通る。床を作る点ごとに、床の env だけで起動した git で、当てる側の値が見えないこと (と、床が置いた
  はずの止めない側の値が見えること) を確かめる。確認は床が実際に組んだ env (`self.floor_env` など) を、
  他の検査と同じ `git_in` で見る (床の組み立てを写した別の env を作ると、床の組み立ての側の退行が見えなくなる)
  - `isolate_git_config` (床を作る関数の自己確認): `TestTheIsolatedEnvStopsNothing` (開発者の global の config に
    見立てた file も床を作る前に置き、読まれないことを見る。実行者の HOME に左右されない)。見るのは床を作った
    直後だけなので、trace / spy / push の床は、区間ごとに当てる側の直前で見る (次の項)
  - trace の床 (`_spawned_by_helpers`)・spy の床 (`test_every_git_launched_by_the_helpers_carries_the_env`)・
    push の床 (`test_push_into_a_plain_bare_repo`): 当てる側 (helper / push) を呼ぶ直前に
    `assert_the_floor_stops_nothing` を呼ぶ。`isolate_git_config` から当てる側の呼び出しまでの区間に足した
    値を見るため。spy の床は値を key ごとに比べるので、床が `HERMETIC_GIT_ENV` のどの key も同じ値で持たない
    ことも key ごとに見る
  - `_HermeticConfigChecks.setUp` は `test_the_floor_alone_stops_nothing`、`_OuterRepoEnvChecks.setUp` は
    `test_the_floor_holds_the_outer_variables`。この 2 つの自己確認は setUp のスナップショット (`self.floor_env`)
    を見るので、当てた直後の env (`self.applied_env`) から問い合わせの時点まで変わっていないことを、`query` /
    `launched_env` の中で起動の前後に確かめる (setUp の後段や継承先の setUp、継承先の起動の中身で足した値は、
    自己確認からは見えず、問い合わせには届くため)。継承先は起動の中身 (`launch` / `launch_and_capture_env`) だけを
    実装する (継承先が `query` / `launched_env` を上書きしていないことは、mixin の `setUp` で見る)。helper を直接
    呼ぶテスト (`TestOuterRepoEnvAndTheHelpers` の 2 本) も、同じ確認 (`assert_env_unchanged_since_applied`) を
    前後で呼ぶ。残り: 起動の中身が当てる側の値を自分で足す形は、前後の確認では見えない。`os.environ` を範囲を
    限って変えて戻す形 (`mock.patch.dict(os.environ, ...)` など)、git に渡す env に足す形、床の env が指す file
    の中身 (system の目印・空の global など) を書き換える形を含む
- **直接の起動**: test module が `subprocess` で git を直接起動していないこと。repo を作る git が
  helper を迂回すると、上の床は helper しか見ないので気付けない。走査した module に repo を作る
  module が含まれること (対象 0 件で黙って通らない) と、入れ子の呼び出しも見ることも確かめる
- **測る道具の自己確認** (`TestMeasuringTools`): 起動を記録する spy (`record_git_launches`) は、起動の
  **時点**の env を記録する。spy が記録に当てる側の値を足す形に変わると、env を渡さない helper も「届いた」と
  読めて前提が黙って通る (spy の床が helper の当て損ねを見逃す)。そこで、env を渡さない起動と渡す起動を
  spy に通して、記録が実際の env と一致することを確かめる。helper が `os.environ` の patch で env を届ける
  形 (起動の引数には渡さない) に変わった場合も、床の外側の env が答えを持つことになるので、起動の時点の
  `os.environ` に答えが無いことを確かめる

用語: 床 (「patch していない」外側の env) が持ってはいけない値を「当てる側の値」と呼ぶ。
`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の `GIT_CONFIG_COUNT` が当たる。

「patch していない」状態は `isolate_git_config` で作る。`GIT_CONFIG_*` と外側の repo / config / template を
指す変数を外し、global を空にし、system の config を目印の file に向けるが、**`GIT_CONFIG_NOSYSTEM` は
床の側で立てない**。床の側で立てると、helper・基底クラス・定数が `GIT_CONFIG_NOSYSTEM` /
`GIT_CONFIG_GLOBAL` を当て損ねても (部分適用)、床が埋めて通ってしまう。目印は `NOSYSTEM` が効いて
いなければ読めるので、当て損ねが見える。開発者の本物の system / global の config (そこに
`maintenance.auto=false` があると、迂回した git も maintenance を起動せず、床が黙って通る) は、目印と
空の HOME で置き換わるので読まれない。
ただし Apple の git が読む Xcode / Command Line Tools 同梱の config (`--show-scope` で unknown) は
`GIT_CONFIG_SYSTEM` では置き換わらず、`GIT_CONFIG_NOSYSTEM` でだけ外れる。この床は
`GIT_CONFIG_NOSYSTEM` を立てないので、その config は読まれる。中身はこの床の検査には効かない
(実測: `credential.helper` と `init.defaultbranch` だけで、自動 maintenance を止める設定も目印も無い)。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config` は未設定 / 読めないとき exit 1 などになる
ので、`check=True` を使わず、「無い」は例外ではなく値の不一致 (assertion) として出す。
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
from _testutil import HermeticGitTestCase

import checker  # noqa: E402  (`_testutil` が sys.path に check-sensitive-files/ を足してから import する)

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
# 外側の env にあると、git が別の repo や別の config、別の template (`git init` が写す hook と除外ファイル) を
# 見てしまう変数。`_testutil.OUTER_GIT_LEAK_ENV` とは別に、ここにリテラルで持つ (同じ定数から導くと、そちらから
# 1 つ消えても期待値ごと消えて通る)
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
    外側の本物の repo を触らない)。`GIT_CONFIG_PARAMETERS` は maintenance を止めない側の値。
    `GIT_TEMPLATE_DIR` が指す dir はここでは作らない (中身は、効かないことを見るテストが置く)。"""
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


def write_lf(path: Path, text: str) -> None:
    """改行を変換せずに書く (`Path.write_text` の `newline=` は Python 3.10 以上)。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def git_in(env: dict, cwd, args: list) -> tuple:
    """`env` をそのまま渡して git を起動し、`(returncode, stdout)` を返す。床の env を見る検査が共通で使う。"""
    res = subprocess.run(
        ["git", *args], cwd=str(cwd), env=dict(env), capture_output=True, text=True, encoding="utf-8"
    )
    return res.returncode, res.stdout


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

    `GIT_CONFIG_*` (`GIT_CONFIG_NOSYSTEM` を含む) と、外側の repo / config / template を指す変数
    (`OUTER_REPO_ENV_NAMES`。旧来の `GIT_CONFIG` と `GIT_TEMPLATE_DIR` を含む) を外し、`HOME` /
    `XDG_CONFIG_HOME` を空の `home` に向け、`GIT_CONFIG_SYSTEM` を目印 (`hermetic.system=read`) の file に
    向ける。`home` には空の `.gitconfig` を置く: global の config が無いと `git config --global --list` は
    exit 128 になり、空の global を `(0, "")` で見られないため (実測)。`GIT_CONFIG_NOSYSTEM` を立てない
    のが要点 (モジュール docstring)。`GIT_CONFIG_SYSTEM` は git 2.32 以上。
    この床が外す / 向け直すものを外し損ねないことは `TestTheIsolatedEnvStopsNothing` が見る。それはこの関数の
    直後の env なので、この床を使う側が当てる側を呼ぶまでに足した値は、使う側が当てる側の直前で見る
    (`assert_the_floor_stops_nothing`、mixin の `query` / `launched_env`)。
    """
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_") or n in OUTER_REPO_ENV_NAMES]:
        del os.environ[name]
    system = os.path.join(home, "system.gitconfig")
    Path(system).write_text(SYSTEM_MARKER, encoding="utf-8")
    Path(home, ".gitconfig").write_text("", encoding="utf-8")
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_SYSTEM": system})


def assert_the_floor_stops_nothing(case: unittest.TestCase, cwd: str) -> None:
    """当てる側を呼ぶ直前の床 (今の `os.environ`) が、当てる側の値を持たないこと。

    `TestTheIsolatedEnvStopsNothing` は `isolate_git_config` の直後しか見ないので、wrapper の中
    (`isolate_git_config(...)` から当てる側の呼び出しまで) で足した値は、ここで見る。見るのは他の床と同じ
    `git_in` で、自動 maintenance の起動を止める設定 (`maintenance.auto`) が無いこと、global が空であること、
    system の目印が読めること (`GIT_CONFIG_NOSYSTEM` が無い)。`GIT_TRACE2_EVENT` は除いて起動する (この確認の
    git を、床が数える trace に載せない)。
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


class GitLaunch:
    """spy が捕まえた git の起動 1 件。起動の**時点**の値だけを持つ。"""

    def __init__(self, explicit_env: dict | None, process_env: dict, argv: list | None = None):
        self.argv = list(argv or [])  # 起動した argv (`["git", "init", ...]`)
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


def make_repo_with_one_commit(repo: str) -> None:
    """helper だけで repo を作り、commit まで進める (自動 maintenance が起動しうる最小の操作)。"""
    _testutil.init_repo(repo)
    Path(repo, "seed.txt").write_text("seed\n", encoding="utf-8")
    _testutil.git(["add", "seed.txt"], repo)
    _testutil.git(["commit", "-qm", "init"], repo)


class TestHelpersStopBackgroundMaintenance(unittest.TestCase):
    """テストクラスが env を patch していなくても、repo を作る helper 自身が止める。

    「patch していない」状態は `isolate_git_config` で作る。他のテストの patch 漏れや、開発者の
    shell / `~/.gitconfig` の値に左右されないため。

    見るのは**起動された git の挙動** (maintenance / gc の子が 0 件) で、helper が後から問い合わせた
    設定値ではない。`init_repo` の commit だけが env を持たずに起動されても、問い合わせ
    (`_testutil.git` 経由) は env を足し直すので、値を見るテストでは気付けない。
    """

    def _spawned_by_helpers(self, *, with_fixture: bool) -> list[list[str]]:
        """helper が repo を作って commit する間に起動された maintenance / gc の argv。

        `with_fixture=False` なら、この呼び出しの間だけ `HERMETIC_GIT_ENV` の `GIT_CONFIG_GLOBAL` を
        空の config file に上書きする。helper が渡す設定のうち fixture を外して、残りの経路 (env) だけで
        止まるかを見るために使う。

        上書きが helper の git に届いたことも前提として確かめる。helper が `HERMETIC_GIT_ENV` を
        呼び出しのたびに読まない形 (初回に固めたコピーを使うなど) に変わると、上書きが届かず
        env 経路の床が黙って空になるため。届いたかは**起動の引数の env** (`explicit_env`) で見る:
        `os.environ` の patch で届ける形に変わると、起動の時点の `os.environ` が答えを持ち、
        外側の env が床の検査を埋めるため。`all` ではなく `any` で見るのは、commit だけが helper を
        迂回する変異でも前提は満たしたまま、maintenance の起動の assertion で落とすため
        (起動した git の全部が env を持つことは `test_every_git_launched_by_the_helpers_carries_the_env`)。

        床 (`isolate_git_config` の後にここで足した env を含む) が当てる側の値を持たないことは、helper を呼ぶ直前に
        見る (`assert_the_floor_stops_nothing`)。
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

        これが成り立たない環境 (git の版で起動の形が変わった等) では、この file の「0 件」は何も
        見ていない。後始末と重ならないよう、背景へ切り離さない設定 (autoDetach=false) だけは渡す。
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
            # 外側の repo / config / template を指す変数だけ外す (外側の env に `GIT_DIR` があると、この直接の起動が
            # 外側の repo に書く)。止める設定は渡したくないので `hermetic_env()` ではなくここで外す
            env = {k: v for k, v in os.environ.items() if k not in OUTER_REPO_ENV_NAMES}
            for args in (
                ["init", "-q"],
                ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                 "commit", "--allow-empty", "-qm", "x"],
            ):
                subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
            self.assertTrue(os.path.isdir(os.path.join(repo, ".git")), "前提: repo が自分の場所に作られている")
            events = trace_events(trace)
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        self.assertNotEqual(spawned_maintenance(events), [])

    def test_every_git_launched_by_the_helpers_carries_the_env(self):
        """repo を作る helper (`init_repo` / `git`) が起動する git の全部が、`HERMETIC_GIT_ENV` の全項目を
        起動の引数 (`env=`) で持つこと。

        trace の床は maintenance を起動する git (commit など) しか見ない。`init` / `config` / `add` を 1 つだけ
        env を渡さずに起動する形は、maintenance の起動を数える床には差が出ないが、開発者の global の
        `init.templateDir` や `core.hooksPath` を読みうる (`isolate_git_config` の HOME に置くと、`init` の
        template がコピーされ、`defaultObjectFormat` も効く)。そこで起動を全件記録して、`any` ではなく
        `all` で見る。前提として、起動の件数と種類 (init / config 3 件 / add / commit の 6 件) を確かめる
        (記録が空や一部だと、`all` は何も見ていない)。

        床が当てる側の値を持たないことは、helper を呼ぶ直前に見る (`assert_the_floor_stops_nothing`)。この床の
        検査は記録した env の値を key ごとに比べるので、床が `HERMETIC_GIT_ENV` のどの key も同じ値で持たない
        ことも key ごとに見る (3 つの問い合わせは `GIT_CONFIG_NOSYSTEM` の値までは見ないので、それだけでは
        床が同じ値を持っても通る)。
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


class TestPlainBareOriginStartsNoMaintenance(unittest.TestCase):
    """repo 自身の config に設定が無い bare repo (`git init --bare` を直接呼んだもの) へ push しても、
    受け側 (`receive-pack`) が自動 maintenance を起動しないこと。

    `git push` がローカルの path へ送るとき、受け側は repo 用の env (`GIT_CONFIG_COUNT` など) を
    外されて起動する。env の設定だけだと、設定の無い bare repo では maintenance が起動する。外されない
    `GIT_CONFIG_GLOBAL` の fixture が止めていることを、起動された子プロセスで見る。helper を迂回した
    git が開発者の `~/.gitconfig` を読まないよう、global / system の config は差し替える
    (`isolate_git_config`)。この suite のテスト本体は push しない (この床だけが push する)。
    この床が守る当てる側は受け側の `receive-pack` なので、床が当てる側の値を持たないことは push の直前に
    見る (`assert_the_floor_stops_nothing`。repo と bare repo を作る間に足した値も含む)。
    """

    def test_push_into_a_plain_bare_repo(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            repo = os.path.join(tmp, "work")
            os.makedirs(repo)
            make_repo_with_one_commit(repo)
            plain = os.path.join(tmp, "plain.git")
            _testutil.git(["init", "--bare", "-q", plain], tmp)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            assert_the_floor_stops_nothing(self, tmp)
            _testutil.git(["push", "-q", plain, "HEAD:refs/heads/main"], repo)
            events = trace_events(trace)
            sent = _testutil.git(["rev-parse", "HEAD"], repo).stdout
            received = _testutil.git(["rev-parse", "refs/heads/main"], plain).stdout
        self.assertEqual(received, sent, "前提: push が成功し、受け側に届いている")
        self.assertIn(
            "receive-pack", command_names(events), "前提: 受け側まで trace に載っている (空の床にしない)"
        )
        self.assertEqual(spawned_maintenance(events), [])


class TestTheIsolatedEnvStopsNothing(unittest.TestCase):
    """`isolate_git_config` だけで作った env (当てる側が何も当てていない床) が、当てる側の値を持たないこと。

    `isolate_git_config` は、上の trace の床 (`TestHelpersStopBackgroundMaintenance` など) が直接使う床で、
    `_HermeticConfigChecks` の床もこの上に作る。ここが当てる側の値 (止める側の `GIT_CONFIG_COUNT`、fixture を
    指す `GIT_CONFIG_GLOBAL`、`GIT_CONFIG_NOSYSTEM`) を持つと、当てる側がその値を落としても床が埋めて、
    それを使う床が黙って通る。開発者の global の config (外側の `HOME` の `.gitconfig` と
    `XDG_CONFIG_HOME` の `git/config`) を読んでしまっても同じ。そこで、床を作る前の env にこれらを置いてから
    `isolate_git_config` を呼び、床が組んだ env (`self.env`) だけで起動した git で、どれも見えないこと
    (4 設定は未設定、global は空、system の目印は読める) を、他の床と同じ `git_in` で確かめる。
    外側の repo / config / template を指す変数が残っていないことも見る。開発者の config はこのテストが床を
    作る前に自分で置くので、この検査は実行者の HOME に左右されない。
    見るのは `isolate_git_config` の直後の env だけなので、この床を使う側が当てる側を呼ぶまでに足した値は、
    使う側が当てる側の直前で見る (`assert_the_floor_stops_nothing`、mixin の `query` / `launched_env`)。
    """

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        # 外側の env に、床が外す / 向け直すべきものを置いてから床を作る (床が外し損ねると、下の検査が見える)
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

        どちらにも止める側の設定を 1 つずつ書く (`HOME` 側は `maintenance.auto`、`XDG_CONFIG_HOME` 側は
        `gc.auto`)。床が向け直し損ねると、`HOME` は `maintenance.auto` の問い合わせと `--global --list` に、
        `XDG_CONFIG_HOME` は `gc.auto` の問い合わせに出る。後者は `--global --list` には出ない: `~/.gitconfig`
        がある間、`--global --list` は XDG の config を読まない (実測)。そのため、4 設定の問い合わせでも見ている。
        """
        dev_home = os.path.join(self.tmp, "dev-home")
        dev_xdg = os.path.join(self.tmp, "dev-xdg")
        os.makedirs(dev_home)
        os.makedirs(os.path.join(dev_xdg, "git"))
        Path(dev_home, ".gitconfig").write_text("[maintenance]\n\tauto = false\n", encoding="utf-8")
        Path(dev_xdg, "git", "config").write_text("[gc]\n\tauto = 0\n", encoding="utf-8")
        return {"HOME": dev_home, "XDG_CONFIG_HOME": dev_xdg}

    def test_the_outer_values_were_there_before_isolating(self):
        """前提: 床を作る前の env に、外すべきものがある (置き損ねると、下の「外れている」は何も見ていない)。

        見るのは `self.outer` (床を作る直前の env) で、`GIT_CONFIG_NOSYSTEM` / fixture を指す
        `GIT_CONFIG_GLOBAL` / 止める側の `GIT_CONFIG_COUNT`、外側の repo / config / template を指す変数、
        開発者の global の config に見立てた 2 つの file (中身がある)。
        """
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
        """`outer_repo_env` が置く変数が、名前の一覧 (`OUTER_REPO_ENV_NAMES`) の全部であること。

        一覧に無い変数は、`isolate_git_config` や当てる側が外し損ねても、起動の仕方ごとの検査
        (`_OuterRepoEnvChecks`) が見ない。
        """
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
        """env を渡さない起動は「引数なし」、渡す起動はその値、として記録されること。

        spy が記録に値を足す形 (`HERMETIC_GIT_ENV` を混ぜるなど) に変わると、env を渡さない helper も
        「届いた」と読めて、spy の床の前提が黙って通る。渡す env は `isolate_git_config` の床の上に作り、
        当てる側のどの key も同じ値で持たないことを key ごとに前提として確かめる (同じ値で持つ key は、spy が
        その key を足しても記録が変わらない)。
        """
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
        """`_testutil.git` が env を起動の引数で渡し、`os.environ` を patch しないこと。

        helper が自分の周りに `os.environ` の patch を張る形に変わると、起動の時点の `os.environ` が
        答えを持ち、floor の検査 (外側の env が空の状態で、当てる側の値が届くか) が空になる。
        """
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

    起動の仕方 (`launch`) は継承先が決める。テストは `query` を通して問い合わせる。`setUp` は「patch していない」
    状態を先に作ってから (`isolate_git_config`)、外側の env として止めない側の値を置き、その env を
    `self.floor_env` に控えてから、`super().setUp()` で基底クラスがあれば env を当てさせる。床が当てる側の値を
    持つと、当てる側の当て損ねを床が埋めてしまうので、床は当てる側の値を持たない
    (`test_the_floor_alone_stops_nothing` で確かめる。床は外側の値を足しているが、その値は止めない側であり、
    global は空の file を指すので、当て損ねは埋まらない)。外側の環境 (開発者の shell など) に
    `GIT_CONFIG_NOSYSTEM` があっても、`isolate_git_config` が先に外すので、当て損ねは隠れない。

    自己確認は setUp のスナップショット (`self.floor_env`) を見るので、当てた直後の env (`self.applied_env`) から
    問い合わせの時点まで変わっていないことを、`query` の中で `launch` の前後に確かめる。setUp の後段や継承先の
    setUp、継承先の `launch` の中で足した値は、自己確認からは見えず、問い合わせには届くため。継承先は `launch`
    だけを実装し、`query` と `assert_env_unchanged_since_applied` は上書きしない (上書きしていないことは `setUp` で
    確かめる)。`launch` の中身が当てる側の値を自分で足す形 (`os.environ` を範囲を限って変えて戻す・git に渡す env
    に足す・床の env が指す file の中身を書き換える) は、前後の確認では見えない (残り)。

    外側の env には、止めない側の値 (`GIT_CONFIG_GLOBAL` = 空の file、`GIT_CONFIG_COUNT` で
    `maintenance.auto=true`) を置く。当てる側 (定数 / helper / 基底クラス) は外側の env に勝つこと。
    外側の env が空のままだと、helper が外側の env を後から混ぜる向き
    (`{**HERMETIC_GIT_ENV, **os.environ}`) に変わっても、混ぜる値が無いので気付けない。

    外側の env には `GIT_CONFIG_PARAMETERS` (`maintenance.auto=true`) も置く (`git -c` の配下から流すと入る)。
    これは `GIT_CONFIG_COUNT` に勝つので、`OUTER_GIT_LEAK_ENV` から外れると maintenance が黙って復活する。
    置かない継承先は `outer_config_parameters = False` にする (理由は `TestConstantAlone`)。
    """

    outer_config_parameters = True

    def launch(self, args: list[str]) -> tuple[int, str]:
        """継承先が使う起動の仕方で git を起動して `(returncode, stdout)` を返す。cwd は `self.repo`。"""
        raise NotImplementedError

    def assert_env_unchanged_since_applied(self) -> None:
        self.assertEqual(dict(os.environ), self.applied_env, "前提: 当てる側を当てた後に env が変わっていない")

    def query(self, args: list[str]) -> tuple[int, str]:
        """`launch` で git を起動する。その前後で、当てる側を当てた後に env が変わっていないことを確かめる。"""
        self.assert_env_unchanged_since_applied()
        try:
            return self.launch(args)
        finally:
            self.assertEqual(dict(os.environ), self.applied_env, "前提: 起動の中で env を変えていない")

    def setUp(self) -> None:
        # 継承先が `query` を上書きすると、上の確認を通らずに問い合わせる
        self.assertIs(type(self).query, _HermeticConfigChecks.query, "前提: 継承先が query を上書きしていない")
        self.assertIs(
            type(self).assert_env_unchanged_since_applied,
            _HermeticConfigChecks.assert_env_unchanged_since_applied,
            "前提: 継承先が assert_env_unchanged_since_applied を上書きしていない",
        )
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
        # 当てる側が当てる前の env。床が当てる側の値を持たないことの確認に使う
        self.floor_env = dict(os.environ)
        super().setUp()
        # 当てる側を当てた直後の env (setUp で当てない継承先では床と同じ)。問い合わせの前に、これから
        # 変わっていないことを確かめる (自己確認が見るスナップショットと、問い合わせが見る env の時点を揃える)
        self.applied_env = dict(os.environ)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _testutil.init_repo(self.repo)
        for key, value in OPPOSITE.items():
            _testutil.git(["config", key, value], self.repo)

    def test_the_floor_alone_stops_nothing(self):
        """床の env (`self.floor_env`) だけで起動した git に、当てる側の値が見えないこと。床が置いたはずの止めない
        側の値は見えること。

        床が当てる側の値 (`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の
        `GIT_CONFIG_COUNT`) を持つ形に戻ると、当てる側の当て損ねを床が埋めて、下の検査が黙って通る。逆に床が
        止めない側の値を置き損ねると、混ぜる向きの逆転 (外側の env が勝つ) に気付けない。どちらも
        `self.floor_env` を、他の検査と同じ `git_in` で見る (床の組み立てを写した別の env を作らない)。
        `self.floor_env` は setUp のスナップショットなので、当てた直後の env から問い合わせの時点まで変わって
        いないことは、`query` が `launch` の前に確かめる。
        """
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
        # repo の外では env の値だけが見える。床が置いた止めない側の値 (maintenance.auto=true) だけがあり、残りは無い
        for key in EXPECTED:
            with self.subTest(outside_a_repo=key):
                want = (0, "true\n") if key == "maintenance.auto" else (1, "")
                self.assertEqual(git_in(self.floor_env, self.tmp, ["config", "--get", key]), want)
        # global は空 (fixture を指していない)。system の目印は読める (`GIT_CONFIG_NOSYSTEM` が無い)
        self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--global", "--list"]), (0, ""))
        self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--get", "hermetic.system"]), (0, "read\n"))

    def test_env_beats_the_repos_own_config(self):
        """`GIT_CONFIG_COUNT` が効いていること。

        repo 自身の config に止めない側の値を置いてある。env は repo 自身の config より優先され、
        global の fixture は負けるので、止める側の値が見えるのは env が効いているときだけ。外側の env にも
        止めない側の値 (`maintenance.auto=true`) があるので、当てる側が外側に勝たないと落ちる。
        """
        for key, expected in EXPECTED.items():
            with self.subTest(key=key):
                rc, out = self.query(["config", "--get", key])
                self.assertEqual((rc, out.strip()), (0, expected))

    def test_global_is_the_fixture_only(self):
        """global として fixture を読み、それだけを読むこと (`--global --list` が 5 設定の完全一致)。

        キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
        将来足された `core.hooksPath` など、テストの前提を変えるもの) が増えても通る。fixture は
        git が読む global なので、増えると git を起動する全テストに効く。外側の env の
        `GIT_CONFIG_GLOBAL` (空の file) に勝つことも見る。
        """
        rc, out = self.query(["config", "--global", "--list"])
        self.assertEqual((rc, sorted(out.splitlines())), (0, FIXTURE_LINES))

    def test_system_config_is_not_read(self):
        """system の config を読まないこと (`GIT_CONFIG_SYSTEM` が指す目印が読まれない)。"""
        # 前提: 床の env (`GIT_CONFIG_NOSYSTEM` が無い) では目印が読める。これが成り立たない環境 (git が古い等) では、
        # 下の「読まれない」は何も見ていない
        self.assertEqual(
            git_in(self.floor_env, self.repo, ["config", "--get", "hermetic.system"]),
            (0, "read\n"),
            "前提: GIT_CONFIG_NOSYSTEM が無ければ system の目印は読める (空の床にしない)",
        )
        self.assertEqual(self.query(["config", "--get", "hermetic.system"]), (1, ""))


class TestConstantAlone(_HermeticConfigChecks, unittest.TestCase):
    """`HERMETIC_GIT_ENV` の定数だけで git を起動したとき (helper も基底クラスも通さない)。

    外側の env に `GIT_CONFIG_PARAMETERS` を置かない。外側の `GIT_CONFIG_PARAMETERS` は `hermetic_env()` と
    基底クラスが外す (`OUTER_GIT_LEAK_ENV`) もので、定数だけでは外せない。定数だけの起動に置くと、
    `GIT_CONFIG_COUNT` に勝って、外す仕組みの無いこの起動が必ず落ちる。外す側の検査は helper と基底クラス
    の 2 クラスと `_OuterRepoEnvChecks` が持つ。
    """

    outer_config_parameters = False

    def launch(self, args: list[str]) -> tuple[int, str]:
        return git_in({**os.environ, **_testutil.HERMETIC_GIT_ENV}, self.repo, args)


class TestHelperLaunchedGit(_HermeticConfigChecks, unittest.TestCase):
    """repo を作る helper (`_testutil.git`) が起動する git。helper が毎回 env を足すので、
    env を patch していない状態でも、定数と同じ設定が見えること。"""

    def launch(self, args: list[str]) -> tuple[int, str]:
        res = _testutil.git(args, self.repo, check=False)
        return res.returncode, res.stdout.decode("utf-8").replace("\r\n", "\n")


class TestHookLaunchedGit(_HermeticConfigChecks, HermeticGitTestCase):
    """hook (製品コード) が起動する git。基底クラス `HermeticGitTestCase` が当てた env を継承すること。

    `checker._run_git_raw` は env を渡さず `os.environ` を継承するので、基底クラスが patch した env が
    そのまま見える。ここが外れる (基底クラスが当て損ねる / hook が env を絞って起動する) と、hook の git
    だけ、開発者の global / system の config と自動 maintenance の既定に戻る。

    `_run_git` ではなく `_run_git_raw` を使う: `_run_git` は非ゼロ終了を空のリストにするので、
    git の失敗が値の不一致として見えなくなる。起動できなかった (`None`) ときは前提の assertion で落とす。

    外側の環境に `GIT_CONFIG_NOSYSTEM` があっても、`setUp` が先に外してから基底クラスに当てさせるので、
    当て損ねは隠れない。
    """

    def launch(self, args: list[str]) -> tuple[int, str]:
        res = checker._run_git_raw(args, self.repo)
        self.assertIsNotNone(res, "前提: hook の git を起動できた")
        return res.returncode, res.stdout


class _OuterRepoEnvChecks:
    """外側の env に、repo / config / template を指す変数 (`outer_repo_env`) があっても、起動の仕方ごとに git に
    届かないこと。

    `setUp` は床 (`isolate_git_config`) を作り、外側の repo を作ってから、その変数を置く。置いた env を
    `self.floor_env` に控えてから、`super().setUp()` で基底クラスがあれば env を当てさせる (基底クラスは
    `setUp` で env を張るので、置くのはその前)。床が置いたはずの変数が `self.floor_env` にあることも
    確かめる (置き損ねた床では、下の検査が何も見ていない)。

    この自己確認は setUp のスナップショット (`self.floor_env`) を見るので、当てた直後の env (`self.applied_env`)
    から問い合わせの時点まで変わっていないことを、`launched_env` の中で起動の前後に確かめる。setUp の後段や
    継承先の setUp、継承先の起動の中身で外した変数は、自己確認からは見えず、「届かない」を素通りさせるため。
    継承先は起動の中身 (`launch_and_capture_env`) だけを実装し、`launched_env` は上書きしない (上書きしていない
    ことは `setUp` で確かめる)。`launched_env` を通らずに helper を直接呼ぶテストは、先頭と末尾で
    `assert_env_unchanged_since_applied` を呼ぶ (上書きしていないことも `setUp` で確かめる)。起動の中身が
    当てる側の値を自分で足す形 (範囲を限って env を変えて戻す・git に渡す env に足す) は、前後の確認では見えない
    (残り)。
    """

    def launch_and_capture_env(self) -> dict:
        """継承先が使う起動の仕方で git を起動したとき、その git が持つ env。"""
        raise NotImplementedError

    def assert_env_unchanged_since_applied(self) -> None:
        self.assertEqual(dict(os.environ), self.applied_env, "前提: 当てる側を当てた後に env が変わっていない")

    def launched_env(self) -> dict:
        """`launch_and_capture_env` で git を起動する。その前後で、当てる側を当てた後に env が変わっていないことを
        確かめる。"""
        self.assert_env_unchanged_since_applied()
        try:
            return self.launch_and_capture_env()
        finally:
            self.assertEqual(dict(os.environ), self.applied_env, "前提: 起動の中で env を変えていない")

    def setUp(self) -> None:
        # 継承先が `launched_env` を上書きすると、上の確認を通らずに起動する
        self.assertIs(
            type(self).launched_env, _OuterRepoEnvChecks.launched_env, "前提: 継承先が launched_env を上書きしていない"
        )
        self.assertIs(
            type(self).assert_env_unchanged_since_applied,
            _OuterRepoEnvChecks.assert_env_unchanged_since_applied,
            "前提: 継承先が assert_env_unchanged_since_applied を上書きしていない",
        )
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        isolate_git_config(self.tmp)
        self.other = os.path.join(self.tmp, "outer")
        os.makedirs(self.other)
        # 外側の repo (変数を置く前に作る)。helper (`_testutil.git`) が外し損ねる変異でも、ここで床が crash
        # しないよう、外側の変数を外した env で作る
        build_env = {k: v for k, v in os.environ.items() if k not in OUTER_REPO_ENV_NAMES}
        build_env.update(_testutil.HERMETIC_GIT_ENV)
        Path(self.other, "seed.txt").write_text("seed\n", encoding="utf-8")
        for args in (
            ["init", "-q", "--initial-branch=main"],
            ["config", "user.name", "t"],
            ["config", "user.email", "t@t"],
            ["config", "commit.gpgsign", "false"],
            ["add", "seed.txt"],
            ["commit", "-qm", "outer"],
        ):
            self.assertEqual(git_in(build_env, self.other, args)[0], 0, f"前提: 外側の repo を作れる: {args}")
        os.environ.update(outer_repo_env(self.tmp))
        self.floor_env = dict(os.environ)
        super().setUp()
        # 当てる側を当てた直後の env (setUp で当てない継承先では床と同じ)。`launched_env` の前に、これから
        # 変わっていないことを確かめる (自己確認が見るスナップショットと、問い合わせが見る env の時点を揃える)
        self.applied_env = dict(os.environ)

    def test_the_floor_holds_the_outer_variables(self):
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, self.floor_env, "前提: 床が置いた変数が、当てる側が当てる前の env にある")

    def test_none_of_the_outer_variables_reaches_git(self):
        env = self.launched_env()
        # 前提: 起動した git の env を記録できている (空の env では、下の「届かない」が素通りする)。床が置いた HOME で見る
        self.assertEqual(env.get("HOME"), self.floor_env["HOME"], "前提: 起動した git の env を記録できている")
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, env)


class TestOuterRepoEnvAndTheHelpers(_OuterRepoEnvChecks, unittest.TestCase):
    """repo を作る helper (`_testutil.git` / `init_repo`)。"""

    def launch_and_capture_env(self) -> dict:
        with record_git_launches() as launches:
            _testutil.git(["--version"], self.tmp)
        self.assertEqual(len(launches), 1, "前提: helper が git を 1 回起動している")
        return launches[0].effective_env

    def test_the_helpers_leave_the_outer_repo_alone(self):
        """外側に別の repo を指す `GIT_DIR` などがあっても、helper が repo を作る間、その repo を変えないこと。

        外さないと、`init` / `config` / `commit` が外側の repo に書き込む (config に user.name などが入り、
        commit も増える)。外側の repo の全 file の中身が前後で一致すること、旧来の `GIT_CONFIG` が指す file が
        作られないこと (`git config` の書き込み先になる) で見る。外側の変数が届いた helper の git が失敗する形も
        検出のうち: 例外のまま crash させず、失敗として報告する。
        """
        self.assert_env_unchanged_since_applied()
        before = tree_state(Path(self.other))
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        try:
            make_repo_with_one_commit(work)
        except subprocess.CalledProcessError as e:
            self.fail(f"helper の git が失敗した: {' '.join(map(str, e.cmd))}\n{e.stderr!r}")
        self.assert_env_unchanged_since_applied()
        self.assertTrue(os.path.isdir(os.path.join(work, ".git")), "前提: repo が自分の場所に作られている")
        self.assertEqual(tree_state(Path(self.other)), before)
        self.assertIn("GIT_CONFIG", self.floor_env, "前提: 床が旧来の GIT_CONFIG を置いている")
        self.assertFalse(os.path.exists(self.floor_env["GIT_CONFIG"]))

    def test_the_outer_template_does_not_reach_the_helpers(self):
        """外側の `GIT_TEMPLATE_DIR` が指す template が、helper の `init` / `commit` に効かないこと。

        template の `hooks/pre-commit` は失敗し (走れば helper の commit が落ちる)、`info/exclude` は
        `seed.txt` を除外する (効けば commit から外れる)。前提として、`OUTER_REPO_ENV_NAMES` を外して template
        だけを戻した env では、この template が `git init` に写ることを確かめる (写らない git の版では、
        下の「走らない」「外れない」は何も見ていない)。
        """
        self.assert_env_unchanged_since_applied()
        self.assertIn("GIT_TEMPLATE_DIR", self.floor_env, "前提: 床が外側の GIT_TEMPLATE_DIR を置いている")
        template = Path(self.floor_env["GIT_TEMPLATE_DIR"])
        ran = Path(self.tmp, "template-hook-ran")
        (template / "hooks").mkdir(parents=True)
        (template / "info").mkdir()
        hook = template / "hooks" / "pre-commit"
        write_lf(hook, f"#!/bin/sh\necho ran >> '{ran.as_posix()}'\nexit 1\n")
        hook.chmod(0o755)
        write_lf(template / "info" / "exclude", "seed.txt\n")
        reaching = {k: v for k, v in self.floor_env.items() if k not in OUTER_REPO_ENV_NAMES}
        reaching["GIT_TEMPLATE_DIR"] = str(template)
        probe = os.path.join(self.tmp, "probe")
        self.assertEqual(git_in(reaching, self.tmp, ["init", "-q", probe]), (0, ""))
        exclude = Path(probe, ".git", "info", "exclude")
        self.assertEqual(
            exclude.read_text(encoding="utf-8") if exclude.exists() else None,
            "seed.txt\n",
            "前提: 外側の template は、届けば git init に写る",
        )
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        try:
            make_repo_with_one_commit(work)
        except subprocess.CalledProcessError as e:
            self.fail(f"helper の git が失敗した: {' '.join(map(str, e.cmd))}\n{e.stderr!r}")
        self.assert_env_unchanged_since_applied()
        self.assertFalse(ran.exists(), "外側の template の pre-commit が helper の commit で走った")
        self.assertEqual(_testutil.git(["ls-files"], work).stdout.split(), [b"seed.txt"])


class TestOuterRepoEnvAndTheBaseClass(_OuterRepoEnvChecks, HermeticGitTestCase):
    """hook (製品コード) を in-process で動かすテストの基底クラス (`HermeticGitTestCase`)。hook の git は
    env を渡さず `os.environ` を継承するので、基底クラスが外さないと外側の変数が届く。"""

    def launch_and_capture_env(self) -> dict:
        with record_git_launches() as launches:
            res = checker._run_git_raw(["--version"], self.tmp)
        self.assertIsNotNone(res, "前提: hook の git を起動できた")
        self.assertEqual(len(launches), 1, "前提: hook の git を 1 回起動できた")
        return launches[0].effective_env


_SUBPROCESS_LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def launches_git_literally(node: ast.AST) -> bool:
    """`subprocess.run(["git", ...])` のように、argv の先頭を literal の `"git"` で書いた git の起動か。

    見つけるのは、`subprocess` を名前で参照した呼び出しのうち、argv (位置引数の先頭か `args=`) が
    次のどれかの形だけ: 先頭が literal の `"git"` の list / tuple、その list / tuple を左辺に置いた
    連結 (`["git"] + rest`)、先頭の語が `git` の文字列 (`shell=True` で渡す `"git commit ..."`)。
    argv を変数に入れて渡す形、2 段以上の連結 (`["git"] + a + b`)、f-string、`from subprocess import run`
    のような別名や `subprocess.getoutput` / `os.system` での起動は見つけられない (目印を足すのは
    その形が出てきてから)。
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
        argv = argv.left  # ["git"] + rest
    if isinstance(argv, ast.Constant) and isinstance(argv.value, str):
        return argv.value.split()[:1] == ["git"]  # "git commit ..." (shell=True)
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
    helper の挙動しか見ないので、迂回した起動は別の検査で拾う。このファイル自身は、helper を通さない
    git の挙動を見るために直接起動するので対象に含めない。
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
            {"test_checker.py", "test_main.py"},
            set(scanned),
            f"前提: repo を作る module を走査している (対象 0 件で黙って通らない): {scanned}",
        )
        self.assertEqual(
            offenders, [], "git の直接の起動は `_testutil.git` に置き換える (env を足すため)"
        )

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
