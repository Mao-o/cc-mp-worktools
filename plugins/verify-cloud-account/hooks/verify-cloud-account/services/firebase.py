"""Firebase アカウント (プロジェクト) 検証。

accounts.local.json の "firebase" は 2 形式を受け付ける:
- 文字列: `"firebase": "my-project"` — 単一プロジェクト
- オブジェクト: `"firebase": {"default": "proj-dev", "prod": "proj-prod"}`
  alias 名 → project ID のマップ。現在のアクティブがいずれかの値に一致すれば OK
  (`.firebaserc` の projects マップ形式と対応。複数環境運用向け)

現在値の解決順は firebase-tools 本体 (lib/command.js applyRC) に合わせる:

1. `firebase use` (非 TTY) が 1 行で出力する**解決済み project ID**。
   `firebase use <alias|project>` の切替先は configstore (activeProjects) にしか
   保存されず、`.firebaserc` は `--add` / `--alias` 時しか更新されない。
   つまり切替後の現在値を知っているのは CLI と configstore だけ。
   CLI の cwd は `firebase.json` を親方向に探した project root (無ければ
   project_dir) に固定し、プロセスの cwd を継承しない (2. の起点と同じ)。
2. CLI から取れないとき (PATH に無い / 実行不可 / 非ゼロ終了 / 出力が空 / 複数行
   ヘルプ) は、CLI と同じローカル設定ファイルから同じ規則で解決する:
   `firebase.json` を親方向に探した project root (無ければ project_dir) を起点に、
   configstore の activeProjects (親方向に探索) を `.firebaserc` の alias で解決
   → 無ければ `.firebaserc` の alias が 1 つならその値 → `default`。
   configstore を読むのは、`npx firebase ...` 等で hook の PATH に `firebase` が
   無い環境でも `firebase use` の切替を見落とさないため (`.firebaserc` だけを
   読むと default のまま照合して false-allow になる)。`.firebaserc` を firebase-tools と
   同じ内容に読めると確かめられないとき (`_read_firebaserc`) は解決しない (取得不可)。
3. CLI が timeout したときは fallback せず専用メッセージで deny する
   (fail-closed。他 service の timeout と同じ扱い)。

コマンドの `--config` / `-c` は project root を指定したファイルのあるディレクトリに移す
(firebase-tools の detectProjectRoot と同じ。相対パスは project_dir を symlink を解いた実体の
パスにしてから解決する)。1. の cwd と 2. の起点はそこになる。2. の configstore は、そのパスの
親方向だけを探す (実体のパスは試さない。firebase-tools と同じ)。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from core import budget, cli_options, shell_word

# CLI 名の許容形。`\b` だとハイフン付き別コマンド全般を拾ってしまうので空白/終端に
# 限定するが、npm 経由の 2 つの正当な形は明示的に許可する:
# - `npx firebase-tools deploy` → wrapper 剥がし後 `firebase-tools deploy`
# - `npx firebase-tools@13.31.0 deploy` → 同 `firebase-tools@13.31.0 deploy`
#   (npx は `<pkg>@<version>` で版を固定でき、CI や再現手順で頻出する)
# `@<version>` を許可しないと lookahead が `@` で失敗し、**検証対象から丸ごと
# 外れる** (v0.9.0 開発中に実際に作り込んだ退行)。PATTERNS / READONLY /
# STATE_CHANGING / self-remediation の全てで同じ prefix を使うこと — 片方だけ
# 許可すると `npx firebase-tools@13 login` が切替として認識されず成功 cache が残る。
_CLI = r"firebase(?:-tools)?(?:@\S+)?"
PATTERNS = [rf"^{_CLI}(?=\s|$)"]
READONLY = [
    rf"^{_CLI}\s+use\s*$",
    # 認証操作 (login / login:ci / login:add / login:use / logout) は project を
    # 変更しない (OAuth token の取得・ローカル保存のみ。gh の SSH 鍵アップロードの
    # ようなリモート write は無い)。未ログインだと `firebase use` が requireAuth で
    # 失敗して現在値を CLI から取れず、login 自体が deny されるデッドロックになるため
    # 素通しする。
    rf"^{_CLI}\s+(login|logout)(:\S+)?\b",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    rf"^{_CLI}\s+(--version|--help|version|help)\b",
]
# 認証情報を出力する形。READONLY / QUERY を取り消して WRITE 扱いにする。
# `login:ci` は CI 用の refresh token を stdout に出す (上の READONLY の
# `(login|logout)(:\S+)?` に当たって素通ししていた)。**期待外アカウントの token を
# 出しうる**ので検証対象に戻す。deny 文面が案内するのは `firebase login` (引数なし)
# だけなので remediation loop にはならない。
DISCLOSING = [
    (rf"^{_CLI}\s+login:ci(?=\s|$)", frozenset()),
]
# リモート read (資源を変更しない)。不一致でも deny せず警告のみで通す。
# **`use` は入れない** — 引数なしの `firebase use` は既に READONLY で、引数付きの
# `firebase use <alias>` はアクティブ project を切り替える STATE_CHANGING。
# QUERY にすると「期待値以外への切替」が deny から warn + allow に緩むため。
QUERY = [
    rf"^{_CLI}\s+(projects:list|apps:list|functions:list|hosting:sites:list)(?=\s|$)",
]
# アクティブ project (configstore の activeProjects) や認証状態を変えうるコマンド。
# dispatcher が検出すると firebase の成功 cache を破棄する。引数なしの `firebase use`
# は表示のみ (READONLY) で対象外。`use --clear` / `--add` / `--unalias` は含む。
STATE_CHANGING = [
    rf"^{_CLI}\s+use\s+\S",
    rf"^{_CLI}\s+(login|logout)\b",
]
# CLI 名直後に置ける global option (`firebase -P prod use ...`)。dispatcher が剥がした
# 形でも READONLY / STATE_CHANGING / self-remediation を判定する (core/cli_options.py)。
GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "--project", "-P", "--account", "--config", "-c", "--token",
})
GLOBAL_FLAGS = frozenset({"--debug", "--json", "--non-interactive", "--interactive"})
# 「どの project に対して実行するか」をコマンド側で指定する option (v0.9.0)。
# firebase-tools は `--project` / `-P` の値を `.firebaserc` の alias として解決し、
# 該当が無ければ project ID そのものとして使う (requireProject)。
# `--config` / `-c` (v0.18.0) は firebase.json を名指しし、firebase-tools はそのファイルの
# あるディレクトリを project root にする (detectProjectRoot の configPath)。読む
# `.firebaserc` と、configstore の切替先を探す起点がそこに移るので、照合先を変える option
# として扱う (verify() の `context["config"]`)。
CONTEXT_OPTIONS = {
    "--project": "project",
    "-P": "project",
    "--config": "config",
    "-c": "config",
}
# CLI がどの project / アカウントで動くかを決める env (成功 cache のキーに含める。
# services/__init__.py の IDENTITY_ENV_* 契約)。project を選ぶ env は firebase-tools に
# 無い (公式 docs に記載なし) ので、アクティブ project を記録する configstore の場所
# (`XDG_CONFIG_HOME` / `HOME`) と、認証を差し替える env を拾う。
IDENTITY_ENV_VARS = frozenset(
    {"XDG_CONFIG_HOME", "HOME", "FIREBASE_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"}
)
IDENTITY_ENV_PREFIXES: tuple[str, ...] = ()
ACCOUNT_KEY = "firebase"

# deny 文面で案内する remediation コマンド (引数付きの実コマンド形) の正規表現。
# dispatcher は verify() の返り値にこれが一致するときだけ「単独で実行せよ」の注記を
# 付ける。コマンド名だけの言及 (「firebase use がタイムアウトしました」等の診断文) や
# インストール案内・設定ファイルの型不正には一致させない。
REMEDIATION_PATTERNS = (r"firebase use\s+[A-Za-z0-9_.<>-]+", r"firebase login\b")
SETUP_HINT = (
    'Firebase 最小例: {"firebase": "my-project-id"}。'
    "firebase use で現在値を確認可。"
    '複数 alias: {"firebase": {"default":"proj-dev","prod":"proj-prod"}}'
)
# builder (scripts/accounts_builder.py) の書込前スキーマ検証が参照する契約。
# alias 名は任意の文字列を許すため DICT_ALLOWED_KEYS は宣言しない
# (builder 側は getattr の既定値 None を「キー制限なし」と解釈する)。
ACCEPTS_DICT = True
# 下の verify() は dict 期待値を `[v for v in expected.values() if isinstance(v, str)
# and v]` で filter し、**使えない値は falsy / truthy を問わず黙って捨てる**
# (1 つも残らないときだけ reject)。値の形を理由に deny することが無いので、
# builder も個々の値の型では reject しない → "none"。
DICT_VALUE_CHECK = "none"
# SCALAR_EQUIVALENT_DICT_KEY は**宣言しない**。firebase の dict キーは
# `.firebaserc` の alias 名で、verify() の照合 (`current in valid`) は値だけを見て
# キーを一切読まない — つまり「scalar と等価になる特定のキー」が存在しない。
# 一方 alias 名は is_self_remediation が `firebase use <alias>` の対象として受理する
# 情報を持つため、dict → scalar の畳み込みは情報を落とす。builder はキー未宣言の
# service の scalar/dict 混在を conflict (手動解決) に倒す。
TIMEOUT_REASON = (
    "Firebase: firebase use がタイムアウトしました。"
    "再試行するか、ネットワーク接続を確認してください。"
)


def _from_cli(root: str, env=None, config: str | None = None) -> tuple[str, str | None]:
    """`firebase use` (非 TTY) を実行し (project_id, error) を返す。

    非 TTY の `firebase use` はアクティブ project があれば解決済み project ID を
    1 行で出力し、無ければ非ゼロ終了する (stdout は空)。project_id は
    「終了コード 0 かつ単一行・単一トークン」のときだけ採用し、それ以外
    (CLI 未検出 / 実行不可 (権限・形式不正等の OSError) / 非ゼロ終了 / 空 /
    複数行ヘルプ) は "" を返す (呼び出し側がローカル設定に fallback する)。
    timeout だけは error に専用メッセージを入れて返す (fallback しない)。

    cwd は project root (`_project_root(project_dir)` = firebase.json のある root、無ければ
    project_dir。`--config` 付きのコマンドではそのファイルのあるディレクトリ) に固定し、
    ローカル設定 fallback と解決の起点を揃える。hook / builder プロセスの cwd を継承すると、
    builder を project_dir の外から起動したときに無関係なディレクトリの project を報告・
    書込しうる。root が存在しなければ cwd 指定で OSError になり、CLI 不可として扱う。

    config (`--config` の絶対パス) があれば同じ option を付ける。ファイル名が firebase.json
    でなくても、CLI がコマンドと同じディレクトリを project root にするため (付けないと
    root から親方向に firebase.json を探し直す)。
    """
    argv = ["firebase", "use"]
    if config is not None:
        argv += ["--config", config]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=budget.call_timeout(10),
            env=env,
            cwd=root,
        )
    except subprocess.TimeoutExpired:
        return "", TIMEOUT_REASON
    except OSError:
        # FileNotFoundError / PermissionError / "Exec format error" / cwd 不在等。
        # 例外を漏らすと hook が異常終了して無音 fail-open になる。
        return "", None
    if result.returncode != 0:
        return "", None
    out = result.stdout.strip()
    if not out:
        return "", None
    # 単一行・単一トークン以外は project ID とみなさない (ヘルプ末尾の
    # "folder." 等の誤検出を防ぐ)。
    lines = out.splitlines()
    if len(lines) != 1:
        return "", None
    tokens = lines[0].split()
    if len(tokens) != 1:
        return "", None
    return tokens[0], None


def _project_root(project_dir: str) -> str:
    """firebase-tools の detectProjectRoot と同じく、`firebase.json` を親方向に探す。

    見つかればそのディレクトリ、無ければ project_dir (CLI は cwd) を返す。
    `.firebaserc` (loadRC) と configstore 探索の起点として使う。
    """
    start = os.path.abspath(project_dir)
    cur = start
    while True:
        if os.path.isfile(os.path.join(cur, "firebase.json")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return start
        cur = parent


def _config_file(project_dir: str, config: str) -> str | None:
    """`--config <config>` が指す firebase.json の絶対パス。ファイルが見つからなければ None。

    firebase-tools (detectProjectRoot) は config を作業ディレクトリから解決し
    (`path.resolve(cwd, config)`)、そのファイルのあるディレクトリを project root にする。
    ファイルが無ければコマンドを始めずにエラーで終わる。hook はコマンドの作業ディレクトリを
    知らないので、このモジュールの他の箇所 (`_project_root`) と同じく project_dir を CLI の
    作業ディレクトリとみなす。ただし symlink は解いた実体のパスから解決する: Node の
    `process.cwd()` は実体のパスを返すので、論理パスから解決すると root が論理パスになり、CLI に
    渡す `--config` で configstore の切替先 (`firebase use` が実体のパスで記録したもの) を引き
    当て損ねる。絶対パスの config は join で前半が捨てられ、そのまま使われる (Node と同じ)。
    見つからなければ (シェルが展開する `~` など)、呼び出し側は
    「どの project で動くかを確かめられない」として deny する。コマンドの中の `cd` は
    `--config` の無いコマンドと同じく追わない (同じ相対パスのファイルが project_dir の側にも
    あれば、そちらのディレクトリを root にして照合する)。
    """
    path = os.path.abspath(os.path.join(os.path.realpath(project_dir), config))
    return path if os.path.isfile(path) else None


def _reject_constant(name: str):
    """`json.loads` の parse_constant: `NaN` / `Infinity` / `-Infinity` を拒否する (JSON.parse と同じ)。"""
    raise ValueError(f"JSON に無い値: {name}")


# firebase-tools と同じ内容に読めると確かめられない `.firebaserc` の、内容の主な条件
# (`_read_firebaserc` が None を返す条件)。網羅ではないので「など」で終える: Python の json が
# 読めない形には、上限が Python の版と設定で変わるもの (桁の多すぎる整数) もあり、並べない。
# ファイル自体を読めないときも None になるが、内容の条件ではないので並べない。pin-env の
# 「固定できません」と verify() の `--project` の deny が同じ文で示す (「…に当たる」で受ける)。
# 判定は保守的で、厳密な JSON でも文字列の中に `//` (URL など) があるだけで弾く。
FIREBASERC_UNCONFIRMED_CONDITIONS = (
    "UTF-8 でない・U+FEFF がある・// か /* がある (文字列の中の URL なども含む)・"
    "JSON として読めない (NaN など JSON に無い値・構文の誤り・深い入れ子)・"
    "projects がオブジェクトでないか文字列でない値を持つ、など"
)


def _read_firebaserc(root: str) -> dict[str, str] | None:
    """`root/.firebaserc` の projects マップ (alias → project ID)。

    firebase-tools と同じ内容に読めると確かめられたときだけ返す (ファイルが無い (stat できない
    ときも)・トップレベルがオブジェクトでない・`projects` が無いときは空 dict。firebase-tools も
    alias 0 件)。
    確かめられなければ None。`.firebaserc` を読むのはこの関数だけで、呼び出し側は
    1 回の読み込みの結果を判定にも解決にも使う (別々に読むと、入れ子の深さの境目で片方だけ
    RecursionError になり、「同じに読める」と言った内容と違う内容で解決しうる)。

    firebase-tools は `.firebaserc` を cjson で読む: ファイル中のすべての U+FEFF を除き、
    `//` / `/* */` のコメントを除いてから JSON.parse する (不正な UTF-8 は置換文字になり、
    `NaN` 等があると JSON.parse が失敗して alias 0 件になる)。厳密な JSON (`json.loads`) で
    読むと、コメント・先頭の U+FEFF・UTF-8 でないバイトのあるファイルでは alias を 0 件と読み、
    alias のキーの中の U+FEFF は別のキーと読み、`NaN` のあるファイルでは firebase-tools が
    読まない alias を読む。どれも alias の行き先の予測が食い違う。

    判定は保守的: UTF-8 として読めない・U+FEFF を含む・`//` か `/*` を含む (文字列の中でも)・
    厳密な JSON として読めない (入れ子が深すぎて RecursionError になるときも、桁の多すぎる整数が
    Python の上限 (版と設定で変わる) で ValueError になるときも)・`projects` が
    オブジェクトでないか文字列でない値を持つ、のどれかなら None (cjson のコメント除去は
    再現しない)。どれでもなければ、cjson の前処理は何も変えず、JSON.parse と `json.loads` は
    同じ内容を返す。firebase-tools の alias の解決 (`projects[alias] || alias`) は文字列でない
    値もそのまま行き先に使うが、値が文字列以外のときは None にするので、解決まで同じ (違うのは、
    JavaScript のオブジェクトが継承するプロパティ名 (`constructor` など) を firebase-tools
    だけが alias と読むことだけ)。空文字の値も返す (`||` で偽になる値。解決は `_resolve_alias`、
    alias の数は firebase-tools と同じく空文字の alias も数える)。
    """
    path = Path(root) / ".firebaserc"
    # `os.path.isfile` を使う: pathlib の `Path.is_file()` は Python 3.13 まで、ENOENT など以外の
    # OSError (長すぎる名前を指す symlink の ENAMETOOLONG・EACCES) をそのまま投げ、例外が hook の
    # 外まで抜けて検証をスキップしていた。stat できないファイルは無いものとして扱う (firebase-tools
    # も同じ。alias 0 件)。
    if not os.path.isfile(path):
        return {}
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if "\ufeff" in text or "//" in text or "/*" in text:
        return None
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    # トップレベルがオブジェクトでなければ、firebase-tools は既定の `projects: {}` のまま
    # (`{ projects: {}, ...data }`)。`projects` の無いオブジェクトも同じ。
    if not isinstance(data, dict) or "projects" not in data:
        return {}
    # firebase-tools の resolveAlias は `projects[alias] || alias` なので、真になる非文字列
    # (配列・数値・true・オブジェクト) も alias として解決する。文字列以外を捨てて読むと
    # 食い違う。`projects` がオブジェクトでない (null・配列・文字列) ときも、firebase-tools は
    # 添字で引くので食い違いうる。どれも形を単純に言える側 (None) に倒す。
    projects = data["projects"]
    if not isinstance(projects, dict) or any(
        not isinstance(value, str) for value in projects.values()
    ):
        return None
    return projects


def _resolve_alias(projects: dict[str, str], target: str) -> str:
    """firebase-tools の resolveAlias (`projects[alias] || alias`) と同じ解決。

    alias にあればその値、無いか空文字なら target そのもの (project ID として扱う)。
    """
    return projects.get(target) or target


def aliases_for(project_dir: str, project_id: str) -> list[str]:
    """`.firebaserc` で project_id を指す alias 名を返す (名前順)。

    プロジェクトごとの固定 (builder の `pin-env`) 用。`firebase use <alias>` を案内する
    ときに、期待値 (project ID) をそのまま出さずに済む。`.firebaserc` は CLI と同じく
    `firebase.json` を親方向に探した project root から読む。firebase-tools と同じ内容に
    読めると確かめられなければ空 (pin-env はその前に `firebaserc_reads_like_cli` で止める)。
    """
    projects = _read_firebaserc(_project_root(project_dir)) or {}
    return sorted(alias for alias, project in projects.items() if project == project_id)


def firebaserc_reads_like_cli(project_dir: str) -> bool:
    """`.firebaserc` を、このモジュールと firebase-tools が同じ内容に読めるか (無ければ True)。

    条件は `_read_firebaserc` を参照。builder の `pin-env` が `firebase use` を案内する前に
    使う。hook の検証 (verify() の `--project` の照合と、CLI から現在値を取れないときの
    ローカル設定の解決) は `_read_firebaserc` を直接使い、同じ読み方で確かめられなければ
    行き先を確かめられないとして deny する。
    """
    return _read_firebaserc(_project_root(project_dir)) is not None


def resolve_target(project_dir: str, target: str) -> str | None:
    """`firebase use <target>` / `--project <target>` が指す project ID。

    firebase-tools と同じく、target を `.firebaserc` の alias として先に解決し、alias に
    無ければ project ID そのものとして扱う (`.firebaserc` は `firebase.json` を親方向に
    探した project root から読む)。`.firebaserc` を firebase-tools と同じ内容に読めると
    確かめられなければ None (行き先を言えない)。builder の `pin-env` が案内する
    `firebase use` の行き先の確認に使う (verify() の `--project` の照合も同じ規則)。
    """
    projects = _read_firebaserc(_project_root(project_dir))
    if projects is None:
        return None
    return _resolve_alias(projects, target)


def _configstore_path(env=None) -> Path | None:
    """firebase-tools の configstore (`$XDG_CONFIG_HOME` または `~/.config` 配下)。

    env (行頭インライン env をマージした CLI 用 env) が渡されたときはその env だけを
    見て CLI と同じ解釈にする。XDG_CONFIG_HOME も HOME も無ければ None。
    """
    if env is None:
        base = os.environ.get("XDG_CONFIG_HOME")
        if not base:
            home = os.environ.get("HOME") or str(Path.home())
            base = os.path.join(home, ".config")
    else:
        base = env.get("XDG_CONFIG_HOME")
        if not base:
            home = env.get("HOME")
            if not home:
                return None
            base = os.path.join(home, ".config")
    return Path(base) / "configstore" / "firebase-tools.json"


def _from_configstore(root: str, env=None, exact: bool = False) -> str:
    """configstore の activeProjects から `firebase use` の切替先 (alias または project ID) を返す。

    firebase-tools の configstoreProject と同じく root から親方向に探索する
    (論理パスと実体パスの両方を試す)。このファイルには認証トークンも含まれるため、
    JSON として読んだ後 activeProjects 以外は使わず、内容をメッセージに出さない。

    exact (`--config` 付きのコマンド) のときは root からだけ探し、実体パスは試さない。root は
    firebase-tools の projectRoot そのもの (`path.resolve(cwd, config)` の dirname。symlink を
    解かない) で、firebase-tools もそのパスの親方向だけを探す。実体パスでも探すと、
    `--config` のパスが symlink を通るとき、firebase-tools が見ない切替先 (`firebase use` が
    実体のパスで記録したもの) を拾い、別の project で照合して allow しうる。
    """
    try:
        path = _configstore_path(env)
        if path is None:
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, RuntimeError):
        return ""
    active = data.get("activeProjects") if isinstance(data, dict) else None
    if not isinstance(active, dict):
        return ""
    starts = [os.path.abspath(root)]
    real = os.path.realpath(root)
    if not exact and real not in starts:
        starts.append(real)
    for start in starts:
        cur = start
        while True:
            value = active.get(cur)
            if isinstance(value, str) and value:
                return value
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return ""


def _from_local(root: str, env=None, exact: bool = False) -> str:
    """CLI が答えられないとき、firebase-tools と同じローカル設定から現在値を解決する。

    applyRC と同じ順: configstore の切替先を `.firebaserc` の alias で解決
    (alias に無ければ project ID そのもの) → alias が 1 つならその値 → `default`。
    root は project root (`firebase.json` のある root、無ければ project_dir。`--config` 付きの
    コマンドではそのファイルのあるディレクトリ)。exact は `_from_configstore` を参照。

    `.firebaserc` を firebase-tools と同じ内容に読めると確かめられなければ "" (解決しない)。
    alias の行き先を取り違えると、firebase-tools と違う project を現在値として照合してしまう。
    呼び出し側は現在値を取得できないとして deny する。
    """
    projects = _read_firebaserc(root)
    if projects is None:
        return ""
    switched = _from_configstore(root, env, exact)
    if switched:
        return _resolve_alias(projects, switched)
    if len(projects) == 1:
        return next(iter(projects.values()))
    return projects.get("default") or ""


def _resolve(root: str, env=None, config: str | None = None) -> tuple[str, str | None]:
    """現在の Firebase project ID を (current, error) で返す。

    解決順は `firebase use` → ローカル設定 (モジュール docstring 参照)。root と config は
    `_from_cli` を参照。error は CLI timeout のときだけ非 None で、その場合 current は ""
    (fallback しない)。
    """
    current, err = _from_cli(root, env, config)
    if err:
        return "", err
    if current:
        return current, None
    return _from_local(root, env, exact=config is not None), None


def get_active_account(project_dir: str) -> str | None:
    """現在アクティブな Firebase project ID を返す。取得不可 (timeout 含む) なら None。"""
    current, _err = _resolve(_project_root(project_dir))
    return current or None


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "firebase" キーに書く値を提案する (現状は scalar のみ)。"""
    return get_active_account(project_dir)


# 期待値が案内するコマンドに入れられない形のとき (core/shell_word.py) の文。
# alias / project ID の許容形は builder の `pin-env` の `firebase use` と同じ
# (shell_word.NAME)。
_CHECK_BY_HAND = (
    f'期待値は{shell_word.UNSAFE}。accounts.local.json の "{ACCOUNT_KEY}" を手で確認してください'
)
# dict 期待値の一部の entry だけを案内行から省いたときに添える行。
_SKIPPED_LINE = (
    f"  (ほかの alias は、alias か project ID が{shell_word.UNSAFE}。"
    f'accounts.local.json の "{ACCOUNT_KEY}" を手で確認してください)'
)
# `--project` の行き先を `.firebaserc` から確かめられないときの deny (v0.18.0)。先頭の文
# (`_PROJECT_FLAG_UNCONFIRMED_HEAD`) の後ろに期待値 (`期待=`) を示し、続けて
# `_PROJECT_FLAG_UNCONFIRMED` を置く (verify())。期待値の形の問題ではないので `_CHECK_BY_HAND` は
# 使わない。コマンドが指定した値は文面に出さない (検出コマンドの行に出る)。`--project` を外した
# コマンドは、`.firebaserc` を読む CLI 自身に現在値を聞いて照合する。ただし外すとコマンドの行き先が
# アクティブな project に変わる (指定していた project では動かない) ので、それを言う。`firebase use`
# の語は入れない (切替を案内したことになり、dispatcher の注記の判定にも当たる)。条件の列挙は網羅
# ではないので、締めの文も「当たらない形にすれば確かめられる」とは言い切らない (pin-env の文と
# 同じ)。
_PROJECT_FLAG_UNCONFIRMED_HEAD = "Firebase: --project の行き先を確かめられません"
_PROJECT_FLAG_UNCONFIRMED = (
    "firebase-tools は --project の値を"
    " .firebaserc の alias として先に解決しますが、.firebaserc を firebase-tools と同じ内容に"
    f"読めると確かめられません ({FIREBASERC_UNCONFIRMED_CONDITIONS}に当たる)。"
    "--project を外すと、コマンドはアクティブな project で動きます (意図した project が"
    "アクティブかを確かめてから外してください)。または .firebaserc をこれらに当たらない形に"
    "すると確かめられることがあります"
)
# `--config` / `-c` のファイルが見つからないときの deny (v0.18.0)。firebase-tools はファイルが
# 無ければコマンドを始めないが、hook の見立て (作業ディレクトリ = プロジェクトのディレクトリ) が
# 外れているだけなら、別のディレクトリの `.firebaserc` と project で動く。
_CONFIG_NOT_FOUND = (
    "Firebase: --config のファイルが見つからないため、どの project で動くかを確かめられません"
    " (firebase-tools はそのファイルのあるディレクトリの .firebaserc と、そこで選んだ project で"
    "動きます。hook は相対パスをプロジェクトのディレクトリから探します)。絶対パスか、"
    "プロジェクトのディレクトリからの相対パスで指定してください"
)
# `--config` / `-c` 付きのコマンドの deny で切替を案内する文 (v0.18.0)。firebase-tools は
# `firebase use` の切替先を project root ごとに記録し、`--config` 付きのコマンドはそのファイルの
# あるディレクトリから親方向に探した切替先で動く。そのディレクトリがプロジェクトのディレクトリと
# 別で、そこに切替先が記録されていると、プロジェクトのディレクトリで `firebase use <期待値>` を
# 打っても変わらない (その形で案内すると、案内どおりに切り替えても同じ deny を繰り返す)。
# そのためコマンドの形では案内しない (REMEDIATION_PATTERNS に当たらない文にする)。括弧の中は、
# そのファイルがプロジェクトのディレクトリにあるときも成り立つ文にする。この deny の先頭行に
# 示す値 (`現在=` と `期待=`) も許容形のものだけにするので、dispatcher の「単独で実行」の注記は
# 付かない (`_shown_current` / `_shown_expected`)。`--config` のパスが symlink を通るときは、
# そのディレクトリで切り替えても効かないことがある (firebase-tools は symlink を解かないパスで
# 探す。README の既知の制限)。
_SWITCH_IN_CONFIG_DIR = (
    "--config のファイルのあるディレクトリで、期待した project に切り替えてください"
    " (切替先はディレクトリごとに記録され、--config 付きのコマンドはそのファイルのあるディレクトリ"
    "から親へ探した切替先で動きます)"
)
# deny の先頭行で、許容形 (`shell_word.WORD`) から外れる値の代わりに示す文。
_NOT_SHOWN = "(表示しない値)"
# `--config` 付きのコマンドの deny と、`--project` の行き先を確かめられない deny で、期待値に
# 許容形から外れる値があるときに添える文 (v0.18.0)。その値は文面に示さない (`期待=` では
# `_NOT_SHOWN`。不一致の deny も、現在値を取得できない deny も同じ)。許容形から外れる値はどの
# project とも一致しないので、案内どおりにしても deny は続く。何を直せばよいかが文面から消えない
# よう、出所だけを言う。理由は言わない (これらの deny は期待値の形に関係なくコマンドの形で案内
# しないので、`_CHECK_BY_HAND` の言う理由は成り立たない)。REMEDIATION_PATTERNS にも
# `shell_word.UNSAFE` の文にも当たらない。
_EXPECTED_NOT_SHOWN = (
    f'表示していない期待値があります (accounts.local.json の "{ACCOUNT_KEY}" を確認してください)'
)


def _config_switch_guide(values) -> str:
    """`--config` 付きのコマンドの deny で、切替を案内する文 (コマンドの形をとらない)。

    `values` (期待値。dict なら有効な値の一覧) に許容形から外れる値があれば、出所を言う文
    (`_EXPECTED_NOT_SHOWN`) を添える。
    """
    if all(shell_word.arg(value) is not None for value in values):
        return _SWITCH_IN_CONFIG_DIR
    return f"{_SWITCH_IN_CONFIG_DIR}。{_EXPECTED_NOT_SHOWN}"


def _shown_current(value: str) -> str:
    """不一致の deny の先頭行の `現在=` に示す値。許容形から外れる値は示さない。

    CLI が無いとき (npx 等)、現在値はリポジトリの `.firebaserc` から解決され、形を確かめて
    いない。値が切替コマンドの形 (`x firebase use evil`) だと、示しただけで dispatcher の
    「単独で実行」の注記の判定に当たり、案内していないコマンドに注記が付く。CLI の答えは
    空白を含まない単一トークンなので当たらない。
    """
    return value if shell_word.arg(value) is not None else _NOT_SHOWN


def _shown_expected(values) -> str:
    """`--config` 付きのコマンドの deny と、`--project` の行き先を確かめられない deny の先頭行の
    `期待=` に示す値 (`, ` 区切り)。

    許容形から外れる値は示さない (`_shown_current` と同じ理由)。これらの deny は期待値の形に
    関係なくコマンドの形で案内しないので、示さない理由の文 (`_CHECK_BY_HAND`) は添えない。出所を
    言う文 (`_EXPECTED_NOT_SHOWN`) は、`--config` の deny では `_config_switch_guide` が、
    `--project` の deny では verify() が添える。
    """
    return ", ".join(
        sorted({value if shell_word.arg(value) is not None else _NOT_SHOWN for value in values})
    )


def _target(value) -> str | None:
    """`firebase use` / `--project` に入れる alias / project ID。許容形でなければ None。"""
    return shell_word.arg(value, shell_word.NAME)


def _alias_lines(expected: dict, command: str) -> list[str]:
    """dict 期待値の案内行 (`<command> <alias>  # → <project>`)。

    command は `firebase use` (切替。各行は self-remediation で通る) か `--project`
    (flag を直す形)。alias か project が許容形から外れる entry は行にしない。alias は
    コマンドの引数なので `firebase use` の許容形 (`shell_word.NAME`)。`#` の後ろの project は
    コメントで、問題になるのは改行 (コメントの外に出てコマンドになる) なので、一般の許容形
    (`shell_word.WORD`。改行・空白・制御文字・非 ASCII を弾く) に限る — ドメイン付きの
    project ID (`example.com:my-project`) も行にできる。省いた entry があれば、その旨と
    手での確認を最後の行に添える。案内できる行が 1 つも無ければ空。
    """
    lines, skipped = [], False
    for alias, project in expected.items():
        if not (isinstance(project, str) and project):
            continue
        alias_arg, project_arg = _target(alias), shell_word.arg(project)
        if alias_arg is None or project_arg is None:
            skipped = True
            continue
        lines.append(f"  {command} {alias_arg}  # → {project_arg}")
    if lines and skipped:
        lines.append(_SKIPPED_LINE)
    return lines


def _project_flag_lines(expected: dict) -> list[str]:
    """dict 期待値に対する `--project <alias>` の候補行。

    `--project` 指定による不一致では、アクティブ project を切り替える
    `firebase use <alias>` を案内しても解決しない — そのコマンドは書かれた
    `--project` の値で実行されるので、切り替えても同じ deny を繰り返す
    (案内どおりに直しても通らない = remediation loop)。flag 自体を直す形を案内する。
    kubectl の `--context` / gcloud の `--project` 不一致文面と同じ方針。
    """
    return _alias_lines(expected, "--project")


def verify(expected, project_dir: str, env=None, context=None) -> str | None:
    """context: 候補コマンドのコンテキスト option (`{"project": "<alias|id>", "config": "<path>"}`)。

    `--project` / `-P` はその実行だけ対象 project を差し替えるため、アクティブ
    project ではなく **flag の値を `.firebaserc` で解決したもの**を照合する
    (CLI 本体の解決規則と同じ: alias にあれば対応 project ID、無ければ値そのもの)。
    `.firebaserc` を firebase-tools と同じ内容に読めると確かめられなければ、行き先を
    確かめられないとして deny する (v0.18.0。fail-closed)。この deny も先頭の文に期待値を
    示す (`--config` 付きのコマンドの deny と同じく、許容形のものだけを示し、外れる値があれば
    出所を言う文を添える)。

    `--config` / `-c` (v0.18.0) は project root (読む `.firebaserc`、configstore の切替先を
    探す起点、`firebase use` の cwd) を、指定したファイルのあるディレクトリにする
    (firebase-tools の detectProjectRoot と同じ)。ファイルが見つからなければ deny する。
    `--config` 付きのコマンドの deny は、切替をコマンドの形で案内せず、そのディレクトリで
    切り替えるよう文で案内する (`_SWITCH_IN_CONFIG_DIR`)。先頭行に示す現在値と期待値も
    許容形のものだけにし (`_shown_current` / `_shown_expected`)、期待値に許容形から外れる値が
    あれば出所を言う文を添える (`_config_switch_guide`)。CLI から現在値を取れない
    ときのローカル設定の解決は、configstore を root (firebase-tools の projectRoot) からだけ
    探す (`_from_configstore` の exact)。
    """
    # 期待値の形を先に検証する (不正な設定のために CLI を叩かない)。
    if isinstance(expected, dict):
        valid = [v for v in expected.values() if isinstance(v, str) and v]
        if not valid:
            return (
                'Firebase: accounts.local.json の "firebase" オブジェクトに'
                " 有効な (文字列値の) project ID が見つかりません。"
            )
    elif not isinstance(expected, str):
        return (
            f'Firebase: accounts.local.json の "firebase" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    context = context or {}
    config = context.get("config")
    if config is None:
        root, config_file = _project_root(project_dir), None
    else:
        config_file = _config_file(project_dir, config)
        if config_file is None:
            return _CONFIG_NOT_FOUND
        root = os.path.dirname(config_file)

    override = context.get("project")
    if override is not None:
        # `.firebaserc` は 1 回だけ読み、確かめた内容でそのまま解決する (`_read_firebaserc`)。
        projects = _read_firebaserc(root)
        if projects is None:
            # 期待値は示す (許容形のものだけ。`--config` 付きのコマンドの deny と同じ部品)。0.17.1 は
            # 同じコマンドの deny (`--project` の不一致) で期待値を示していた。「意図した project が
            # アクティブかを確かめてから外す」には、どの project かが要る。
            values = valid if isinstance(expected, dict) else [expected]
            either = " のいずれか" if isinstance(expected, dict) else ""
            tail = "" if all(shell_word.arg(v) is not None for v in values) else f"。{_EXPECTED_NOT_SHOWN}"
            return (
                f"{_PROJECT_FLAG_UNCONFIRMED_HEAD} (期待={_shown_expected(values)}{either})。"
                f"{_PROJECT_FLAG_UNCONFIRMED}{tail}"
            )
        resolved = _resolve_alias(projects, override)
        # コマンド自身が指定した値は、検証せず quote だけ通して示す (core/shell_word.py)。
        shown = f"--project {shlex.quote(override)}"
        if resolved != override:
            # 行き先はリポジトリの `.firebaserc` の値。この deny は切替を案内しないので、値が
            # 切替コマンドの形 (`x; firebase use other` / 改行入り) だと、表示だけで dispatcher
            # の「案内したコマンドは単独で実行」の注記が付く。許容形のときだけ出す。
            shown += f" (→ {resolved})" if shell_word.arg(resolved) else " (→ 表示しない値)"
        if isinstance(expected, dict):
            if resolved in valid:
                return None
            head = (
                f"Firebase プロジェクト不一致: コマンド指定 {shown}, "
                f"期待={', '.join(sorted(set(valid)))} のいずれか\n"
            )
            lines = _project_flag_lines(expected)
            if not lines:
                return f"{head}--project を外してください ({_CHECK_BY_HAND})"
            return (
                f"{head}--project を外すか、以下のいずれかを指定してください:\n"
                + "\n".join(lines)
            )
        if resolved == expected:
            return None
        head = f"Firebase プロジェクト不一致: コマンド指定 {shown}, 期待={expected}"
        target = _target(expected)
        if target is None:
            return f"{head} — --project を外してください ({_CHECK_BY_HAND})"
        return f"{head} — --project を外すか --project {target} を指定してください"

    current, err = _resolve(root, env, config_file)
    if err:
        return err
    if not current:
        if shutil.which("firebase") is None:
            return (
                "Firebase: firebase コマンドが見つかりません。"
                "npm install -g firebase-tools でインストールしてください。"
            )
        head = "Firebase: 現在のプロジェクトを取得できません。"
        if config_file is not None:
            # 期待値は示す (許容形のものだけ。不一致の deny と同じ)。0.17.1 は同じコマンドの deny で
            # `firebase use <期待値>` を案内し、どの project に切り替えるかを言っていた。この deny は
            # コマンドの形で案内しないので、示さないとそれが文面から消える。
            values = valid if isinstance(expected, dict) else [expected]
            either = " のいずれか" if isinstance(expected, dict) else ""
            return (
                f"{head}期待={_shown_expected(values)}{either}。"
                f"ログインしたうえで、{_config_switch_guide(values)}。"
            )
        if isinstance(expected, dict):
            # `firebase use YOUR_PROJECT` のような placeholder は self-remediation に
            # 乗らず同じ deny を繰り返すため、alias ごとの具体コマンドを案内する。
            lines = _alias_lines(expected, "firebase use")
            if not lines:
                return f"{head}firebase login の後、期待した project に切り替えてください ({_CHECK_BY_HAND})。"
            return (
                f"{head}firebase login の後、以下のいずれかで切り替えてください:\n"
                + "\n".join(lines)
            )
        target = _target(expected)
        if target is None:
            return f"{head}firebase login の後、期待した project に切り替えてください ({_CHECK_BY_HAND})。"
        return f"{head}firebase login && firebase use {target} を実行してください。"

    if isinstance(expected, dict):
        if current in valid:
            return None
        if config_file is not None:
            return (
                f"Firebase プロジェクト不一致: 現在={_shown_current(current)}, "
                f"期待={_shown_expected(valid)} のいずれか\n{_config_switch_guide(valid)}"
            )
        expected_display = ", ".join(sorted(set(valid)))
        head = (
            f"Firebase プロジェクト不一致: 現在={_shown_current(current)}, "
            f"期待={expected_display} のいずれか\n"
        )
        lines = _alias_lines(expected, "firebase use")
        if not lines:
            return f"{head}{_CHECK_BY_HAND}"
        return f"{head}切り替え:\n" + "\n".join(lines)

    if current != expected:
        if config_file is not None:
            return (
                f"Firebase プロジェクト不一致: 現在={_shown_current(current)}, "
                f"期待={_shown_expected([expected])} — {_config_switch_guide([expected])}"
            )
        head = f"Firebase プロジェクト不一致: 現在={_shown_current(current)}, 期待={expected}"
        target = _target(expected)
        if target is None:
            return f"{head} — {_CHECK_BY_HAND}"
        return f"{head} — 切り替え: firebase use {target}"

    return None


_USE_RE = re.compile(rf"^{_CLI}\s+use\s+(\S+)\s*$")

# self-remediation 判定で**剥がしてよい** option の allow-list。基準は
# 「`firebase use` が書き込む先を変えないこと」— `--non-interactive` / `--debug` /
# `--json` は対話と出力の制御だけで、configstore の activeProjects (verify() が
# `firebase use` で読む先) に対する書込先を変えない。
# **`--project` / `-P` / `--config` / `-c` / `--account` / `--token` は入れない** —
# 前 2 つは照合先を差し替える context option、`--config` は firebase.json を
# 名指しして project の解決先を変え、残りは認証主体を変える。
_DECORATION_FLAGS = frozenset({"--non-interactive", "--debug", "--json"})
_DECORATION_OPTIONS_WITH_VALUE: frozenset[str] = frozenset()


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待プロジェクト / alias への firebase use」なら True。

    dict 期待値は alias 名 (キー) と project ID (値) の両方を受け付ける
    (deny メッセージが `firebase use <alias>` を案内するため)。装飾 option
    (`--non-interactive` / `--debug` / `--json`) は剥がしてから照合し、
    **それ以外の option が付いていたら保守的に False** (通常検証に落とす)。
    `use --add` / `--clear` / `--unalias` もここで False になる (位置引数の意味が
    変わる = 期待値への切替とは言えない)。
    """
    normalized = cli_options.strip_allowed_options(
        candidate, _DECORATION_FLAGS, _DECORATION_OPTIONS_WITH_VALUE
    )
    if normalized is None:
        return False
    m = _USE_RE.match(normalized)
    if not m:
        return False
    target = m.group(1)
    if isinstance(expected, str):
        return target == expected
    if isinstance(expected, dict):
        for alias, project in expected.items():
            if not (isinstance(project, str) and project):
                continue
            if target in (alias, project):
                return True
    return False
