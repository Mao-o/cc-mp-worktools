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

# `\b` だと `gh-ost --help` のようなハイフン付き別コマンドまで拾うため、
# 空白または終端が続く形だけに限定する。
PATTERNS = [r"^gh(?=\s|$)"]
READONLY = [
    r"^gh\s+auth\s+(status|list)\b",
    # 認証系の素通しは「コマンド名で括る」のではなく **リモートに何も書かないと
    # 証明できる形だけ** に絞る。名前で括ると、オプション次第で write に化ける形まで
    # 巻き込む (PR #43 Codex R2: `gh auth login` の SSH 鍵アップロード /
    # R3: `--skip-ssh-key=false` / R4: `gh auth refresh --scopes`)。
    # 証明できないものは READONLY から外すか、
    # regex で表せないなら `is_readonly()` で実効値を解釈する (下記 `_login_is_keyless`)。
    # - `logout`: ローカルの hosts.yml からホストエントリを消すだけ。アカウント側の
    #   OAuth grant は残る (revoke は GitHub の設定画面が必要)
    # - `setup-git`: ローカル git config に credential.helper を書くだけ
    # どちらも取りうるオプション (`--hostname` / `--user` / `--force`) を足しても
    # ローカル設定の範囲を出ない。
    # `gh auth refresh` は **外した** (Codex R4): 保存済み認証情報の権限を拡張・修正する
    # コマンドで、`--scopes admin:org` のように CLI OAuth app の grant scope を
    # アカウント側 (= リモート) で変更しうる。期待外アカウントに対しては
    # リモート write と同じ扱いにし、通常の検証対象に戻す。STATE_CHANGING には
    # 残すので、実行後の成功 cache は従来どおり破棄される。
    # deny 文面が案内するのは `gh auth login --skip-ssh-key` / `gh auth switch` だけで
    # logout / setup-git / refresh は案内しないため、外しても remediation loop に
    # ならない。直後の write は (STATE_CHANGING で成功 cache も破棄されるため)
    # 次回 hook で再検証される。
    r"^gh\s+auth\s+(logout|setup-git)\b",
    # `gh auth login` がリモートに書きうる経路は 2 つあり、両方が塞がれた形だけを
    # readonly にする (regex では表せないので `is_readonly()` = 下記
    # `_login_is_keyless` が token 列を解釈する):
    # (a) SSH git protocol を選ぶと既存の SSH 公開鍵を GitHub アカウントに
    #     **アップロード**しうる (gh 2.96 `gh auth login --help`: SSH 選択時に鍵を
    #     検出してアップロード、`--skip-ssh-key` で抑止)。flag の実効 boolean を解釈する
    # (b) `-s` / `--scopes` はアカウント側の OAuth grant を拡張する
    #     (gh 2.98 `gh-auth-login.1`: `-s, --scopes <strings>  Additional
    #     authentication scopes to request`)。`refresh` を外したのと同じ理由で、
    #     付いていれば readonly にしない (値を取る flag なので `=false` で無効化不可)
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^gh\s+(--version|--help|version|help)\b",
]
# アクティブアカウント (hosts.yml) や認証情報の権限を変えうるコマンド。dispatcher が
# 検出すると github の成功 cache を破棄する。`switch` は期待値向きなら
# self-remediation で検証なし、期待値以外なら通常検証 (実行前の状態) だが、どちらも
# cache は残さない。`refresh` は READONLY から外した後もここには残す — 外すと
# `gh auth refresh --scopes ...` 成功後に古い成功 cache が TTL 分残ってしまう。
STATE_CHANGING = [r"^gh\s+auth\s+(switch|login|logout|refresh)\b"]
ACCOUNT_KEY = "github"
SETUP_HINT = (
    'GitHub 最小例: {"github": "YOUR_USERNAME"}。'
    "gh auth status で現在値を確認可。"
    'GHE 別指定: {"github": {"github.com":"USER","ghe.corp.com":"USER"}}'
)
# builder (scripts/accounts_builder.py) の書込前スキーマ検証が参照する契約。
# hostname は任意の文字列を許すため DICT_ALLOWED_KEYS は宣言しない
# (builder 側は getattr の既定値 None を「キー制限なし」と解釈する)。
ACCEPTS_DICT = True
# 下の verify() は dict 期待値の**全キー**の値を `isinstance(want, str)` で検査し、
# 1 つでも非 str なら (falsy な None でも) その host のエラーを積む。よって builder は
# migrate の緩和モードでも非 str 値を素通ししてはならない → "all"。
DICT_VALUE_CHECK = "all"
# SCALAR_EQUIVALENT_DICT_KEY は**宣言しない**。github の scalar 期待値は
# 「github.com が active ならそれ、無ければ**最初の active host**」を照合する
# (下の verify() の str 分岐 = `if "github.com" in active: ... else:
# host = next(iter(active))`)。照合先が実行時の active 状態で変わる**動的な
# 意味論**なので、どの静的な hostname キーとも等価にならない。
# 反例: ghe.example.com だけが active で値が USER のとき、scalar "USER" は
# allow だが `{"github.com": "USER"}` は「このホストにログインしていません」で
# deny する。`"github.com"` を宣言すると builder の migrate が両者を非衝突と
# 判定し、明示された github.com の要求を無警告で捨てる/書き換えてしまう。
# 未宣言なので builder は scalar/dict の混在を**両方向とも conflict** に倒し、
# 利用者が両側を見て選ぶ (test_services.py の lock テストで再宣言を防ぐ)。

_LOGGED_IN_RE = re.compile(r"Logged in to (\S+) account (\S+)")
# gh < 2.40 の単一アカウント形式: `✓ Logged in to github.com as Mao-o (keyring)`。
# 2.40 で複数アカウント対応 (`Active account: true/false` marker) が入る前は
# host 1 つにつきアカウント 1 つのみで、marker 行自体が存在しない。
_LOGGED_IN_LEGACY_RE = re.compile(r"Logged in to (\S+) as (\S+)")


def parse_active_accounts(output_text: str) -> dict[str, str]:
    """gh auth status の出力から {hostname: active_account} を返す。

    gh 2.40+ (複数アカウント対応): 各 `Active account: true` について、直前の
    `Active account: true` より後の範囲を逆順にスキャンして最初の
    `Logged in to <host> account <user>` を採用する。これにより複数 host が
    ある場合も各 host のアクティブアカウントが正しくペア化される。

    gh < 2.40 (単一アカウント形式): `Active account:` marker 行そのものが
    出力に存在しない。この場合は `Logged in to <host> as <user>` 形式を
    host ごとに 1 件ずつ拾う fallback を使う (旧形式は host あたり常に
    1 アカウントのみなので曖昧さは無い)。
    """
    lines = output_text.splitlines()
    if not any("Active account:" in line for line in lines):
        result: dict[str, str] = {}
        for line in lines:
            m = _LOGGED_IN_LEGACY_RE.search(line)
            if m:
                host, user = m.group(1), m.group(2)
                result.setdefault(host, user)
        return result

    result = {}
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
    """gh auth status を実行し (active_accounts, error_reason) を返す。

    `active` が空になる理由を 2 つに区別する:
    - 出力に `Logged in to` が一切現れない → 本当に未ログイン
      (`gh auth status` は未ログイン時 stderr に "You are not logged into any
      GitHub hosts." のような案内を出すだけで host ブロックを持たない)
    - `Logged in to` はあるのに `parse_active_accounts` がどの host にも
      アカウントを対応付けられない → gh の出力形式を解釈できない (未知の
      将来フォーマット等)。gh 2.40 未満は `Active account:` marker を持たない
      旧形式に `parse_active_accounts` 自体が fallback するため、ここに来るのは
      それでも解釈できない場合のみ。
    """
    combined, err = _run_gh_auth_status(env)
    if err:
        return None, err
    active = parse_active_accounts(combined)
    if not active:
        if "Logged in to" in combined:
            return None, (
                "GitHub: gh auth status の出力を解釈できません。"
                "gh --version を確認してください (2.40 以上を推奨)。"
            )
        return None, (
            "GitHub: アクティブアカウントを取得できません。"
            "gh auth login --skip-ssh-key を実行してください "
            "(--skip-ssh-key / --with-token / --git-protocol https 付きの login は"
            "検証なしで実行できます)。"
        )
    return active, None


def get_active_account(project_dir: str) -> dict[str, str] | None:
    """現在のアクティブ GitHub アカウントを {hostname: user} の dict で返す。

    取得不可・未ログインの場合は None。
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


def verify(expected, project_dir: str, env=None, context=None) -> str | None:
    """context: 他 service と揃えた interface。gh では**使わない**。

    `gh` の `--hostname` / `--user` は「どのアカウントで実行するか」ではなく
    **操作対象**の指定 (例: `gh auth refresh --hostname ghe.example.com` は
    アクティブアカウントのままリモートを指定するだけ) なので、`CONTEXT_OPTIONS`
    を宣言せず照合先は常にアクティブアカウントとする (README 既知の制限)。
    """
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
    """`gh auth login` がリモートに何も書かない形なら True。

    2 つの条件を両方満たす必要がある。

    (a) **SSH 鍵のアップロードが起きない** — flag 文字列の有無ではなく実効 boolean を
    解釈する (gh は `--flag=false` を無効と扱い、SSH 鍵アップロード経路に入る):
    - `--skip-ssh-key` / `--with-token`: 裸または `=true|1|yes` のときだけ有効、
      `=false|0|no` は無効
    - `--git-protocol <v>` / `--git-protocol=<v>` / `-p <v>` / `-p=<v>` / `-p<v>`:
      値が `https` のときだけ有効 (`ssh` は無効)
    有効な flag が 1 つでもあれば鍵操作は起きない (`--with-token` は token を stdin
    から保存するだけ、`--skip-ssh-key` は鍵ステップを抑止、https では鍵ステップ無し)。

    (b) **OAuth grant scope を要求していない** — `-s` / `--scopes` は
    `gh auth refresh --scopes` と同じくアカウント側の grant を拡張する
    (gh 2.98 `gh-auth-login.1`: `-s, --scopes <strings>  Additional authentication
    scopes to request`)。値を取る flag なので `=false` では無効化できず、
    付いていれば無条件に readonly から外す (Codex R4 の論拠を login にも適用)。

    同じ flag の繰り返しは後勝ち (cobra と同じ)。`--` 以降は引数として見ない。
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
    scopes_requested = False
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
        elif name in ("--scopes", "-s"):
            # 値を取る flag なので `=false` で無効化できない。付いていれば無条件に
            # readonly から外す。分離形 (`-s admin:org`) は値 token も消費して
            # 後続の誤解釈を防ぐ。
            scopes_requested = True
            if not eq and i < len(tokens):
                i += 1
        elif tok.startswith("-p") and not tok.startswith("--") and len(tok) > 2:
            protocol = tok[2:].strip().lower()
        elif tok.startswith("-s") and not tok.startswith("--") and len(tok) > 2:
            # 連結形 (`-sadmin:org`)
            scopes_requested = True
    if scopes_requested:
        return False
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
