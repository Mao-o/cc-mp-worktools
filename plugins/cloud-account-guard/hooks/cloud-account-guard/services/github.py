"""GitHub (gh CLI) アカウント検証。

accounts.local.json の "github" は 2 形式を受け付ける:
- 文字列: `"github": "Mao-o"`
  任意の host のアクティブアカウントを照合 (後方互換)
- オブジェクト: `"github": {"github.com": "Mao-o", "ghe.example.com": "mao-corp"}`
  hostname ごとのアクティブアカウントを個別照合 (GHE / 複数インスタンス対応)
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from core import budget, cli_config, shell_word

# `\b` だと `gh-ost --help` のようなハイフン付き別コマンドまで拾うため、
# 空白または終端が続く形だけに限定する。
PATTERNS = [r"^gh(?=\s|$)"]
# option を審査する READONLY エントリは名前付き定数にする (READONLY_SAFE_OPTIONS /
# DISCLOSING が同じ文字列をキーに参照するため。literal を 2 箇所に書くと、片方を
# 直したときに宣言が黙って無効化される)。
_RO_AUTH_STATUS = r"^gh\s+auth\s+(status|list)\b"

# --- サブコマンドの後ろの `--help` (v0.30.0) ---------------------------------------
#
# `gh <コマンド> <サブコマンド> ... --help` は gh がヘルプを出して終わり、コマンドを実行しない
# (cobra は flag を解釈した直後、PersistentPreRun より前に help を見る)。0.29.x までは
# READONLY の情報系が CLI 名直後の形 (`gh --help`) だけだったので、`gh auth switch --help` は
# 切替 (STATE_CHANGING)、`gh auth token --help` は開示 (DISCLOSING) として扱われ、不一致で
# deny されていた。この形を READONLY に足し、STATE_CHANGING と DISCLOSING からは外す
# (下の `_unless_help`)。読み取りにするのは、次をすべて満たす形だけ:
#
# 1. コマンドのパスが gh の組み込み (下の `_HELP_LEAVES` / `_HELP_GROUPS`)。alias と extension は
#    `--help` をそのまま中身に渡す (gh 2.105 で実測: `gh alias set --shell 'pr zz' 'echo RAN "$@"'`
#    の後の `gh pr zz --help` は `RAN --help` を出す。トップレベルの alias の `gh zz --help` も同じ。
#    `gh extension exec <name> --help` も extension を起動する)。alias はトップレベルのほか、
#    グループ (子を持つコマンド。`pr` / `auth` / `repo deploy-key` など) の下にも置ける
#    (`gh alias set 'issue mine' ...` は gh 自身のヘルプの例)。置けないのは、組み込みと同じ名前と、
#    末端 (子を持たないコマンド。`pr create` / `api` など) の下 (実測: どちらも `gh alias set` が
#    「already a gh command」で拒否し、設定ファイルに直接書いても組み込みが勝つ)。extension は
#    トップレベルにしか置けず、組み込みと同じ名前を持てない。そこで:
#    - 末端は、後ろに語を置いてよい (`gh pr view 123 --help`。2〜5 の条件のもとで)
#    - グループは、直後が `--help` の形 (`gh pr --help`) だけ。直後の語が組み込みの子でない形
#      (`gh pr ship --help` / `gh pr -R o/r ship --help`) は alias でありうるので読み取りにしない
# 2. `--help` がコマンド行の最後の語 (後ろはリダイレクトだけ)。後ろの `--help=false` は後勝ちで
#    ヘルプを取り消す (実測: `gh pr create --help --help=false` は create を実行する)
# 3. 直前の語が「値を取るかもしれない option」(`-` で始まり `=` を含まない語。`-` 単独は除く)
#    ではない。値を取る option の直後の `--help` は値になる (実測: `gh pr create --title --help` /
#    `-dt --help` / `--body-file --help` / `gh auth switch -h --help` はどれも実行する)。
#    どの option が値を取るかはサブコマンドごとに違い、hook は表を持たないので、bool の flag の
#    直後 (`--draft --help`。実際はヘルプ) も読み取りにしない (検証は従来どおり)
# 4. 前に `--` が無い (`--` の後ろは位置引数で、ヘルプにならない)
# 5. 語が記号を含まない (英数字と `_./:@%+=,-` だけ。引用符・`$`・`*`・`{`・`~`・`#`・
#    `\` などがあると、シェルが語を分け直す・値を差し込む・コメントにする余地があり、語の位置を
#    文字列から決められない)。2 語目以降の語は `=` で始まらない (zsh の `=cmd` はパスに展開される)
#
# `-h` は数えない (gh の `auth` 配下などで `--hostname` の短い形。`gh auth switch -h <host>` は
# 切替のまま)。`--help` を含む語の解釈 (2〜4) は gh 2.105 で、組み込みの全サブコマンドについて
# `<path> --help` / `<path> x --help` / `<path> x y --help` の 3 形がヘルプを出すことを、認証の無い
# 一時 HOME で実測して決めた (位置引数の後ろの `--help` もヘルプになる = flag を最後まで読む)。
#
# 組み込みのコマンドのパス。gh 2.105 の `gh reference` の見出しから作り、子を持つパスをグループ、
# 持たないパスを末端に分けた (`codespace ports` は実行もできるが子を持つのでグループに数える。
# 安全側)。外したもの:
# - `copilot` / `extension` と配下: 引数を別のプログラム (Copilot CLI / extension) に渡す
# - `(preview)` と表示されるもの (`agent-task` / `discussion` / `event` / `skill`) と配下: 引数の
#   扱いが版で変わりうる
# - 組み込みの別名 (`gh issue ls` / `gh cs` など) と、`gh reference` に載らない hidden のコマンド。
#   hidden の `auth git-credential` だけは、ヘルプを出すことを実測して末端に足した
# 外したコマンドと alias (`gh co` など) の `--help` は従来どおり検証する (`gh help <command>` の形は
# 読み取り)。gh の版が上がって組み込みが消えると、その名前を alias に使えるようになるので、表は
# gh の版を上げたときに同じ規則で作り直す (docs/DEVELOPMENT.md の D13)。
_HELP_GROUPS = (
    "alias", "attestation", "auth", "cache", "codespace", "codespace ports", "config", "gist",
    "gpg-key", "issue", "issue artifact", "label", "org", "pr", "preview", "project", "release",
    "repo", "repo autolink", "repo deploy-key", "repo gitignore", "repo license", "ruleset", "run",
    "search", "secret", "ssh-key", "variable", "workflow",
)
_HELP_LEAVES = (
    "alias delete", "alias import", "alias list", "alias set", "api", "attestation download",
    "attestation trusted-root", "attestation verify", "auth git-credential", "auth login",
    "auth logout", "auth refresh", "auth setup-git", "auth status", "auth switch", "auth token",
    "browse", "cache delete", "cache list", "codespace code", "codespace cp", "codespace create",
    "codespace delete", "codespace edit", "codespace jupyter", "codespace list", "codespace logs",
    "codespace ports forward", "codespace ports visibility", "codespace rebuild", "codespace ssh",
    "codespace stop", "codespace view", "completion", "config clear-cache", "config get",
    "config list", "config set", "gist clone", "gist create", "gist delete", "gist edit",
    "gist list", "gist rename", "gist view", "gpg-key add", "gpg-key delete", "gpg-key list",
    "issue artifact create", "issue artifact delete", "issue artifact download",
    "issue artifact edit", "issue artifact list", "issue artifact view", "issue close",
    "issue comment", "issue create", "issue delete", "issue develop", "issue edit", "issue list",
    "issue lock", "issue pin", "issue reopen", "issue status", "issue transfer", "issue unlock",
    "issue unpin", "issue view", "label clone", "label create", "label delete", "label edit",
    "label list", "licenses", "org list", "pr checkout", "pr checks", "pr close", "pr comment",
    "pr create", "pr diff", "pr edit", "pr list", "pr lock", "pr merge", "pr ready", "pr reopen",
    "pr revert", "pr review", "pr status", "pr unlock", "pr update-branch", "pr view",
    "preview prompter", "project close", "project copy", "project create", "project delete",
    "project edit", "project field-create", "project field-delete", "project field-list",
    "project item-add", "project item-archive", "project item-create", "project item-delete",
    "project item-edit", "project item-list", "project link", "project list",
    "project mark-template", "project unlink", "project view", "release create", "release delete",
    "release delete-asset", "release download", "release edit", "release list", "release upload",
    "release verify", "release verify-asset", "release view", "repo archive",
    "repo autolink create", "repo autolink delete", "repo autolink list", "repo autolink view",
    "repo clone", "repo create", "repo delete", "repo deploy-key add", "repo deploy-key delete",
    "repo deploy-key list", "repo edit", "repo fork", "repo gitignore list", "repo gitignore view",
    "repo license list", "repo license view", "repo list", "repo read-dir", "repo read-file",
    "repo rename", "repo set-default", "repo sync", "repo unarchive", "repo view", "ruleset check",
    "ruleset list", "ruleset view", "run cancel", "run delete", "run download", "run list",
    "run rerun", "run view", "run watch", "search code", "search commits", "search issues",
    "search prs", "search repos", "secret delete", "secret list", "secret set", "ssh-key add",
    "ssh-key delete", "ssh-key list", "status", "variable delete", "variable get", "variable list",
    "variable set", "workflow disable", "workflow enable", "workflow list", "workflow run",
    "workflow view",
)


def _path_alternation(paths) -> str:
    """パスの表を regex の選択にする (語の間は空白かタブ。長いパスを先に試す)。"""
    return "(?:" + "|".join(
        r"[ \t]+".join(re.escape(word) for word in path.split())
        for path in sorted(paths, key=len, reverse=True)
    ) + ")"


# 語 (記号を含まない。先頭は `=` 以外)。
_HELP_WORD = r"[A-Za-z0-9_./:@%+,-][A-Za-z0-9_./:@%+=,-]*"
# `--help` の直前に置ける語: `-` で始まらない語 / `-` 単独 / `=` を含む option (`--title=x`)。
_HELP_CLOSED_WORD = (
    r"(?:[A-Za-z0-9_./:@%+,][A-Za-z0-9_./:@%+=,-]*"
    r"|-"
    r"|-[A-Za-z0-9_./:@%+,-]*=[A-Za-z0-9_./:@%+=,-]*)"
)
# `--help` の後ろに置けるリダイレクト (`2>&1` / `>/dev/null` / `> out.txt` / `< /dev/null` /
# `&>f`)。数字の付く形は空白の後ろだけ (`--help2>&1` は `--help2` という語になる)。
# 空白の前置きは 1 通りにしか読めない形にする (`[ \t]+[0-9]*` と `[ \t]*` を並べると、`--help` の
# 後ろのリダイレクトの数に指数の時間がかかる)。
_HELP_REDIRECT = (
    r"(?:(?:[ \t]+[0-9]*)?(?:>>|>&|>|<&|<)|[ \t]*&>>?)[ \t]*" + _HELP_WORD
)
_SUBCOMMAND_HELP = (
    r"gh[ \t]+(?:"
    # 末端: 後ろに語を置いてよい
    + _path_alternation(_HELP_LEAVES)
    + r"(?:(?:[ \t]+(?!--(?:[ \t]|$))" + _HELP_WORD + r")*[ \t]+" + _HELP_CLOSED_WORD + r")?"
    # グループ: 直後が `--help` の形だけ
    + r"|" + _path_alternation(_HELP_GROUPS)
    + r")[ \t]+--help(?:" + _HELP_REDIRECT + r")*[ \t]*$"
)
_RO_SUBCOMMAND_HELP = r"^" + _SUBCOMMAND_HELP


def _unless_help(pattern: str) -> str:
    """anchored な `pattern` から、サブコマンドの後ろの `--help` の形を除いた pattern。

    STATE_CHANGING (成功 cache の破棄と連結規則の切替側) と DISCLOSING (READONLY の取り消し) に
    使う。除くのは `_RO_SUBCOMMAND_HELP` に当たる形だけなので、「pattern に当たり、ヘルプの形
    ではない」ものは従来どおり当たる (`gh auth switch -h ghe.example.com` / `gh auth switch
    --user --help` は切替のまま)。
    """
    assert pattern.startswith("^"), pattern
    return r"^(?!" + _SUBCOMMAND_HELP + r")" + pattern[1:]


READONLY = [
    _RO_AUTH_STATUS,
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
    # サブコマンドの後ろの `--help` (v0.30.0。上の `_SUBCOMMAND_HELP` の説明)。
    _RO_SUBCOMMAND_HELP,
]
# `gh auth status` / `gh auth list` で「これが付いていても readonly」と言える option。
# これ以外が付いていたら READONLY を取り消して QUERY に降格する (= 検証は走るが
# 不一致でも止めない)。`--hostname` は照合先ではなく**表示対象の host** を絞るだけ
# (CONTEXT_OPTIONS を宣言していない理由と同じ) なので安全側に数えられる。
READONLY_SAFE_OPTIONS = {
    _RO_AUTH_STATUS: frozenset({"--hostname", "-h", "--active", "-a"}),
}
# 認証情報を出力する形 / option。READONLY / QUERY を取り消して WRITE 扱いにする。
# `--show-token` (`-t`) は期待外アカウントのトークンを平文で出すため、
# 「READONLY のコマンド名に一致する」だけでは素通しさせない (内部バックログ)。
# `gh auth token` は READONLY に無いので現状も検証対象だが、**形そのものが開示**
# であることを宣言側に残す (将来 READONLY に足したときに素通しに戻らないように)。
# ヘルプの形 (`gh auth token --help`。トークンは出ない) だけは除く (v0.30.0。`_unless_help`)。
# `_RO_AUTH_STATUS` の行は除かない: `--show-token --help` は直前が option なのでヘルプの形に
# 当たらない。`--show-token=true --help` / `-t=1 --help` はヘルプの形に当たるが、DISCLOSING が
# READONLY より先に効くので WRITE のまま (実際はヘルプを出すだけ。止める側に倒れる)。
DISCLOSING = [
    (_RO_AUTH_STATUS, frozenset({"--show-token", "-t"})),
    (_unless_help(r"^gh\s+auth\s+token(?=\s|$)"), frozenset()),
]
# リモート read (資源を変更しない)。不一致でも deny せず警告のみで通す。
# **列挙した形だけ**を QUERY にする — ここは判定表を緩める唯一の方向なので、
# 「読むだけと証明できる形」に限る。`secret` / `variable` の `list` / `view` は
# 名前とメタデータだけで値は出ない (値は create/set 側)。
QUERY = [
    r"^gh\s+(pr|issue|repo|release|run|workflow|search|gist|label|cache"
    r"|secret|variable)\s+(list|view|status|checks|diff|download)(?=\s|$)",
    r"^gh\s+(status|browse)(?=\s|$)",
]
# アクティブアカウント (hosts.yml) や認証情報の権限を変えうるコマンド。dispatcher が
# 検出すると github の成功 cache を破棄する。`switch` は期待値向きなら
# self-remediation で検証なし、期待値以外なら通常検証 (実行前の状態) だが、どちらも
# cache は残さない。`refresh` は READONLY から外した後もここには残す — 外すと
# `gh auth refresh --scopes ...` 成功後に古い成功 cache が TTL 分残ってしまう。
# ただし `--user` 無しの `refresh` はアクティブアカウントを変えないので、連結規則の
# 切替側には数えない (下の `changes_identity`)。
# ヘルプの形 (`gh auth switch --help`) は状態を変えないので除く (v0.30.0。`_unless_help`)。
# 除くと cache の破棄と連結規則の切替側の両方から外れる (dispatcher はどちらもこの表で決める)。
STATE_CHANGING = [_unless_help(r"^gh\s+auth\s+(switch|login|logout|refresh)\b")]
ACCOUNT_KEY = "github"

# deny 文面で案内する remediation コマンド (引数付きの実コマンド形) の正規表現。
# dispatcher は verify() の返り値にこれが一致するときだけ「単独で実行せよ」の注記を
# 付ける。コマンド名だけの言及 (「firebase use がタイムアウトしました」等の診断文) や
# インストール案内・設定ファイルの型不正には一致させない。
REMEDIATION_PATTERNS = (r"gh auth switch\s+--", r"gh auth login\b")
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


def parse_logged_in_accounts(output_text: str) -> dict[str, list[str]]:
    """gh auth status の出力から {hostname: [ログイン済みの全アカウント]} を返す。

    `parse_active_accounts` がアクティブなものだけを拾うのに対し、こちらは
    **非アクティブなアカウントも含める** (自動切替の切替先になれるのはこの一覧に
    あるものだけ)。トークンの検証に失敗したアカウントは gh が
    `Failed to log in to <host> account <user>` と出すので、`Logged in to` の
    正規表現には一致せず一覧に入らない (切り替えても使えないため)。
    gh < 2.40 の旧形式 (`Logged in to <host> as <user>`) は host あたり 1 アカウント。
    """
    result: dict[str, list[str]] = {}
    for line in output_text.splitlines():
        m = _LOGGED_IN_RE.search(line) or _LOGGED_IN_LEGACY_RE.search(line)
        if not m:
            continue
        host, user = m.group(1), m.group(2)
        users = result.setdefault(host, [])
        if user not in users:
            users.append(user)
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
            timeout=budget.call_timeout(10),
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


# gh の設定ファイルからアクティブアカウントを読む経路 (CLI 実行の回避)。
#
# `gh auth status` は全 host のトークンを **API で検証**するため往復が入り
# (〜500ms)、オフラインでは失敗する。一方 gh はアクティブアカウントを
# `hosts.yml` の `<host>.user` に書いており、ここを読めばネットワーク無しで
# 決まる (実測: gh 2.9x の hosts.yml は host ごとに `users:` (紐付く全
# アカウント) と `user:` (アクティブ) を持つ)。
#
# **ローカル読取を使わない条件** (どれかに当たれば従来の CLI 実行に落ちる):
# - トークンを env で渡している (`GH_TOKEN` 等) — この場合 gh は hosts.yml では
#   なく env のトークンで動き、アカウント名は API 経由でしか分からない
# - `GH_HOST` が立っている — gh 側の host 列挙がどう変わるかを実測で確定できて
#   いないため、照合先がずれる可能性を避けて CLI に委ねる
# - `HOME` が hook プロセスと違う (`cli_config.home_overridden`) — 実行される gh は
#   別の設定ファイルを読む
# - 設定ディレクトリを決められない (Windows で `GH_CONFIG_DIR` / `XDG_CONFIG_HOME` / `AppData` の
#   どれも無い・Cygwin / MSYS の Python。v0.35.1、`cli_config.config_dir_rule`)
# - 設定ファイルが読めない / 最小 YAML サブセットで解釈できない
#
# **この列挙は閉じている = 漏れると黙って false allow 経路になる** (gcloud 側は
# `CLOUDSDK_` prefix の denylist なので未知の変数でも自動 bail するが、gh 側は
# 列挙した名前しか見ない)。列挙は公式の環境変数一覧
# <https://cli.github.com/manual/gh_help_environment> と突合して決めた
# (auth / host / 設定ディレクトリに効くのは下の 4 トークン env + `GH_HOST` +
# `GH_CONFIG_DIR` / `XDG_CONFIG_HOME` / `HOME`、Windows の `AppData`)。
# **gh に認証・host を変える env が増えたらここに足すこと** — 機械検出できない
# ので、gh の major update 時に上記 URL を読み直すのが唯一の担保。
_TOKEN_ENV_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
_HOST_ENV_VAR = "GH_HOST"
_CONFIG_DIR_ENV_VAR = "GH_CONFIG_DIR"
_XDG_CONFIG_HOME_ENV_VAR = "XDG_CONFIG_HOME"
# CLI がどのアカウントで動くかを決める env (成功 cache のキーに含める。
# services/__init__.py の IDENTITY_ENV_* 契約)。上の列挙と同じ根拠で、
# トークン・host・設定ディレクトリを決めるもの。
IDENTITY_ENV_VARS = frozenset(
    {
        *_TOKEN_ENV_VARS,
        _HOST_ENV_VAR,
        _CONFIG_DIR_ENV_VAR,
        _XDG_CONFIG_HOME_ENV_VAR,
        "HOME",
        "APPDATA",
    }
)
IDENTITY_ENV_PREFIXES: tuple[str, ...] = ()
_HOSTS_FILE = "hosts.yml"
_ACTIVE_USER_KEY = "user"
# Windows の gh の設定ディレクトリの決まり (v0.35.1。go-gh の `ConfigDir` と `gh help environment`):
# `GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `%AppData%\GitHub CLI`。
_WINDOWS_CONFIG_DIRS = (
    (_CONFIG_DIR_ENV_VAR, None),
    (_XDG_CONFIG_HOME_ENV_VAR, "gh"),
    (cli_config.APPDATA_ENV_VAR, "GitHub CLI"),
)


def _config_dir(env, home: Path | None = None) -> Path | None:
    """gh の設定ディレクトリ。決められないなら None (呼び出し側は CLI に聞く)。

    macOS / Linux は `GH_CONFIG_DIR` → `$XDG_CONFIG_HOME/gh` → `~/.config/gh`。Windows は `~/.config/gh` の
    代わりに `%AppData%\\GitHub CLI` で、AppData も無ければ決めない (v0.35.1。`cli_config.config_dir_rule`)。

    home: `~` に使う HOME。None なら hook プロセスの `Path.home()`。Windows では使わない。
    """
    rule = cli_config.config_dir_rule()
    if rule == cli_config.WINDOWS:
        return cli_config.windows_config_dir(env, _WINDOWS_CONFIG_DIRS)
    if rule is None:
        return None
    explicit = env.get(_CONFIG_DIR_ENV_VAR)
    if explicit:
        return Path(explicit)
    xdg = env.get(_XDG_CONFIG_HOME_ENV_VAR)
    if xdg:
        return Path(xdg) / "gh"
    if home is None:
        try:
            home = Path.home()
        except (RuntimeError, OSError):
            return None
    return home / ".config" / "gh"


def state_files(env=None, context=None) -> list[Path] | None:
    """hook を通らない切替 (ターミナルでの `gh auth switch` など) で gh が書き換えるファイル (v0.35.0)。

    gh は host ごとのアクティブアカウントを `hosts.yml` の `<host>.user` に書き、切替
    (`gh auth switch`)・ログイン・ログアウトのたびに書き直す。トークンを keyring に置く構成でも、
    keyring の中のトークンを入れ替えたうえで `user:` を書き換えて hosts.yml を書く (gh のソースの
    `internal/config/config.go` の `activateUser`)。dispatcher はこのファイルの指紋
    (`core/cache.state_fingerprint`) を成功 cache に記録し、変わっていれば照合し直す。

    設定ディレクトリはローカル読取と同じ `_config_dir` で決める。HOME は実行される gh のもの
    (行頭の `HOME=...` があればそれ。`cli_config.overriding_home`)。Windows では `%AppData%\\GitHub CLI`
    の下 (v0.35.1)。場所を決められないときは `cli_config.state_files_when_dir_unknown` (Windows で AppData が
    無いときなどは None = 成功 cache を使わない)。context は使わない。
    """
    e = os.environ if env is None else env
    config_dir = _config_dir(e, cli_config.overriding_home(e))
    if config_dir is None:
        return cli_config.state_files_when_dir_unknown()
    return [config_dir / _HOSTS_FILE]


def _local_active_accounts(env=None) -> dict[str, str] | None:
    """`hosts.yml` から {hostname: active_account} を読む。決められないなら None。

    env: インライン環境変数をマージ済みの完全 env (None なら hook プロセスの環境)。
    検証 subprocess に渡すものと同じ env を見る (`GH_CONFIG_DIR=... gh ...` の形も
    コマンド実行時と同条件で解決するため)。
    """
    e = os.environ if env is None else env
    if any(e.get(name) for name in _TOKEN_ENV_VARS):
        return None
    if e.get(_HOST_ENV_VAR):
        return None
    if cli_config.home_overridden(e):
        return None
    config_dir = _config_dir(e)
    if config_dir is None:
        return None
    text = cli_config.read_text(config_dir / _HOSTS_FILE)
    if text is None:
        return None
    parsed = cli_config.parse_nested_scalar_map(text)
    if parsed is None:
        return None
    active = {}
    for host, props in parsed.items():
        user = props.get(_ACTIVE_USER_KEY)
        if isinstance(user, str) and user:
            active[host] = user
    return active or None


def _scalar_target_is_ambiguous(active: dict[str, str], expected) -> bool:
    """str 期待値の照合先が「ファイルの記載順」に依存してしまう形なら True。

    `scalar_target_host()` は github.com が無いとき**最初の host** を使う。
    hosts.yml の記載順と `gh auth status` の列挙順が一致する保証は無いので、
    github.com が無く host が複数あるローカル読取結果は使わず CLI に委ねる。
    """
    return (
        isinstance(expected, str)
        and "github.com" not in active
        and len(active) > 1
    )


def get_active_account(project_dir: str) -> dict[str, str] | None:
    """現在のアクティブ GitHub アカウントを {hostname: user} の dict で返す。

    取得不可・未ログインの場合は None。**ローカル設定ファイルは読まず CLI を使う**
    — builder (`show` / `--from-cli`) が期待値の提案・突合に使う経路で、対話的な
    ので所要時間より「gh 自身が報告する値であること」を優先する。
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


def scalar_target_host(active: dict[str, str]) -> str:
    """str 期待値をどの host と照合するかを返す (`active` は非空前提)。

    複数ホストにログイン中でも GHE 側のアカウントで誤 deny しないよう
    **github.com を優先**し、無ければ最初の host を使う。gh の列挙順は
    設定ファイルの順序に依存する (GHE が先に来ることがある) ため、
    「最初の host」だけを見ると照合先が環境依存になる。

    `verify()` と `matches()` (builder の show が使う) の**唯一の実装**。
    以前は builder 側が「最初の host」を別実装で持っており、GHE が先に
    列挙される環境で show が [mismatch]、hook は allow という乖離が出ていた
    (内部バックログ)。
    """
    return "github.com" if "github.com" in active else next(iter(active))


def matches(expected, current) -> bool:
    """CLI 実測値 `current` が期待値 `expected` を満たすかを bool で返す。

    `verify()` と同じ規則の述語版。verify() は deny 理由の文面を組み立てる
    責務があるため戻り値が str だが、**判定規則そのものは service 側に一本化**
    して builder (`scripts/accounts_builder.py` の show) と共有する。任意入力
    (accounts.local.json の生値) を受けるため例外は投げない。
    """
    if isinstance(expected, str):
        if isinstance(current, str):
            return current == expected
        if isinstance(current, dict) and current:
            return current.get(scalar_target_host(current)) == expected
        return False
    if isinstance(expected, dict):
        # 空 dict は verify() が「オブジェクトが空です」で deny する形なので
        # match ではない。非空なら宣言された全 host が期待どおりであること。
        if not expected or not isinstance(current, dict):
            return False
        return all(
            isinstance(want, str) and current.get(host) == want
            for host, want in expected.items()
        )
    return False


def _expected_shape_error(expected) -> str | None:
    """期待値そのものの形の不正 (現在値を取得しなくても決まる deny 理由)。"""
    if isinstance(expected, dict):
        if not expected:
            return (
                'GitHub: accounts.local.json の "github" オブジェクトが空です。'
                ' {"github": {"github.com": "YOUR_ACCOUNT"}} の形式で'
                ' ホスト名とアカウントのマップを記述してください。'
            )
        return None
    if not isinstance(expected, str):
        return (
            f'GitHub: accounts.local.json の "github" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )
    return None


def verify(expected, project_dir: str, env=None, context=None) -> str | None:
    """context: 他 service と揃えた interface。gh では**使わない**。

    `gh` の `--hostname` / `--user` は「どのアカウントで実行するか」ではなく
    **操作対象**の指定 (例: `gh auth refresh --hostname ghe.example.com` は
    アクティブアカウントのままリモートを指定するだけ) なので、`CONTEXT_OPTIONS`
    を宣言せず照合先は常にアクティブアカウントとする (README 既知の制限)。

    現在値は**まず `hosts.yml` から読む** (`_local_active_accounts`)。ただし
    ローカル読取で通せるのは **allow だけ**で、エラー (不一致 / 未ログイン) を
    返しそうなときは必ず `gh auth status` で取り直してから判断する。
    こうすると:

    - 速くなるのは成功ケース (= 大多数)。CLI 起動もネットワーク往復も無くなる
    - ローカル読取を誤っても **deny を新造しない** (誤読は「CLI を 1 回呼ぶ」
      コストにしかならず、deny 文面と判定は従来どおり gh の出力から作られる)

    残る差は「hosts.yml にアクティブとして書かれているアカウントのトークンが
    失効している」場合で、従来 deny だったものが allow になる (実行した gh 側が
    認証エラーで失敗する。別アカウントでの書き込みにはならない)。
    """
    shape_error = _expected_shape_error(expected)
    if shape_error:
        return shape_error

    local = _local_active_accounts(env)
    if local is not None and not _scalar_target_is_ambiguous(local, expected):
        if _verify_against(local, expected) is None:
            return None

    active, err = _fetch_active_accounts(env)
    if err:
        return err
    return _verify_against(active, expected)


# host 名 / 期待値が案内するコマンドに入れられない形のとき (core/shell_word.py) の文。
# host は期待値 (dict のキー) か、gh が報告したアクティブな host から来る。
_CHECK_BY_HAND = (
    f"ホスト名か期待値が{shell_word.UNSAFE}。gh のログイン状態と accounts.local.json の"
    f' "{ACCOUNT_KEY}" を手で確認してください'
)


def _host_label(host) -> str:
    """文面に出す host。示せる形 (`shell_word.can_show`) でなければ出さない (表示が注記の判定に
    当たらないように。0.20.0 までは案内に使える形のときだけ)。

    host は accounts.local.json のキーか gh が報告した host。期待値の型の誤りの deny は切替を
    案内しないので、host が切替コマンドの形だと、表示だけで dispatcher の「案内したコマンドは
    単独で実行」の注記が付く。
    """
    return host if shell_word.can_show(host) else "表示しない host"


def _switch_guidance(host: str, user: str) -> str:
    """不一致の deny 文面の末尾 (` — 切り替え: gh auth switch ...`)。"""
    host_arg, user_arg = shell_word.arg(host), shell_word.arg(user)
    if host_arg is None or user_arg is None:
        return f" — {_CHECK_BY_HAND}"
    return f" — 切り替え: gh auth switch --hostname {host_arg} --user {user_arg}"


def _verify_against(active: dict[str, str], expected) -> str | None:
    """アクティブアカウント `active` (非空) を期待値と照合する。

    現在値の取得はしない (取得元が CLI かローカル設定ファイルかに依らず、
    照合規則を 1 箇所に保つため)。
    """
    if isinstance(expected, dict):
        errors: list[str] = []
        for host, want in expected.items():
            if not isinstance(want, str):
                errors.append(
                    f"GitHub [{_host_label(host)}]: 期待値は文字列で指定してください "
                    f"(現在: {type(want).__name__})。"
                )
                continue
            current = active.get(host)
            if current is None:
                host_arg = shell_word.arg(host)
                if host_arg is None:
                    errors.append(
                        f"GitHub [{_host_label(host)}]: このホストにログインしていません — "
                        f"{_CHECK_BY_HAND}。"
                    )
                else:
                    errors.append(
                        f"GitHub [{_host_label(host)}]: このホストにログインしていません — "
                        f"gh auth login --hostname {host_arg} --skip-ssh-key を実行してください。"
                    )
            elif current != want:
                errors.append(
                    f"GitHub [{_host_label(host)}] アカウント不一致: "
                    f"現在={shell_word.shown(current)}, 期待={shell_word.shown(want)}"
                    + _switch_guidance(host, want)
                )
        return "\n".join(errors) if errors else None

    if not isinstance(expected, str):
        # verify() は先に `_expected_shape_error()` で弾くので通常ここには来ない。
        # 文面を二重に持たないよう同じ関数へ委譲する。
        return _expected_shape_error(expected)

    # str 形式では github.com を優先照合 (照合先の決定は scalar_target_host に
    # 一本化 — builder の show も同じ関数経由で同じ verdict を出す)。
    host = scalar_target_host(active)
    current = active[host]

    if current != expected:
        msg = (
            f"GitHub [{_host_label(host)}] アカウント不一致: "
            f"現在={shell_word.shown(current)}, 期待={shell_word.shown(expected)}"
            + _switch_guidance(host, expected)
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


_API_RE = re.compile(r"^gh\s+api(?=\s|$)")
# `gh api` の option (gh 2.9x `gh api --help`)。**安全側を列挙する allow-list**で、
# 未知の option は「読むだけ」と証明できないので QUERY にしない。危険な option を
# 列挙する denylist にすると、gh に option が増えるたび黙って穴が開く
# (同型の穴が反復した経緯は内部バックログ)。
_API_SAFE_OPTIONS_WITH_VALUE = frozenset({
    "--cache", "--header", "-H", "--hostname", "-h", "--jq", "-q",
    "--method", "-X", "--preview", "-p", "--template", "-t",
})
_API_SAFE_FLAGS = frozenset({
    "--include", "-i", "--paginate", "--silent", "--slurp", "--verbose",
})
# body / field を送る option。GET でも「書く API を叩く」形に化けるため除外する。
_API_BODY_OPTIONS = frozenset({"--field", "-F", "--raw-field", "-f", "--input"})
_API_READ_METHODS = frozenset({"GET", "HEAD"})


def _api_is_read_only(candidate: str) -> bool:
    """`gh api` が読み取りだけと**証明できる**形なら True。

    3 条件をすべて満たす必要がある:

    1. body / field を送る option (`-f` / `-F` / `--field` / `--raw-field` /
       `--input`) が無い
    2. `-X` / `--method` が無い、または値が `GET` / `HEAD`
    3. 付いている option が**すべて** allow-list に載っている
       (未知の option / 短縮の連結形 / 値の欠けた option は証明にならない)

    `-t` はここでは `--template` で、`gh auth status` の `--show-token` とは
    別物。DISCLOSING をコマンド形ごとに宣言しているのはこの衝突を避けるため。
    """
    if not _API_RE.search(candidate):
        return False
    try:
        tokens = shlex.split(candidate)
    except ValueError:
        return False
    method = ""
    i = 2  # `gh api` の後ろから
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok == "--":
            break
        if not tok.startswith("-") or tok == "-":
            continue  # endpoint などの operand
        name, eq, value = tok.partition("=")
        if not eq and not name.startswith("--") and len(name) > 2:
            # 短縮の連結形 (`-Xpost` / `-iq`) は値と flag の切り分けが曖昧なので
            # 「読むだけ」と証明しない。
            return False
        if name in _API_BODY_OPTIONS:
            return False
        if name in _API_SAFE_OPTIONS_WITH_VALUE:
            if eq:
                resolved = value
            elif i < len(tokens):
                resolved = tokens[i]
                i += 1
            else:
                return False
            if name in ("--method", "-X"):
                method = resolved.strip()
            continue
        if name in _API_SAFE_FLAGS:
            continue
        return False
    return not method or method.upper() in _API_READ_METHODS


def is_query(candidate: str) -> bool:
    """正規表現 (QUERY) で表せない QUERY 判定: option 次第で write になる `gh api`。"""
    return _api_is_read_only(candidate)


_SWITCH_RE = re.compile(r"^gh\s+auth\s+switch\b")


def _parse_auth_args(candidate: str) -> tuple[str | None, str | None]:
    """`gh auth <sub>` 候補から (--hostname, --user) の値を取り出す。

    `switch` と `refresh` の両方で使う (どちらも `-h/--hostname` と `-u/--user`
    を受ける)。位置ではなく token で読むので option の順序 / `=` 形 / 短縮形の
    違いに影響されない。
    """
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
    hostname, user = _parse_auth_args(candidate)
    if not user:
        return False
    if isinstance(expected, str):
        return user == expected
    if isinstance(expected, dict):
        want = expected.get(hostname or "github.com")
        return isinstance(want, str) and user == want
    return False


# --- 自動切替 (auto-switch、`core/auto_switch.py`) -------------------------------
#
# 不一致で deny する代わりに hook が `gh auth switch` を実行する opt-in の経路。
# 切替先は **gh に既にログイン済みのアカウント**に限る (ブラウザ認証の要る login は
# 自動化しない)。`gh auth switch --hostname <host> --user <user>` は非対話で、
# ネットワークに出ず hosts.yml と keyring の書き換えだけで終わる
# (gh 2.101.0 で実測: 約 50ms。未ログインのアカウント / host は rc=1 で何も変えない。
# 既にアクティブなアカウントへの切替は rc=0)。

_SWITCH_TIMEOUT_SEC = 10
_MAX_DETAIL_CHARS = 200

# 切替えても実行されるアカウントが変わらない / 切替先の host を決められない env。
# `_local_active_accounts` がローカル読取を諦める条件と同じ集合。
_NO_SWITCH_REASON_TOKEN = (
    "GH_TOKEN などのトークン用環境変数が設定されているため、gh の設定を切り替えても"
    "実行されるアカウントは変わりません。"
)
_NO_SWITCH_REASON_HOST = (
    "GH_HOST が設定されているため、切り替える host を hook からは決められません。"
)
_NOT_LOGGED_IN_TAIL = (
    " (未ログインか、保存されたトークンが無効です)。ログインはブラウザでの認証が"
    "要るため自動化しません。"
)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:_MAX_DETAIL_CHARS]
    return "詳細なし"


def plan_switch(expected, project_dir: str, env=None):
    """期待値へ切り替えるための `[(host, 現在のアカウント, 切替先)]` を返す。

    戻り値は `(steps, reason)`。切り替えられないなら `(None, 理由)`、既に一致して
    いて切り替える必要が無いなら `([], None)`。

    **全 host が切り替えられるときだけ** steps を返す (dict 期待値で 1 つでも
    切替先がログインしていなければ何も切り替えない)。一部だけ切り替えて結局 deny、
    という「副作用だけ残る」形を作らないため。

    現在値と切替先の候補は **`gh auth status` (CLI) から取る**。ローカル読取
    (`hosts.yml`) は allow にしか使わない方針 (`verify()` の docstring) で、ここは
    状態を書き換える前の確認なので gh 自身の報告に拠る。呼ばれるのは deny になる
    不一致のときだけなので、CLI 1 回分の時間は許容する。
    """
    shape_error = _expected_shape_error(expected)
    if shape_error:
        return None, shape_error
    e = os.environ if env is None else env
    if any(e.get(name) for name in _TOKEN_ENV_VARS):
        return None, _NO_SWITCH_REASON_TOKEN
    if e.get(_HOST_ENV_VAR):
        return None, _NO_SWITCH_REASON_HOST

    combined, err = _run_gh_auth_status(env)
    if err:
        return None, err
    active = parse_active_accounts(combined)
    logged_in = parse_logged_in_accounts(combined)
    if not active:
        return None, "gh のアクティブアカウントを取得できませんでした。"

    if isinstance(expected, str):
        wanted = {scalar_target_host(active): expected}
    else:
        wanted = expected

    steps = []
    missing = []
    for host, want in wanted.items():
        current = active.get(host)
        if current == want:
            continue
        if want not in logged_in.get(host, ()):
            missing.append(f"{shell_word.shown(host)} の {shell_word.shown(want)}")
            continue
        steps.append((host, current, want))
    if missing:
        reason = (
            f"{'、'.join(missing)} のログイン済みアカウントが見つかりません"
            + _NOT_LOGGED_IN_TAIL
        )
        if len(wanted) > 1:
            reason += " 切り替えられない host が 1 つでもあるときは、どの host も切り替えません。"
        return None, reason
    return steps, None


def apply_switch(steps, env=None):
    """`plan_switch` の steps を順に実行する。`(切り替えた steps, エラー)` を返す。

    CLI を起動する前ごとに予算を確かめる (`core/budget.py` の超過見積りは「最後の
    確認の後に起動する呼び出し数」を前提にしている)。途中で失敗したら残りは
    実行しない — 切り替えた分は戻り値の 1 要素目で呼び出し側に伝わる。
    """
    done = []
    for host, current, want in steps:
        if budget.expired():
            return done, "検証時間の予算を使い切ったため、残りの host を切り替えませんでした。"
        try:
            result = subprocess.run(
                ["gh", "auth", "switch", "--hostname", host, "--user", want],
                capture_output=True,
                text=True,
                timeout=budget.call_timeout(_SWITCH_TIMEOUT_SEC),
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return done, "gh コマンドが見つかりません。"
        except OSError as e:
            return done, f"gh コマンドを実行できません ({e})。"
        except subprocess.TimeoutExpired:
            return done, f"{shell_word.shown(host)} の切替がタイムアウトしました。"
        if result.returncode != 0:
            # CLI の出力の転記。端末の制御シーケンスや BEL を文面に残さない (v0.21.0)。
            detail = shell_word.escape_controls(_first_line(result.stderr or result.stdout))
            return done, f"{shell_word.shown(host)} の切替に失敗しました ({detail})。"
        done.append((host, current, want))
    return done, None


def describe_switch(switched) -> str:
    """自動切替したことを伝える本文 (`core/auto_switch.notice` が前置きを付ける)。"""
    changes = ", ".join(
        f"{shell_word.shown(host)}: {shell_word.shown(before) if before else '(なし)'} → "
        f"{shell_word.shown(after)}"
        for host, before, after in switched
    )
    return (
        f"gh のアクティブアカウントを切り替えました ({changes})。gh の設定は"
        "このマシンの全ターミナル・セッションで共有されるため、並行して動いている"
        "作業の gh もこのアカウントで動きます。"
    )


_AUTH_REFRESH_RE = re.compile(r"^gh\s+auth\s+refresh(?=\s|$)")


def changes_identity(candidate: str, expected) -> bool:
    """このセグメントが「次の gh がどのアカウントで動くか」を変えうるなら True。

    `STATE_CHANGING` は**成功キャッシュの破棄**が目的なので、アクティブアカウントを
    変えない `gh auth refresh` (既存アカウントの scope 追加) も含めてある。一方
    連結規則 (`core/dispatcher.py` の `_unexpected_switch_before_write`) の切替側は
    「identity が変わる形」だけに絞る必要があるため、inert な形をここで
    **allow-list として列挙**する (宣言しない service は既定で True = 従来どおり)。

    inert と言えるのは **`--user` / `-u` を伴わない `gh auth refresh`** だけ。
    `gh auth refresh --user other` はアクティブアカウントを other へ切り替えるため
    inert にしてはいけない (subcommand 単位で inert を宣言すると穴になる)。
    `switch` / `login` / `logout` はいずれも identity を変えるので対象外。
    """
    if _AUTH_REFRESH_RE.search(candidate):
        _hostname, user = _parse_auth_args(candidate)
        return user is not None
    return True
