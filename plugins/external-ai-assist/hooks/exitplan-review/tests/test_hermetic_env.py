"""テストが作る git repo で、自動 gc / maintenance が止まっていること。

理由は `_testutil.NO_BACKGROUND_GIT_SETTINGS` のコメント (post-implementation-review の同名の
テストと同じ床。そちらは commit するので実害が出る側で、こちらは `init_repo` を揃えるだけ)。

見るのは `init_repo` が**実際に git へ渡した env** (`subprocess.run` を spy で包んで捕まえる)。
テストが自分で組んだ env で確かめると、`init_repo` が env を渡し忘れても気付けない。止める経路
(env の `GIT_CONFIG_COUNT` / global の fixture) は同じ値を持つので、有効値だけを見ると片方が欠けても
もう片方が埋めて通ってしまう。1 本ずつ単独で見る: env は 4 設定を 1 項目ずつ、fixture は内容の
完全一致 (5 設定だけを持つこと。余計な設定が増えると、git を起動する全テストに効く)。

加えて、system の config を読ませないこと (`GIT_CONFIG_NOSYSTEM` が届いていること) を値で見る。
system の config は `GIT_CONFIG_SYSTEM` で目印の file (`hermetic.system = read` だけを持つ) に向けて
おき、`GIT_CONFIG_NOSYSTEM` が効いていれば読まれない。**床の側では `GIT_CONFIG_NOSYSTEM` を立てない**:
立てると、`init_repo` や定数がそれを渡し損ねても、床が埋めて通ってしまう。外側の env には、止めない
側の値 (`OUTER_NON_STOPPING_ENV`) を置く: `init_repo` が env を混ぜる向きを逆にして外側の値を勝たせる
変異も、止まっていないことで拾える。床の env だけでは、どの経路も止める側にならないこと (fixture を指す
`GIT_CONFIG_GLOBAL` や止める側の `GIT_CONFIG_COUNT` を持たないこと) を `TestTheIsolatedEnvStopsNothing` で
見る。

期待値は `_testutil` の定義とは**別に**リテラルで持つ。同じ定数から導くと、`_testutil` から
1 項目消えても期待値ごと消えて通ってしまう。`git config` は未設定 / 読めないとき exit 1 などに
なるので、`check=True` を使わず、「無い」は例外ではなく値の不一致 (assertion) として出す。
"""
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

import _testutil

EXPECTED = {
    "maintenance.auto": "false",
    "maintenance.autoDetach": "false",
    "gc.auto": "0",
    "gc.autoDetach": "false",
}
# global の fixture が持つ設定 (`receive.autogc` は push の受け側 `receive-pack` 用)
EXPECTED_WITH_RECEIVE = {**EXPECTED, "receive.autogc": "false"}


def empty_global_config(home: str) -> None:
    """git が読む global の config を空にし、system の config を目印の file に向ける。

    global 側は `HOME` / `XDG_CONFIG_HOME` を空の `home` に向ける。system 側は `GIT_CONFIG_SYSTEM` で
    `home` の中の目印 file (`hermetic.system = read` だけを持つ) に向ける: 開発者の system config は
    読まれず、しかも `GIT_CONFIG_NOSYSTEM` が効いていれば目印も読まれないので、効いているかを値
    (`git config --get hermetic.system`) で見られる。**`GIT_CONFIG_NOSYSTEM` は立てない。** 床の側が
    立てると、`init_repo` や定数がそれを渡し損ねても、床が埋めて通ってしまう。

    `mock.patch.dict(os.environ)` の中で呼ぶこと (環境を戻すため)。
    """
    system = os.path.join(home, "system.gitconfig")
    with open(system, "w", encoding="utf-8") as f:
        f.write("[hermetic]\n\tsystem = read\n")
    os.environ.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_SYSTEM": system})


# 外側の env (開発者の shell など) が持ちうる「止めない側」の git の設定。global は空、env の設定は
# `maintenance.auto=true`。`isolate_git_config` が置き、`init_repo` や定数が env を混ぜる向きを床が
# 見られるようにする (次の項)。
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

    外側を全部外しただけだと、`init_repo` が env を混ぜる向きを逆にして (`{**HERMETIC_GIT_ENV,
    **os.environ}`) 外側の値を勝たせても、床の側に外側の値が無いので気付けない。止めない側の値を
    置いておけば、`init_repo` が渡した env の値が勝っていること (止まっていること) が、向きを逆にした
    変異で崩れる。

    `GIT_CONFIG_*` の前方一致では、接尾辞なしの旧来の `GIT_CONFIG` と、repo の場所を変える env は外れない。
    これらも外す (`git_env_names_to_drop`)。`init_repo` や基底クラスが外すことは、ここでは置かず、
    `plant_outer_leaks` を使う別の確認が見る (ここに置くと、これを使う全部の床が外側の repo を指す)。
    """
    for name in git_env_names_to_drop():
        del os.environ[name]
    empty_global_config(home)
    os.environ.update(OUTER_NON_STOPPING_ENV)


# 外側の env から、テストが起動する git に漏れてはいけないもの。`_testutil.OUTER_GIT_LEAKS` とは**別に**
# リテラルで持つ (同じ定数から導くと、`_testutil` から 1 項目消えても期待値ごと消えて通ってしまう)。
# repo の場所を変えるもの、`GIT_CONFIG_PARAMETERS` (`GIT_CONFIG_COUNT` に勝つ)、旧来の接尾辞なしの
# `GIT_CONFIG` (あると `git config --get` はその file だけを読み、`--global --list` は rc 129 で落ちる)、
# `git rev-parse --local-env-vars` が挙げる残り、`GIT_TEMPLATE_DIR` (`git init` が外側の hook を写す)。
OUTER_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_TEMPLATE_DIR",
)


def git_env_names_to_drop() -> list[str]:
    """床が「patch していない」状態を作るとき、`os.environ` から外す名前。`GIT_CONFIG_*` の全部
    (`GIT_CONFIG_PARAMETERS` を含む) と、接尾辞なしの `GIT_CONFIG`、repo の場所を変えるもの。"""
    return [n for n in os.environ if n.startswith("GIT_CONFIG_") or n in OUTER_LEAKS]


def make_decoy_repo(parent: str) -> str:
    """外側の env が指してしまう「別 repo」に見立てた repo (commit 1 件)。`init_repo` が外側の repo を
    触ると config / refs / reflog / index のどれかが変わる。"""
    decoy = os.path.join(parent, "decoy")
    os.makedirs(decoy)
    env = {k: v for k, v in os.environ.items() if k not in OUTER_LEAKS}
    env.update(_testutil.HERMETIC_GIT_ENV)
    for args in (
        ["init", "-q"],
        ["-c", "user.name=decoy", "-c", "user.email=decoy@example.com", "commit", "--allow-empty", "-qm", "decoy"],
    ):
        subprocess.run(["git", *args], cwd=decoy, env=env, capture_output=True, check=True)
    return decoy


def snapshot_repo(repo: str) -> dict[str, object]:
    """repo の状態の指紋 (config の中身 / refs / reflog / index のバイト列)。外側の env の影響を受けない
    ように、`OUTER_LEAKS` を外した env で `git` を起動する。"""
    env = {k: v for k, v in os.environ.items() if k not in OUTER_LEAKS}
    env.update(_testutil.HERMETIC_GIT_ENV)

    def out(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
        ).stdout

    git_dir = os.path.join(repo, ".git")
    with open(os.path.join(git_dir, "config"), encoding="utf-8") as f:
        config = f.read()
    with open(os.path.join(git_dir, "index"), "rb") as f:
        index = f.read()
    return {"config": config, "refs": out("for-each-ref"), "reflog": out("reflog", "--all"), "index": index}


# 外側の `GIT_TEMPLATE_DIR` が指す template に置く hook。走ると `outer_hook_marker` に 1 行ずつ書く
OUTER_HOOKS = ("pre-commit", "post-commit")


def outer_hook_marker(decoy: str) -> str:
    """`plant_outer_leaks` が置く template の hook が、走るたびに 1 行書く目印の file。"""
    return os.path.join(os.path.dirname(decoy), "outer-hook-ran")


def outer_template_traces(decoy: str, repo: str) -> dict[str, object]:
    """外側の template の痕跡: hook が走った回数と、`repo` に写された外側の hook の名前。
    どちらも無ければ `{"hook_runs": 0, "copied": []}`。この suite の `init_repo` は commit しないので、
    hook が走るより先に「写された」ことで見る。"""
    marker = outer_hook_marker(decoy)
    runs = 0
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            runs = len(f.read().splitlines())
    hooks = os.path.join(repo, ".git", "hooks") if repo else ""
    copied = sorted(h for h in OUTER_HOOKS if hooks and os.path.exists(os.path.join(hooks, h)))
    return {"hook_runs": runs, "copied": copied}


def plant_outer_leaks(decoy: str) -> dict[str, str]:
    """`OUTER_LEAKS` の 16 個すべてを `os.environ` に置く (`mock.patch.dict(os.environ)` の中で呼ぶこと)。
    repo の場所を変えるものは `decoy` を指し、`GIT_CONFIG_PARAMETERS` は止めない側の値 (`GIT_CONFIG_COUNT`
    に勝つ)、`GIT_CONFIG` は空の file (あると `git config --get` はその file だけを読む)、`GIT_TEMPLATE_DIR` は
    目印を書く hook (`OUTER_HOOKS`) を持つ template (`decoy` の隣に作る)。置いた dict を返す。"""
    git_dir = os.path.join(decoy, ".git")
    template = os.path.join(os.path.dirname(decoy), "outer-template")
    os.makedirs(os.path.join(template, "hooks"), exist_ok=True)
    for hook in OUTER_HOOKS:
        path = os.path.join(template, "hooks", hook)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\necho {hook} >> {shlex.quote(outer_hook_marker(decoy))}\n")
        os.chmod(path, 0o755)
    planted = {
        "GIT_DIR": git_dir,
        "GIT_WORK_TREE": decoy,
        "GIT_INDEX_FILE": os.path.join(git_dir, "index"),
        "GIT_COMMON_DIR": git_dir,
        "GIT_OBJECT_DIRECTORY": os.path.join(git_dir, "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(git_dir, "objects"),
        "GIT_NAMESPACE": "outer",
        "GIT_CONFIG_PARAMETERS": "'maintenance.auto=true'",
        "GIT_CONFIG": os.devnull,
        "GIT_IMPLICIT_WORK_TREE": "0",
        "GIT_GRAFT_FILE": os.path.join(git_dir, "info", "grafts"),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_REPLACE_REF_BASE": "refs/outer-replace/",
        "GIT_PREFIX": "outer/",
        "GIT_SHALLOW_FILE": os.path.join(git_dir, "shallow"),
        "GIT_TEMPLATE_DIR": template,
    }
    assert set(planted) == set(OUTER_LEAKS)
    os.environ.update(planted)
    return planted


@contextmanager
def record_git_launches():
    """この中で起動された git の (argv, 渡した env) を記録する (`subprocess.Popen` の層。`run` /
    `check_*` / `Popen` 直接を同じ場所で捕まえる)。env を渡さなければ、そのとき継いだ `os.environ` を
    記録する。`os.system` や `os.posix_spawn` で起動した git は見えない。

    記録が受け取った env そのものであることは `TestTheGitLaunchRecorder` が見る: 記録器が値を足す退行は、
    記録を見る床 (`all` で `HERMETIC_GIT_ENV` を持つか) を、`init_repo` の当て損ねごと埋めて黙らせる。"""
    launches: list[tuple[list[str], dict[str, str]]] = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        argv = list(args[0] if args else kwargs["args"])
        if argv[:1] == ["git"]:
            env = kwargs.get("env")
            launches.append((argv, dict(os.environ if env is None else env)))
        return real_popen(*args, **kwargs)

    with mock.patch.object(subprocess, "Popen", spy):
        yield launches


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


class _InitRepoFloor(unittest.TestCase):
    """init_repo の床。`isolate_git_config` で「patch していない」状態を作り、`init_repo` が git に渡した
    env を spy で捕まえる。床の自己確認 (`TestTheSystemMarkerIsLive` / `TestTheIsolatedEnvStopsNothing`) も
    この床を通し、床の env をそのまま渡す git を spy に通して spy が捕まえた env (`self.floor_env`) を見る:
    `isolate_git_config` を直接呼ぶと、ここで足された止める側の値 (0.12.3 は `GIT_CONFIG_NOSYSTEM` をここで
    立てていた) も、spy が足した値も見ない。

    問い合わせは、床と `TestTheIsolatedEnvStopsNothing` が同じ `query_git` で、同じ HOME を cwd にして
    行う。問い合わせの道具が床と自己確認で別だと、床の道具だけに足された値 (止める側の値) を、自己確認が
    見ない。`TestTheSystemMarkerIsLive` は `system_marker` で見るので、`query_git` に足された
    `GIT_CONFIG_NOSYSTEM` は見ない。それは system の床の前提が、床と同じ `query_git` で落とす (前提を
    `system_marker` に替えると、この組がまた黙って通る)。
    """

    def setUp(self) -> None:
        # 隔離した HOME と system の目印は、`init_repo` が渡した env と床の env (`self.floor_env`) を
        # 使い終わるまで残す。env が指す file が消えていると、system の確認が「読めないから未設定」で
        # 通ってしまう。
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = home.name

    def _envs_passed_by_init_repo(self, *, floor_only: bool = False) -> list[tuple[str, dict]]:
        """`init_repo` が起動した git ごとの (サブコマンド, 渡した env)。

        env を渡していなければ、そのとき継いだ `os.environ` を返す。「patch していない」状態は
        `GIT_CONFIG_*` を外し、global の config を空に、system の config を目印の file に向けて作る
        (`isolate_git_config`。`GIT_CONFIG_NOSYSTEM` は立てない)。開発者の shell や `~/.gitconfig` の
        値に左右されないため (env を渡し損ねた git が開発者の設定を読むと、有効値が揃って床が黙って
        通ってしまう)。

        `floor_only=True` なら `init_repo` を呼ばず、床の env をそのまま渡す git (`git --version`) を spy に
        通し、spy が捕まえた env を `self.floor_env` に残して返す (床の自己確認用。当てる側の前提を通らずに、
        床だけを見る)。spy を通すのは、spy (記録の道具) が値を足す退行も自己確認に見せるため。
        """
        passed: list[tuple[str, dict]] = []
        real_run = subprocess.run

        def spy(argv, *args, **kwargs):
            if argv[:1] == ["git"]:
                passed.append((argv[1], dict(kwargs.get("env") or os.environ)))
            return real_run(argv, *args, **kwargs)

        with mock.patch.dict(os.environ):
            isolate_git_config(self.home)
            with mock.patch.object(subprocess, "run", side_effect=spy):
                if floor_only:
                    # 床の自己確認: init_repo の代わりに、床の env をそのまま渡す git を spy に通し、spy が
                    # 捕まえた env を見る (spy や、init_repo の直前までの床が足した値も見る)
                    subprocess.run(["git", "--version"], env=dict(os.environ), capture_output=True, check=True)
                    self.floor_env = passed[-1][1]
                    return []
                _testutil.init_repo(os.path.join(self.home, "repo"))
        self.assertTrue(passed, "前提: init_repo が git を起動している (空の床にしない)")
        return passed


class TestInitRepoSettings(_InitRepoFloor):
    def test_the_env_alone_has_the_four_settings(self):
        """global の fixture を外しても、渡した env の `GIT_CONFIG_COUNT` だけで 4 設定が効くこと。"""
        for sub, passed in self._envs_passed_by_init_repo():
            env = {**passed, "GIT_CONFIG_GLOBAL": os.devnull}
            for key, expected in EXPECTED.items():
                with self.subTest(git=sub, key=key):
                    self.assertEqual(query_git(self.home, env, "config", "--get", key), (0, expected))

    def test_the_global_fixture_holds_only_the_five_settings(self):
        """渡した env の `GIT_CONFIG_GLOBAL` が指す fixture が、5 設定だけを持つこと (完全一致)。

        キーごとの `--get` だと、余計な設定 (誤って `git config --global` で書かれた `user.name` や、
        将来足された `init.defaultBranch` など) が増えても通る。`git config --list` はキーを小文字で出す。
        """
        expected = sorted(f"{key.lower()}={value}" for key, value in EXPECTED_WITH_RECEIVE.items())
        for sub, passed in self._envs_passed_by_init_repo():
            code, out = query_git(self.home, passed, "config", "--global", "--list")
            with self.subTest(git=sub):
                self.assertEqual((code, sorted(out.splitlines())), (0, expected))

    def test_the_env_does_not_read_the_system_config(self):
        """渡した env が system の config を読ませないこと (`GIT_CONFIG_NOSYSTEM` が届いている)。

        system は目印の file に向けてあり (`isolate_git_config`)、`GIT_CONFIG_NOSYSTEM` が効いて
        いれば読まれない (`hermetic.system` が未設定のまま)。目印が読めること自体は
        `TestTheSystemMarkerIsLive` が見る。ただし、そちらが問い合わせるのは床の env (`init_repo` を
        呼ぶ前) で、この床が問い合わせる env ではない。この床が問い合わせる時点で目印が読めることは、
        同じ env から `GIT_CONFIG_NOSYSTEM` だけを外し、同じ `query_git` で前提として見る (目印が
        消えていると、「読めないから未設定」で通ってしまう)。床の側で `GIT_CONFIG_NOSYSTEM` を立てて
        いると、`init_repo` や定数が渡し損ねても、床の env に残って通ってしまう。
        """
        for sub, passed in self._envs_passed_by_init_repo():
            with self.subTest(git=sub):
                # 目印は setUp の HOME に置いたまま。問い合わせの時点でも読めることを、同じ env から
                # `GIT_CONFIG_NOSYSTEM` だけを外して先に確かめる (消えていると「読めないから未設定」で通る)
                readable = {k: v for k, v in passed.items() if k != "GIT_CONFIG_NOSYSTEM"}
                self.assertEqual(
                    query_git(self.home, readable, "config", "--get", "hermetic.system"),
                    (0, "read"),
                    "前提: 渡した env の GIT_CONFIG_SYSTEM が指す目印が、問い合わせの時点で読める",
                )
                self.assertEqual(query_git(self.home, passed, "config", "--get", "hermetic.system"), (1, ""))


class TestTheSystemMarkerIsLive(_InitRepoFloor):
    """system の目印が、`GIT_CONFIG_NOSYSTEM` を立てなければ読まれ、立てれば読まれないこと。

    「目印が読まれない (`hermetic.system` が未設定)」という確認は、目印がそもそも読めない状態でも
    成り立つ。`isolate_git_config` が目印を置き損ねる / `GIT_CONFIG_SYSTEM` を向け損ねる / 床の側で
    `GIT_CONFIG_NOSYSTEM` を立てる、のどれでも、`GIT_CONFIG_NOSYSTEM` が届いているかを見る床が黙って
    空になるので、道具の側を先に確かめる。床 (`isolate_git_config` と、それを呼ぶ init_repo の床) を
    通し、床の env をそのまま渡す git を spy に通して、spy が捕まえた env で見る (0.12.3 は `GIT_CONFIG_NOSYSTEM` を init_repo の床の中で
    立てていた)。
    """

    def test_the_marker_is_read_unless_nosystem_is_set(self):
        self._envs_passed_by_init_repo(floor_only=True)
        readable = system_marker(self.home, self.floor_env)
        skipped = system_marker(self.home, {**self.floor_env, "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertEqual((readable, skipped), ((0, "read"), (1, "")))


class TestTheIsolatedEnvStopsNothing(_InitRepoFloor):
    """床 (`isolate_git_config` と、それを呼ぶ init_repo の床と spy) が、当てる側の代わりに git に渡す env だけでは、
    どの経路も止める側にならないこと。

    この suite の床は、すべてこの関数で「patch していない」状態を作る。床が止める側の値 (fixture を指す
    `GIT_CONFIG_GLOBAL`、止める側の `GIT_CONFIG_COUNT`) を持つ形に戻ると、`init_repo` や定数の当て損ねを
    床が埋めて黙って通る (`GIT_CONFIG_NOSYSTEM` は `TestTheSystemMarkerIsLive` が見る)。global は空で、
    4 設定は外側に置いた値そのもの (`maintenance.auto=true` だけが見え、残りは未設定) であることを、
    完全一致で見る。
    """

    def test_the_isolated_env_alone_stops_nothing(self):
        self._envs_passed_by_init_repo(floor_only=True)
        global_list = query_git(self.home, self.floor_env, "config", "--global", "--list")
        settings = {key: query_git(self.home, self.floor_env, "config", "--get", key) for key in EXPECTED}
        self.assertEqual(
            global_list, (0, ""), "床の global が空の file でない (何かを読ませているか、global を読めない)"
        )
        self.assertEqual(
            settings,
            {
                "maintenance.auto": (0, "true"),
                "maintenance.autoDetach": (1, ""),
                "gc.auto": (1, ""),
                "gc.autoDetach": (1, ""),
            },
            "床の env の 4 設定が、外側に置いた止めない側の値そのものでない",
        )


class TestTheFloorDropsTheOuterLeaks(unittest.TestCase):
    """床 (`isolate_git_config`) が「patch していない」状態を作るとき、`OUTER_LEAKS` の 16 個を外すこと。

    接尾辞なしの `GIT_CONFIG` は `GIT_CONFIG_*` の前方一致では外れない。外側にあると、`git config --get`
    がその file だけを読み、設定の値を見る床が誤って落ちる (止める側の値を持つ file なら、定数の抜けを
    埋めうる)。repo の場所を変えるものは、床が直接起動する git を外側の repo に向ける。
    """

    def test_isolate_git_config_removes_every_outer_leak(self):
        with mock.patch.dict(os.environ), tempfile.TemporaryDirectory() as tmp:
            plant_outer_leaks(os.path.join(tmp, "decoy"))
            isolate_git_config(tmp)
            remaining = sorted(n for n in OUTER_LEAKS if n in os.environ)
            non_stopping = {k: os.environ.get(k) for k in OUTER_NON_STOPPING_ENV}
        self.assertEqual(remaining, [])
        self.assertEqual(non_stopping, OUTER_NON_STOPPING_ENV)


class TestOuterRepoEnvDoesNotReachInitRepo(_InitRepoFloor):
    """外側の env に `OUTER_LEAKS` があっても、`init_repo` は外側の repo を触らず、渡された path に repo
    を作ること。起動された git のすべてが、`OUTER_LEAKS` を持たず `HERMETIC_GIT_ENV` を持つこと。

    外側 (開発者の shell、git の hook の中、`git -c` の配下) の `GIT_DIR` などは、`HERMETIC_GIT_ENV` を
    足すだけでは外れない。残ると `git init` が外側の repo に対して走る。起動した git は
    `subprocess.Popen` の層で全部記録する (`run` / `check_*` / `Popen` 直接を同じ場所で捕まえる)。件数の
    下限 (`init_repo` は `git init` を 1 回起動する) を前提として先に確かめる: 何も記録できていない
    状態は `all` が空で通る。`os.system` や `os.posix_spawn` で起動した git はここでは見えない。
    """

    def _run_init_repo_with_leaks(self):
        """記録は `record_git_launches` (記録が受け取った env そのものであることは
        `TestTheGitLaunchRecorder` が見る)。"""
        with mock.patch.dict(os.environ):
            isolate_git_config(self.home)
            decoy = make_decoy_repo(self.home)
            before = snapshot_repo(decoy)
            plant_outer_leaks(decoy)
            already = {
                k
                for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT")
                if os.environ.get(k, "<unset>") == _testutil.HERMETIC_GIT_ENV.get(k, "<absent>")
            }
            self.assertEqual(already, set(), "前提: init_repo が git に渡す前の os.environ は止める側の値を持たない")
            # 漏れた env で git が失敗しても (`check=True`)、テストの error ではなく assertion で落とす
            error: Exception | None = None
            repo = ""
            with record_git_launches() as launches:
                try:
                    repo = _testutil.init_repo(os.path.join(self.home, "repo"))
                except subprocess.CalledProcessError as e:
                    error = e
            after = snapshot_repo(decoy)
            self.template_traces = outer_template_traces(decoy, repo)
        return repo, launches, before, after, error

    def test_init_repo_leaves_the_outer_repo_alone(self):
        repo, _launches, before, after, error = self._run_init_repo_with_leaks()
        self.assertEqual(after, before, "外側の repo が変わった (init_repo の git に GIT_DIR などが漏れている)")
        self.assertEqual(
            self.template_traces,
            {"hook_runs": 0, "copied": []},
            "外側の GIT_TEMPLATE_DIR の hook が写された / 走った",
        )
        self.assertIsNone(error, "外側の env が漏れて、init_repo の git が失敗した")
        self.assertTrue(os.path.isdir(os.path.join(repo, ".git")), "渡された path に repo が作られていない")

    def test_every_git_launched_by_init_repo_carries_the_hermetic_env(self):
        _repo, launches, _before, _after, error = self._run_init_repo_with_leaks()
        want = dict(_testutil.HERMETIC_GIT_ENV)
        without_env = [argv for argv, env in launches if not all(env.get(k) == v for k, v in want.items())]
        with_leaks = [argv for argv, env in launches if any(n in env for n in OUTER_LEAKS)]
        self.assertEqual(without_env, [], "HERMETIC_GIT_ENV を持たずに起動された git がある")
        self.assertEqual(with_leaks, [], "OUTER_LEAKS を持ったまま起動された git がある")
        self.assertGreaterEqual(len(launches), 1, "前提: init_repo の git の起動を記録できている")
        self.assertIsNone(error, "外側の env が漏れて、init_repo の git が失敗した")


class TestTheGitLaunchRecorder(unittest.TestCase):
    """記録器 (`record_git_launches`) が、起動された git に渡された env を**そのまま**記録すること。

    `init_repo` の起動を見る床 (`test_every_git_launched_by_init_repo_carries_the_hermetic_env`) は、記録した
    env が `HERMETIC_GIT_ENV` を持つことを見る。記録器が記録に値を足す (`{**env, **HERMETIC_GIT_ENV}` など) と、
    `init_repo` を迂回した git (env を渡さない、または `HERMETIC_GIT_ENV` を持たない env を渡す) も床を通る。
    `HERMETIC_GIT_ENV` を持たない既知の env で git を起動し、記録が完全一致することで見る。env を省略した
    起動では、そのとき継いだ `os.environ` と一致すること。
    """

    def test_the_recorder_records_the_env_it_was_given(self):
        with mock.patch.dict(os.environ):
            for name in _testutil.HERMETIC_GIT_ENV:
                os.environ.pop(name, None)
            given = {k: v for k, v in os.environ.items() if k not in _testutil.HERMETIC_GIT_ENV}
            with record_git_launches() as launches:
                passed = subprocess.run(["git", "--version"], env=given, capture_output=True)
                inherited_run = subprocess.run(["git", "--version"], capture_output=True)
            inherited = dict(os.environ)
        self.assertEqual((passed.returncode, inherited_run.returncode), (0, 0), "前提: git が起動できる")
        self.assertEqual(launches, [(["git", "--version"], given), (["git", "--version"], inherited)])


class TestHookTestCaseDropsTheOuterGitEnv(_testutil.HookTestCase):
    """基底クラス (`HookTestCase`) の setUp が、外側の `OUTER_LEAKS` を `os.environ` から外すこと。

    hook (製品コード) の `rev-parse` は env を渡さず `os.environ` を継承して git を起動するので、
    ここに残ると外側の repo を見る。外側の repo を指す env を、基底クラスの setUp の前に置く。
    """

    def setUp(self) -> None:
        outer = mock.patch.dict(os.environ)
        outer.start()
        self.addCleanup(outer.stop)
        decoy_dir = tempfile.TemporaryDirectory()
        self.addCleanup(decoy_dir.cleanup)
        self.decoy = make_decoy_repo(decoy_dir.name)
        self.decoy_before = snapshot_repo(self.decoy)
        plant_outer_leaks(self.decoy)
        super().setUp()

    def test_the_outer_leaks_are_gone(self):
        self.assertEqual(sorted(n for n in OUTER_LEAKS if n in os.environ), [])

    def test_a_repo_made_in_the_test_is_its_own(self):
        # 漏れた env で git が失敗しても、error ではなく assertion で落とす (外側の repo が変わったかを先に見る)
        error: Exception | None = None
        git_dir = ""
        try:
            repo = _testutil.init_repo(os.path.join(self._tmp.name, "repo"))
            res = subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"], cwd=repo, capture_output=True, text=True, check=True
            )
            git_dir = res.stdout.strip()
        except subprocess.CalledProcessError as e:
            error = e
        self.assertEqual(snapshot_repo(self.decoy), self.decoy_before)
        self.assertIsNone(error, "外側の env が漏れて、git が失敗した")
        self.assertEqual(git_dir, os.path.join(repo, ".git"))


if __name__ == "__main__":
    unittest.main()
