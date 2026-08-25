"""Google Cloud (gcloud CLI) プロジェクト / アカウント検証。

accounts.local.json の "gcloud" は 2 形式を受け付ける:
- 文字列: `"gcloud": "my-project"` — project のみ検証 (後方互換)
- オブジェクト: `"gcloud": {"project": "my-project", "account": "me@example.com"}`
  project と account を個別検証。どちらかだけ省略も可
"""
from __future__ import annotations

import re
import subprocess

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
# アクティブ project / account を変えうるコマンド。dispatcher が検出すると gcloud の
# 成功 cache を破棄する。`configurations create` は既定で作成した configuration を
# activate する。`init` は対話的に account / project を設定し直す。
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
SETUP_HINT = (
    'GCP 最小例: {"gcloud": "my-project-id"}。'
    "gcloud config get-value project で現在値を確認可。"
    'account 併用: {"gcloud": {"project":"p","account":"me@example.com"}}'
)


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
            timeout=10,
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


def _flag_mismatch(label: str, flag: str, override: str, expected: str) -> str | None:
    """`--project` / `--account` の値を期待値と直接照合する (一致なら None)。

    これらの flag はその実行だけ対象を差し替えるので、アクティブ設定ではなく
    **書かれた値そのもの**が実際に使われる対象になる。
    """
    if override == expected:
        return None
    return (
        f"GCP {label}不一致: コマンド指定 {flag}={override}, 期待={expected}"
        f" — {flag} を外すか {flag} {expected} を指定してください"
    )


def _check_project(expected: str, env=None, configuration=None, override=None) -> str | None:
    if override is not None:
        return _flag_mismatch("プロジェクト", "--project", override, expected)
    current, err = _get("project", env, configuration)
    if err:
        return err
    if current is None:
        return (
            f"GCP: アクティブプロジェクトが設定されていません。"
            f"gcloud config set project {expected} を実行してください。"
        )
    if current != expected:
        return (
            f"GCP プロジェクト不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: gcloud config set project {expected}"
        )
    return None


def _check_account(expected: str, env=None, configuration=None, override=None) -> str | None:
    if override is not None:
        return _flag_mismatch("アカウント", "--account", override, expected)
    current, err = _get("account", env, configuration)
    if err:
        return err
    if current is None:
        return (
            f"GCP: アクティブアカウントが設定されていません。"
            f"gcloud config set account {expected} を実行してください。"
        )
    if current != expected:
        return (
            f"GCP アカウント不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: gcloud config set account {expected}"
        )
    return None


def get_active_account(project_dir: str) -> dict[str, str | None] | None:
    """{"project": ..., "account": ...} を返す。両方取得不可なら None。

    片方だけ取れた場合は、取れなかった側のキーの値を None にして返す。
    """
    project, _ = _get("project")
    account, _ = _get("account")
    if project is None and account is None:
        return None
    return {"project": project, "account": account}


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


def verify(expected, project_dir: str, env=None, context=None) -> str | None:
    """context: 候補コマンドのコンテキスト option。

    `--project` / `--account` は**キーごとに独立して**上書きする。
    `gcloud --project other run deploy` は project の照合先を other に変えるが、
    account の期待値がある限り account は従来どおりアクティブ値と照合する
    (project だけ見て早期 return すると account の false-allow を作ってしまう)。
    `--configuration` は上書きされなかったキーの現在値取得に引き渡す。
    """
    ctx = context or {}
    configuration = ctx.get("configuration")
    project_override = ctx.get("project")
    account_override = ctx.get("account")

    if isinstance(expected, dict):
        project_want = expected.get("project")
        account_want = expected.get("account")
        if not project_want and not account_want:
            return (
                'GCP: accounts.local.json の "gcloud" オブジェクトに '
                '"project" または "account" キーが必要です。'
            )
        errors: list[str] = []
        if project_want:
            if not isinstance(project_want, str):
                errors.append(
                    f"GCP: project 期待値は文字列で指定してください "
                    f"(現在: {type(project_want).__name__})。"
                )
            else:
                err = _check_project(
                    project_want, env, configuration, project_override
                )
                if err:
                    errors.append(err)
        if account_want:
            if not isinstance(account_want, str):
                errors.append(
                    f"GCP: account 期待値は文字列で指定してください "
                    f"(現在: {type(account_want).__name__})。"
                )
            else:
                err = _check_account(
                    account_want, env, configuration, account_override
                )
                if err:
                    errors.append(err)
        if not errors:
            return None
        if len(errors) == 1:
            return errors[0]
        return "GCP 検証エラー (複数):\n" + "\n".join(f"  - {e}" for e in errors)

    if not isinstance(expected, str):
        return (
            f'GCP: accounts.local.json の "gcloud" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    return _check_project(expected, env, configuration, project_override)


_CONFIG_SET_RE = re.compile(r"^gcloud\s+config\s+set\s+(project|account)\s+(\S+)\s*$")


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待値への gcloud config set」なら True。

    str 期待値は project のみ照合 (verify と同じ解釈)。dict 期待値は set 対象
    キー (project / account) の期待値と照合する。余分なフラグ付きは保守的に
    False で通常検証に落とす。
    """
    m = _CONFIG_SET_RE.match(candidate)
    if not m:
        return False
    key, value = m.group(1), m.group(2)
    if isinstance(expected, str):
        return key == "project" and value == expected
    if isinstance(expected, dict):
        want = expected.get(key)
        return isinstance(want, str) and value == want
    return False
