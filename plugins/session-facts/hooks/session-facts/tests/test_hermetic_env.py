"""テストが作る git repo と、製品コード (`core.git`) が起動する git で、自動 gc / maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に `TemporaryDirectory` の後始末が走ると、`Directory not empty` で
落ちうる (object の hash 次第で偶発的)。直接は検出できないので、原因の側に床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、`init_repo` の
  中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける。前提として、commit / receive-pack が trace に載っていること (空の床にしない) と、上書き
  した env が helper の git に**起動の引数として**届いていること (spy) も確かめる。陽性対照として、
  止める設定が無い commit では起動が trace に見えることも確かめる (見えない git の版では「0 件」は
  何も見ていない)
- **設定の出どころ別** (`_HermeticConfigChecks`): 止める経路は env の `GIT_CONFIG_COUNT` と global の
  fixture と system の無効化 (`GIT_CONFIG_NOSYSTEM`) で、有効値だけを見ると 1 本が欠けても残りが
  埋めて通ってしまう。そこで 1 本ずつ別の検査で見る。起動の仕方 (定数だけ / helper / 製品コードの git)
  ごとに同じ 3 本を流すので、env を当てる各点 (定数・helper・基底クラス) で、`COUNT` だけ・
  `NOSYSTEM` 抜き・`GLOBAL` 抜きのどれが起きても、どれかが assertion で落ちる。外側の env には
  止めない側の値を置き、当てる側がそれに勝つことも見る
  - `GIT_CONFIG_COUNT`: repo 自身の config に反対の値を置き、git が見る値が止める側であること
    (env は repo 自身の config より優先される。fixture は負ける)
  - `GIT_CONFIG_GLOBAL`: `git config --global --list` が fixture の 5 設定と完全一致すること
  - `GIT_CONFIG_NOSYSTEM`: system の config の代わりに置いた目印が読まれないこと
- **外側の repo / config / template を指す変数** (`_OuterRepoEnvChecks`): 外側の env に `GIT_DIR` や
  `GIT_CONFIG_PARAMETERS`、`GIT_TEMPLATE_DIR` などがあっても (git の hook や `git -c` の配下から suite を
  流すと入る)、helper の git と基底クラスが起動する git に届かないこと。届くと helper の `init` / `config` /
  `commit` が外側の repo に書き込み、`GIT_CONFIG_PARAMETERS` は `GIT_CONFIG_COUNT` に勝って maintenance を
  黙って復活させ、外側の template の pre-commit が helper の commit で走る。外側の repo の中身が前後で
  一致することと、起動した git の env に変数が無いことで見る。helper が起動する git の**全件**が
  `HERMETIC_GIT_ENV` の全項目を持つことも、起動を記録して見る (`init` だけ env を足さない形は、
  maintenance を数える床には差が出ないが、開発者の global の `init.templateDir` などを読む)
- **直接の起動**: test module が `subprocess` で git を直接起動していないこと。repo を作る git が
  helper を迂回すると、上の床は helper しか見ないので気付けない。走査した module に repo を作る
  module が含まれること (対象 0 件で黙って通らない) と、入れ子の呼び出しも見ることも確かめる
- **基底クラス**: `_make_repo` を使うテストクラスは `HermeticGitTestCase` を継承すること。製品コードが
  起動する git は env を渡さず `os.environ` を継承するので、継承が抜けたクラスだけ開発者の global の
  config を読む
- **測る道具の自己確認**: 起動を記録する spy (`record_git_launches`) は、起動の**時点**の env を
  記録する。spy が記録に値を足す形に変わると、env を渡さない helper も「届いた」と読めて前提が
  黙って通るので、env を渡さない起動と渡す起動を spy に通して、記録が実際の env と一致することを
  確かめる。helper が `os.environ` の patch で env を届ける形 (起動の引数には渡さない) に変わった
  場合も、床の外側の env が答えを持つことになるので、起動の時点の `os.environ` に答えが無いことを
  確かめる

「patch していない」状態は `isolate_git_config` で作る。`GIT_CONFIG_*` を外し、global を空にし、
system の config を目印の file に向けるが、**`GIT_CONFIG_NOSYSTEM` は床の側で立てない**。床の側で
立てると、helper・基底クラス・定数が `GIT_CONFIG_NOSYSTEM` / `GIT_CONFIG_GLOBAL` を当て損ねても
(部分適用)、床が埋めて通ってしまう。目印は `NOSYSTEM` が効いていなければ読めるので、当て損ねが
見える。開発者の本物の system / global の config (そこに `maintenance.auto=false` があると、
迂回した git も maintenance を起動せず、床が黙って通る) は、目印と空の HOME で置き換わるので読まれない。

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

from core import git as gitmod  # noqa: E402  (`_testutil` が sys.path に pkg ルートを足してから import する)

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
    """`env` で git を起動して `(returncode, stdout)`。"""
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    return res.returncode, res.stdout


def empty_file(directory: str) -> str:
    """空の config file を作ってパスを返す (global の fixture の代わりに指す。何も設定しない)。

    `os.devnull` ではなく実体のある空 file にするのは、Windows の `nul` を git が config として
    読めるかが git の版に依存しうるため。
    """
    path = os.path.join(directory, "empty.gitconfig")
    Path(path).write_text("", encoding="utf-8")
    return path


def isolate_git_config(home: str) -> None:
    """「patch していない」状態を作る。`mock.patch.dict(os.environ)` の中で呼ぶこと (環境を戻すため)。

    `GIT_CONFIG_*` (`GIT_CONFIG_NOSYSTEM` を含む) を外し、`HOME` / `XDG_CONFIG_HOME` を空の `home` に
    向け、`GIT_CONFIG_SYSTEM` を目印 (`hermetic.system=read`) の file に向ける。`GIT_CONFIG_NOSYSTEM` を
    立てないのが要点 (モジュール docstring)。`GIT_CONFIG_SYSTEM` は git 2.32 以上。
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
        迂回する変異でも前提は満たしたまま、maintenance の起動の assertion で落とすため。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            overrides = {} if with_fixture else {"GIT_CONFIG_GLOBAL": empty_file(tmp)}
            with mock.patch.dict(_testutil.HERMETIC_GIT_ENV, overrides):
                repo = os.path.join(tmp, "repo")
                os.makedirs(repo)
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
        (記録が空や一部だと、`all` は何も見ていない)。床が当てる側の値を key ごとに持たないことも、
        helper を呼ぶ前に見る。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
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
            _testutil.git(["push", "-q", plain, "HEAD:refs/heads/main"], repo)
            events = trace_events(trace)
            sent = _testutil.git(["rev-parse", "HEAD"], repo).stdout
            received = _testutil.git(["rev-parse", "refs/heads/main"], plain).stdout
        self.assertEqual(received, sent, "前提: push が成功し、受け側に届いている")
        self.assertIn(
            "receive-pack", command_names(events), "前提: 受け側まで trace に載っている (空の床にしない)"
        )
        self.assertEqual(spawned_maintenance(events), [])


class TestMeasuringTools(unittest.TestCase):
    """床の測り方 (spy と helper の env の渡し方) 自体が空でないこと。"""

    def test_the_spy_records_the_env_at_launch_time(self):
        """env を渡さない起動は「引数なし」、渡す起動はその値、として記録されること。

        spy が記録に値を足す形 (`HERMETIC_GIT_ENV` を混ぜるなど) に変わると、env を渡さない helper も
        「届いた」と読めて、`_spawned_by_helpers` の前提が黙って通る。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            with record_git_launches() as launches:
                subprocess.run(["git", "--version"], capture_output=True, check=True)
                subprocess.run(
                    ["git", "--version"], capture_output=True, check=True, env={**os.environ, "MARK": "1"}
                )
        bare, explicit = launches
        self.assertIsNone(bare.explicit_env)
        self.assertNotIn("GIT_CONFIG_COUNT", bare.effective_env)
        self.assertNotIn("GIT_CONFIG_GLOBAL", bare.effective_env)
        self.assertEqual(explicit.explicit_env.get("MARK"), "1")
        self.assertNotIn("GIT_CONFIG_COUNT", explicit.explicit_env)

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
    """git が見る設定を、止める経路ごとに 1 本ずつ確かめる共通の検査。

    起動の仕方 (`query`) は継承先が決める。`setUp` は「patch していない」状態を先に作ってから
    (`isolate_git_config`)、`super().setUp()` で基底クラスがあれば env を当てさせる。床が先に global /
    system を空にしたり `GIT_CONFIG_NOSYSTEM` を立てたりすると、基底クラスの当て損ねを床が埋めて
    しまうので、床は外すだけで足さない。

    その間に、外側の env として止めない側の値 (`GIT_CONFIG_GLOBAL` = 空の file、`GIT_CONFIG_COUNT` で
    `maintenance.auto=true`) を置く。当てる側 (定数 / helper / 基底クラス) は外側の env に勝つこと。
    外側の env が空のままだと、helper が外側の env を後から混ぜる向き
    (`{**HERMETIC_GIT_ENV, **os.environ}`) に変わっても、混ぜる値が無いので気付けない。

    外側の env には `GIT_CONFIG_PARAMETERS` (`maintenance.auto=true`) も置く (`git -c` の配下から流すと入る)。
    これは `GIT_CONFIG_COUNT` に勝つので、`OUTER_GIT_LEAK_ENV` から外れると maintenance が黙って復活する。
    置かない継承先は `outer_config_parameters = False` にする (理由は `TestConstantAlone`)。
    """

    outer_config_parameters = True

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
        if self.outer_config_parameters:
            os.environ["GIT_CONFIG_PARAMETERS"] = "'maintenance.auto'='true'"
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _testutil.init_repo(self.repo)
        for key, value in OPPOSITE.items():
            _testutil.git(["config", key, value], self.repo)

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

        キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
        将来足された `core.hooksPath` など、テストの前提を変えるもの) が増えても通る。fixture は
        git が読む global なので、増えると git を起動する全テストに効く。
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
    """`HERMETIC_GIT_ENV` の定数だけで git を起動したとき (helper も基底クラスも通さない)。

    外側の env に `GIT_CONFIG_PARAMETERS` を置かない。外側の `GIT_CONFIG_PARAMETERS` は `hermetic_env()` と
    基底クラスが外す (`OUTER_GIT_LEAK_ENV`) もので、定数だけでは外せない。定数だけの起動に置くと、
    `GIT_CONFIG_COUNT` に勝って、外す仕組みの無いこの起動が必ず落ちる。外す側の検査は helper と基底クラス
    の 2 クラスと `_OuterRepoEnvChecks` が持つ。
    """

    outer_config_parameters = False

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
    """repo を作る helper (`_testutil.git`) が起動する git。helper が毎回 env を足すので、
    env を patch していない状態でも、定数と同じ設定が見えること。"""

    def query(self, args: list[str]) -> tuple[int, str]:
        res = _testutil.git(args, self.repo, check=False)
        return res.returncode, res.stdout.decode("utf-8").replace("\r\n", "\n")


class TestProductLaunchedGit(_HermeticConfigChecks, HermeticGitTestCase):
    """製品コード (`core.git.run`) が起動する git。基底クラス `HermeticGitTestCase` が当てた env を
    継承すること。

    `core.git.run` は env を渡さず `os.environ` を継承するので、基底クラスが patch した env がそのまま
    見える。ここが外れる (基底クラスが当て損ねる / 製品コードが env を絞って起動する) と、製品コードの
    git だけ、開発者の global / system の config と自動 maintenance の既定に戻る。

    外側の環境に `GIT_CONFIG_NOSYSTEM` があっても、`setUp` が先に外してから基底クラスに当てさせるので、
    当て損ねは隠れない。
    """

    def query(self, args: list[str]) -> tuple[int, str]:
        res = gitmod.run(["git", *args], Path(self.repo))
        return res.returncode, res.stdout


class TestProductLaunchedLsFiles(_HermeticConfigChecks, HermeticGitTestCase):
    """製品コードのもう 1 つの起動経路 (`core.git.git_ls_files`) が起動する git。`core.git.run` を通らず
    `subprocess.run` を直接呼ぶので、`run` だけが env を継承していても、こちらが env を絞る形に変わると
    気付けない。

    `git_ls_files` は引数を取れないので、起動した git の env を記録し、その env で設定を問い合わせる。
    """

    def query(self, args: list[str]) -> tuple[int, str]:
        with record_git_launches() as launches:
            gitmod.git_ls_files(Path(self.repo))
        self.assertEqual(
            [launch.argv[1:3] for launch in launches], [["ls-files", "-z"]], "前提: git_ls_files が git を 1 回起動した"
        )
        return git_in(launches[0].effective_env, self.repo, args)


class _OuterRepoEnvChecks:
    """外側の env に、repo / config / template を指す変数 (`outer_repo_env`) があっても、起動の仕方ごとに git に
    届かないこと。

    `setUp` は床 (`isolate_git_config`) を作り、外側の repo を作ってから、その変数を置く。置いた env を
    `self.floor_env` に控えてから、`super().setUp()` で基底クラスがあれば env を当てさせる (基底クラスは
    `setUp` で env を張るので、置くのはその前)。床が置いたはずの変数が `self.floor_env` にあることも
    確かめる (置き損ねた床では、下の検査が何も見ていない)。
    """

    def launch_and_capture_env(self) -> dict:
        """継承先が使う起動の仕方で git を起動したとき、その git が持つ env。"""
        raise NotImplementedError

    def setUp(self) -> None:
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

    def test_the_floor_holds_the_outer_variables(self):
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, self.floor_env, "前提: 床が置いた変数が、当てる側が当てる前の env にある")

    def test_none_of_the_outer_variables_reaches_git(self):
        env = self.launch_and_capture_env()
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
        作られないこと (`git config` の書き込み先になる) で見る。
        """
        before = tree_state(Path(self.other))
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        try:
            make_repo_with_one_commit(work)
        except subprocess.CalledProcessError as e:
            self.fail(f"helper の git が失敗した: {' '.join(map(str, e.cmd))}\n{e.stderr!r}")
        self.assertTrue(os.path.isdir(os.path.join(work, ".git")), "前提: repo が自分の場所に作られている")
        self.assertEqual(tree_state(Path(self.other)), before)
        self.assertFalse(os.path.exists(self.floor_env["GIT_CONFIG"]))

    def test_the_outer_template_does_not_reach_the_helpers(self):
        """外側の `GIT_TEMPLATE_DIR` が指す template が、helper の `init` / `commit` に効かないこと。

        template の `hooks/pre-commit` は失敗し (走れば helper の commit が落ちる)、`info/exclude` は
        `seed.txt` を除外する (効けば commit から外れる)。前提として、`OUTER_REPO_ENV_NAMES` を外して template
        だけを戻した env では、この template が `git init` に写ることを確かめる (写らない git の版では、
        下の「走らない」「外れない」は何も見ていない)。
        """
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
        self.assertFalse(ran.exists(), "外側の template の pre-commit が helper の commit で走った")
        self.assertEqual(_testutil.git(["ls-files"], work).stdout.split(), [b"seed.txt"])


class TestOuterRepoEnvAndTheBaseClass(_OuterRepoEnvChecks, HermeticGitTestCase):
    """製品コードを in-process で動かすテストの基底クラス (`HermeticGitTestCase`)。製品コードの git は env を
    渡さず `os.environ` を継承するので、基底クラスが外さないと外側の変数が届く。"""

    def launch_and_capture_env(self) -> dict:
        with record_git_launches() as launches:
            gitmod.run(["git", "--version"], Path(self.tmp))
        self.assertEqual(len(launches), 1, "前提: 製品コードの git を 1 回起動できた")
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


def classes_using_make_repo_without_hermetic_base(source: str) -> tuple[int, list[str]]:
    """`_make_repo(` を呼ぶ module-level のクラスの数と、`HermeticGitTestCase` を継承していないクラス名。"""
    tree = ast.parse(source)
    lines = source.splitlines()
    using, missing = 0, []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        if "_make_repo(" not in body:
            continue
        using += 1
        if not any(isinstance(b, ast.Name) and b.id == "HermeticGitTestCase" for b in node.bases):
            missing.append(node.name)
    return using, missing


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
            {"test_cli.py", "test_fs.py", "test_git_progress.py"},
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


class TestRepoMakingClassesInheritTheBase(unittest.TestCase):
    """`_make_repo` を使うテストクラスは `HermeticGitTestCase` を継承すること。

    製品コードが起動する git は env を渡さず `os.environ` を継承するので、継承が抜けたクラスだけ
    開発者の global / system の config と自動 maintenance の既定に戻る。
    """

    def test_every_class_that_makes_a_repo_inherits_the_base(self):
        tests_dir = Path(__file__).resolve().parent
        total, missing = 0, []
        for path in sorted(tests_dir.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue  # この file の自己検査用の見本のソースを拾わない
            using, bad = classes_using_make_repo_without_hermetic_base(path.read_text(encoding="utf-8"))
            total += using
            missing += [f"{path.name}:{name}" for name in bad]
        self.assertGreaterEqual(total, 10, "前提: repo を作るクラスを走査している (対象 0 件で黙って通らない)")
        self.assertEqual(missing, [], "`_make_repo` を使うクラスは HermeticGitTestCase を継承する")

    def test_the_scan_sees_a_class_without_the_base(self):
        source = (
            "class A(unittest.TestCase):\n    def t(self):\n        _make_repo(tmp)\n"
            "class B(HermeticGitTestCase):\n    def t(self):\n        _make_repo(tmp)\n"
            "class C(unittest.TestCase):\n    def t(self):\n        pass\n"
        )
        self.assertEqual(classes_using_make_repo_without_hermetic_base(source), (2, ["A"]))


if __name__ == "__main__":
    unittest.main()
