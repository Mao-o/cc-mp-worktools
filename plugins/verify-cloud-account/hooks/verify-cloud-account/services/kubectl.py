"""Kubernetes (kubectl CLI) アクティブコンテキスト検証。"""
from __future__ import annotations

import re
import subprocess

from core import budget, cli_options

# `\b` だと `kubectl-foo` のような plugin バイナリまで kubectl として拾うため、
# 空白または終端が続く形だけに限定する。
PATTERNS = [r"^kubectl(?=\s|$)"]
# option を審査する READONLY エントリは名前付き定数にする (READONLY_SAFE_OPTIONS /
# DISCLOSING が同じ文字列をキーに参照するため。literal を 2 箇所に書くと、片方を
# 直したときに宣言が黙って無効化される)。
_RO_CONFIG_VIEW = (
    r"^kubectl\s+config\s+(current-context|get-contexts|view|get-clusters|get-users)\b"
)
READONLY = [
    _RO_CONFIG_VIEW,
    r"^kubectl\s+cluster-info\b",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^kubectl\s+(--version|--help|version|help)\b",
]
# `kubectl config` の表示系で「これが付いていても readonly」と言える option。
# ここに無い option (`--merge` 等) が付いていたら READONLY を取り消して QUERY に
# 降格する (= 検証は走るが不一致でも止めない)。`--raw` / `--flatten` は DISCLOSING
# 側で WRITE 扱いにする。
READONLY_SAFE_OPTIONS = {
    _RO_CONFIG_VIEW: frozenset({"-o", "--output", "--minify"}),
}
# 認証情報を出力する形 / option。READONLY / QUERY を取り消して WRITE 扱いにする。
# - `config view --raw` / `--flatten`: 既定では redact される kubeconfig の
#   credential を平文で出す。**`--flatten` は `--raw` 無しでも平文で出る**
#   (実測 kubectl v1.34.1: `config view --flatten` は token / client-key-data を
#   そのまま出力し、option 無し / `--minify` だけなら REDACTED / DATA+OMITTED)。
#   `--flatten` は「self-contained な kubeconfig を作る」option なので、
#   redact された値では用途を満たさない = 開示されるのが仕様
# - `cluster-info dump`: 形そのものが広範なダンプ (option 無しで開示)
# - `get secret(s) -o yaml|json`: Secret の `data` は base64 エンコードだけで実質
#   平文。`aws secretsmanager get-secret-value` と同じ「リモートの secret を
#   出力する read」なので、QUERY の `^kubectl\s+get` より先に WRITE へ倒す。
#   option を付けない `kubectl get secrets` (名前一覧) / `describe secret`
#   (値を出さない) は QUERY のまま。resource 名は option の後ろにも置けるので
#   (`get -o yaml secret x`)、位置を固定せず「どこかに secret token がある形」で書く
DISCLOSING = [
    (_RO_CONFIG_VIEW, frozenset({"--raw", "--flatten"})),
    (r"^kubectl\s+cluster-info\s+dump(?=\s|$)", frozenset()),
    (
        r"^kubectl\s+get\b(?=(?:\s+\S+)*\s+(secrets?|secret/\S+)(?=\s|$))",
        frozenset({"-o", "--output", "--template"}),
    ),
]
# リモート read (資源を変更しない)。不一致でも deny せず警告のみで通す。
QUERY = [
    r"^kubectl\s+(get|describe|logs|top|explain|api-resources|api-versions)(?=\s|$)",
]
# current-context (kubeconfig) を変えうるコマンド。dispatcher が検出すると kubectl の
# 成功 cache を破棄する。`set-context --current --namespace=x` のように context 名を
# 変えない操作も含むが、過剰な破棄は再検証 1 回のコストで済む。連結規則の切替側だけが
# identity を変える形に絞る (下の `changes_identity`)。
STATE_CHANGING = [
    r"^kubectl\s+config\s+(use-context|use|set-context|set-cluster|set-credentials"
    r"|set|unset|delete-context|delete-cluster|delete-user|rename-context)\b",
    # 別 CLI / plugin 経由で kubeconfig の current-context を書き換える形。PATTERNS
    # (`^kubectl`) には一致しないが、dispatcher は全 service の STATE_CHANGING を
    # 全候補に当てるため、これらの直後の kubectl write も再検証される。
    r"^kubectl\s+ctx\b",
    r"^kubectx\b",
    # gcloud は同じコマンドを alpha / beta でも公開しており、どちらも kubeconfig を
    # 更新して current context を変える (SDK 生成物 `data/cli/gcloud_completions.py`
    # の command tree で `beta`/`alpha` 配下の実在を確認。preview 形は無い)。
    # GA 形だけを anchor していると track 形が無効化をすり抜ける (Codex R5 P1-B)。
    r"^gcloud\s+(?:(?:alpha|beta)\s+)?container\s+clusters\s+get-credentials\b",
    r"^aws\s+eks\s+update-kubeconfig\b",
    r"^az\s+aks\s+get-credentials\b",
]
# CLI 名直後に置ける global option (`kubectl --context x config use-context ...`)。
# dispatcher が剥がした形でも READONLY / STATE_CHANGING / self-remediation を判定する
# (core/cli_options.py)。
GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    "--context", "--kubeconfig", "--namespace", "-n", "--cluster", "--user", "--server",
    "-s", "--token", "--as", "--as-group", "--as-uid", "--cache-dir",
    "--certificate-authority", "--client-certificate", "--client-key", "--password",
    "--username", "--request-timeout", "--tls-server-name", "--profile",
    "--profile-output", "--log-flush-frequency", "-v", "--v", "--vmodule",
})
GLOBAL_FLAGS = frozenset({
    "--insecure-skip-tls-verify", "--match-server-version", "--warnings-as-errors",
    "--disable-compression",
})
# 「どの context / kubeconfig に対して実行するか」をコマンド側で指定する option
# (v0.9.0)。dispatcher が候補全体から値を拾い verify に渡す。
# 旧 `_context_override` (このモジュール専用の regex) は共通スキャナ
# (core/cli_options.find_context_options) に置き換えて削除した — `--context=x` /
# `--context x` しか見ておらず、値を取る他 option の値に現れた `--context` を
# 誤採用しうるうえ、どこからも呼ばれていなかった。
CONTEXT_OPTIONS = {"--context": "context", "--kubeconfig": "kubeconfig"}
ACCOUNT_KEY = "kubectl"

# deny 文面で案内する remediation コマンド (引数付きの実コマンド形) の正規表現。
# dispatcher は verify() の返り値にこれが一致するときだけ「単独で実行せよ」の注記を
# 付ける。コマンド名だけの言及 (「firebase use がタイムアウトしました」等の診断文) や
# インストール案内・設定ファイルの型不正には一致させない。
REMEDIATION_PATTERNS = (r"kubectl config use-context\s+\S",)
SETUP_HINT = (
    'kubectl 最小例: {"kubectl": "my-context-name"}。'
    "kubectl config current-context で現在値を確認可"
)
# builder (scripts/accounts_builder.py) の書込前スキーマ検証が参照する契約。
# kubectl の期待値は context 名の文字列のみ (verify() も isinstance str のみ受理)。
# dict を受け付けないので DICT_ALLOWED_KEYS / DICT_VALUE_CHECK /
# SCALAR_EQUIVALENT_DICT_KEY はいずれも宣言しない (builder は dict 自体を弾く)。
ACCEPTS_DICT = False


def _run_current_context(env=None, kubeconfig=None) -> tuple[str | None, str | None]:
    """kubectl config current-context を実行し (context, error_reason) を返す。

    env: コマンド行頭のインライン環境変数をマージした完全 env (`KUBECONFIG` 等)。
    None なら hook プロセスの環境を継承する。
    kubeconfig: 候補コマンドが `--kubeconfig X` を指定していた場合の値。
    同じ option を検証コマンドにも渡し、実行時と同じファイルの current-context を読む。
    """
    argv = ["kubectl", "config", "current-context"]
    if kubeconfig:
        argv += ["--kubeconfig", kubeconfig]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=budget.call_timeout(10),
            env=env,
        )
    except FileNotFoundError:
        return None, "kubectl: kubectl コマンドが見つかりません。"
    except OSError as e:
        # 実行権限なし / 形式不正等。例外を漏らすと hook が異常終了して無音 fail-open になる。
        return None, f"kubectl: kubectl コマンドを実行できません ({e})。"
    except subprocess.TimeoutExpired:
        return None, "kubectl: kubectl config current-context がタイムアウトしました。再試行するか、ネットワーク接続を確認してください。"
    current = result.stdout.strip()
    return (current or None), None


def get_active_account(project_dir: str) -> str | None:
    """現在のアクティブ kubectl context 名を返す。取得不可なら None。"""
    current, _err = _run_current_context()
    return current


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "kubectl" キーに書く値を提案する (context 文字列)。"""
    return get_active_account(project_dir)


def verify(expected, project_dir: str, env=None, context=None) -> str | None:
    """context: 候補コマンドのコンテキスト option (`{"context": ..., "kubeconfig": ...}`)。

    `--context X` はその実行だけ context を差し替えるので、現在の
    current-context ではなく **X 自体**を期待値と照合する。`--kubeconfig X` は
    検証コマンドにも渡して同じファイルの current-context を読む。
    """
    if not isinstance(expected, str):
        return (
            f'kubectl: accounts.local.json の "{ACCOUNT_KEY}" 値は文字列で指定してください。'
        )

    ctx = context or {}
    override = ctx.get("context")
    if override is not None:
        if override == expected:
            return None
        return (
            f"kubectl コンテキスト不一致: コマンド指定 --context={override}, "
            f"期待={expected} — --context を外すか --context {expected} を指定してください"
        )

    current, err = _run_current_context(env, ctx.get("kubeconfig"))
    if err:
        return err

    if current is None:
        return (
            f"kubectl: アクティブコンテキストが設定されていません。"
            f"kubectl config use-context {expected} を実行してください。"
        )

    if current != expected:
        return (
            f"kubectl コンテキスト不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: kubectl config use-context {expected}"
        )

    return None


_USE_CONTEXT_RE = re.compile(r"^kubectl\s+config\s+use-context\s+(\S+)\s*$")

# self-remediation 判定で**剥がしてよい** option の allow-list。基準は
# 「`use-context` が書き込む先を変えないこと」。kubectl の global option は
# ほとんどが着地先を変えてしまう (`--kubeconfig` は別ファイルへ書き、`--context`
# はその実行の照合先を差し替える) ため、**残るのは verbosity / timeout だけ**。
# 短い allow-list になるが、これが「着地先が verify() の見る場所
# (既定 kubeconfig の current-context) と一致し続ける」と言える範囲。
_DECORATION_FLAGS: frozenset[str] = frozenset()
_DECORATION_OPTIONS_WITH_VALUE = frozenset({"--v", "-v", "--request-timeout"})


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待コンテキストへの use-context」なら True。

    装飾 option (`--v=` / `--request-timeout=`) は剥がしてから照合し、**それ以外の
    option が付いていたら保守的に False** (通常検証に落とす)。`--kubeconfig` /
    `--context` は着地先そのものを変えるので剥がさない。
    """
    normalized = cli_options.strip_allowed_options(
        candidate, _DECORATION_FLAGS, _DECORATION_OPTIONS_WITH_VALUE
    )
    if normalized is None:
        return False
    m = _USE_CONTEXT_RE.match(normalized)
    if not m:
        return False
    return isinstance(expected, str) and m.group(1) == expected


# inert と言える唯一の形。宣言 option を全部剥がした残りがこれと一致し、かつ
# `--current` が書かれているときだけ「current-context 名を変えない」と主張する
# (位置引数が残れば一致しない)。
_INERT_SET_CONTEXT_RE = re.compile(r"^kubectl\s+config\s+set-context\s*$")
# `set-context --current` に付いていても identity を変えないと言える option。
# `--namespace` / `-n` は同じ context 内の既定 namespace を変えるだけ。
# **`--user` / `--cluster` は入れない** — context 名を変えないまま認証情報 /
# 接続先を差し替える = `set-credentials` と同じ「同名で別 identity」になる。
# `--kubeconfig` も入れない (着地先が既定ファイルと言い切れない)。
_INERT_SET_CONTEXT_FLAGS = frozenset({"--current"})
_INERT_SET_CONTEXT_OPTIONS_WITH_VALUE = frozenset({"--namespace", "-n"})


def changes_identity(candidate: str, expected) -> bool:
    """このセグメントが「次の kubectl がどの context で動くか」を変えうるなら True。

    `STATE_CHANGING` は**成功キャッシュの破棄**が目的なので `kubectl config` の
    書込系と別 CLI 経由の kubeconfig 書換えを全部含めてある。一方連結規則
    (`core/dispatcher.py` の `_unexpected_switch_before_write`) の切替側は
    「identity が変わる形」だけに絞る必要があるため、inert な形をここで
    **allow-list として列挙**する (宣言しない service は既定で True = 従来どおり)。

    kubectl の期待値は **current-context 名**で、verify() は
    `kubectl config current-context` の値だけを読む。inert と言えるのは
    `kubectl config set-context --current [--namespace=x]` の形
    (最頻出 idiom) だけ — current-context 名を変えないので、実行前の検証結果が
    実行後の状態も記述する。

    inert にしない形:

    - `set-credentials` / `set-cluster` — context 名は変わらないまま identity が
      差し替わる (同名で別 identity)
    - `set-context <名前> ...` (位置引数あり) — 別 context の定義を書き換える
    - `--user` / `--cluster` / `--kubeconfig` 等の宣言外 option 付き
    """
    # `--current` が**書かれていること**を先に要求する。剥がした後の形だけで見ると
    # `set-context --namespace=foo` (名前も --current も無い不正形) が inert に
    # 見えてしまう。
    names = cli_options.find_option_names(candidate, GLOBAL_OPTIONS_WITH_VALUE)
    if "--current" not in names:
        return True
    normalized = cli_options.strip_allowed_options(
        candidate,
        _INERT_SET_CONTEXT_FLAGS,
        _INERT_SET_CONTEXT_OPTIONS_WITH_VALUE,
    )
    if normalized is None:
        return True
    return not _INERT_SET_CONTEXT_RE.match(normalized)
