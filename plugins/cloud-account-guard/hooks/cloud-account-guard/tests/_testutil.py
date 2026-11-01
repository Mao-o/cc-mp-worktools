"""テスト共通のパス設定と実環境からの隔離。

パス設定: cloud-account-guard/ と hooks/ を sys.path に通す。

隔離: v0.13.0 以降、`verify()` は CLI を起動する前に **ローカル設定ファイル**
(`~/.config/gh/hosts.yml` / `~/.config/gcloud/configurations/config_<name>`) を読み、
dispatcher は accounts.local.json が無いとき `$HOME` のグローバル既定を見る。
何もしないと**開発者の実環境が verdict を変える** (実際に、開発者の hosts.yml の
アクティブアカウントが fixture の期待値と一致して「CLI を呼ばずに allow」になり、
CLI モックを前提にしたテストが壊れた)。`start_isolation()` で設定ディレクトリと
`$HOME` を使い捨て dir に固定し、判定を変えうる env を落とす。
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


def _isolate_git_discovery() -> None:
    """外側の環境が git の向き先を決めないようにする (テスト実行プロセスの間ずっと)。

    - `GIT_DIR` などが export されていると、テスト内の git が別の repo を見る
    - 一時 dir が git の作業ツリーの中にあると、「repo の外」を前提にしたテストが
      親の repo を見つける。一時 dir 自体を探索の天井にして、その親へ遡らせない
    """
    import tempfile

    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        os.environ.pop(name, None)
    base = Path(tempfile.gettempdir()).resolve()
    os.environ["GIT_CEILING_DIRECTORIES"] = str(base)
    # production 側の「起動リポジトリ」探索は `.git` を親方向に辿る純 Python で、
    # GIT_CEILING_DIRECTORIES の対象外。一時 dir が作業ツリーの中なら、repo の外にある
    # /tmp へ逃がす (「repo の外」を前提にするテストのため)。
    if any((d / ".git").exists() for d in (base, *base.parents)):
        fallback = Path("/tmp").resolve()
        if fallback.is_dir() and not any(
            (d / ".git").exists() for d in (fallback, *fallback.parents)
        ):
            tempfile.tempdir = str(fallback)
            os.environ["GIT_CEILING_DIRECTORIES"] = str(fallback)


_isolate_git_discovery()


# -- 本物のクラウド CLI を起動させない ------------------------------------------------------------
#
# hook と builder は aws / gcloud / gh / firebase / kubectl を `subprocess.run` で起動する。テストで
# mock を書き忘れると、手元や CI の本物の CLI が走る (実際に、`show` が現在値を問い合わせる経路の 7 件が
# 本物の `aws sts get-caller-identity` と `gh auth status` を起動していた。aws は IMDS の探索待ちで
# 1 件 4 秒以上かかり、認証情報があれば実際に STS に問い合わせる)。builder の `show` は CLI の失敗を
# `except Exception` で握るので、起動の時点で例外を投げてもテストは落ちない。そこで:
#
# 1. `subprocess.Popen` の起動の前に、コマンド語が下の CLI で、解決先が一時 dir の外にあるものを
#    `REAL_CLI_LAUNCHES` に記録し、起動せずに FileNotFoundError にする (「CLI が無い」と同じ扱い)
# 2. 各テストの終わりに、そのテストの間に記録が増えていたら assertion の失敗にする
#
# 一時 dir の中の偽の CLI (テストが PATH の先頭に置くもの) は対象外。シェル (`bash -c` など) や子の
# Python が起動する CLI は、このプロセスの Popen を通らないので見えない。
GUARDED_CLIS = frozenset({"aws", "gcloud", "gh", "firebase", "kubectl"})
REAL_CLI_LAUNCHES: list[tuple[str, tuple[str, ...]]] = []
_REAL_POPEN_INIT = subprocess.Popen.__init__
_REAL_TESTCASE_RUN = unittest.TestCase.run


def _is_under(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def real_cli_target(args, kwargs) -> str | None:
    """`Popen(args, **kwargs)` が一時 dir の外の対象 CLI を起動するなら、その解決先 (それ以外は None)。"""
    if kwargs.get("shell"):
        return None
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [args]
    else:
        try:
            argv = list(args)
        except TypeError:
            return None
    program = kwargs.get("executable") or (argv[0] if argv else None)
    if program is None:
        return None
    word = os.fsdecode(program)
    if os.path.basename(word) not in GUARDED_CLIS:
        return None
    env = kwargs.get("env")
    search = (os.environ if env is None else env).get("PATH", os.defpath)
    if os.sep in word:
        cwd = kwargs.get("cwd")
        resolved = os.path.join(os.fsdecode(cwd), word) if cwd is not None else word
        if not os.path.exists(resolved):
            return None
    else:
        resolved = shutil.which(word, path=search)
        if resolved is None:
            return None
    real = os.path.realpath(resolved)
    if _is_under(real, os.path.realpath(tempfile.gettempdir())):
        return None
    return real


def _guarded_popen_init(self, args, *rest, **kwargs):
    target = real_cli_target(args, kwargs)
    if target is not None:
        argv = (args,) if isinstance(args, (str, bytes, os.PathLike)) else tuple(args)
        REAL_CLI_LAUNCHES.append((target, tuple(os.fsdecode(a) for a in argv)))
        raise FileNotFoundError(
            errno.ENOENT, "テストからは本物のクラウド CLI を起動しない (mock すること)", target
        )
    _REAL_POPEN_INIT(self, args, *rest, **kwargs)


def _fail_if_real_cli_launched(case: unittest.TestCase, start: int) -> None:
    launched = REAL_CLI_LAUNCHES[start:]
    if launched:
        case.fail(
            "本物のクラウド CLI を起動しようとした (get_active_account などを mock する): "
            + "; ".join(f"{target} {list(argv)!r}" for target, argv in launched)
        )


def _run_with_cli_guard(self, result=None):
    # 後始末の最後 (登録が最初なので LIFO で最後) に、このテストの間の記録を確かめる。
    self.addCleanup(_fail_if_real_cli_launched, self, len(REAL_CLI_LAUNCHES))
    return _REAL_TESTCASE_RUN(self, result)


subprocess.Popen.__init__ = _guarded_popen_init
unittest.TestCase.run = _run_with_cli_guard


# テストの repo で git に自動 gc / maintenance を起こさせない設定 (key, value)。
#
# `git commit` は終わりに `git maintenance run --auto --detach` を起動する。git 2.55 は auto
# maintenance の既定の戦略が geometric で、小さな repo でも `.git/objects/17` に loose object が 2 件
# あるだけで repack を始めうる。`--detach` は自動条件の判定の前に背景へ切り離すので、commit は待たずに
# 戻る。その repack が `.git/objects/pack` に書いている間に一時 dir の後始末が走ると
# `Directory not empty` で落ちうる (CI の flaky の予防。この suite は `rmtree(ignore_errors=True)` で
# 後始末するので落ちず、tmp に残骸と、テストより長く生きる背景の git が残る)。git 2.50 は戦略が gc で
# しきい値が高く、同じ条件でも repack は起きないが、maintenance の起動そのものは commit のたびに起きる。
#
#   maintenance.auto=false: そもそも起動しない
#   gc.auto=0: 起動された maintenance の gc を走らせない (2.50 では gc.auto=0 だけだと起動は止まらない)
#   maintenance.autoDetach=false / gc.autoDetach=false: 何かが走っても背景へ切り離さない
#
# env で渡す (`GIT_CONFIG_COUNT`。git 2.31 以上) のは、repo 自身の config より優先させるため。
# `git push` の受け側 (`receive-pack`) には届かないので、同じ設定を fixture (`hermetic.gitconfig`) にも
# 置き、`GIT_CONFIG_GLOBAL` で指す。
NO_BACKGROUND_GIT_SETTINGS = (
    ("maintenance.auto", "false"),
    ("maintenance.autoDetach", "false"),
    ("gc.auto", "0"),
    ("gc.autoDetach", "false"),
)


def git_config_env(settings: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """`(key, value)` の並びを `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` にする。

    件数は並びから数える (手で書くと、項目を足し引きしたときに COUNT がずれる)。
    """
    env = {"GIT_CONFIG_COUNT": str(len(settings))}
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# repo を作る git に読ませる global / system の設定。global の代わりに読ませるのは tests 配下の fixture
# (自動 maintenance を止める設定だけ)。テストから `git config --global` で書かないこと (tracked の
# file が書き換わる)。既定の除外ファイル (`$XDG_CONFIG_HOME/git/ignore`) はこの指定では外れない。
HERMETIC_GIT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermetic.gitconfig")
HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": HERMETIC_GIT_CONFIG,
    "GIT_CONFIG_NOSYSTEM": "1",
    **git_config_env(NO_BACKGROUND_GIT_SETTINGS),
}

# 外側の env にあると、テストの git が別の repo / config / template を見てしまう変数。外さないと
# repo を作る helper の `init` / `config` / `commit` が外側の repo に書き込む。`GIT_CONFIG_PARAMETERS` は
# `GIT_CONFIG_COUNT` に勝ち、`GIT_TEMPLATE_DIR` は `git init` が写す hook / 除外を差し替える。
OUTER_GIT_LEAK_ENV = (
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


def hermetic_env() -> dict[str, str]:
    """git を起動するときの env: `os.environ` から `OUTER_GIT_LEAK_ENV` を外し、`HERMETIC_GIT_ENV` を足す
    (後から足すので、外側に同名の変数があっても定数が勝つ)。"""
    env = {k: v for k, v in os.environ.items() if k not in OUTER_GIT_LEAK_ENV}
    env.update(HERMETIC_GIT_ENV)
    return env


def git(args: list[str], cwd, *, check: bool = True) -> subprocess.CompletedProcess:
    """テストが repo を作る / 操作するために起動する git。毎回 `hermetic_env()` を env で渡す。

    テストから git を直接起動しないこと (直接の起動は `test_hermetic_env.py` が検出する)。出力は bytes。
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=hermetic_env(),
        check=check,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def init_repo(cwd) -> None:
    """commit できる状態の空の repo を作る (`git init` と user / gpgsign の設定)。"""
    git(["init", "-q", "--initial-branch=main"], cwd)
    git(["config", "user.name", "test"], cwd)
    git(["config", "user.email", "test@example.com"], cwd)
    git(["config", "commit.gpgsign", "false"], cwd)


# 実環境から漏れるとローカル読取 / モード解決の結果が変わる env。
LEAKY_ENV_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "CLOUD_ACCOUNT_GUARD_MODE",
    "CLOUD_ACCOUNT_GUARD_DEBUG",
    "CLOUD_ACCOUNT_GUARD_AUTO_SWITCH",
)

_CLOUDSDK_PREFIX = "CLOUDSDK_"

# 隔離中の `$HOME`。**実在しない絶対パス**を使う:
# - 実 HOME を読まない / 書かない
# - fixture (tempfile 配下) と親子関係を持たないので、親遡及の停止条件
#   (`core/paths._crosses_home`) を実 HOME のときと同じ形に保てる
#   (tmp 配下に置くと共通の親で遡及が止まり、既存テストの前提が変わる)
ISOLATED_HOME = Path("/nonexistent-home-cloud-account-guard")


def cli_config_env(root: Path, home: Path = ISOLATED_HOME) -> dict[str, str]:
    """gh / gcloud のローカル設定 dir を `root` 配下 (= 存在しない) に向ける env。

    `HOME` も含める。`Path.home()` の patch だけでは `os.environ["HOME"]` が実
    環境のまま残り、**「hook プロセスの HOME」の 2 つの読み方が食い違う**:
    設定ディレクトリの解決は patch 後の `Path.home()` を見るのに、
    `cli_config.home_overridden()` は `os.environ["HOME"]` と比べる。ここで
    揃えておかないと、実 `os.environ` からコピーした env を渡すテスト
    (`_LocalConfigBase._env()` / `test_main`) で「HOME 上書き」の判定が実
    `$HOME` を基準に行われ、隔離の内側で実環境が判定に混ざる。
    """
    return {
        "GH_CONFIG_DIR": str(root / "gh"),
        "XDG_CONFIG_HOME": str(root / "xdg"),
        "CLOUDSDK_CONFIG": str(root / "gcloud"),
        "HOME": str(home),
    }


def _leaky_names(env) -> list[str]:
    """`env` に含まれる「判定を変えうる env」の名前 (pop 対象)。

    反復中に削除できるよう list を先に作る。
    """
    names = [name for name in LEAKY_ENV_VARS if name in env]
    names += [
        name
        for name in env
        if name.startswith(_CLOUDSDK_PREFIX) and name != "CLOUDSDK_CONFIG"
    ]
    return names


def sanitized_env(base) -> dict[str, str]:
    """`base` のコピーから判定を変えうる env を落とす (`base` は変更しない)。

    子プロセスを起動するテスト (`test_main`) が `start_isolation()` と**同じ
    除去規則**を使うための共有点。prefix ループを各所で再実装すると、
    `LEAKY_ENV_VARS` に足したときに一方だけ更新される。
    """
    env = dict(base)
    for name in _leaky_names(env):
        env.pop(name, None)
    return env


class _Isolation:
    def __init__(self, patchers, home: Path):
        self._patchers = patchers
        self.home = home

    def stop(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()


# pathlib の `Path.is_file()` が Python 3.13 まで握りつぶす errno。これ以外の OSError
# (ENAMETOOLONG・EACCES など) はそのまま投げる (3.14 からは False を返す)。
_PATHLIB_IGNORED_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)
_REAL_IS_FILE = Path.is_file
# 本物の `Path.is_file()` が、上の errno 以外の stat の失敗を例外にする版 (3.14 から False)。
REAL_IS_FILE_RAISES = sys.version_info < (3, 14)


def _is_file_like_py313(self, *args, **kwargs):
    # 3.13 の本物と同じく、follow_symlinks を stat に渡し、パスの ValueError (NUL を含む等) は
    # False にする。
    try:
        os.stat(self, follow_symlinks=kwargs.get("follow_symlinks", True))
    except OSError as e:
        if e.errno not in _PATHLIB_IGNORED_ERRNOS:
            raise
        return False
    except ValueError:
        return False
    return _REAL_IS_FILE(self, *args, **kwargs)


def patch_is_file_like_py313():
    """`Path.is_file` を Python 3.13 までの挙動 (上の errno 以外の OSError を投げる) に差し替える。

    try の外の `Path.is_file()` から例外が抜ける経路を、3.14 以降でも再現するために使う
    (返り値は patcher。`start()` / `stop()` か with で使う)。使う側は、本物がその版で fixture に
    どう振る舞うか (`assert_real_is_file_on_this_version`) も前提として確かめる。
    """
    return mock.patch.object(Path, "is_file", _is_file_like_py313)


def assert_real_is_file_on_this_version(case, path) -> None:
    """前提: 本物の `Path.is_file` が、この版で stat できない path を 3.13 までは OSError に、
    3.14 からは False にする (差し替えが再現しているのが本物の旧い挙動であること)。"""
    if REAL_IS_FILE_RAISES:
        case.assertRaises(OSError, _REAL_IS_FILE, path)
        return
    try:
        result = _REAL_IS_FILE(path)
    except OSError as e:
        case.fail(f"本物の Path.is_file がこの版 ({sys.version.split()[0]}) で OSError を投げた: {e!r}")
    case.assertFalse(result)


def start_isolation(root: Path, home: Path = ISOLATED_HOME) -> _Isolation:
    """実環境の CLI 設定 / `$HOME` / env から切り離す。

    `setUpModule()` か `setUp()` から呼び、返り値の `stop()` で解除する
    (`addCleanup` / `tearDownModule` に登録すること)。
    """
    patchers = [
        mock.patch.dict(os.environ, cli_config_env(Path(root), home)),
        mock.patch.object(Path, "home", staticmethod(lambda: home)),
    ]
    for patcher in patchers:
        patcher.start()
    # patch.dict は stop() で元の内容を復元するため、開始後の pop は安全。
    for name in _leaky_names(os.environ):
        os.environ.pop(name, None)
    return _Isolation(patchers, home)


def assert_utf8(test, text: str) -> None:
    """text が UTF-8 で書けることを確かめる。書けないときは例外ではなく assertion の失敗にする。"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as e:
        test.fail(f"UTF-8 で書けない文字が残った ({e}): {text!r}")
