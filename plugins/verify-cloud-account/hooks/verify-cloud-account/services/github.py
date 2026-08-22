"""GitHub (gh CLI) アカウント検証。

accounts.local.json の "github" は 2 形式を受け付ける:
- 文字列: `"github": "Mao-o"`
  任意の host のアクティブアカウントを照合 (後方互換)
- オブジェクト: `"github": {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}`
  hostname ごとのアクティブアカウントを個別照合 (GHE / 複数インスタンス対応)
"""
from __future__ import annotations

import re
import shlex
import subprocess

PATTERNS = [r"^gh\b"]
READONLY = [
    r"^gh\s+auth\s+(status|list)\b",
    # 認証取得系 (logout / refresh / setup-git) はリポジトリ等の資源を変更せず、
    # ローカルの認証状態を作るだけ。未ログイン・別アカウントのとき deny 文面が案内する
    # login 自体が deny される remediation loop を防ぐ。直後の write は
    # (STATE_CHANGING で成功 cache も破棄されるため) 次回 hook で再検証される。
    r"^gh\s+auth\s+(logout|refresh|setup-git)\b",
    # `gh auth login` は SSH git protocol を選ぶと既存の SSH 公開鍵を GitHub アカウントに
    # **アップロード**しうる (gh 2.96 `gh auth login --help`: SSH 選択時に鍵を検出して
    # アップロード、`--skip-ssh-key` で抑止)。期待外アカウントへの SSH login はリモート
    # write になるため、鍵操作が起きない形だけを readonly にする (regex ではなく
    # `is_readonly()` で flag の実効 boolean を解釈する。下記 `_login_is_keyless`)。
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^gh\s+(--version|--help|version|help)\b",
]
# アクティブアカウント (hosts.yml) を変えうるコマンド。dispatcher が検出すると
# github の成功 cache を破棄する。`switch` は期待値向きなら self-remediation で
# 検証なし、期待値以外なら通常検証 (実行前の状態) だが、どちらも cache は残さない。
STATE_CHANGING = [r"^gh\s+auth\s+(switch|login|logout|refresh)\b"]
ACCOUNT_KEY = "github"
SETUP_HINT = (
    'GitHub 最小例: {"github": "YOUR_USERNAME"}。'
    "gh auth status で現在値を確認可。"
    'GHE 別指定: {"github": {"github.com":"USER","ghe.corp.com":"USER"}}'
)

_LOGGED_IN_RE = re.compile(r"Logged in to (\S+) account (\S+)")


def parse_active_accounts(output_text: str) -> dict[str, str]:
    """gh auth status の出力から {hostname: active_account} を返す。

    各 `Active account: true` について、直前の `Active account: true` より後の
    範囲を逆順にスキャンして最初の `Logged in to <host> account <user>` を採用する。
    これにより複数 host がある場合も各 host のアクティブアカウントが正しく
    ペア化される。
    """
    result: dict[str, str] = {}
    lines = output_text.splitlines()
    last_active_idx = -1
    for i, line in enumerate(lines):
        if "Active account: true" not in line:
            continue
        start = last_active_idx + 1
        for j in range(i, start - 1, -1):
            m = _LOGGED_IN_RE.search(lines[j])
            if m:
                host, user = m.group(1), m.group(2)
                result.setdefault(host, user)
                break
        last_active_idx = i
    return result


_parse_active_accounts = parse_active_accounts


def _run_gh_auth_status(env=None) -> tuple[str, str | None]:
    """gh auth status を実行し (combined_output, error) を返す。

    env: コマンド行頭のインライン環境変数をマージした完全 env (`GH_HOST` 等)。
    None なら hook プロセスの環境を継承する。
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except FileNotFoundError:
        return "", "GitHub: gh コマンドが見つかりません。brew install gh を実行してください。"
    except OSError as e:
        # 実行権限なし / 形式不正等。例外を漏らすと hook が異常終了して無音 fail-open になる。
        return "", f"GitHub: gh コマンドを実行できません ({e})。"
    except subprocess.TimeoutExpired:
        return "", "GitHub: gh auth status がタイムアウトしました。再試行するか、ネットワーク接続を確認してください。"
    return result.stdout + result.stderr, None


def _fetch_active_accounts(env=None) -> tuple[dict[str, str] | None, str | None]:
    """gh auth status を実行し (active_accounts, error_reason) を返す。"""
    combined, err = _run_gh_auth_status(env)
    if err:
        return None, err
    active = parse_active_accounts(combined)
    if not active:
        return None, (
            "GitHub: アクティブアカウントを取得できません。"
            "gh auth login --skip-ssh-key を実行してください "
            "(--skip-ssh-key / --with-token / --git-protocol https 付きの login は"
            "検証なしで実行できます)。"
        )
    return active, None


def _cli_error_reason() -> str | None:
    """CLI エラーまたは未ログイン時の理由を返す (正常取得時は None)。"""
    _active, err = _fetch_active_accounts()
    return err


def get_active_account(project_dir: str) -> dict[str, str] | None:
    """現在のアクティブ GitHub アカウントを {hostname: user} の dict で返す。

    取得不可・未ログインの場合は None。詳細な理由は `_cli_error_reason()` で
    取得できる。
    """
    active, _err = _fetch_active_accounts()
    return active


def suggest_accounts_entry(project_dir: str) -> str | dict | None:
    """accounts.local.json の "github" キーに書く値を提案する。

    - host が 1 つだけなら scalar (user 文字列)
    - 複数 host なら dict[host, user]
    - 取得不可なら None
    """
    active = get_active_account(project_dir)
    if not active:
        return None
    if len(active) == 1:
        return next(iter(active.values()))
    return dict(active)


def verify(expected, project_dir: str, env=None) -> str | None:
    active, err = _fetch_active_accounts(env)
    if err:
        return err

    if isinstance(expected, dict):
        if not expected:
            return (
                'GitHub: accounts.local.json の "github" オブジェクトが空です。'
                ' {"github": {"github.com": "YOUR_ACCOUNT"}} の形式で'
                ' ホスト名とアカウントのマップを記述してください。'
            )
        errors: list[str] = []
        for host, want in expected.items():
            if not isinstance(want, str):
                errors.append(
                    f"GitHub [{host}]: 期待値は文字列で指定してください "
                    f"(現在: {type(want).__name__})。"
                )
                continue
            current = active.get(host)
            if current is None:
                errors.append(
                    f"GitHub [{host}]: このホストにログインしていません — "
                    f"gh auth login --hostname {host} --skip-ssh-key を実行してください。"
                )
            elif current != want:
                errors.append(
                    f"GitHub [{host}] アカウント不一致: 現在={current}, 期待={want}"
                    f" — 切り替え: gh auth switch --hostname {host} --user {want}"
                )
        return "\n".join(errors) if errors else None

    if not isinstance(expected, str):
        return (
            f'GitHub: accounts.local.json の "github" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    # str 形式では github.com を優先照合。複数 host がある場合に
    # GHE のアカウントで誤 deny しないようにする。
    if "github.com" in active:
        host = "github.com"
    else:
        host = next(iter(active))
    current = active[host]

    if current != expected:
        msg = (
            f"GitHub [{host}] アカウント不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: gh auth switch --hostname {host} --user {expected}"
        )
        if len(active) > 1:
            msg += (
                "\n(複数ホストにログイン中。ホスト別に検証するには "
                'accounts.local.json を dict 形式に変更してください: '
                '{"github": {"github.com": "USER"}})'
            )
        return msg

    return None


_LOGIN_RE = re.compile(r"^gh\s+auth\s+login\b")
_TRUE_VALUES = frozenset({"true", "1", "yes"})


def _bool_flag_value(value: str) -> bool:
    """`--flag=<value>` の実効 boolean。true/1/yes (大文字小文字非依存) のみ True。

    false/0/no は False、それ以外 (gh がエラーにする値) も保守的に False。
    """
    return value.strip().lower() in _TRUE_VALUES


def _login_is_keyless(candidate: str) -> bool:
    """`gh auth login` が SSH 鍵のアップロードを伴わない形なら True。

    flag 文字列の有無ではなく**実効 boolean を解釈**する (gh は `--flag=false` を無効と
    扱い、SSH 鍵アップロード経路に入る):
    - `--skip-ssh-key` / `--with-token`: 裸または `=true|1|yes` のときだけ有効、
      `=false|0|no` は無効
    - `--git-protocol <v>` / `--git-protocol=<v>` / `-p <v>` / `-p=<v>` / `-p<v>`:
      値が `https` のときだけ有効 (`ssh` は無効)
    同じ flag の繰り返しは後勝ち (cobra と同じ)。`--` 以降は引数として見ない。
    有効な flag が 1 つでもあれば鍵操作は起きない (`--with-token` は token を stdin
    から保存するだけ、`--skip-ssh-key` は鍵ステップを抑止、https では鍵ステップ無し)。
    """
    if not _LOGIN_RE.search(candidate):
        return False
    try:
        tokens = shlex.split(candidate)
    except ValueError:
        tokens = candidate.split()
    skip_ssh_key = False
    with_token = False
    protocol = ""
    i = 3  # `gh auth login` の後ろから
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok == "--":
            break
        name, eq, value = tok.partition("=")
        if name in ("--skip-ssh-key", "--with-token"):
            effective = _bool_flag_value(value) if eq else True
            if name == "--skip-ssh-key":
                skip_ssh_key = effective
            else:
                with_token = effective
        elif name in ("--git-protocol", "-p"):
            if eq:
                protocol = value.strip().lower()
            elif i < len(tokens):
                protocol = tokens[i].strip().lower()
                i += 1
            else:
                protocol = ""
        elif tok.startswith("-p") and not tok.startswith("--") and len(tok) > 2:
            protocol = tok[2:].strip().lower()
    return skip_ssh_key or with_token or protocol == "https"


def is_readonly(candidate: str) -> bool:
    """正規表現 (READONLY) で表せない readonly 判定: 鍵操作を伴わない `gh auth login`。"""
    return _login_is_keyless(candidate)


_SWITCH_RE = re.compile(r"^gh\s+auth\s+switch\b")


def _parse_switch_args(candidate: str) -> tuple[str | None, str | None]:
    """gh auth switch 候補から (--hostname, --user) の値を取り出す。"""
    try:
        tokens = shlex.split(candidate)
    except ValueError:
        tokens = candidate.split()
    hostname = None
    user = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--hostname", "-h") and i + 1 < len(tokens):
            hostname = tokens[i + 1]
            i += 2
            continue
        if tok in ("--user", "-u") and i + 1 < len(tokens):
            user = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--hostname="):
            hostname = tok.split("=", 1)[1]
        elif tok.startswith("--user="):
            user = tok.split("=", 1)[1]
        i += 1
    return hostname, user


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待アカウントへの切替」なら True。

    `gh auth switch --user <expected>` のみ許可 (dict 期待値は --hostname も
    照合、省略時は github.com)。--user 無し (インタラクティブ選択) や期待値
    以外への切替は False で通常検証に落とす。
    """
    if not _SWITCH_RE.search(candidate):
        return False
    hostname, user = _parse_switch_args(candidate)
    if not user:
        return False
    if isinstance(expected, str):
        return user == expected
    if isinstance(expected, dict):
        want = expected.get(hostname or "github.com")
        return isinstance(want, str) and user == want
    return False
