"""テストが作る git repo と、hook が起動する git で、自動 gc / maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に tempdir の後始末が走ると、後始末が `Directory not empty` で
落ちる。この flaky は直接は検出できない (object の hash 次第で偶発的) ので、原因の側に床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、`init_repo` の
  中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける。前提として、commit / receive-pack が trace に載っていること (空の床にしない) と、上書き
  した env が helper の git に届いていること (spy) も確かめる
- **設定の出どころ別** (`_HermeticConfigChecks`): 止める経路は env の `GIT_CONFIG_COUNT` と global の
  fixture と system の無効化 (`GIT_CONFIG_NOSYSTEM`) で、有効値だけを見ると 1 本が欠けても残りが
  埋めて通ってしまう。そこで 1 本ずつ別の検査で見る。起動の仕方 (定数だけ / helper / hook の git) ごとに
  同じ 3 本を流すので、env を当てる各点 (定数・helper・基底クラス・hook の起動) で、`COUNT` だけ・
  `NOSYSTEM` 抜き・`GLOBAL` 抜きのどれが起きても、どれかが assertion で落ちる
  - `GIT_CONFIG_COUNT`: repo 自身の config に反対の値を置き、git が見る値が止める側であること
    (env は repo 自身の config より優先される。fixture は負ける)
  - `GIT_CONFIG_GLOBAL`: `git config --global --list` が fixture の 5 設定と完全一致すること
  - `GIT_CONFIG_NOSYSTEM`: system の config の代わりに置いた目印が読まれないこと
- **直接の起動**: test module が `subprocess` で git を直接起動していないこと。repo を作る git が
  helper を迂回すると、上の床は helper しか見ないので気付けない

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
            trace = os.path.join(tmp, "trace2.jsonl")
            os.environ["GIT_TRACE2_EVENT"] = trace
            overrides = {} if with_fixture else {"GIT_CONFIG_GLOBAL": empty_file(tmp)}
            with mock.patch.dict(_testutil.HERMETIC_GIT_ENV, overrides):
                repo = os.path.join(tmp, "repo")
                os.makedirs(repo)
                with mock.patch.object(subprocess, "run", side_effect=spy):
                    make_repo_with_one_commit(repo)
                want = dict(_testutil.HERMETIC_GIT_ENV)
            events = trace_events(trace)
        self.assertTrue(
            any(all(env.get(k) == v for k, v in want.items()) for env in passed),
            "前提: 上書きした HERMETIC_GIT_ENV が helper の git に届いている",
        )
        self.assertIn("commit", command_names(events), "前提: trace が取れている (空の床にしない)")
        return spawned_maintenance(events)

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


class _HermeticConfigChecks:
    """git が見る設定を、止める経路ごとに 1 本ずつ確かめる共通の検査。

    起動の仕方 (`query`) は継承先が決める。`setUp` は「patch していない」状態を先に作ってから
    (`isolate_git_config`)、`super().setUp()` で基底クラスがあれば env を当てさせる。床が先に global /
    system を空にしたり `GIT_CONFIG_NOSYSTEM` を立てたりすると、基底クラスの当て損ねを床が埋めて
    しまうので、床は外すだけで足さない。
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
    """repo を作る helper (`_testutil.git`) が起動する git。helper が毎回 env を足すので、
    env を patch していない状態でも、定数と同じ設定が見えること。"""

    def query(self, args: list[str]) -> tuple[int, str]:
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

    def query(self, args: list[str]) -> tuple[int, str]:
        res = checker._run_git_raw(args, self.repo)
        self.assertIsNotNone(res, "前提: hook の git を起動できた")
        return res.returncode, res.stdout


_SUBPROCESS_LAUNCHERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def launches_git_literally(node: ast.AST) -> bool:
    """`subprocess.run(["git", ...])` のように、argv を literal の list / tuple で書いた git の起動か。

    見つけるのは、`subprocess` を名前で参照し、argv の先頭を literal の `"git"` で書いた形だけ。
    argv を変数に入れて渡す形や `from subprocess import run` は見つけられない (目印を足すのは
    その形が出てきてから)。
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_LAUNCHERS):
        return False
    if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
        return False
    if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)) or not node.args[0].elts:
        return False
    first = node.args[0].elts[0]
    return isinstance(first, ast.Constant) and first.value == "git"


class TestNoTestLaunchesGitOutsideTheHelper(unittest.TestCase):
    """repo を作る / commit する git は `_testutil.git` を通すこと。

    helper を迂回した git には `HERMETIC_GIT_ENV` が足されず、自動 maintenance が起動する。上の床は
    helper の挙動しか見ないので、迂回した起動は別の検査で拾う。このファイル自身は、helper を通さない
    git の挙動を見るために直接起動するので対象に含めない。
    """

    def test_no_test_module_runs_git_directly(self):
        tests_dir = Path(__file__).resolve().parent
        offenders = []
        for path in sorted(tests_dir.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders += [f"{path.name}:{n.lineno}" for n in ast.walk(tree) if launches_git_literally(n)]
        self.assertEqual(
            offenders, [], "git の直接の起動は `_testutil.git` に置き換える (env を足すため)"
        )

    def test_the_detector_sees_a_direct_launch(self):
        """検出の規則自体が空でないこと (見つけるべき形を 1 つずつ通す)。"""

        def call(source: str) -> ast.AST:
            return ast.parse(source).body[0].value

        for source in (
            'subprocess.run(["git", "init"], cwd=x)',
            'subprocess.check_call(("git", "init"))',
            'subprocess.Popen(["git", *args], cwd=x)',
        ):
            with self.subTest(source=source):
                self.assertTrue(launches_git_literally(call(source)))
        for source in (
            "subprocess.run([sys.executable, '-c', code])",
            "subprocess.run(cmd)",
            "real_run(['git', 'init'])",
            "_git(['init'], cwd)",
        ):
            with self.subTest(source=source):
                self.assertFalse(launches_git_literally(call(source)))


if __name__ == "__main__":
    unittest.main()
