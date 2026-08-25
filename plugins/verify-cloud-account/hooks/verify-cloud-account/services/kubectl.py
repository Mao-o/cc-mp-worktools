"""Kubernetes (kubectl CLI) アクティブコンテキスト検証。"""
from __future__ import annotations

import re
import subprocess

# `\b` だと `kubectl-foo` のような plugin バイナリまで kubectl として拾うため、
# 空白または終端が続く形だけに限定する。
PATTERNS = [r"^kubectl(?=\s|$)"]
READONLY = [
    r"^kubectl\s+config\s+(current-context|get-contexts|view|get-clusters|get-users)\b",
    r"^kubectl\s+cluster-info\b",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^kubectl\s+(--version|--help|version|help)\b",
]
# current-context (kubeconfig) を変えうるコマンド。dispatcher が検出すると kubectl の
# 成功 cache を破棄する。`set-context --current --namespace=x` のように context 名を
# 変えない操作も含むが、過剰な破棄は再検証 1 回のコストで済む。
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
SETUP_HINT = (
    'kubectl 最小例: {"kubectl": "my-context-name"}。'
    "kubectl config current-context で現在値を確認可"
)


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
            timeout=10,
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


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待コンテキストへの use-context」なら True。"""
    m = _USE_CONTEXT_RE.match(candidate)
    if not m:
        return False
    return isinstance(expected, str) and m.group(1) == expected
