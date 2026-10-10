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


def _cjson_decomment(text: str) -> str:
    """cjson 0.3.3 の `decomment` と同じ規則でコメントを除いた文字列。

    `_firebase_json_blocks_local` が「cjson の前処理が何も変えないか」を確かめるためだけに
    使う (除いた結果で JSON を読むことはしない)。cjson の文字列の追跡は、直前の 1 文字が
    `\\` の `"` を常に「エスケープされた引用符」とみなす (`"x\\\\"` の閉じ引用符を見落とす)。
    厳密な JSON でもこの食い違いで文字列の外と見なした `//` / `/*` を除くことがあるので、
    同じ追跡を再現して比べる。
    """
    out: list[str] = []
    in_string = False
    in_comment: int | bool = False
    i = 0
    n = len(text)
    while i < n:
        cur = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if not in_comment and cur == '"' and (text[i - 1] if i > 0 else "") != "\\":
            in_string = not in_string
        if not in_string:
            if not in_comment and cur + nxt == "//":
                i += 1
                in_comment = 1
            elif in_comment == 1 and cur == "\n":
                in_comment = False
            elif not in_comment and cur + nxt == "/*":
                i += 1
                in_comment = 2
                cur = ""
            elif in_comment == 2 and cur + nxt == "*/":
                i += 1
                in_comment = False
                cur = ""
            if in_comment:
                cur = ""
        out.append(cur)
        i += 1
    return "".join(out)


def _firebase_json_blocks_local(path: str) -> bool:
    """firebase.json (`path`) が、ローカル設定からの解決を止める形か。

    firebase-tools の applyRC は、configstore の切替先が無いとき、`.firebaserc` の alias より
    先に firebase.json の旧形式キー `"firebase"` (config.defaults.project) を使う。この規則
    (値を alias として解決するか等) は再現せず、次のどれかなら True (解決しない) を返す:

    - トップレベルのオブジェクトに `"firebase"` キーがある (値に関わらず)
    - firebase-tools と同じ内容に読めると確かめられない: 読めない・UTF-8 でない・U+FEFF を
      含む (cjson はすべての U+FEFF を除くので、キーの中の U+FEFF で別のキーに読める)・
      cjson のコメント除去が何かを除く (コメントがある・cjson の文字列の追跡が外れる)・厳密な
      JSON として読めない (NaN など JSON に無い値・構文の誤り・深い入れ子)

    ファイルが無い (通常のファイルでない) ときと空 (0 バイト) のときは False: firebase-tools も
    設定なし・`{}` として読み、旧形式キーは無い。
    """
    if not os.path.isfile(path):
        return False
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return True
    if not raw:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    if "\ufeff" in text:
        return True
    if ("//" in text or "/*" in text) and _cjson_decomment(text) != text:
        return True
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return True
    return isinstance(data, dict) and "firebase" in data


def _from_local(root: str, env=None, config_file: str | None = None) -> str:
    """CLI が答えられないとき、firebase-tools と同じローカル設定から現在値を解決する。

    applyRC と同じ順: configstore の切替先を `.firebaserc` の alias で解決
    (alias に無ければ project ID そのもの) → alias が 1 つならその値 → `default`。
    root は project root (`firebase.json` のある root、無ければ project_dir。`--config` 付きの
    コマンドではそのファイルのあるディレクトリ)。config_file は `--config` のファイルの絶対パス
    (`--config` 付きのコマンドのとき。configstore は `_from_configstore` の exact で探す)。

    `.firebaserc` を firebase-tools と同じ内容に読めると確かめられなければ "" (解決しない)。
    alias の行き先を取り違えると、firebase-tools と違う project を現在値として照合してしまう。
    firebase.json (`--config` のファイル、無ければ root の firebase.json) が旧形式キー
    `"firebase"` を持つか、その有無を確かめられないときも "" (`_firebase_json_blocks_local`。
    v0.20.0)。firebase-tools は configstore の切替先が無いときそのキーの project で動く。
    呼び出し側は現在値を取得できないとして deny する。
    """
    exact = config_file is not None
    config_path = config_file if exact else os.path.join(root, "firebase.json")
    if _firebase_json_blocks_local(config_path):
        return ""
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
    return _from_local(root, env, config_file=config), None


# プロジェクトごとの固定 (v0.19.0)。firebase-tools は `firebase use` の切替先を
# ディレクトリごとに記録する (公式のディレクトリ単位の仕組み)。期待値が未登録でも、その
# ディレクトリで切替先が記録されていれば dispatcher は止めない。commit される
# `.firebaserc` の `default` は数えない (チームで共有するファイルで、利用者がその
# ディレクトリで選んだ値ではない。数えると firebase のほぼ全リポジトリで未登録の
# deploy が通る)。
PIN_HINT = (
    "Firebase: project をディレクトリごとに固定するには、そのディレクトリで "
    "firebase use <alias または project ID> を 1 回実行してください "
    "(/verify-cloud-account:project-accounts)。"
)


def is_pinned(env, project_dir: str) -> bool:
    """そのディレクトリで `firebase use` の切替先が記録されているか (期待値が未登録のときの判定)。

    firebase-tools と同じく project root から親方向に configstore を探す。CLI は呼ばない。
    """
    return bool(_from_configstore(_project_root(project_dir), env))


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
# `firebase use <x>` / `--project <x>` が期待した project に着く案内を出せないときの文。
# firebase-tools は x を `.firebaserc` の alias として先に解決するので、期待値の alias や project ID
# と同じ名前の alias が別の project を指していたり、alias が無かったり、`.firebaserc` を
# firebase-tools と同じ内容に読めると確かめられなかったりすると、案内どおりにしても期待した
# project にならず、同じ deny に戻る。コマンドの形では案内しない (REMEDIATION_PATTERNS に
# 当たらない文にする)。
_UNREACHABLE_BY_HAND = (
    ".firebaserc の alias の解決先が期待値と合わないか確かめられないため、期待値の名前を渡す"
    f'切り替えでは期待した project になりません。.firebaserc と accounts.local.json の "{ACCOUNT_KEY}"'
    " を手で確認してください"
)
# dict 期待値の一部の entry だけを案内行から省いたときに添える行。
_SKIPPED_LINE = (
    f"  (ほかの alias は、alias か project ID が{shell_word.UNSAFE}。"
    f'accounts.local.json の "{ACCOUNT_KEY}" を手で確認してください)'
)
# dict 期待値の一部の entry を、`.firebaserc` の解決先が合わないため案内行から省いたときに添える行。
_UNREACHABLE_LINE = (
    "  (ほかの alias は、.firebaserc の解決先が期待した project と合わないか確かめられないため"
    "案内していません。.firebaserc を手で確認してください)"
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
# 示す値 (`現在=` と `期待=`) も示せる形のものだけにするので、dispatcher の「単独で実行」の注記は
# 付かない (`shell_word.shown` / `shell_word.shown_all`)。`--config` のパスが symlink を通るときは、
# そのディレクトリで切り替えても効かないことがある (firebase-tools は symlink を解かないパスで
# 探す。README の既知の制限)。
_SWITCH_IN_CONFIG_DIR = (
    "--config のファイルのあるディレクトリで、期待した project に切り替えてください"
    " (切替先はディレクトリごとに記録され、--config 付きのコマンドはそのファイルのあるディレクトリ"
    "から親へ探した切替先で動きます)"
)
# `--config` 付きのコマンドの deny と、`--project` の行き先を確かめられない deny で、期待値に
# 示せない値 (`shell_word.can_show`。0.20.0 までは許容形から外れる値) があるときに添える文
# (v0.18.0)。その値は文面に示さない (`期待=` では `shell_word.NOT_SHOWN`。不一致の deny も、
# 現在値を取得できない deny も同じ)。空白や改行を含む値 (示せない値) はどの
# project とも一致しないので、案内どおりにしても deny は続く。何を直せばよいかが文面から消えない
# よう、出所だけを言う。理由は言わない (これらの deny は期待値の形に関係なくコマンドの形で案内
# しないので、`_CHECK_BY_HAND` の言う理由は成り立たない)。REMEDIATION_PATTERNS にも
# `shell_word.UNSAFE` の文にも当たらない。
_EXPECTED_NOT_SHOWN = (
    f'表示していない期待値があります (accounts.local.json の "{ACCOUNT_KEY}" を確認してください)'
)


def _config_switch_guide(values) -> str:
    """`--config` 付きのコマンドの deny で、切替を案内する文 (コマンドの形をとらない)。

    `values` (期待値。dict なら有効な値の一覧) に示せない値があれば、出所を言う文
    (`_EXPECTED_NOT_SHOWN`) を添える。
    """
    if all(shell_word.can_show(value) for value in values):
        return _SWITCH_IN_CONFIG_DIR
    return f"{_SWITCH_IN_CONFIG_DIR}。{_EXPECTED_NOT_SHOWN}"


def _target(value) -> str | None:
    """`firebase use` / `--project` に入れる alias / project ID。許容形でなければ None。"""
    return shell_word.arg(value, shell_word.NAME)


def _reaching_word(projects: dict[str, str] | None, name: str, project: str) -> str | None:
    """`firebase use <name>` / `--project <name>` に入れる語。project に着かなければ None。

    firebase-tools は name を `.firebaserc` の alias として先に解決する (`_resolve_alias`)。
    解決先が project でない (別の project を指す alias・alias が無く名前が project ID として
    扱われる)、`.firebaserc` を firebase-tools と同じ内容に読めると確かめられない
    (`projects` が None)、`firebase use` の許容形から外れる、のどれかなら None (案内しない)。
    """
    if projects is None or _resolve_alias(projects, name) != project:
        return None
    return _target(name)


def _alias_lines(
    expected: dict, command: str, projects: dict[str, str] | None
) -> tuple[list[str], bool]:
    """dict 期待値の案内行 (`<command> <alias>  # → <project>`) と、解決先で省いた行の有無。

    command は `firebase use` (切替。各行は self-remediation で通る) か `--project`
    (flag を直す形)。`projects` は `.firebaserc` の alias → project ID (`_read_firebaserc`。
    読めると確かめられなければ None)。案内する語は firebase-tools が期待した project に
    解決するものだけ: entry ごとに alias を試し、着かなければ期待値の project ID を試し、
    どちらも着かなければ行にしない (`_reaching_word`)。alias か project が許容形から外れる
    entry も行にしない。alias はコマンドの引数なので `firebase use` の許容形
    (`shell_word.NAME`)。`#` の後ろの project はコメントで、問題になるのは改行 (コメントの外に
    出てコマンドになる) なので、一般の許容形 (`shell_word.WORD`。改行・空白・制御文字・非 ASCII
    を弾く) に限る — ドメイン付きの project ID (`example.com:my-project`) も行にできる。
    省いた entry があれば、その旨と手での確認を最後の行に添える (許容形が理由なら
    `_SKIPPED_LINE`、解決先が理由なら `_UNREACHABLE_LINE`)。案内できる行が 1 つも無ければ空。
    返す bool は、解決先を理由に省いた entry があるか (行が無いときの文を選ぶ)。
    """
    lines, skipped, unreachable = [], False, False
    for alias, project in expected.items():
        if not (isinstance(project, str) and project):
            continue
        project_arg = shell_word.arg(project)
        if project_arg is None or _target(alias) is None:
            skipped = True
            continue
        word = _reaching_word(projects, alias, project) or _reaching_word(projects, project, project)
        if word is None:
            unreachable = True
            continue
        line = f"  {command} {word}  # → {project_arg}"
        if line not in lines:
            lines.append(line)
    if lines and skipped:
        lines.append(_SKIPPED_LINE)
    if lines and unreachable:
        lines.append(_UNREACHABLE_LINE)
    return lines, unreachable


def _login_then(unreachable: bool) -> str:
    """「ログインしたうえで」の前置き。コマンドの形を案内する deny は `firebase login` と書く。

    案内できるコマンドが無い deny (解決先で省いた) は、`firebase login` の語を書かない:
    dispatcher は REMEDIATION_PATTERNS (`firebase login\\b`) に当たる文面に「案内されたコマンドは
    単独で実行」の注記を付けるので、案内していないのに付くことになる。
    """
    return "ログインしたうえで、" if unreachable else "firebase login の後、"


def _scalar_word(projects: dict[str, str] | None, project: str) -> str | None:
    """scalar 期待値 (project ID) に切り替わる `firebase use` / `--project` の語。無ければ None。

    project ID そのもの。ただし `.firebaserc` で同じ名前の alias が別の project を指している
    (影になっている) とき、`.firebaserc` を firebase-tools と同じ内容に読めると確かめられない
    ときは None。影のときに project を指す別の alias を案内しない: scalar の期待値に対する
    self-remediation (`is_self_remediation`) は期待値そのものの名前しか通さないので、alias の
    `firebase use` は検証に回って deny され、案内がまた行き止まりになる (判定は変えない)。
    """
    return _reaching_word(projects, project, project)


def _project_flag_lines(
    expected: dict, projects: dict[str, str] | None
) -> tuple[list[str], bool]:
    """dict 期待値に対する `--project <alias>` の候補行。

    `--project` 指定による不一致では、アクティブ project を切り替える
    `firebase use <alias>` を案内しても解決しない — そのコマンドは書かれた
    `--project` の値で実行されるので、切り替えても同じ deny を繰り返す
    (案内どおりに直しても通らない = remediation loop)。flag 自体を直す形を案内する。
    kubectl の `--context` / gcloud の `--project` 不一致文面と同じ方針。
    """
    return _alias_lines(expected, "--project", projects)


def verify(expected, project_dir: str, env=None, context=None) -> str | None:
    """context: 候補コマンドのコンテキスト option (`{"project": "<alias|id>", "config": "<path>"}`)。

    `--project` / `-P` はその実行だけ対象 project を差し替えるため、アクティブ
    project ではなく **flag の値を `.firebaserc` で解決したもの**を照合する
    (CLI 本体の解決規則と同じ: alias にあれば対応 project ID、無ければ値そのもの)。
    `.firebaserc` を firebase-tools と同じ内容に読めると確かめられなければ、行き先を
    確かめられないとして deny する (v0.18.0。fail-closed)。この deny も先頭の文に期待値を
    示す (`--config` 付きのコマンドの deny と同じく、示せる形のものだけを示し、外れる値があれば
    出所を言う文を添える)。

    `--config` / `-c` (v0.18.0) は project root (読む `.firebaserc`、configstore の切替先を
    探す起点、`firebase use` の cwd) を、指定したファイルのあるディレクトリにする
    (firebase-tools の detectProjectRoot と同じ)。ファイルが見つからなければ deny する。
    `--config` 付きのコマンドの deny は、切替をコマンドの形で案内せず、そのディレクトリで
    切り替えるよう文で案内する (`_SWITCH_IN_CONFIG_DIR`)。先頭行に示す現在値と期待値も
    示せる形のものだけにし (`shell_word.shown` / `shell_word.shown_all`)、期待値に示せない値が
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
            # 期待値は示す (示せる形のものだけ。`--config` 付きのコマンドの deny と同じ部品)。0.17.1 は
            # 同じコマンドの deny (`--project` の不一致) で期待値を示していた。「意図した project が
            # アクティブかを確かめてから外す」には、どの project かが要る。
            values = valid if isinstance(expected, dict) else [expected]
            either = " のいずれか" if isinstance(expected, dict) else ""
            tail = "" if all(shell_word.can_show(v) for v in values) else f"。{_EXPECTED_NOT_SHOWN}"
            return (
                f"{_PROJECT_FLAG_UNCONFIRMED_HEAD} (期待={shell_word.shown_all(values)}{either})。"
                f"{_PROJECT_FLAG_UNCONFIRMED}{tail}"
            )
        resolved = _resolve_alias(projects, override)
        # コマンド自身が指定した値も、示せる形のときだけ示す (core/shell_word.py の shown。値は
        # 検出コマンドの行に出る)。
        shown = f"--project {shell_word.shown(override)}"
        if resolved != override:
            # 行き先はリポジトリの `.firebaserc` の値。この deny は切替を案内しないので、値が
            # 切替コマンドの形 (`x; firebase use other` / 改行入り) だと、表示だけで dispatcher
            # の「案内したコマンドは単独で実行」の注記が付く。示せる形 (`can_show`) のときだけ出す。
            shown += f" (→ {resolved})" if shell_word.can_show(resolved) else " (→ 表示しない値)"
        if isinstance(expected, dict):
            if resolved in valid:
                return None
            head = (
                f"Firebase プロジェクト不一致: コマンド指定 {shown}, "
                f"期待={shell_word.shown_all(valid)} のいずれか\n"
            )
            lines, unreachable = _project_flag_lines(expected, projects)
            if not lines:
                by_hand = _UNREACHABLE_BY_HAND if unreachable else _CHECK_BY_HAND
                return f"{head}--project を外してください ({by_hand})"
            return (
                f"{head}--project を外すか、以下のいずれかを指定してください:\n"
                + "\n".join(lines)
            )
        if resolved == expected:
            return None
        head = f"Firebase プロジェクト不一致: コマンド指定 {shown}, 期待={shell_word.shown(expected)}"
        # 期待値の名前は `.firebaserc` の alias として先に解決される。別の project を指す alias と
        # 同じ名前だと、`--project <期待値>` は期待した project にならない (案内が自分の deny と
        # 矛盾する)。着く語 (期待値そのもの、影のときは期待した project を指す alias) だけを案内する。
        target = _scalar_word(projects, expected)
        if target is None:
            by_hand = _CHECK_BY_HAND if _target(expected) is None else _UNREACHABLE_BY_HAND
            return f"{head} — --project を外してください ({by_hand})"
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
            # 期待値は示す (示せる形のものだけ。不一致の deny と同じ)。0.17.1 は同じコマンドの deny で
            # `firebase use <期待値>` を案内し、どの project に切り替えるかを言っていた。この deny は
            # コマンドの形で案内しないので、示さないとそれが文面から消える。
            values = valid if isinstance(expected, dict) else [expected]
            either = " のいずれか" if isinstance(expected, dict) else ""
            return (
                f"{head}期待={shell_word.shown_all(values)}{either}。"
                f"ログインしたうえで、{_config_switch_guide(values)}。"
            )
        if isinstance(expected, dict):
            # `firebase use YOUR_PROJECT` のような placeholder は self-remediation に
            # 乗らず同じ deny を繰り返すため、alias ごとの具体コマンドを案内する。
            lines, unreachable = _alias_lines(expected, "firebase use", _read_firebaserc(root))
            if not lines:
                # 案内できる行が無くても期待値は示す (許容形のものだけ。`--config` 付きのコマンドの
                # 同じ deny と同じ部品)。この deny の案内は「期待した project に切り替えて」で、どの
                # project かが要る。出所は `_CHECK_BY_HAND` / `_UNREACHABLE_BY_HAND` が言うので
                # `_EXPECTED_NOT_SHOWN` は添えない。
                by_hand = _UNREACHABLE_BY_HAND if unreachable else _CHECK_BY_HAND
                return (
                    f"{head}期待={shell_word.shown_all(valid)} のいずれか。"
                    f"{_login_then(unreachable)}期待した project に切り替えてください ({by_hand})。"
                )
            return (
                f"{head}firebase login の後、以下のいずれかで切り替えてください:\n"
                + "\n".join(lines)
            )
        target = _scalar_word(_read_firebaserc(root), expected)
        if target is None:
            # 期待値は示す (dict で案内できる行が無いときと同じ)。
            by_hand = _CHECK_BY_HAND if _target(expected) is None else _UNREACHABLE_BY_HAND
            return (
                f"{head}期待={shell_word.shown_all([expected])}。"
                f"{_login_then(by_hand is _UNREACHABLE_BY_HAND)}"
                f"期待した project に切り替えてください ({by_hand})。"
            )
        return f"{head}firebase login && firebase use {target} を実行してください。"

    if isinstance(expected, dict):
        if current in valid:
            return None
        if config_file is not None:
            return (
                f"Firebase プロジェクト不一致: 現在={shell_word.shown(current)}, "
                f"期待={shell_word.shown_all(valid)} のいずれか\n{_config_switch_guide(valid)}"
            )
        head = (
            f"Firebase プロジェクト不一致: 現在={shell_word.shown(current)}, "
            f"期待={shell_word.shown_all(valid)} のいずれか\n"
        )
        lines, unreachable = _alias_lines(expected, "firebase use", _read_firebaserc(root))
        if not lines:
            return f"{head}{_UNREACHABLE_BY_HAND if unreachable else _CHECK_BY_HAND}"
        return f"{head}切り替え:\n" + "\n".join(lines)

    if current != expected:
        if config_file is not None:
            return (
                f"Firebase プロジェクト不一致: 現在={shell_word.shown(current)}, "
                f"期待={shell_word.shown_all([expected])} — {_config_switch_guide([expected])}"
            )
        head = (
            f"Firebase プロジェクト不一致: 現在={shell_word.shown(current)}, "
            f"期待={shell_word.shown(expected)}"
        )
        target = _scalar_word(_read_firebaserc(root), expected)
        if target is None:
            by_hand = _CHECK_BY_HAND if _target(expected) is None else _UNREACHABLE_BY_HAND
            return f"{head} — {by_hand}"
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


def is_pin_action(candidate: str) -> bool:
    """期待値が未登録のとき、PIN_HINT が案内する固定の操作 (`firebase use <x>`) なら True。

    未登録の service には照合する期待値が無く、`firebase use <x>` はそのディレクトリの
    切替先をローカルに記録するだけ (リモートの資源を変えない)。これを止めると、固定を
    案内した deny が案内どおりの操作を止める (dispatcher のキー欠落の経路で使う)。
    option の扱いは is_self_remediation と同じ (装飾 option だけを剥がし、`--add` /
    `--clear` / `--unalias` / `--project` などは False)。
    """
    normalized = cli_options.strip_allowed_options(
        candidate, _DECORATION_FLAGS, _DECORATION_OPTIONS_WITH_VALUE
    )
    return normalized is not None and _USE_RE.match(normalized) is not None
