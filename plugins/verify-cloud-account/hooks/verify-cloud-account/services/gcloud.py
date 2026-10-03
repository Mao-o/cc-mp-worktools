"""Google Cloud (gcloud CLI) プロジェクト / アカウント検証。

accounts.local.json の "gcloud" は 2 形式を受け付ける:
- 文字列: `"gcloud": "my-project"` — project のみ検証 (後方互換)
- オブジェクト: `"gcloud": {"project": "my-project", "account": "me@example.com"}`
  project と account を個別検証。どちらかだけ省略も可
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from core import budget, cli_config, cli_options, shell_word

# `\b` だとハイフン付き別コマンドまで gcloud として拾うため、空白または終端が
# 続く形だけに限定する。
PATTERNS = [r"^gcloud(?=\s|$)"]
# release track prefix (`gcloud beta config set ...` / `gcloud alpha auth login`)。
# gcloud はほぼ全てのコマンドを alpha / beta (一部 preview) でも公開しており、
# 同じ操作が同じ副作用で走る。anchored pattern を GA 形だけで書くと track 形が
# READONLY / STATE_CHANGING をすり抜ける (Codex R5 P1-B は cross-CLI の
# `gcloud beta container clusters get-credentials` を指摘したが、自己 sweep の
# 結果 gcloud 自身の STATE_CHANGING **全パターン**が同じ穴だった)。
# 実在確認は SDK 生成物 `data/cli/gcloud_completions.py` の command tree で行った
# (config set|unset / configurations activate|create / init は alpha・beta・preview、
# auth 系と container clusters get-credentials は alpha・beta)。
# 存在しない track × command の組に当たっても実行が失敗するだけなので、
# 全パターンで同じ prefix を使う。
_TRACK = r"(?:(?:alpha|beta|preview)\s+)?"
READONLY = [
    rf"^gcloud\s+{_TRACK}auth\s+list\b",
    rf"^gcloud\s+{_TRACK}config\s+get-value\s+(project|account)\b",
    # 認証取得系 (login / application-default ... / activate-service-account / revoke)
    # は GCP 資源を変更せず、ローカルの認証状態を作るだけ (`auth login` はブラウザ認可
    # + credential のローカル保存、`activate-service-account` は鍵ファイルの読込、
    # `revoke` は token 失効のみ。gh の SSH 鍵アップロードのようなリモート write は
    # 無い)。未ログインで
    # `gcloud config get-value` が取れないとき deny 文面から回復できる経路を残す。
    # 直後の write は (STATE_CHANGING で成功 cache も破棄されるため) 再検証される。
    # `application-default` は login / revoke / set-quota-project のみ (ADC の
    # print-access-token は gcloud auth print-access-token と同じく検証対象のまま)。
    rf"^gcloud\s+{_TRACK}auth\s+"
    r"(login|application-default\s+(login|revoke|set-quota-project)"
    r"|activate-service-account|revoke)\b",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^gcloud\s+(--version|--help|version|help)\b",
]
# 認証情報を出力する形。READONLY / QUERY を取り消して WRITE 扱いにする。
# どちらも READONLY には載っていない (= 現状も検証対象) が、**形そのものが開示**で
# あることを宣言側に残す。READONLY の carve-out (`application-default` の
# login / revoke / set-quota-project だけを許す negative list) を将来広げたときに
# 素通しへ戻らないようにするため。
DISCLOSING = [
    (rf"^gcloud\s+{_TRACK}auth\s+print-(access|identity)-token(?=\s|$)", frozenset()),
    (
        rf"^gcloud\s+{_TRACK}auth\s+application-default\s+print-access-token(?=\s|$)",
        frozenset(),
    ),
]
# group token (`compute instances` 等) の繰り返しを止める語。**mutating verb を
# 跨がせない**ための停止条件で、denylist ではない (未知の verb は従来どおり跨げるので
# 誤 deny 側に穴を開けない)。跨がせると末尾の operand が read verb に見える形
# —— `gcloud functions deploy list` (`list` という名前の function を deploy) や
# `gcloud config set project list` —— で **write が QUERY に落ちて素通しする**。
# `run` は入れない: `gcloud run services list` の `run` は group 名で、停止語に
# すると正しい read が WRITE になる (`gcloud run deploy list` は 2 語目の `deploy`
# で止まるので、`run` を入れなくても write 側に残る)。
_MUTATING_SUB = (
    r"create|delete|remove|update|patch|deploy|add|set|unset|import|export|revoke|"
    r"disable|enable|reset|restart|stop|start|move|clone|attach|detach|abandon|"
    r"destroy|undelete|replace|rollback|resize|drain|apply|call|publish|copy|cp|mv|"
    r"rm|sign|activate|promote"
)
# リモート read (資源を変更しない)。不一致でも deny せず警告のみで通す。
# **サブコマンドの位置**にある `list` / `describe` / `get-iam-policy` だけを見る
# (`gcloud ... --format=list` のような option の値や引用符の中に現れた同じ語を
# 拾わないため)。CLI 名直後の global option は dispatcher が剥がした形で照合される。
#
# 既知の代償: group 名が停止語と同じ綴りの系統 (`gcloud deploy ...` = Cloud Deploy)
# は read でも QUERY にならず WRITE 扱いになる。0.13.0 までと同じ扱いが 1 系統だけ
# 残る形で、緩和が届かないだけなので誤 deny を新造しない (README の既知の制限)。
QUERY = [
    rf"^gcloud\s+{_TRACK}(?:(?!(?:{_MUTATING_SUB})(?=\s))[a-z][\w-]*\s+)*"
    rf"(list|describe|get-iam-policy)(?=\s|$)",
]
# アクティブ project / account を変えうるコマンド。dispatcher が検出すると gcloud の
# 成功 cache を破棄する。`configurations create` は既定で作成した configuration を
# activate する。`init` は対話的に account / project を設定し直す。
# `config set` は identity を変えない key (`compute/region` 等) も含むが、cache 破棄は
# 過剰でも再検証 1 回で済むのでここでは絞らない。連結規則の切替側だけが
# identity を変える形に絞る (下の `changes_identity`)。
STATE_CHANGING = [
    rf"^gcloud\s+{_TRACK}config\s+(set|unset)\b",
    rf"^gcloud\s+{_TRACK}config\s+configurations\s+(activate|create)\b",
    rf"^gcloud\s+{_TRACK}auth\s+"
    r"(login|activate-service-account|revoke"
    r"|application-default\s+(login|revoke))\b",
    rf"^gcloud\s+{_TRACK}init\b",
]
# CLI 名直後に置ける global flag (`gcloud --project x config set ...`)。dispatcher が
# 剥がした形でも READONLY / STATE_CHANGING / self-remediation を判定する
# (core/cli_options.py)。
GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "--account", "--billing-project", "--configuration", "--flags-file", "--flatten",
    "--format", "--project", "--verbosity", "--access-token-file",
    "--impersonate-service-account", "--trace-token", "--universe-domain",
})
GLOBAL_FLAGS = frozenset({
    "--quiet", "-q", "--log-http", "--user-output-enabled", "--no-user-output-enabled",
})
# 「どの project / account / configuration に対して実行するか」をコマンド側で指定する
# option (v0.9.0)。dispatcher が候補全体から値を拾い verify に渡す。
# `--project` / `--account` は値そのものが照合対象になり、`--configuration` は
# 「どの設定セットの現在値を読むか」を変えるので検証コマンドに引き渡す。
CONTEXT_OPTIONS = {
    "--project": "project",
    "--account": "account",
    "--configuration": "configuration",
}
ACCOUNT_KEY = "gcloud"

# deny 文面で案内する remediation コマンド (引数付きの実コマンド形) の正規表現。
# dispatcher は verify() の返り値にこれが一致するときだけ「単独で実行せよ」の注記を
# 付ける。コマンド名だけの言及 (「firebase use がタイムアウトしました」等の診断文) や
# インストール案内・設定ファイルの型不正には一致させない。
REMEDIATION_PATTERNS = (r"gcloud config set (?:project|account)\s+\S",)
SETUP_HINT = (
    'GCP 最小例: {"gcloud": "my-project-id"}。'
    "gcloud config get-value project で現在値を確認可。"
    'account 併用: {"gcloud": {"project":"p","account":"me@example.com"}}'
)
# builder (scripts/accounts_builder.py) の書込前スキーマ検証が参照する契約。
# gcloud の dict 形は project/account の固定キーのみ (verify() が読むキーもこの
# 2 つだけ)。
ACCEPTS_DICT = True
DICT_ALLOWED_KEYS = frozenset({"project", "account"})
# 下の verify() は dict 期待値の project / account を `if <key>_want:` で拾うため、
# **falsy な値 (None / "" 等) は黙って無視**する一方、**truthy な非 str**
# (例: `{"account": 123}`) は「文字列で指定してください」で reject する。
# builder は migrate の緩和モードでもこの非対称に合わせる → "truthy"。
# DICT_ALLOWED_KEYS 外のキーは verify() が読まないので値の形は問わない。
DICT_VALUE_CHECK = "truthy"
# scalar 期待値と等価になる dict キー。verify() の str 分岐は
# `_check_project(expected, ...)` だけを呼ぶ (account は照合しない)。
SCALAR_EQUIVALENT_DICT_KEY = "project"


def _get(key: str, env=None, configuration=None) -> tuple[str | None, str | None]:
    """`gcloud config get-value <key>` を実行し (value, error) を返す。

    env: コマンド行頭のインライン環境変数をマージした完全 env
    (`CLOUDSDK_CORE_PROJECT` / `CLOUDSDK_ACTIVE_CONFIG_NAME` 等)。
    None なら hook プロセスの環境を継承する。
    configuration: 候補コマンドが `--configuration X` を指定していた場合の値。
    `--configuration` は gcloud の global flag でどのコマンドにも付けられるため、
    検証コマンドにも同じ値を渡して実行時と同じ設定セットの現在値を読む。
    """
    argv = ["gcloud", "config", "get-value", key]
    if configuration:
        argv += ["--configuration", configuration]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=budget.call_timeout(10),
            env=env,
        )
    except FileNotFoundError:
        return None, "GCP: gcloud コマンドが見つかりません。"
    except OSError as e:
        # 実行権限なし / 形式不正等。例外を漏らすと hook が異常終了して無音 fail-open になる。
        return None, f"GCP: gcloud コマンドを実行できません ({e})。"
    except subprocess.TimeoutExpired:
        return None, f"GCP: gcloud config get-value {key} がタイムアウトしました。再試行するか、ネットワーク接続を確認してください。"
    value = result.stdout.strip()
    if not value or value == "(unset)":
        return None, None
    return value, None


# gcloud の設定ファイルから現在値を読む経路 (CLI 実行の回避)。
#
# `gcloud config get-value <key>` は Python 製 CLI の起動込みで 1 回 〜1s かかり、
# dict 期待値では project / account の 2 回走る。一方 gcloud はアクティブな
# configuration 名を `<config_dir>/active_config` に、その中身を
# `<config_dir>/configurations/config_<name>` の `[core]` セクションに書いている
# (実測: account / project の 2 キーが `key = value` 形式)。
#
# **ローカル読取を使わない条件** (どれかに当たれば従来の CLI 実行に落ちる):
# - 下の 2 つ以外の `CLOUDSDK_*` / `GOOGLE_CLOUD_PROJECT` 等が env にある
#   (gcloud のプロパティは env でも上書きでき、優先順位を実測で確定できていない。
#   **エミュレートを諦めて CLI に委ねる**方が、取り違えた値で allow するより安全)
# - `HOME` が hook プロセスと違う (`cli_config.home_overridden`) — 実行される
#   gcloud は別の設定ディレクトリ (`$HOME/.config/gcloud`) を読む
# - configuration 名が gcloud の命名規則から外れる (パス要素の混入を防ぐ)
# - 設定ファイルが読めない / INI として解釈できない
_CONFIG_DIR_ENV_VAR = "CLOUDSDK_CONFIG"
_ACTIVE_CONFIG_ENV_VAR = "CLOUDSDK_ACTIVE_CONFIG_NAME"
# ローカル読取を続けてよい `CLOUDSDK_*` (どちらも「どのファイルを読むか」だけを
# 決める構造的な変数で、値そのものを上書きしない)。
_LOCAL_SAFE_ENV_VARS = frozenset({_CONFIG_DIR_ENV_VAR, _ACTIVE_CONFIG_ENV_VAR})
_CLOUDSDK_ENV_PREFIX = "CLOUDSDK_"
# `CLOUDSDK_*` 以外で project を上書きしうる env。
_OTHER_OVERRIDE_ENV_VARS = frozenset(
    {"GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT"}
)
# CLI がどのアカウント / project で動くかを決める env (成功 cache のキーに含める。
# services/__init__.py の IDENTITY_ENV_* 契約)。ローカル読取を諦める条件と同じ集合
# (`CLOUDSDK_*` 全部 + 上の override) に、設定ディレクトリの既定を決める `HOME`。
IDENTITY_ENV_VARS = frozenset({*_OTHER_OVERRIDE_ENV_VARS, "HOME"})
IDENTITY_ENV_PREFIXES = (_CLOUDSDK_ENV_PREFIX,)
_ACTIVE_CONFIG_FILE = "active_config"
_CONFIGURATIONS_DIR = "configurations"
_CONFIG_FILE_PREFIX = "config_"
_DEFAULT_CONFIG_NAME = "default"
_CORE_SECTION = "core"
# gcloud の configuration 名 (英字始まり + 英数字 / ハイフン)。`..` や `/` を
# 含む値でファイルパスを組まないためのガード。
_CONFIG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")


def _config_dir(env) -> Path | None:
    """gcloud の設定ディレクトリ (`CLOUDSDK_CONFIG` → `~/.config/gcloud`)。"""
    explicit = env.get(_CONFIG_DIR_ENV_VAR)
    if explicit:
        return Path(explicit)
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return None
    return home / ".config" / "gcloud"


def _local_config_name(env, config_dir: Path, configuration=None) -> str | None:
    """読むべき configuration 名 (`--configuration` → env → active_config → default)。"""
    name = configuration or env.get(_ACTIVE_CONFIG_ENV_VAR)
    if not name:
        raw = cli_config.read_text(config_dir / _ACTIVE_CONFIG_FILE)
        # active_config が無い環境では gcloud は "default" を使う。
        name = raw.strip() if raw is not None else _DEFAULT_CONFIG_NAME
        if not name:
            name = _DEFAULT_CONFIG_NAME
    if not _CONFIG_NAME_RE.match(name):
        return None
    return name


def _local_core_properties(env=None, configuration=None) -> dict[str, str] | None:
    """`[core]` の project / account をローカル設定から読む。決められないなら None。

    env: インライン環境変数をマージ済みの完全 env (None なら hook プロセスの環境)。
    検証 subprocess に渡すものと同じ env を見る (`CLOUDSDK_CONFIG=... gcloud ...`
    の形もコマンド実行時と同条件で解決するため)。
    """
    e = os.environ if env is None else env
    for name in e:
        if name.startswith(_CLOUDSDK_ENV_PREFIX) and name not in _LOCAL_SAFE_ENV_VARS:
            return None
    if any(e.get(name) for name in _OTHER_OVERRIDE_ENV_VARS):
        return None
    if cli_config.home_overridden(e):
        return None
    config_dir = _config_dir(e)
    if config_dir is None:
        return None
    name = _local_config_name(e, config_dir, configuration)
    if name is None:
        return None
    text = cli_config.read_text(
        config_dir / _CONFIGURATIONS_DIR / f"{_CONFIG_FILE_PREFIX}{name}"
    )
    if text is None:
        return None
    sections = cli_config.parse_ini_sections(text)
    if sections is None:
        return None
    core = sections.get(_CORE_SECTION, {})
    values = {}
    for key in ("project", "account"):
        value = core.get(key)
        if isinstance(value, str) and value.strip():
            values[key] = value.strip()
    return values


def _cli_value_getter(env, configuration):
    """`gcloud config get-value <key>` から現在値を取る getter を返す。"""
    def get(key: str) -> tuple[str | None, str | None]:
        return _get(key, env, configuration)

    return get


def _local_value_getter(values: dict[str, str]):
    """ローカル設定から読んだ値を返す getter (CLI と同じ `(value, error)` 形)。"""
    def get(key: str) -> tuple[str | None, str | None]:
        return values.get(key), None

    return get


# 期待値が案内するコマンドに入れられない形のとき (core/shell_word.py) の文。
_CHECK_BY_HAND = (
    f'期待値は{shell_word.UNSAFE}。accounts.local.json の "{ACCOUNT_KEY}" を手で確認してください'
)


def _flag_mismatch(label: str, flag: str, override: str, expected: str) -> str | None:
    """`--project` / `--account` の値を期待値と直接照合する (一致なら None)。

    これらの flag はその実行だけ対象を差し替えるので、アクティブ設定ではなく
    **書かれた値そのもの**が実際に使われる対象になる。
    """
    if override == expected:
        return None
    head = f"GCP {label}不一致: コマンド指定 {flag}={shlex.quote(override)}, 期待={expected}"
    value = shell_word.arg(expected)
    if value is None:
        return f"{head} — {flag} を外してください ({_CHECK_BY_HAND})"
    return f"{head} — {flag} を外すか {flag} {value} を指定してください"


def _check_key(key: str, label: str, expected: str, get_value) -> str | None:
    """`key` (project / account) の現在値を期待値と照合する。

    `get_value` は `(value, error)` を返す getter。切り替えの案内
    (`gcloud config set <key> <期待値>`) は、期待値が許容形のときだけ出す。
    """
    current, err = get_value(key)
    if err:
        return err
    value = shell_word.arg(expected)
    if current is None:
        head = f"GCP: アクティブ{label}が設定されていません。"
        if value is None:
            return f"{head}{_CHECK_BY_HAND}。"
        return f"{head}gcloud config set {key} {value} を実行してください。"
    if current != expected:
        head = f"GCP {label}不一致: 現在={current}, 期待={expected}"
        if value is None:
            return f"{head} — {_CHECK_BY_HAND}"
        return f"{head} — 切り替え: gcloud config set {key} {value}"
    return None


def _check_project(expected: str, get_value, override=None) -> str | None:
    """期待値と現在値を照合する。`get_value` は `(value, error)` を返す getter。"""
    if override is not None:
        return _flag_mismatch("プロジェクト", "--project", override, expected)
    return _check_key("project", "プロジェクト", expected, get_value)


def _check_account(expected: str, get_value, override=None) -> str | None:
    """期待値と現在値を照合する。`get_value` は `(value, error)` を返す getter。"""
    if override is not None:
        return _flag_mismatch("アカウント", "--account", override, expected)
    return _check_key("account", "アカウント", expected, get_value)


def get_active_account(project_dir: str) -> dict[str, str | None] | None:
    """{"project": ..., "account": ...} を返す。両方取得不可なら None。

    片方だけ取れた場合は、取れなかった側のキーの値を None にして返す。
    """
    project, _ = _get("project")
    account, _ = _get("account")
    if project is None and account is None:
        return None
    return {"project": project, "account": account}


def pin_fields(expected) -> dict[str, str] | None:
    """固定 (builder の `pin-env`) で照合する project / account。期待値の形が不正なら None。

    verify() と同じ基準で読む (DICT_VALUE_CHECK = "truthy")。dict の falsy な値
    (None / "" など) は書かれていないものとして扱い、truthy で文字列でない値
    (例: `{"project": 123}`) は verify() が拒否するので不正にする。空白だけの文字列と
    前後に空白のある文字列も、verify() ではどの現在値 (CLI の出力も設定ファイルの値も
    前後の空白を除いて読む) とも完全一致しないので不正にする。片方を黙って落として残り
    だけで照合したり、空白を除いた値で照合したりすると、固定した後も verify() が同じ
    期待値で deny し続ける (マージ前レビューの指摘)。scalar は project だけ (verify() の
    str 分岐は account を照合しない)。値は書かれたまま返す。
    """
    if isinstance(expected, str):
        return {"project": expected} if _pinnable(expected) else None
    if not isinstance(expected, dict):
        return None
    fields: dict[str, str] = {}
    for key in ("project", "account"):
        value = expected.get(key)
        if not value:
            continue
        if not _pinnable(value):
            return None
        fields[key] = value
    return fields or None


def _pinnable(value) -> bool:
    """verify() が現在値と一致させうる文字列 (空白だけでも、前後に空白があるのでもない)。"""
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def configurations_matching(expected, env=None) -> list[str] | None:
    """期待値 (project / account) に一致する名前付き構成の**名前**を返す。

    プロジェクトごとの固定 (`CLOUDSDK_ACTIVE_CONFIG_NAME`。builder の `pin-env`) 用。
    `<config_dir>/configurations/config_<name>` の `[core]` を読み、期待値に書かれた
    キーがすべて一致する構成を名前順に返す (期待値が project だけなら account は
    問わない)。中身は返さない。期待値の形が不正なら None、設定ディレクトリが無い /
    読めないなら []。
    """
    wanted = pin_fields(expected)
    if wanted is None:
        return None
    e = os.environ if env is None else env
    config_dir = _config_dir(e)
    if config_dir is None:
        return []
    try:
        entries = sorted((config_dir / _CONFIGURATIONS_DIR).iterdir())
    except OSError:
        return []
    names = []
    for path in entries:
        if not path.name.startswith(_CONFIG_FILE_PREFIX) or not path.is_file():
            continue
        name = path.name[len(_CONFIG_FILE_PREFIX):]
        if not _CONFIG_NAME_RE.match(name):
            continue
        text = cli_config.read_text(path)
        sections = cli_config.parse_ini_sections(text) if text is not None else None
        if sections is None:
            continue
        core = sections.get(_CORE_SECTION, {})
        if all(
            isinstance(core.get(key), str) and core[key].strip() == value
            for key, value in wanted.items()
        ):
            names.append(name)
    return names


def suggest_accounts_entry(project_dir: str) -> str | dict | None:
    """accounts.local.json の "gcloud" キーに書く値を提案する。

    - project のみ取得可 → scalar (project 文字列)
    - account も取得可 → dict[project, account]
    - 両方取得不可 → None
    """
    active = get_active_account(project_dir)
    if not active:
        return None
    project = active.get("project")
    account = active.get("account")
    if project and not account:
        return project
    entry: dict[str, str] = {}
    if project:
        entry["project"] = project
    if account:
        entry["account"] = account
    return entry or None


def _expected_shape_error(expected) -> str | None:
    """期待値そのものの形の不正 (現在値を取得しなくても決まる deny 理由)。"""
    if isinstance(expected, dict):
        if not expected.get("project") and not expected.get("account"):
            return (
                'GCP: accounts.local.json の "gcloud" オブジェクトに '
                '"project" または "account" キーが必要です。'
            )
        return None
    if not isinstance(expected, str):
        return (
            f'GCP: accounts.local.json の "gcloud" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )
    return None


def verify(expected, project_dir: str, env=None, context=None) -> str | None:
    """context: 候補コマンドのコンテキスト option。

    `--project` / `--account` は**キーごとに独立して**上書きする。
    `gcloud --project other run deploy` は project の照合先を other に変えるが、
    account の期待値がある限り account は従来どおりアクティブ値と照合する
    (project だけ見て早期 return すると account の false-allow を作ってしまう)。
    `--configuration` は上書きされなかったキーの現在値取得に引き渡す。

    現在値は**まずローカル設定ファイルから読む** (`_local_core_properties`)。
    ただしローカル読取で通せるのは **allow だけ**で、エラー (不一致 / 未設定) を
    返しそうなときは必ず `gcloud config get-value` で取り直してから判断する
    (`services/github.py` の verify() と同じ方針 — 誤読が deny を新造せず
    「CLI を 1 回呼ぶ」コストに留まるようにするため。gcloud のプロパティは
    installation 単位の設定など設定ファイル以外からも来るため、これが無いと
    「ローカルでは未設定に見えるが gcloud は値を持っている」形で誤 deny する)。
    """
    ctx = context or {}
    configuration = ctx.get("configuration")

    shape_error = _expected_shape_error(expected)
    if shape_error:
        return shape_error

    local = _local_core_properties(env, configuration)
    if local is not None:
        if _verify_against(expected, ctx, _local_value_getter(local)) is None:
            return None

    return _verify_against(expected, ctx, _cli_value_getter(env, configuration))


def _verify_against(expected, ctx: dict, get_value) -> str | None:
    """期待値を現在値 getter と照合する (取得元は CLI / ローカル設定のどちらでも同じ)。"""
    project_override = ctx.get("project")
    account_override = ctx.get("account")

    if isinstance(expected, dict):
        project_want = expected.get("project")
        account_want = expected.get("account")
        errors: list[str] = []
        if project_want:
            if not isinstance(project_want, str):
                errors.append(
                    f"GCP: project 期待値は文字列で指定してください "
                    f"(現在: {type(project_want).__name__})。"
                )
            else:
                err = _check_project(project_want, get_value, project_override)
                if err:
                    errors.append(err)
        if account_want:
            if not isinstance(account_want, str):
                errors.append(
                    f"GCP: account 期待値は文字列で指定してください "
                    f"(現在: {type(account_want).__name__})。"
                )
            else:
                err = _check_account(account_want, get_value, account_override)
                if err:
                    errors.append(err)
        if not errors:
            return None
        if len(errors) == 1:
            return errors[0]
        return "GCP 検証エラー (複数):\n" + "\n".join(f"  - {e}" for e in errors)

    if not isinstance(expected, str):
        # verify() は先に `_expected_shape_error()` で弾くので通常ここには来ない。
        return _expected_shape_error(expected)

    return _check_project(expected, get_value, project_override)


_CONFIG_SET_RE = re.compile(
    rf"^gcloud\s+{_TRACK}config\s+set\s+(project|account)\s+(\S+)\s*$"
)

# self-remediation / inert 判定で**剥がしてよい** option の allow-list。
# 基準は「`gcloud config set` が書き込む先を変えないこと」— 書き込む先が
# アクティブ configuration のままなら、verify() が読む先
# (`gcloud config get-value project|account`、context の `--configuration` は
# dispatcher が別途 verify に渡す) と一致し続けるので、実行後の状態が期待値に
# なることを option 無審査の形と同じ根拠で主張できる。
#
# **`--configuration` / `--flags-file` は入れない** — 前者は非アクティブな
# configuration を名指しして書くのでアクティブ値が期待値にならず、後者は
# ファイル経由で任意の flag を注入できる (allow-list の意味が消える)。
# `--project` / `--account` も入れない (照合先を差し替える context option)。
_DECORATION_FLAGS = frozenset({"--quiet", "-q", "--no-user-output-enabled"})
_DECORATION_OPTIONS_WITH_VALUE = frozenset({"--verbosity", "--format"})
_DECORATION_OPTIONS = _DECORATION_FLAGS | _DECORATION_OPTIONS_WITH_VALUE


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待値への gcloud config set」なら True。

    str 期待値は project のみ照合 (verify と同じ解釈)。dict 期待値は set 対象
    キー (project / account) の期待値と照合する。装飾 option
    (`--quiet` / `--verbosity=` / `--format=` / `--no-user-output-enabled`) は
    剥がしてから照合し、**それ以外の option が付いていたら保守的に False**
    (通常検証に落とす)。
    """
    normalized = cli_options.strip_allowed_options(
        candidate, _DECORATION_FLAGS, _DECORATION_OPTIONS_WITH_VALUE
    )
    if normalized is None:
        return False
    m = _CONFIG_SET_RE.match(normalized)
    if not m:
        return False
    key, value = m.group(1), m.group(2)
    if isinstance(expected, str):
        return key == "project" and value == expected
    if isinstance(expected, dict):
        want = expected.get(key)
        return isinstance(want, str) and value == want
    return False


# `gcloud config set <key> <value>` のうち、**アクティブな identity
# (account / project) を変えない** key の allow-list。`core/` 接頭辞は省略できる
# (`gcloud config set project X` == `core/project`) ので両形を列挙する。
#
# ここに**載せていない** key は identity を変える側に倒れる (allow-list)。
# 特に `project` / `account` / `core/project` / `core/account` /
# `auth/impersonate_service_account` は identity そのもので、
# `container/cluster` は `container clusters get-credentials` の既定を変えて
# kubectl 側の identity に波及しうるため載せない。
_INERT_CONFIG_KEYS = frozenset({
    # 対話・出力・ログの制御 (どのアカウントで動くかを変えない)
    "disable_prompts", "core/disable_prompts",
    "disable_usage_reporting", "core/disable_usage_reporting",
    "verbosity", "core/verbosity",
    "user_output_enabled", "core/user_output_enabled",
    "disable_color", "core/disable_color",
    # **`core/log_http` は載せない。** identity は変えない (この述語の基準は満たす)
    # が、full HTTP ログは認証ヘッダを出す = この plugin の他の allow-list
    # (`DISCLOSING` / `READONLY_SAFE_OPTIONS`) が塞いでいるクラスそのもの。
    # 「inert」と名の付いた集合に置くと、読んだ人が「安全」と一般化する。
    # 開示の扱いは `DISCLOSING` の担当なので、この集合には入れない
    # リソースの既定の置き場所 (identity ではない)
    "compute/region", "compute/zone",
    "run/region",
    "functions/region",
})
_CONFIG_SET_KEY_RE = re.compile(rf"^gcloud\s+{_TRACK}config\s+set\s+(\S+)(?=\s|$)")


def changes_identity(candidate: str, expected) -> bool:
    """このセグメントが「次の gcloud がどのアカウント / project で動くか」を
    変えうるなら True。

    `STATE_CHANGING` は**成功キャッシュの破棄**が目的なので `gcloud config set`
    全体を含めてある。一方連結規則 (`core/dispatcher.py` の
    `_unexpected_switch_before_write`) の切替側は「identity が変わる形」だけに
    絞る必要があるため、inert な形をここで **allow-list として列挙**する
    (宣言しない service は既定で True = 従来どおり)。

    inert と言えるのは `config set <inert key> ...` だけ。key 自体が allow-list に
    あっても**宣言外の option が付いていたら True** に倒す (`--configuration other`
    は別 configuration を名指しする形で、そこまで inert と言い切る根拠が無い)。
    `config unset` / `configurations activate|create` / `auth ...` / `init` は
    いずれも identity を変えうるので対象外。
    """
    m = _CONFIG_SET_KEY_RE.match(candidate)
    if not m or m.group(1) not in _INERT_CONFIG_KEYS:
        return True
    names = cli_options.find_option_names(candidate, GLOBAL_OPTIONS_WITH_VALUE)
    return not names <= _DECORATION_OPTIONS
