"""ローカル CLI 設定ファイルの読み取り (最小 YAML / INI サブセット)。

**なぜ必要か**: アクティブアカウントを CLI 実行 (`gh auth status` /
`gcloud config get-value`) で取ると PreToolUse hook 1 回あたり数百 ms〜数秒かかる。
`gh auth status` は全 host のトークンを API で検証するため**ネットワーク往復**まで
含み、オフラインでは失敗して「アクティブアカウントを取得できません」で deny する。
一方どちらの CLI も**アクティブアカウントをローカルの設定ファイルに書いている**
ので、読めるときはファイルから決めたほうが速く、オフラインでも動く
(内部バックログ: 離脱率低減)。

**どこまで解釈するか**: 汎用の YAML / INI パーサは持たない (標準ライブラリに YAML は
無く、外部依存を入れない方針)。ここで受け付けるのは実際の設定ファイルが使っている
最小形だけで、**少しでも想定外の形が出てきたら `None` を返す**。呼び出し側は `None`
を「ローカルからは決められない」と解釈して従来の CLI 実行へ落とす。

「読めたつもりで違う値を返す」ほうが「読めなかった」より危険なため、判読できない形は
すべて後者へ倒す (fail to CLI)。CLI 実行は遅いだけで、判定そのものは従来と同じになる。
"""
from __future__ import annotations

import configparser
import errno
import os
import stat
import sys
from pathlib import Path

# 設定ファイルの読み取り上限。gh の hosts.yml / gcloud の config_<name> はいずれも
# 数 KB 以下なので、これを超える内容は「想定している設定ファイルではない」として
# 読まない (巨大ファイルを hook の中で全部読まないためのガードでもある)。
#
# JSON として読む信頼できないファイル (.firebaserc・firebase.json・accounts.local.json・
# cache の状態ファイル) にも同じ上限を使う。Python 3.9 の `json.loads` は大きな整数の parse が
# 桁数の二乗の時間になり (2.2 MB で 25 秒。3.11 以降は 4,300 桁で ValueError)、hook の
# timeout を超えると出力が捨てられて検証が素通しになる。parse の前に大きさで弾く。
MAX_FILE_BYTES = 256 * 1024


class FileTooLargeError(ValueError):
    """`read_bounded_text` が上限 (既定 `MAX_FILE_BYTES`) を超えるファイルを見つけた。"""


class NotRegularFileError(OSError):
    """読むと止まりうるので読まなかった (`read_regular_bytes` は通常のファイルでないもの、
    `read_bounded_text` は FIFO)。

    OSError の子なので、呼び出し側は「読めない」と同じ except で受けられる。
    """

# 値の先頭に現れたら解釈を諦める YAML の記法 (ブロックスカラー / anchor / alias /
# tag / flow collection)。単純な `key: value` 以外は扱わない。
_UNSUPPORTED_VALUE_HEADS = frozenset("|>&*!{}[]?%@`")

_QUOTES = ("'", '"')

HOME_ENV_VAR = "HOME"


def home_overridden(env) -> bool:
    """コマンド実行時の `HOME` が hook プロセスと違うなら True (= CLI に委ねる)。

    `HOME` は **gh も gcloud も設定ディレクトリの解決に使う** env である
    (gh: `GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `$HOME/.config/gh`、
    gcloud: `CLOUDSDK_CONFIG` → `$HOME/.config/gcloud`)。`HOME=<other> gh ...` の
    形でインライン指定されたコマンドは**別の設定ファイル**を読んで動くため、
    hook プロセス側の設定ファイルを読んで一致と判断すると、実行される CLI とは
    違うアカウントで allow しうる (ローカル読取の導入前は検証 subprocess にも
    同じ env を渡していたため、この形は不一致として deny されていた)。

    設定ディレクトリを `HOME` から組み直す方向 (env の追従) は採らない —
    gh と gcloud で解決の分岐が違い、推測で実装して取り違えた値で allow する
    より、このモジュールの一貫した方針どおり**エミュレートせず CLI に委ねる**
    方が安全。コストは「CLI を 1 回呼ぶ」だけ。

    `GH_CONFIG_DIR` / `CLOUDSDK_CONFIG` が明示されていて `HOME` が効かない場合も
    区別せず bail する (判断を単純に保つ側に倒す)。Windows の gh / gcloud は既定の設定ディレクトリを
    `HOME` からは組まない (`config_dir_rule`) が、同じく区別せず bail する。
    """
    value = env.get(HOME_ENV_VAR)
    return bool(value) and value != os.environ.get(HOME_ENV_VAR)


def overriding_home(env) -> Path | None:
    """コマンドの行頭の `HOME=...` が hook プロセスと違う (`home_overridden`) なら、その HOME。

    違わなければ None (呼び出し側は hook プロセスの `Path.home()` を使う)。成功 cache の指紋を取る
    状態ファイル (service の `state_files`) の場所を、実行される CLI と同じ HOME で決めるために使う
    (v0.35.0)。ローカル読取は HOME が違えば CLI に委ねるので、これを使わない。
    """
    return Path(env[HOME_ENV_VAR]) if home_overridden(env) else None


# Windows の gh / gcloud が既定の設定ディレクトリを置く env (v0.35.1。gh は `%AppData%\GitHub CLI`、
# gcloud は `%APPDATA%\gcloud`)。Windows の環境変数の名前は大文字小文字を区別しないので、gh の `AppData` と
# gcloud の `APPDATA` は同じ変数を指す。hook の Python の `os.environ` は Windows で名前を大文字にして持つ。
APPDATA_ENV_VAR = "APPDATA"
WINDOWS = "windows"
POSIX = "posix"


def config_dir_rule() -> str | None:
    """gh / gcloud の既定の設定ディレクトリの決まりを、hook の Python の OS で選ぶ (v0.35.1)。

    - `WINDOWS` (`os.name == "nt"`): gh は `GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `%AppData%\\GitHub CLI`
      (gh のソースの go-gh `ConfigDir` と `gh help environment`)、gcloud は `CLOUDSDK_CONFIG` →
      `%APPDATA%\\gcloud` (gcloud の公式文書の configurations の頁)。どちらも `HOME` から組まない
    - `POSIX` (macOS / Linux): gh は `GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `~/.config/gh`、gcloud は
      `CLOUDSDK_CONFIG` → `~/.config/gcloud` (0.35.0 までと同じ)
    - None: Cygwin / MSYS の Python (`os.name` は `posix`)。hook の Python は POSIX の `HOME` を見るが、
      そこから起動する gh / gcloud は Windows の決まりで動きうるので、場所を決められない

    呼び出し側は、場所を決められないとき (None、または Windows で値のある env が無いとき) は
    ローカル読取をせず CLI に聞く (`windows_config_dir` の docstring)。0.35.0 までは Windows でも
    `~/.config` の下を読み、CLI が使っていない古いファイルで一致と判定することがあった。
    """
    if os.name == "nt":
        return WINDOWS
    if sys.platform.startswith(("cygwin", "msys")):
        return None
    return POSIX


def windows_env(env, name: str) -> str | None:
    """Windows の決まりで env から name の値を読む。未設定と空の値は ""、決められないなら None。

    Windows の環境変数の名前は大文字小文字を区別しない。行頭の inline env (`AppData=... gh ...`) は書かれた
    綴りのまま env に入るので、大文字小文字だけ違う名前が別々の値で並びうる。CLI がどちらを読むかは
    決められないので None を返す (値が同じなら、その値)。
    """
    wanted = name.upper()
    values = {value for key, value in env.items() if key.upper() == wanted}
    if len(values) > 1:
        return None
    return next(iter(values)) if values else ""


def windows_config_dir(env, candidates) -> Path | None:
    """Windows の CLI の設定ディレクトリを、candidates の順に env を見て決める (v0.35.1)。

    candidates は (env の名前, その値の下の dir 名。値そのものなら None) の列。CLI と同じく、最初に値の
    ある (空でない) env で決まる。どの env にも値が無いとき、または途中の env を決められないとき
    (`windows_env` が None) は None。AppData も無いときの行き先 (gh は go-gh の `os.UserHomeDir()` =
    `USERPROFILE` の下の `.config\\gh`。gcloud は公式の文書に書かれていない) は組まない。普段の Windows には
    APPDATA があり、無い環境は確かめられないため (呼び出し側はローカル読取をせず CLI に聞き、成功 cache の
    指紋も取らない)。
    """
    for name, child in candidates:
        value = windows_env(env, name)
        if value is None:
            return None
        if value:
            return Path(value) / child if child else Path(value)
    return None


def state_files_when_dir_unknown() -> list[Path] | None:
    """設定ディレクトリを決められないときに service の `state_files` が返す値 (v0.35.1)。

    POSIX で `HOME` を決められないとき (`Path.home()` が失敗) は 0.35.0 のまま `[]` (指紋は一定で、
    成功 cache は今までどおり hit する)。Windows / Cygwin / MSYS で場所を決められないときは None
    (dispatcher はその target の成功 cache を読まず書かない = 毎回照合する)。CLI が実際に書き換える
    ファイルを見ずに cache で通すと、hook を通らない切替の後に期待と違うアカウントの write が通りうるので、
    通す側に倒さない。
    """
    return [] if config_dir_rule() == POSIX else None


def _read_without_blocking(path: Path, limit: int | None, *, regular_only: bool) -> bytes:
    """開くときにも読むときにも止まらずに、limit バイトまで読む (None なら全部)。

    FIFO (書き手のいない名前付きパイプ) を普通に開くと open が書き手を待って戻らない (デバイスも
    読みが終わらないものがある)。hook の中の読み取りには subprocess の timeout (検証時間の予算) が
    効かず、Claude Code の hook timeout で打ち切られると出力が捨てられて検証が飛ぶ (公式仕様上の
    fail-open)。そこで `O_NONBLOCK` で開き (FIFO の open がすぐ戻る)、fstat で種類を確かめてから
    読む。FIFO は読まずに `NotRegularFileError` (OSError の子)。regular_only なら、通常のファイルで
    ないもの (デバイス・ディレクトリなど) も同じ。通常のファイルの読みは `O_NONBLOCK` の影響を
    受けない。デバイスは `O_NONBLOCK` のまま読むので、データの無い端末などは待たずに
    `BlockingIOError` (OSError の子) になる。`O_NONBLOCK` / `O_BINARY` の無い OS では 0 として扱う
    (Windows に FIFO のパスは無く、`O_BINARY` は Windows で改行の変換をさせないため)。

    開けない・読めないときは OSError、パスに NUL があるときは ValueError (どちらもそのまま投げる)。
    """
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISFIFO(mode) or (regular_only and not stat.S_ISREG(mode)):
            raise NotRegularFileError("FIFO などの通常のファイルでないものは読みません")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read() if limit is None else handle.read(limit)
    finally:
        os.close(fd)
    if raw is None:
        # O_NONBLOCK のデバイスに、まだ読めるデータが無い (BufferedReader は None を返す)
        raise BlockingIOError(errno.EAGAIN, "読めるデータがありません")
    return raw


def read_regular_bytes(path: Path, limit: int | None = None) -> bytes:
    """通常のファイルを、開くときにも読むときにも止まらずに読む (limit バイトまで。None なら全部)。

    通常のファイルでないもの (FIFO・デバイス・ディレクトリなど) は開いても読まずに
    `NotRegularFileError` (OSError の子。`_read_without_blocking`)。gh / gcloud の設定 (`read_text`。
    v0.35.0)、aws の config (`aws.profiles_for_account`)、linked worktree の gitdir と commondir
    (`paths._read_worktree_meta`) がこれで読む (後の 2 つは v0.36.3。それまでは普通に開いていたので、
    書き手のいない FIFO で読みが止まり、hook timeout で検証が飛んでいた)。
    """
    return _read_without_blocking(path, limit, regular_only=True)


def read_bounded_text(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    """limit (既定 `MAX_FILE_BYTES`) を超えない範囲で UTF-8 の文字列として読む (parse の前の大きさの確認)。

    超えるときは `FileTooLargeError`、読めないときは OSError、UTF-8 でないときは
    UnicodeDecodeError (どちらも ValueError / OSError の子で、呼び出し側が JSON の失敗と
    同じ except で受けられる)。

    FIFO は開いても読まずに `NotRegularFileError` (OSError の子。`_read_without_blocking`)。
    0.36.2 までは普通に開いていたので、firebase の configstore (`isfile` で確かめずに読む) が書き手の
    いない FIFO だと読みが止まり、hook timeout で検証が飛んでいた (v0.36.3)。

    デバイスは 0.36.2 と同じく読む (`/dev/zero` は大きすぎる、`/dev/null` は空。データの無い端末などは
    待たずに OSError)。`read_regular_bytes` のように読めない側にまとめないのは、configstore では
    「大きすぎる」(切替先を確かめられない) と「読めない」で結果が違い、まとめると `/dev/zero` の
    configstore で、照合を止めていたのが `.firebaserc` の `default` で照合するように変わるため。
    """
    raw = _read_without_blocking(path, limit + 1, regular_only=False)
    if len(raw) > limit:
        raise FileTooLargeError(f"{limit} バイトを超えています")
    return raw.decode("utf-8")


def read_text(path: Path) -> str | None:
    """設定ファイルを文字列で読む。読めない / 大きすぎる / UTF-8 でない / 通常のファイルでないなら None。

    **開くときにも読むときにも止まらない** (`read_regular_bytes`)。成功 cache の指紋のパスを決める
    読み (gcloud の `state_files`) は cache を見る前に毎回走るので、止まると cache hit の経路も
    止まる。通常のファイルでなければ読まずに None (呼び出し側は CLI に委ねる)。パスに NUL が
    あるときの ValueError も読めないのと同じに扱う。
    """
    try:
        raw = read_regular_bytes(path, MAX_FILE_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _unquote(token: str) -> str | None:
    """`'x'` / `"x"` の引用符を外す。閉じていない引用符は解釈不能 (None)。"""
    if len(token) >= 2 and token[0] in _QUOTES and token[-1] == token[0]:
        return token[1:-1]
    if token[:1] in _QUOTES:
        return None
    return token


def parse_nested_scalar_map(text: str) -> dict[str, dict[str, str]] | None:
    """`{トップレベルキー: {直下のキー: スカラー}}` だけを読む最小 YAML パーサ。

    gh の `hosts.yml` が実際に使っている形 (実測: gh 2.9x) に限定して解釈する::

        github.com:
            git_protocol: https
            users:
                some-user:
                other-user:
            user: some-user

    - インデント 0 の `key:` (値なし) がブロックの開始。ブロック内で**最初に現れた
      インデント**をそのブロックの「直下」とみなす
    - 直下の `key: value` だけを採用する。**より深い行は読まない** —
      `users:` の下に並ぶのは「そのホストに紐付く全アカウント」で、アクティブな
      アカウント (`user:`) とは別物。深い行を混ぜると非アクティブなアカウントを
      アクティブと誤認し、**期待値と一致する非アクティブアカウントで allow する**
      という最悪の誤りになる
    - 直下より浅い (かつ 0 でない) インデントが出てきたら形を掴めていないので None

    タブインデント / シーケンス (`- `) / ブロックスカラー / anchor / alias /
    インデント 0 のスカラー (`version: 2` のような将来のフォーマット変更) は
    いずれも None を返す = 呼び出し側が CLI 実行に落ちる。

    Returns:
        トップレベルキーごとの直下スカラー辞書。解釈できない形なら None。
    """
    result: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    child_indent: int | None = None
    for raw_line in text.splitlines():
        if "\t" in raw_line:
            return None
        line = raw_line.rstrip()
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            return None
        indent = len(line) - len(stripped)
        key_part, sep, value_part = stripped.partition(":")
        if not sep:
            return None
        key = _unquote(key_part.strip())
        if not key:
            return None
        value = value_part.strip()
        if indent == 0:
            # トップレベルにスカラーが現れる形は想定していない (hosts.yml には
            # 現れない)。形が変わったら読まずに CLI へ委ねる。
            if value:
                return None
            current = {}
            child_indent = None
            result[key] = current
            continue
        if current is None:
            return None
        if child_indent is None:
            child_indent = indent
        if indent > child_indent:
            continue
        if indent < child_indent:
            return None
        if not value:
            # 直下のキーがさらに入れ子を持つ形 (`users:`)。中身は読まない。
            continue
        if value[0] in _UNSUPPORTED_VALUE_HEADS:
            return None
        scalar = _unquote(value)
        if scalar is None:
            return None
        current[key] = scalar
    return result


def parse_ini_sections(text: str) -> dict[str, dict[str, str]] | None:
    """`[section]` + `key = value` の INI を `{section: {key: value}}` で返す。

    gcloud の `configurations/config_<name>` が実際に使っている形 (実測)::

        [core]
        account = someone@example.com
        project = my-project-id

    補間 (`%(x)s`) を行わない `RawConfigParser` を使う — 値に `%` が入っていても
    壊れないようにするため。重複キー・セクション外の行など configparser が
    エラーにする形は None を返し、呼び出し側を CLI 実行へ落とす。
    """
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except (configparser.Error, ValueError):
        return None
    return {
        section: {key: value for key, value in parser.items(section)}
        for section in parser.sections()
    }
