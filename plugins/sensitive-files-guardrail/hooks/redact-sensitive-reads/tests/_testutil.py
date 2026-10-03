"""テスト共通のパス設定とヘルパ。

repo を作る / commit する git は、ここの `git()` / `init_repo()` を通す (理由は
`NO_BACKGROUND_GIT_SETTINGS` のコメント)。
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

# Stop hook (check-sensitive-files) 側のモジュール (``checker`` / ``stop_ack``)
# を import したいテストが使う。
_CHECKER_DIR = _HOOKS_DIR / "check-sensitive-files"


def checker_dir_on_path() -> Path:
    """``check-sensitive-files`` を ``sys.path`` の **末尾** に足して返す。

    **``sys.path.insert(0, ...)`` にしてはいけない** (0.31.0、内部バックログ)。
    両 hook はどちらも ``tests`` という名前のパッケージを持つため、Stop 側の
    ディレクトリが先頭に入ると **プロセス全体で ``tests`` の解決先が入れ替わる**。
    ``sys.path`` の変更はプロセス終了まで残るので、以降に走るテストが巻き込まれる:

    - ``tests/test_logging.py`` の並行ローテーションテストは ``multiprocessing``
      の **spawn** 子プロセスを使う。spawn は target 関数を「モジュール名 +
      関数名」で pickle して子で import し直すため、子が ``tests.test_logging``
      を import できず ``ModuleNotFoundError`` になる (親の ``sys.path`` を
      そのまま受け継ぐので、汚染も受け継ぐ)
    - 実測 (0.30.0): ``python3 -m unittest tests.test_shared_import
      tests.test_logging`` で **100% 再現**。``unittest discover`` では
      アルファベット順に ``test_logging`` が先に走るので再現せず、flaky に見えていた

    末尾に足せば、自 hook の ``tests`` が常に先に解決される。``checker`` /
    ``stop_ack`` は名前が一意なので import には影響しない。
    hook 本体の ``sys.path`` 操作 (``check-sensitive-files/__main__.py``) は
    別プロセスで動く本番経路なので触らない。
    """
    if str(_CHECKER_DIR) not in sys.path:
        sys.path.append(str(_CHECKER_DIR))
    return _CHECKER_DIR

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ---------------------------------------------------------------------------
# エンコーディング形状の fixture (0.31.0、内部バックログ)
#
# BOM / UTF-16 のテストが 1 件も無かったため、BOM 付き ``.env`` で先頭 1 鍵が
# 黙って消える / UTF-16 ``.env`` が「空ファイル」と報告される不具合が
# 1,200 件超のテストを通過していた。**バイト列は commit せず、1 本の UTF-8
# ソースからテスト実行時に再エンコードする** — `fixtures/keys/README.md` と
# 同じ方針で、クレデンシャル形状のバイナリを公開 repo に置かないため
# (`fixtures/encodings/README.md` 参照)。
# ---------------------------------------------------------------------------

# 3 鍵の dotenv (値はすべて明示的なダミー)。先頭鍵が BOM の影響を受けるので、
# 「1 鍵目が消えたか」を検証できるように 1 鍵目にも意味のある名前を置く。
ENCODING_SAMPLE_DOTENV = (
    "DATABASE_URL=postgres://dummy-user:dummy-pass@127.0.0.1/dummydb\n"
    "JWT_SECRET=dummydummydummydummydummydummydummy\n"
    "STRIPE_KEY=dummy_0000000000000000000000\n"
)
ENCODING_SAMPLE_DOTENV_KEYS = ("DATABASE_URL", "JWT_SECRET", "STRIPE_KEY")

# 同じ鍵名を持つ YAML / 構造不明形式 (dotenv 以外のパーサを通す用)。
ENCODING_SAMPLE_YAML = (
    "DATABASE_URL: postgres://dummy\n"
    "JWT_SECRET: dummy\n"
    "STRIPE_KEY: dummy\n"
)
# keys-only scan (構造不明形式) 用。0.31.0 の初版は ``ENCODING_SAMPLE_DOTENV``
# の別名で、マトリクスが dotenv と**同一入力**を 2 回通すだけだった
# (隔離内レビュー P3-6)。``[section]`` 見出しと ``KEY : value`` (コロン + 空白)
# という dotenv パーサが扱わない形にして、``keyonly_scan`` の
# ``^\s*(?:export\s+)?KEY\s*[:=]`` 側の経路を実際に通す。鍵名は 3 形式で
# 揃えておく (同じ assert を使い回すため)。
ENCODING_SAMPLE_OPAQUE = (
    "[section]\n"
    "DATABASE_URL : postgres://dummy\n"
    "JWT_SECRET : dummy\n"
    "STRIPE_KEY : dummy\n"
)

# テスト対象のエンコーディング一覧 (``bytes.encode`` に渡せる名前)。
# ``latin-1`` は「BOM も NUL も無いので推定できない」側の代表で、
# 「取りこぼしを開示する」ことだけを期待する。
ENCODINGS_WITH_DETECTION = ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be")


def encode_sample(text: str, encoding: str) -> bytes:
    """``text`` を ``encoding`` で符号化する (BOM 有無はコーデックに委ねる)。

    ``utf-16`` は Python が BOM を付け、``utf-16-le`` / ``utf-16-be`` は付けない
    (= BOM 無し UTF-16 の検出経路を通す)。
    """
    return text.encode(encoding)

# 内部バックログ: unittest 実行が実ログ (~/.claude/logs/redact-hook.log) を
# 汚染し計測値を誤らせる問題への対処。``core.logging`` はモジュール import 時に
# ``SFG_LOG_PATH`` を読んで書込み先を解決する (``core/logging.py`` 参照) ため、
# 各テストファイルが ``core`` 配下を import するより前に、sys.path bootstrap と
# 同じこの場所で 1 回だけ設定する。全 30 テストファイルが先頭で
# ``from _testutil import FIXTURES`` (または副作用目的の ``import _testutil``)
# する慣例になっており (sys.path 整備が無いと ``from core import ...`` が
# 解決できないため)、``unittest discover`` はモジュールをアルファベット順に
# import するので先頭の ``test_bash_handler.py`` が最初にこのモジュールを
# import し、以降の全モジュールより前に ``SFG_LOG_PATH`` が確定する。
# ``-p <pattern>`` で 1 ファイルだけを対象にする単独実行や、この慣例に沿わない
# モジュールを最初に import する実行順では守られない — 全モジュールが個別に
# この import を持つことで単独実行でも保護が効くようにしている。
# pytest 実行時は ``tests/conftest.py`` が同じガードを持つ (collection が
# テスト module の import より先に走るため、そちらが先に効く)。
if "SFG_LOG_PATH" not in os.environ:
    _tmp_log_dir = tempfile.mkdtemp(prefix="sfg-test-logs-")
    os.environ["SFG_LOG_PATH"] = str(Path(_tmp_log_dir) / "redact-hook.log")
    atexit.register(shutil.rmtree, _tmp_log_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# テストが起動する git の環境
# ---------------------------------------------------------------------------

# テストの repo で git に自動 gc / maintenance を起こさせない設定 (key, value)。
#
# `git commit` / `merge` / `fetch` は終わりに `git maintenance run --auto --detach` を起動する。
# git 2.55 は auto maintenance の既定の戦略が geometric で、小さな repo でも `.git/objects/17` に
# loose object が 2 件あるだけで repack を始めうる。しかも `--detach` は repack の自動条件を判定する
# 前に背景へ切り離すので、commit は待たずに戻る。その repack が `.git/objects/pack` に書いている間に
# `tempfile` の後始末 (`rmtree`) が走ると、後始末が `Directory not empty` で落ちる (CI の flaky。
# object の hash 次第なので偶発的)。git 2.50 は戦略が gc でしきい値 (約 6700 個) が高く、同じ条件でも
# 起きないので、ローカルの実行だけでは気付けない。起動そのものは git 2.50 でも commit のたびに起きる。
#
#   maintenance.auto=false / gc.auto=0: そもそも自動 maintenance を起動しない
#   maintenance.autoDetach=false / gc.autoDetach=false: 何かが走っても背景へ切り離さない
#     (commit が戻る前に終わる)
#
# env で渡す (`GIT_CONFIG_COUNT`。git 2.31 以上) のは、テストが起動する git と、それらが子として
# 起動する git に一括で効かせ、repo 自身の config より優先させるため。ただし `git push` の受け側
# (`receive-pack`) には届かない: ローカルの path へ送るとき git は repo 用の env
# (`GIT_CONFIG_COUNT` など) を外して起動する。外されない `GIT_CONFIG_GLOBAL` が指す fixture
# (`hermetic.gitconfig`) にも同じ設定を置き、そちらで止める (`HERMETIC_GIT_ENV`)。この suite の
# hook は git を起動せず、テスト本体も push しない (床の `test_hermetic_env.py` だけが push する)
# が、もう 1 つの suite (check-sensitive-files) と同じ作りにそろえてある。
NO_BACKGROUND_GIT_SETTINGS = (
    ("maintenance.auto", "false"),
    ("maintenance.autoDetach", "false"),
    ("gc.auto", "0"),
    ("gc.autoDetach", "false"),
)


def git_config_env(settings: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """`(key, value)` の並びを `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` にする。

    件数は並びから数える。手で書くと、項目を足し引きしたときに COUNT がずれる: 多ければ git が
    全コマンドで `missing config key` と言って落ち、少なければ末尾の設定が黙って無視される。
    """
    env = {"GIT_CONFIG_COUNT": str(len(settings))}
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# 開発者の ~/.gitconfig (color.ui=always / diff.external / core.hooksPath 等) と system の config で
# テストが揺れないよう、git に global / system の設定を読ませない。global の代わりに読ませるのは
# tests 配下の fixture で、自動 maintenance を止める設定 (`NO_BACKGROUND_GIT_SETTINGS` と
# `receive.autogc`) だけを持つ。
#
# 止める経路は 2 本あり、どちらも外さない:
#   - env の `GIT_CONFIG_COUNT`: repo 自身の config より優先される。ただし `receive-pack` には届かない
#   - global の fixture: `receive-pack` にも届く。ただし repo 自身の config には負ける
# fixture はテストから `git config --global` で書かないこと (tracked の file が書き換わる)。
HERMETIC_GIT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermetic.gitconfig")
HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": HERMETIC_GIT_CONFIG,
    "GIT_CONFIG_NOSYSTEM": "1",
    **git_config_env(NO_BACKGROUND_GIT_SETTINGS),
}


def git(args: list[str], cwd, *, check: bool = True) -> subprocess.CompletedProcess:
    """テストが起動する git。毎回 `HERMETIC_GIT_ENV` を足す。

    テストクラス側の env patch に頼ると、patch していないクラスが repo を作った時点で自動
    maintenance が復活する (patch 済みなら同じ値の上書き)。repo を作る / commit する git は
    `subprocess.run` を直接書かず、必ずこれを通すこと (直接の起動は `test_hermetic_env.py` が
    検出する)。出力は bytes。`check=False` は、設定の問い合わせのように非ゼロ終了を assertion で
    見たいとき用。
    """
    env = {**os.environ, **HERMETIC_GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True)


def init_repo(cwd) -> None:
    """commit できる状態の空の repo を作る (`git init` と user / gpgsign の設定)。"""
    git(["init", "--initial-branch=main"], cwd)
    git(["config", "user.name", "test"], cwd)
    git(["config", "user.email", "test@example.com"], cwd)
    git(["config", "commit.gpgsign", "false"], cwd)
