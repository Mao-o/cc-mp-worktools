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
import subprocess
import tempfile
import unittest
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
    """
    for name in [n for n in os.environ if n.startswith("GIT_CONFIG_")]:
        del os.environ[name]
    empty_global_config(home)
    os.environ.update(OUTER_NON_STOPPING_ENV)


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
    この床を通し、`init_repo` を呼ぶ直前の env (`self.floor_env`) を見る: `isolate_git_config` を直接呼ぶと、
    ここで足された止める側の値 (0.12.3 は `GIT_CONFIG_NOSYSTEM` をここで立てていた) を見ない。

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

        `init_repo` を呼ぶ直前の env を `self.floor_env` に残す。`floor_only=True` なら `init_repo` を
        呼ばずにそこで返す (床の自己確認用。当てる側の前提を通らずに、床だけを見る)。
        """
        passed: list[tuple[str, dict]] = []
        real_run = subprocess.run

        def spy(argv, *args, **kwargs):
            if argv[:1] == ["git"]:
                passed.append((argv[1], dict(kwargs.get("env") or os.environ)))
            return real_run(argv, *args, **kwargs)

        with mock.patch.dict(os.environ):
            isolate_git_config(self.home)
            # 当てる側 (init_repo) が当てる前の env。床の自己確認が見る (init_repo の呼び出しの直前に置く)
            self.floor_env = dict(os.environ)
            if floor_only:  # 床の自己確認: init_repo を呼ばず、ここまでの床だけを見る
                return []
            with mock.patch.object(subprocess, "run", side_effect=spy):
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
    通し、`init_repo` を呼ぶ直前の env で見る (0.12.3 は `GIT_CONFIG_NOSYSTEM` を init_repo の床の中で
    立てていた)。
    """

    def test_the_marker_is_read_unless_nosystem_is_set(self):
        self._envs_passed_by_init_repo(floor_only=True)
        readable = system_marker(self.home, self.floor_env)
        skipped = system_marker(self.home, {**self.floor_env, "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertEqual((readable, skipped), ((0, "read"), (1, "")))


class TestTheIsolatedEnvStopsNothing(_InitRepoFloor):
    """床 (`isolate_git_config` と、それを呼ぶ init_repo の床) が、当てる側を呼ぶ直前に持つ env だけでは、
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


if __name__ == "__main__":
    unittest.main()
