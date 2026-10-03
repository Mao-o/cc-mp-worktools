"""テストが作る git repo (push 先の bare repo を含む) と、ゲートが起動する git で、自動 gc /
maintenance が止まっていること。

背景は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント。`git commit` が背景へ切り離した repack が
`.git/objects/pack` に書いている間に tempdir の後始末が走ると、tearDown が
`Directory not empty` で落ちる。この flaky は直接は検出できない (object の hash 次第で偶発的)
ので、原因の側に床を置く:

- **挙動**: git の子プロセスの起動を `GIT_TRACE2_EVENT` で数え、maintenance / gc の起動が 0 件で
  あること。「問い合わせた時点の設定値」ではなく**実際に起動したか**を見るので、repo を作る helper
  の中の git だけが env を持たずに起動されても、push の受け側 (`receive-pack`) に env が届かなくても
  気付ける (テストが helper を使わずに repo を作る場合は、この床の対象外)。前提として、commit /
  receive-pack / fetch が trace に載っていること (空の床にしない) と、上書きした env が helper の git
  に届いていること (spy) も確かめる。陽性対照として、止める設定が無い commit では起動が trace に
  見えることも確かめる (見えない git の版では「0 件」は何も見ていない)。trace は maintenance を起動
  しない git (`init` / `config` / `add`) の迂回には気付けないので、helper が起動する git の全部が
  `HERMETIC_GIT_ENV` を持つことは、spy で全件記録して別に見る
- **設定の出どころ別** (`_HermeticConfigChecks`): 止める経路は env の `GIT_CONFIG_COUNT` と global の
  fixture の 2 本で、同じ値を持つので、有効値だけを見ると片方が欠けてももう片方が埋めて通ってしまう。
  加えて system の config は `GIT_CONFIG_NOSYSTEM` で読ませない (止める経路には数えない)。既定の除外
  ファイルは `XDG_CONFIG_HOME` を空の dir に向けて読ませない。そこで 4 つを 1 本ずつ別の検査で見る。
  起動の仕方 (定数だけ / helper / ゲートの git / hook プロセスの env) ごとに同じ検査を流すので、env を
  当てる各点 (定数・helper・基底クラス・`run_hook`) で、`COUNT` だけ・`NOSYSTEM` 抜き・`GLOBAL` 抜き・
  `XDG_CONFIG_HOME` 抜きのどれが起きても、どれかが assertion で落ちる。外側の env には止めない側の
  値を置き、当てる側がそれに勝つことも見る
  - `GIT_CONFIG_COUNT`: repo 自身の config に反対の値を置き、git が見る値が止める側であること
    (env は repo 自身の config より優先される。fixture は負ける)
  - `GIT_CONFIG_GLOBAL`: `git config --global --list` が fixture の 5 設定と完全一致すること。
    キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
    将来足された `core.hooksPath` など、テストの前提を変えるもの) が増えても通る
  - `GIT_CONFIG_NOSYSTEM`: system の config の代わりに置いた目印が読まれないこと
  - `XDG_CONFIG_HOME`: 検査用の file (`PROBE`) を除外する既定の除外ファイルが外側にあっても、その file が未追跡として見えること
- **外側の repo / config を指す変数** (`_OuterRepoEnvChecks`): `GIT_DIR` などが外側の env にあっても、
  起動の仕方ごとに git に届かないこと。helper は外側の repo を書き換えないことも見る
- **床自身**: 床 (外側の env) が当てる側の値を持つと、当てる側が同じ値を落としても床が埋めて、上の検査が
  黙って通る。床を作る点ごとに、床の env だけで起動した git で、当てる側の値が見えないこと (と、床が置いた
  はずの止めない側の値が見えること) を確かめる: `isolate_git_config` は `TestTheIsolatedEnvStopsNothing`
  (開発者の global の config に見立てた file も床を作る前に置き、読まれないことを見る。実行者の HOME に左右
  されない)、`_HermeticConfigChecks.setUp` は `test_the_floor_alone_stops_nothing`、
  `_OuterRepoEnvChecks.setUp` は `test_the_floor_holds_the_outer_variables`。確認は床が実際に組んだ env
  (`self.floor_env` など) を、他の検査と同じ `git_in` で見る。床の組み立てを写した別の env を作ると、床の
  組み立ての側の退行が見えなくなる

用語: 床 (「patch していない」外側の env) が持ってはいけない値を「当てる側の値」と呼ぶ。
`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の `GIT_CONFIG_COUNT`、除外ファイルを
持たない `XDG_CONFIG_HOME` が当たる。

「patch していない」状態は `isolate_git_config` で作る。`GIT_CONFIG_*` を外し、global を空にし、
system の config を目印の file に向けるが、**`GIT_CONFIG_NOSYSTEM` は床の側で立てない**。床の側で
立てると、helper・基底クラス・定数が `GIT_CONFIG_NOSYSTEM` を当て損ねても (部分適用)、床が埋めて
通る。目印は `NOSYSTEM` が効いていなければ読めるので、当て損ねが見える。
開発者の本物の system / global の config (そこに自動 maintenance を止める設定 (`maintenance.auto=false`
など) があると、迂回した git も maintenance を起動せず、床が黙って通る) は、目印と空の HOME で
置き換わるので読まれない。
ただし Apple の git が読む Xcode / Command Line Tools 同梱の config (`--show-scope` で unknown) は
`GIT_CONFIG_SYSTEM` では置き換わらず、`GIT_CONFIG_NOSYSTEM` でだけ外れる (実測: Apple Git-155)。
この床は `GIT_CONFIG_NOSYSTEM` を立てないので、その config は読まれる。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config` は未設定 / 読めないとき exit 1 などになる
ので、`check=True` を使わず、「無い」は例外ではなく値の不一致 (assertion) として出す。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Iterator, Mapping
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
# 既定の除外ファイルの検査に使う、repo の直下の未追跡 file の名前。床の外側の除外ファイルが、この名前だけを除外する
# (すべてを除外すると、当てる側が除外ファイルを外し損ねたとき、repo を作る helper の `git add -A` が何も add せず、
# 検査の前に commit が落ちて、assertion ではなく crash になる)
PROBE = "probe.txt"


# 外側の env にあると、git が別の repo や別の config を見てしまう変数。`_testutil.OUTER_GIT_LEAK_ENV` とは別に、
# ここにリテラルで持つ (同じ定数から導くと、そちらから 1 つ消えても期待値ごと消えて通る)
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
)


def outer_repo_env(directory: str) -> dict[str, str]:
    """外側の env に置く、`OUTER_REPO_ENV_NAMES` の変数。値は `directory` の中を指す (当てる側が外し損ねても、
    外側の本物の repo を触らない)。置いた変数と名前の一覧が一致することは、ここが置く変数が一覧と同じ
    であること (`test_every_placed_variable_is_in_the_name_list`) と、各床の env に一覧の変数が全部ある
    こと (`test_the_floor_holds_the_outer_variables`) の 2 本で見る。"""
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
        "GIT_CONFIG_PARAMETERS": "'maintenance.auto=true'",
    }


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


def isolate_git_config(home: str) -> None:
    """「patch していない」状態の床を作る。`mock.patch.dict(os.environ)` の中で呼ぶこと (環境を戻すため)。

    `GIT_CONFIG_*` (`GIT_CONFIG_NOSYSTEM` を含む) と、外側の repo / config を指す変数
    (`OUTER_REPO_ENV_NAMES`。旧来の `GIT_CONFIG` を含む) を外し、`HOME` / `XDG_CONFIG_HOME` を空の `home`
    に向け、`GIT_CONFIG_SYSTEM` を目印 (`hermetic.system = read`) の file に向ける。`home` には空の
    `.gitconfig` を置く: global の config が無いと `git config --global --list` は exit 128 になり、
    空の global を `(0, "")` で見られないため (実測)。`GIT_CONFIG_NOSYSTEM` を立てないのが要点
    (モジュールの docstring)。`GIT_CONFIG_SYSTEM` は git 2.32 以上。
    この床が外す / 向け直すものを外し損ねないことは `TestTheIsolatedEnvStopsNothing` が見る。
    """
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_") or n in OUTER_REPO_ENV_NAMES]:
        del os.environ[name]
    system = os.path.join(home, "system.gitconfig")
    Path(system).write_text(SYSTEM_MARKER, encoding="utf-8")
    Path(home, ".gitconfig").write_text("", encoding="utf-8")
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_SYSTEM": system})


def non_stopping_outer_env(directory: str) -> dict[str, str]:
    """床の外側の env に置く、止めない側の値。当てる側 (定数・helper・基底クラス・`run_hook`) はこれに勝つこと。

    - `GIT_CONFIG_GLOBAL`: 空の file (fixture ではない)
    - `GIT_CONFIG_COUNT`: `maintenance.auto=true` の 1 件
    - `XDG_CONFIG_HOME`: `git/ignore` が検査用の file (`PROBE`) を除外する dir (既定の除外ファイル)

    外側の env が空のままだと、当てる側が外側の env を後から混ぜる向き (`{**HERMETIC_GIT_ENV, **os.environ}`)
    に変わっても、混ぜる値が無いので気付けない。
    """
    xdg = os.path.join(directory, "xdg-with-ignore")
    os.makedirs(os.path.join(xdg, "git"))
    Path(xdg, "git", "ignore").write_text(PROBE + "\n", encoding="utf-8")
    return {
        "GIT_CONFIG_GLOBAL": empty_config_file(directory),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "maintenance.auto",
        "GIT_CONFIG_VALUE_0": "true",
        "XDG_CONFIG_HOME": xdg,
    }


def write_opposite_config(repo: str) -> None:
    """repo 自身の config に `OPPOSITE` (止めない側の値) を書く。`git config` を 4 回起動する代わりに、
    `init` が作った `.git/config` の末尾へ 1 回で足す (この検査は repo を大量に作るので、起動を減らす)。"""
    sections: dict[str, list[str]] = {}
    for key, value in OPPOSITE.items():
        section, name = key.split(".")
        sections.setdefault(section, []).append(f"\t{name} = {value}\n")
    with open(os.path.join(repo, ".git", "config"), "a", encoding="utf-8", newline="\n") as f:
        for section, lines in sections.items():
            f.write(f"[{section}]\n" + "".join(lines))


def git_in(env: Mapping[str, str], cwd: str | Path, args: list[str]) -> tuple[int, str]:
    """`env` をそのまま渡して git を起動し、`(returncode, stdout)` を返す。床の env を見る検査が共通で使う。"""
    res = subprocess.run(
        ["git", *args], cwd=str(cwd), env=dict(env), capture_output=True, text=True, encoding="utf-8"
    )
    return res.returncode, res.stdout


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


@contextlib.contextmanager
def recorded_git_launches() -> Iterator[list[tuple[list[str], dict[str, str]]]]:
    """with の間に起動された git の `(argv, 実際に渡った env)` を記録する。

    `subprocess.Popen` を包むので、`run` / `check_output` / `Popen` のどの形の起動も拾う。env を渡さない
    起動は `os.environ` を継ぐので、その時点の `os.environ` を記録する。
    """
    launches: list[tuple[list[str], dict[str, str]]] = []
    real_popen = subprocess.Popen

    def spy(args, *a, **kw):
        argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
        if argv[:1] == ["git"]:
            env = kw.get("env")
            launches.append((argv, dict(os.environ if env is None else env)))
        return real_popen(args, *a, **kw)

    with mock.patch.object(subprocess, "Popen", spy):
        yield launches


def tree_state(root: Path) -> dict[str, bytes]:
    """`root` 以下の全 file の中身 (相対 path -> bytes)。外側の repo が変わっていないことを見るため。"""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class TestHelpersStopBackgroundMaintenance(unittest.TestCase):
    """テストクラスが env を patch していなくても、repo を作る helper 自身が止める。

    「patch していない」状態は、`GIT_CONFIG_*` を外し、global の config を空に、system の config を
    目印の file に向けて作る (`isolate_git_config`。`GIT_CONFIG_NOSYSTEM` は立てない)。他のテストの
    patch 漏れや、開発者の shell / `~/.gitconfig` の値に左右されないため。床が `GIT_CONFIG_NOSYSTEM`
    を立てないので、helper が渡す env から `GIT_CONFIG_NOSYSTEM` が抜けることも、下の前提
    (`HERMETIC_GIT_ENV` の全項目が helper の git に届いている) で拾える。

    見るのは**起動された git の挙動** (maintenance / gc の子が 0 件) で、helper が後から問い合わせた
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
        env 経路の床が黙って空になるため。ここは `all` ではなく `any` で見る: commit だけが helper を
        迂回する変異でも前提は満たしたまま、maintenance の起動の assertion で落とすため
        (起動した git の全部が env を持つことは `test_every_git_launched_by_the_helpers_carries_the_env`)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            overrides = {"GIT_CONFIG_GLOBAL": empty_config_file(tmp)} if without_the_global_fixture else {}
            with mock.patch.dict(_testutil.HERMETIC_GIT_ENV, overrides):
                trace = os.path.join(tmp, "trace2.jsonl")
                os.environ["GIT_TRACE2_EVENT"] = trace
                with recorded_git_launches() as launches:
                    _testutil.make_marketplace(Path(tmp) / "repo", ["alpha"])
                events = trace_events(trace)
                want = dict(_testutil.HERMETIC_GIT_ENV)
        self.assertTrue(
            any(all(env.get(k) == v for k, v in want.items()) for _, env in launches),
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
        self.assertEqual(self._spawned_while_making_a_marketplace(without_the_global_fixture=False), [])

    def test_the_env_path_alone_stops_it(self):
        """global の fixture を外し、helper が渡す `GIT_CONFIG_COUNT` だけで止まること。

        fixture が同じ設定を持つので、上のテストだけでは、helper が `GIT_CONFIG_COUNT` を渡し損ねても
        fixture が埋めて通ってしまう。
        """
        self.assertEqual(self._spawned_while_making_a_marketplace(without_the_global_fixture=True), [])

    def test_every_git_launched_by_the_helpers_carries_the_env(self):
        """repo を作る helper (`make_marketplace` / `init_bare_origin`) が起動する git の全部が、
        `HERMETIC_GIT_ENV` の全項目を持って起動されること。

        trace の床は maintenance を起動する git (commit / fetch / receive-pack) しか見ない。`init` /
        `config` / `add` を 1 つだけ env を渡さずに起動する形 (`subprocess.run` を直接呼ぶ) は、
        開発者の `core.hooksPath` などを読みうるのに、maintenance の起動を数える trace の床には差が出ない。
        そこで起動を全件記録して、`any` ではなく `all` で見る。前提として、起動の件数と種類 (helper が今
        起動している分) を確かめる (記録が空や一部だと、`all` は何も見ていない)。
        """
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            isolate_git_config(tmp)
            with recorded_git_launches() as launches:
                _testutil.make_marketplace(Path(tmp) / "repo", ["alpha"])
                _testutil.init_bare_origin(Path(tmp))
            want = dict(_testutil.HERMETIC_GIT_ENV)
        subcommands = {argv[1] for argv, _ in launches if len(argv) > 1}
        self.assertGreaterEqual(len(launches), 13, "前提: make_marketplace の 7 件と init_bare_origin の 6 件を記録できている")
        self.assertLessEqual({"init", "config", "add", "commit"}, subcommands, "前提: 種類も記録できている")
        for argv, env in launches:
            with self.subTest(argv=" ".join(argv[1:4])):
                self.assertEqual({k: env.get(k) for k in want}, want)


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


class TestTheIsolatedEnvStopsNothing(unittest.TestCase):
    """`isolate_git_config` だけで作った env (当てる側が何も当てていない床) が、止める側の値を持たないこと。

    `isolate_git_config` は、上の trace の床 (`TestHelpersStopBackgroundMaintenance` など) が直接使う床で、
    `_HermeticConfigChecks` の床もこの上に作る。ここが当てる側の値 (止める側の `GIT_CONFIG_COUNT`、fixture を
    指す `GIT_CONFIG_GLOBAL`、`GIT_CONFIG_NOSYSTEM`) を持つと、当てる側がその値を落としても床が埋めて、
    それを使う床が黙って通る。開発者の global の config (外側の `HOME` の `.gitconfig` と
    `XDG_CONFIG_HOME` の `git/config`) を読んでしまっても同じ。そこで、床を作る前の env にこれらを置いてから
    `isolate_git_config` を呼び、床が組んだ env (`self.env`) だけで起動した git で、どれも見えないこと
    (4 設定は未設定、global は空、system の目印は読める) を、他の床と同じ `git_in` で確かめる。
    外側の repo / config を指す変数が残っていないことも見る。開発者の config はこのテストが床を作る前に
    自分で置くので、この検査は実行者の HOME に左右されない。
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
        self.assertEqual(git_in(self.env, self.repo, ["init", "-q", "-b", "main"]), (0, ""))

    def plant_a_developers_global_config(self) -> dict[str, str]:
        """開発者の global の config に見立てた file を作り、それを指す `HOME` / `XDG_CONFIG_HOME` を返す。

        どちらにも止める側の設定を 1 つずつ書く (`HOME` 側は `maintenance.auto`、`XDG_CONFIG_HOME` 側は
        `gc.auto`)。床が向け直し損ねると、`HOME` は `maintenance.auto` の問い合わせと `--global --list` に、
        `XDG_CONFIG_HOME` は `gc.auto` の問い合わせに出る。後者は `--global --list` には出ない: `~/.gitconfig`
        がある間、`--global --list` は XDG の config を読まない (実測: git 2.50.1)。そのため、4 設定の問い合わせ
        でも見ている。
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
        `GIT_CONFIG_GLOBAL` / 止める側の `GIT_CONFIG_COUNT`、外側の repo / config を指す変数、
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


class _HermeticConfigChecks:
    """git が見る設定を、出どころ (env / global の fixture / system / 既定の除外ファイル) ごとに 1 本ずつ
    確かめる共通の検査。

    起動の仕方 (`query`) は継承先が決める。`setUp` は「patch していない」状態を先に作ってから
    (`isolate_git_config`)、外側の env として止めない側の値を置き (`non_stopping_outer_env`)、その env を
    `self.floor_env` に控えてから、`super().setUp()` で基底クラスがあれば env を当てさせる。床が当てる側の値を
    持つと、当てる側の当て損ねを床が埋めてしまうので、床は当てる側の値を持たない
    (`test_the_floor_alone_stops_nothing` で確かめる)。外側の env (開発者の shell など) に
    `GIT_CONFIG_NOSYSTEM` があっても、`isolate_git_config` が先に外すので、当て損ねは隠れない。

    外側の env には、止めない側の値 (`GIT_CONFIG_GLOBAL` = 空の file、`GIT_CONFIG_COUNT` で
    `maintenance.auto=true`、`PROBE` を除外する既定の除外ファイルを持つ `XDG_CONFIG_HOME`) を置く。
    当てる側 (定数 / helper / 基底クラス / `run_hook`) は外側の env に勝つこと。外側の env が空のままだと、
    当てる側が外側の env を後から混ぜる向き (`{**HERMETIC_GIT_ENV, **os.environ}`) に変わっても、混ぜる値が
    無いので気付けない。
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
        os.environ.update(non_stopping_outer_env(self.tmp))
        # 当てる側が当てる前の env。床が当てる側の値を持たないことの確認に使う
        self.floor_env = dict(os.environ)
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _testutil.sh(Path(self.repo), "init", "-q", "-b", "main")
        write_opposite_config(self.repo)

    def test_the_floor_alone_stops_nothing(self):
        """床の env (`self.floor_env`) だけで起動した git に、当てる側の値が見えないこと。床が置いたはずの止めない
        側の値は見えること。

        床が当てる側の値 (`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の
        `GIT_CONFIG_COUNT`、除外ファイルを持たない `XDG_CONFIG_HOME`) を持つ形に戻ると、当てる側の当て
        損ねを床が埋めて、下の検査が黙って通る。逆に床が止めない側の値を置き損ねると、混ぜる向きの逆転
        (外側の env が勝つ) に気付けない。どちらも `self.floor_env` を、他の検査と同じ `git_in` で見る
        (床の組み立てを写した別の env を作らない)。
        """
        Path(self.repo, PROBE).write_text("x\n", encoding="utf-8")
        # repo 自身の config に止めない側の値がある: 床の env が止める側の値を持てば、この値が変わる
        for key, value in OPPOSITE.items():
            with self.subTest(key=key):
                self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--get", key]), (0, value + "\n"))
        # repo の外では env の値だけが見える。床が置いた止めない側の値 (maintenance.auto=true) だけがあり、残りは無い
        for key in EXPECTED:
            with self.subTest(outside_a_repo=key):
                want = (0, "true\n") if key == "maintenance.auto" else (1, "")
                self.assertEqual(git_in(self.floor_env, self.tmp, ["config", "--get", key]), want)
        self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--global", "--list"]), (0, ""))
        self.assertEqual(git_in(self.floor_env, self.repo, ["config", "--get", "hermetic.system"]), (0, "read\n"))
        # 床の除外ファイルが効いている: 未追跡の probe が status に出ない
        self.assertEqual(git_in(self.floor_env, self.repo, ["status", "--porcelain"]), (0, ""))

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

        fixture は git が読む global なので、余計な設定が増えると git を起動する全テストに効く。外側の
        env の `GIT_CONFIG_GLOBAL` (空の file) に勝つことも見る。
        """
        rc, out = self.query(["config", "--global", "--list"])
        self.assertEqual((rc, sorted(out.splitlines())), (0, FIXTURE_LINES))

    def test_system_config_is_not_read(self):
        """system の config を読まないこと (`GIT_CONFIG_SYSTEM` が指す目印が読まれない)。"""
        # 前提: 床の env (`GIT_CONFIG_NOSYSTEM` が無い) では目印が読める。これが成り立たない環境 (git が古い、
        # 目印が空など) では、下の「読まれない」は何も見ていない
        self.assertEqual(
            git_in(self.floor_env, self.repo, ["config", "--get", "hermetic.system"]),
            (0, "read\n"),
            "前提: GIT_CONFIG_NOSYSTEM が無ければ system の目印は読める (空の床にしない)",
        )
        self.assertEqual(self.query(["config", "--get", "hermetic.system"]), (1, ""))

    def test_default_excludes_are_not_read(self):
        """既定の除外ファイル (`$XDG_CONFIG_HOME/git/ignore`) を読まないこと。

        床の外側の env は、`PROBE` を除外する `git/ignore` を持つ dir を `XDG_CONFIG_HOME` にしてある。
        `GIT_CONFIG_GLOBAL` を差し替えてもこの除外ファイルは外れないので (実測)、当てる側が
        `XDG_CONFIG_HOME` を空の dir に向けなければ、未追跡の probe が status に出ない。
        """
        Path(self.repo, PROBE).write_text("x\n", encoding="utf-8")
        self.assertEqual(self.query(["status", "--porcelain"]), (0, f"?? {PROBE}\n"))


class _RunHookEnv:
    def env_passed_to_the_hook(self) -> dict[str, str]:
        """`run_hook` が hook プロセスに渡す env。hook は起動せず、`subprocess.run` を差し替えて捕まえる。"""
        seen: list[dict[str, str]] = []

        def fake_run(argv, *args, **kwargs):
            # env を渡さない起動は `os.environ` を継ぐ (`recorded_git_launches` と同じ扱い)
            env = kwargs.get("env")
            seen.append(dict(os.environ if env is None else env))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            self.assertIsNone(_testutil.run_hook({"tool_name": "Read", "tool_input": {}}))
        self.assertEqual(len(seen), 1, "前提: run_hook が hook を 1 回だけ起動している")
        return seen[0]


class TestConstantAlone(_HermeticConfigChecks, unittest.TestCase):
    """`HERMETIC_GIT_ENV` の定数だけで git を起動したとき (helper も基底クラスも通さない)。"""

    def query(self, args: list[str]) -> tuple[int, str]:
        return git_in({**os.environ, **_testutil.HERMETIC_GIT_ENV}, self.repo, args)


class TestHelperLaunchedGit(_HermeticConfigChecks, unittest.TestCase):
    """repo を作る helper (`_testutil.sh`) が起動する git。helper が毎回 env を足すので、
    env を patch していない状態でも、定数と同じ設定が見えること。"""

    def query(self, args: list[str]) -> tuple[int, str]:
        try:
            return 0, _testutil.sh(Path(self.repo), *args)
        except subprocess.CalledProcessError as e:  # 設定の問い合わせは非ゼロ終了を値として見る
            return e.returncode, e.stdout


class TestGateLaunchedGit(_HermeticConfigChecks, HermeticGitTestCase):
    """ゲート (製品コード) を in-process で動かすテスト (`GateTest` / `ReadyRefsTest`) で、ゲートが起動する
    git。基底クラス `HermeticGitTestCase` が当てた env を継承すること。

    `runner.run` は env を渡さず `os.environ` を継ぐので、基底クラスが patch した env がそのまま
    見える。ここが外れると、ゲートが起動する git だけ、開発者の global / system の config と自動
    maintenance の既定、既定の除外ファイルに戻る。外側の env に `GIT_CONFIG_NOSYSTEM` があっても、
    `setUp` が先に外してから基底クラスに当てさせるので、当て損ねは隠れない。
    """

    def query(self, args: list[str]) -> tuple[int, str]:
        res = gate_git(args, Path(self.repo), Deadline(30))
        return res.returncode, res.stdout

    def test_fetch_started_by_the_gate_starts_no_maintenance(self):
        """ゲートが自分で起動する git のうち、自動 maintenance の起点になりうるのは `fetch` だけ。

        上の検査は設定値を問い合わせるだけなので、ゲートの `fetch` が起動する子を `GIT_TRACE2_EVENT`
        で数える (挙動の床)。この suite の他のテストは設定ファイルで `fetch` を無効にしてゲートを
        動かすので、ゲートの `fetch` を実際に走らせるのはここだけ。外側の env には止めない側の値が
        あるので、基底クラスが外側に負けると起動が trace に見える。

        fetch が成功したことも前提にする (`Report.notes` が空)。`resolve_base` は fetch の失敗を
        notes に入れて握りつぶし、失敗した fetch は終わりの maintenance の起動まで進まない。前提が
        無いと、fetch が失敗する形では、基底クラスが何も張らなくても通る空の床になる。
        """
        root = _testutil.make_marketplace(Path(self.tmp) / "work", ["alpha"])
        origin = _testutil.init_bare_origin(Path(self.tmp))
        _testutil.sh(root, "remote", "add", "origin", str(origin))
        _testutil.sh(root, "push", "-q", "origin", "main")
        trace = os.path.join(self.tmp, "trace2.jsonl")
        os.environ["GIT_TRACE2_EVENT"] = trace
        rep = gate.Report()
        base = gate.resolve_base(root, "main", Config(fetch=True), Deadline(60), rep)
        events = trace_events(trace)
        self.assertEqual(
            rep.notes, [], "前提: ゲートの fetch が成功している (失敗すると maintenance の起点まで進まない)"
        )
        self.assertEqual(base, "origin/main")
        self.assertIn("fetch", command_names(events), "前提: ゲートが fetch を起動している (空の床にしない)")
        self.assertEqual(spawned_maintenance(events), [])


class TestRunHookLaunchedGit(_RunHookEnv, _HermeticConfigChecks, unittest.TestCase):
    """hook プロセスの env (`run_hook` が渡すもの)。ゲートが起動する git はこの env を継ぐ (`runner.run` は
    env を渡さない) ので、その env で git を起動して、他の起動の仕方と同じ検査を流す。"""

    def query(self, args: list[str]) -> tuple[int, str]:
        return git_in(self.env_passed_to_the_hook(), self.repo, args)


class _OuterRepoEnvChecks:
    """外側の env に、repo / config を指す変数 (`outer_repo_env`) があっても、起動の仕方ごとに git に届かないこと。

    `setUp` は床 (`isolate_git_config`) を作り、外側の repo を作ってから、その変数を置く。置いた env を
    `self.floor_env` に控えてから、`super().setUp()` で基底クラスがあれば env を当てさせる (基底クラスは
    `setUp` で env を張るので、置くのはその前)。床が置いたはずの変数が `self.floor_env` にあることも
    確かめる (置き損ねた床では、下の検査が何も見ていない)。
    """

    def launched_env(self) -> dict[str, str]:
        """継承先が使う起動の仕方で git を起動したとき、その git が持つ env。"""
        raise NotImplementedError

    def prepare_outer_repo(self) -> None:
        """外側の repo を作る必要がある継承先が、変数を置く前に作る。"""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        isolate_git_config(self.tmp)
        self.prepare_outer_repo()
        os.environ.update(outer_repo_env(self.tmp))
        self.floor_env = dict(os.environ)
        super().setUp()

    def test_the_floor_holds_the_outer_variables(self):
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, self.floor_env, "前提: 床が置いた変数が、当てる側が当てる前の env にある")

    def test_none_of_the_outer_variables_reaches_git(self):
        env = self.launched_env()
        for name in OUTER_REPO_ENV_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, env)


class TestOuterRepoEnvAndTheHelpers(_OuterRepoEnvChecks, unittest.TestCase):
    """repo を作る helper (`_testutil.sh`)。"""

    def prepare_outer_repo(self) -> None:
        self.other = _testutil.make_marketplace(Path(self.tmp) / "outer", ["alpha"])

    def launched_env(self) -> dict[str, str]:
        with recorded_git_launches() as launches:
            _testutil.sh(Path(self.tmp), "--version")
        self.assertEqual(len(launches), 1, "前提: helper が git を 1 回起動している")
        return launches[0][1]

    def test_the_helpers_leave_the_outer_repo_alone(self):
        """外側に別の repo を指す `GIT_DIR` などがあっても、helper が repo を作る間、その repo を変えないこと。

        外さないと、`init` / `config` / `commit` が外側の repo に書き込む。外側の repo の全 file の中身が
        前後で一致すること、旧来の `GIT_CONFIG` が指す file が作られないこと (`git config` の書き込み先に
        なる) で見る。外側の変数が届いた helper の git が失敗する形も検出のうち: 例外のまま crash させず、
        失敗として報告する。
        """
        before = tree_state(self.other)
        work = Path(self.tmp) / "work"
        try:
            _testutil.make_marketplace(work, ["alpha"])
            _testutil.init_bare_origin(Path(self.tmp))
        except subprocess.CalledProcessError as e:
            self.fail(f"helper の git が失敗した: {' '.join(map(str, e.cmd))}\n{e.stderr}")
        self.assertTrue((work / ".git").is_dir(), "前提: repo が自分の場所に作られている")
        self.assertEqual(tree_state(self.other), before)
        self.assertFalse(os.path.exists(self.floor_env["GIT_CONFIG"]))


class TestOuterRepoEnvAndTheBaseClass(_OuterRepoEnvChecks, HermeticGitTestCase):
    """ゲートを in-process で動かすテストの基底クラス (`HermeticGitTestCase`)。"""

    def launched_env(self) -> dict[str, str]:
        with recorded_git_launches() as launches:
            gate_git(["--version"], Path(self.tmp), Deadline(30))
        self.assertEqual(len(launches), 1, "前提: ゲートの git を 1 回起動できた")
        return launches[0][1]


class TestOuterRepoEnvAndRunHook(_RunHookEnv, _OuterRepoEnvChecks, unittest.TestCase):
    """hook プロセスの env (`run_hook` が渡すもの)。"""

    def launched_env(self) -> dict[str, str]:
        return self.env_passed_to_the_hook()


if __name__ == "__main__":
    unittest.main()
