"""AWS アカウント検証。"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

PATTERNS = [r"^aws\b"]
READONLY = [
    r"^aws\s+sts\s+get-caller-identity\b",
    # 認証取得系 (`aws sso login|logout` / `aws login|logout` / `aws configure ...`) は
    # クラウド資源を変更せず、ローカルの認証状態・profile 設定を作るだけ。
    # 未ログインだと sts が失敗して deny され、deny 文面が案内する `aws sso login`
    # 自体が deny される remediation loop になるため素通しする。直後の write は
    # (STATE_CHANGING で成功 cache も破棄されるため) 次回 hook で再検証される。
    # `aws configure export-credentials` (認証情報を stdout に出す) だけは
    # `gh auth token` / `gcloud auth print-access-token` と同じく検証対象のまま。
    r"^aws\s+(sso\s+(login|logout)|login|logout|configure(?!\s+export-credentials\b))\b",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。診断で打つ
    # `command aws --version` 等が誤って検証対象になり deny されるのを防ぐ。
    r"^aws\s+(--version|--help|version|help)\b",
]
# アカウント状態 (次の aws がどの認証情報で動くか) を変えうるコマンド。
# dispatcher が検出すると aws の成功 cache を破棄する。表示系の
# `aws configure list|list-profiles|get|export-credentials` は状態を変えないので除外
# (deny 文面が案内する list-profiles のたびに sts を打ち直さないように)。
STATE_CHANGING = [
    r"^aws\s+(sso\s+(login|logout)|login|logout"
    r"|configure(?!\s+(list|list-profiles|get|export-credentials)\b))\b",
]
ACCOUNT_KEY = "aws"
SETUP_HINT = (
    'AWS 最小例: {"aws": "123456789012"}。'
    "aws sts get-caller-identity --query Account で現在値を確認可"
)

_ROLE_ARN_ACCOUNT_RE = re.compile(r"^arn:aws[\w-]*:iam::(\d{12}):")


def _run_sts_get_caller_identity(env=None) -> tuple[str | None, str | None, str]:
    """aws sts get-caller-identity を実行し (account_id, cli_error, stderr_hint) を返す。

    - 取得成功: (account_id, None, "")
    - CLI 不在 / 実行不可 / timeout: (None, <deny 理由>, "")
    - 認証情報なし (stdout 空): (None, None, <stderr 先頭行または "">)
      切替案内は期待値を知っている verify() 側で組み立てる。

    env: コマンド行頭のインライン環境変数をマージした完全 env (`AWS_PROFILE` 等)。
    None なら hook プロセスの環境を継承する (builder からの呼び出し等)。
    """
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except FileNotFoundError:
        return None, "AWS: aws コマンドが見つかりません。", ""
    except OSError as e:
        # 実行権限なし / 形式不正等。例外を漏らすと hook が異常終了して無音 fail-open になる。
        return None, f"AWS: aws コマンドを実行できません ({e})。", ""
    except subprocess.TimeoutExpired:
        return None, "AWS: aws sts get-caller-identity がタイムアウトしました。再試行するか、ネットワーク接続を確認してください。", ""
    current = result.stdout.strip()
    if not current:
        stderr_hint = result.stderr.strip().splitlines()[0] if result.stderr.strip() else ""
        return None, None, stderr_hint
    return current, None, ""


def _aws_config_path(env=None) -> Path | None:
    """AWS CLI の共有 config ファイル (`$AWS_CONFIG_FILE` または `~/.aws/config`)。

    env (行頭インライン env をマージした CLI 用 env) が渡されたときはその env だけを
    見て CLI と同じ解釈にする。HOME も AWS_CONFIG_FILE も無ければ None。
    """
    src = os.environ if env is None else env
    home = src.get("HOME")
    if not home and env is None:
        try:
            home = str(Path.home())
        except RuntimeError:
            # HOME 未設定 + pwd 不能。例外を漏らすと hook が異常終了して無音 fail-open になる。
            home = None
    explicit = src.get("AWS_CONFIG_FILE")
    if explicit:
        # CLI と同じく `~` を展開する。展開元の HOME は渡された env 側を使う
        # (`~user/...` は pwd で解決するため env 非依存)。
        if explicit == "~" or explicit.startswith("~/"):
            if not home:
                return None
            explicit = home + explicit[1:]
        elif explicit.startswith("~"):
            explicit = os.path.expanduser(explicit)
        return Path(explicit)
    if not home:
        return None
    return Path(home) / ".aws" / "config"


def profiles_for_account(account_id: str, env=None) -> list[str]:
    """AWS config から、期待 Account ID に対応する profile 名を出現順で返す。

    `sso_account_id = <id>` (IAM Identity Center) と
    `role_arn = arn:aws:iam::<id>:role/...` (assume-role) の 2 形式を見る。
    `[default]` は "default" として返す。ファイルが無い / 読めない / 該当なしは
    空 list。profile 名以外の内容 (sso_start_url 等) は返さず、メッセージにも出さない。
    """
    if not account_id:
        return []
    path = _aws_config_path(env)
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    found: list[str] = []
    section: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if raw[0] in " \t":
            # インデントされた行はネストした sub-key (`s3 =` 配下など) で、
            # profile 直下の key ではない。
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            if name == "default":
                section = "default"
            elif name.startswith("profile "):
                section = name[len("profile "):].strip() or None
            else:
                section = None  # [sso-session x] / [services x] 等は profile ではない
            continue
        if section is None or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip().lower()
        value = value.strip()
        matched = False
        if key == "sso_account_id":
            matched = value == account_id
        elif key == "role_arn":
            m = _ROLE_ARN_ACCOUNT_RE.match(value)
            matched = bool(m) and m.group(1) == account_id
        if matched and section not in found:
            found.append(section)
    return found


def _switch_guidance(expected: str, env=None, *, include_configure: bool) -> str:
    """deny 文面の切替案内。案内するコマンドは全て hook の allow 経路にある。

    1. `AWS_PROFILE=<profile> aws ...` — 行頭インライン env。Claude Code の Bash は
       呼出ごとに env を持ち越さず、hook は Claude 本体の env を継承するため、
       `export AWS_PROFILE=...` は次の呼出にも検証にも効かない。検証に反映される
       のはこの形だけ (dispatcher が検証 subprocess に伝播する)。
    2. `aws sso login --profile <profile>` — READONLY (検証なしで実行可)。
    3. `aws configure` (認証情報なしのときのみ) — READONLY。
    `<profile>` は AWS config に期待 Account ID を持つ profile があれば具体名にする。
    """
    profiles = profiles_for_account(expected, env)
    profile = profiles[0] if profiles else "<profile>"
    lines = [
        "切り替え手順 (環境に応じて選択):",
        f"  AWS_PROFILE={profile} aws ...  # 行頭インライン指定 (Claude Code ではこの形のみ検証に反映)",
        f"  aws sso login --profile {profile}  # SSO 再ログイン (検証なしで実行可)",
    ]
    if include_configure:
        lines.append("  aws configure  # 認証情報の再設定 (検証なしで実行可)")
    if profiles:
        lines.append(
            "(AWS config で期待 Account ID に対応する profile: "
            + ", ".join(profiles)
            + ")"
        )
    else:
        lines.append(
            "(AWS config に期待 Account ID の sso_account_id / role_arn を持つ profile は"
            "見つかりません。profile 名は aws configure list-profiles で確認)"
        )
    lines.append(
        "export AWS_PROFILE=... は Claude Code の Bash では次の呼出に持ち越されず"
        "検証にも反映されません (ターミナル側で設定して claude を起動し直す場合のみ有効)。"
    )
    return "\n".join(lines)


def get_active_account(project_dir: str) -> str | None:
    """現在アクティブな AWS Account ID を返す。取得不可なら None。"""
    current, _err, _hint = _run_sts_get_caller_identity()
    return current


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "aws" キーに書く値を提案する (Account ID 文字列)。"""
    return get_active_account(project_dir)


def verify(expected, project_dir: str, env=None) -> str | None:
    if not isinstance(expected, str):
        return (
            f'AWS: accounts.local.json の "aws" は文字列で指定してください '
            f'(現在: {type(expected).__name__})。'
        )

    current, err, hint = _run_sts_get_caller_identity(env)
    if err:
        return err

    if current is None:
        detail = f" ({hint})" if hint else ""
        return (
            f"AWS: 認証情報を取得できません{detail}。\n"
            + _switch_guidance(expected, env, include_configure=True)
        )

    if current != expected:
        return (
            f"AWS アカウント不一致: 現在={current}, 期待={expected}\n"
            + _switch_guidance(expected, env, include_configure=False)
        )

    return None
