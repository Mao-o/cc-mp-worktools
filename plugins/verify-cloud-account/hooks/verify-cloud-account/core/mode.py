"""検証モード (enforce / warn / off) の解決。

**なぜ必要か**: 従来は「不一致なら常に deny」しか無く、一時的に止める手段が
`/plugin disable` か accounts.local.json の書き換えだけだった (後者は builder 経由で
しか触らない運用と衝突する)。user scope で install すると accounts.local.json を
置いていない**全プロジェクト**で `gh` / `aws` の deny が始まるため、
「まず黙らせたい」人の出口が無いことが離脱の直接要因になっていた (内部バックログ)。

**判定表 (allow/deny/warn) そのものは変えない**。mode が決めるのは
「照合をいつ走らせるか / 結果を deny として返すか」だけ:

| mode | 挙動 |
|---|---|
| `enforce` (既定) | 従来どおり。不一致・未設定・競合はすべて deny |
| `warn` | 検証は走らせ、deny 相当の結果を `additionalContext` (通知のみ) で返す |
| `off` | 検証そのものを行わない (CLI も起動しない) |

**既定は `enforce`**。env も `"$mode"` も無い環境の挙動は従来と完全に同じ。

解決順 (先に決まったものが勝つ):

1. 環境変数 `VERIFY_CLOUD_ACCOUNT_MODE` (settings.json の `env` / 起動時 env)
2. accounts.local.json の予約キー `"$mode"` (プロジェクト単位)
3. `enforce`

env を上に置くのは、**ファイルを書き換えずに一時的に外せる**ことが escape hatch の
要件だから。`"$mode"` を下に置くのは、プロジェクトの設定より「今のセッションの
指示」を優先したいため。

`"$mode"` の適用範囲には 2 つの前提がある (どちらも env には無い制約):

- グローバル既定のファイルに書いた `"$mode"` が効くのは
  **accounts.local.json を持たないプロジェクトだけ**。プロジェクト側で解決できた
  ときは global を一切読まない (`core/paths.resolve_accounts_file_for_verification`)
  ため、自前の設定を持つプロジェクトには効かない
- `"$mode"` は**そのファイルを読めたときだけ**参加する。未設定 / JSON 破損 /
  パス競合 (D4) の deny はファイルを読む前に確定するので、そこを弱められるのは
  `VERIFY_CLOUD_ACCOUNT_MODE` のみ (`core/dispatcher.py` の `pre_file_mode`)

不正な値 (`VERIFY_CLOUD_ACCOUNT_MODE=yes` 等) は **enforce に倒す** (fail-closed)。
ただし黙って無視すると「off にしたのに deny される」が理由不明になるため、
`invalid_note` を返して deny / warn の文面に併記する。
"""
from __future__ import annotations

import os

ENV_VAR = "VERIFY_CLOUD_ACCOUNT_MODE"
# accounts.local.json の予約キー。`$` 始まりなので service キー
# (github / firebase / aws / gcloud / kubectl) と衝突しない。
MODE_KEY = "$mode"

ENFORCE = "enforce"
WARN = "warn"
OFF = "off"
VALID_MODES = (ENFORCE, WARN, OFF)

# deny 文面の末尾に添える案内。deny を消すのが目的の相手に**期待値を書き換える**
# 方向 (builder の `set --from-cli --commit`) を勧めると、間違ったアカウントを
# 正解として焼き付ける使い方を誘発する。mode は期待値に触らず「止める / 通す」
# だけを変えるので、こちらを 1 行で案内する。
DENY_HINT = (
    f"※ 一時的に検証を止めるには {ENV_VAR}=warn (警告のみ) または {ENV_VAR}=off "
    f"(検証しない) を設定してください。プロジェクト単位なら accounts.local.json に "
    f'"{MODE_KEY}": "warn" を書きます。'
)

# warn モードで返す additionalContext の前置き。deny と同じ本文を渡すので、
# 「止めていない」ことと「enforce に戻すと deny になる」ことを明示する。
WARN_HEADER = (
    "[verify-cloud-account] warn モードのため実行は止めません "
    f"(enforce に戻すと deny します)。{ENV_VAR}=enforce または accounts.local.json の "
    f'"{MODE_KEY}" 削除で従来の挙動に戻ります。'
)


def _normalize(raw) -> tuple[str | None, bool]:
    """(mode, invalid) を返す。空 / 未指定は (None, False) = 未設定扱い。"""
    if not isinstance(raw, str):
        return None, raw is not None
    value = raw.strip().lower()
    if not value:
        return None, False
    if value in VALID_MODES:
        return value, False
    return None, True


def from_env(env=None) -> tuple[str | None, str | None]:
    """環境変数から mode を読む。`(mode, invalid_note)`。

    env: 参照する環境 (None なら `os.environ`)。
    """
    source = os.environ if env is None else env
    raw = source.get(ENV_VAR)
    if raw is None:
        return None, None
    mode, invalid = _normalize(raw)
    if invalid:
        return None, (
            f"{ENV_VAR}={raw!r} は不正な値です "
            f"({' | '.join(VALID_MODES)} のいずれか)。enforce として扱いました。"
        )
    return mode, None


def from_accounts(accounts) -> tuple[str | None, str | None]:
    """accounts.local.json の `"$mode"` から mode を読む。`(mode, invalid_note)`。"""
    if not isinstance(accounts, dict):
        return None, None
    if MODE_KEY not in accounts:
        return None, None
    raw = accounts[MODE_KEY]
    mode, invalid = _normalize(raw)
    if invalid:
        return None, (
            f'accounts.local.json の "{MODE_KEY}" の値が不正です '
            f"({' | '.join(VALID_MODES)} のいずれか)。enforce として扱いました。"
        )
    return mode, None


def effective(env_mode: str | None, file_mode: str | None) -> str:
    """env → `"$mode"` → 既定 (enforce) の順で有効な mode を決める。"""
    return env_mode or file_mode or ENFORCE
