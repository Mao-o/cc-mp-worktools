"""セグメントの tier 分類 (READONLY / QUERY / WRITE) と開示 option の取り消し。

**なぜ必要か**: 従来の判定は「READONLY (前方一致 regex) に当たれば素通し、
それ以外は全部検証して不一致なら deny」の 2 値だった。ここに 2 種類の欠陥がある:

1. **READONLY が option を一切審査しない。** `gh auth status` は素通しなのに
   `gh auth status --show-token` も素通しする = 期待外アカウントのトークンが
   平文で出る。同型の穴 (`kubectl config view --raw` /
   `aws configure get aws_secret_access_key` / `kubectl cluster-info dump`) は
   「コマンド名は安全だが option で内容・認証情報リーダーに化ける」クラスで、
   1 つずつ塞いでも次に何が化けるかは列挙し切れない (内部バックログ)
2. **リモート read が write と同じ deny 扱い。** `gh pr list` / `aws s3 ls` は
   資源を変更しないのに、不一致だと deny されて `gh auth switch`
   (ユーザー全体の gh 状態を変える副作用) を要求される。読むためにそこまで
   させるのは過剰で、離脱の直接要因になっていた

そこで **3 tier + 1 modifier** にする:

| tier | 意味 | 不一致時 |
|---|---|---|
| `READONLY` | ローカル / 情報系。検証しない | (検証しない) |
| `QUERY` | リモート read。検証はするが止めない | allow + `additionalContext` 警告 |
| `WRITE` (既定) | それ以外 | deny |

**`DISCLOSING` (modifier) は READONLY / QUERY を取り消して WRITE 扱いにする。**
認証情報を出力する形 / option がこれに当たる。判定の順序は
「DISCLOSING → READONLY → QUERY → WRITE」で、DISCLOSING が最優先。
`aws sts get-session-token` のように DISCLOSING と QUERY の両方に当たる形が
あるため、この順序自体をテストで固定してある。

**READONLY エントリは `READONLY_SAFE_OPTIONS` で「安全と言える option 集合」を
宣言できる。** 宣言があるエントリに宣言外の option が付いていたら、READONLY を
取り消して **QUERY へ降格**する (deny ではない)。「安全と証明できた形だけを
素通しする」= option の allow-list であって、コマンドの allow-list ではない。
**宣言が無いエントリは従来どおり option 無審査**なので、既存の READONLY
(ログイン系など、未ログインのデッドロックを解くためのエントリ) の挙動は変わらない。

降格先を deny ではなく QUERY にするのは lenient 方針との整合。降格は
「検証を走らせて結果を伝える」だけなので、未知の option が付いた状態確認コマンドで
新たな deny が生えることはない。

判定表を**緩める方向は QUERY の 1 箇所だけ**なので、QUERY に当てる条件は
「安全と証明できた形」に限る。regex で表せない形 (`gh api` は option 次第で
write になる) は service 側の `is_query()` が option の allow-list で判定する。
"""
from __future__ import annotations

import re

from core import cli_options

READONLY = "readonly"
QUERY = "query"
WRITE = "write"

# accounts.local.json の予約キー。`$` 始まりなので service キー
# (github / firebase / aws / gcloud / kubectl) と衝突しない。`$mode` と同じく
# **builder は書かない手編集キー**。
POLICY_KEY = "$readonly"
# QUERY 不一致を「通知だけ」にする (既定)。
POLICY_WARN = "warn"
# QUERY を WRITE と同じに扱う (= 0.13.0 までの挙動)。
POLICY_DENY = "deny"
VALID_POLICIES = (POLICY_WARN, POLICY_DENY)

# QUERY 不一致で返す additionalContext の前置き。deny と同じ本文
# (verify() が作った「現在=X 期待=Y — 切り替え: ...」) を渡すので、ここでは
# 「止めていないこと」「書込は止まること」「従来挙動に戻す方法」だけを書く。
# 文面を新造しないのは、切替案内を 2 箇所で持つと必ず片方が古くなるため。
QUERY_WARN_HEADER = (
    "[verify-cloud-account] リモート read のみのコマンドなので実行は止めませんが、"
    "アカウント検証は通っていません (書込系コマンドは deny されます)。"
    f'不一致でも従来どおり deny させるには accounts.local.json に "{POLICY_KEY}": '
    f'"{POLICY_DENY}" を書きます。'
)

# READONLY の option 審査で「宣言外」と見なさない名前。`_declared_option_names` が
# service の宣言から組み立てる。
_GLOBAL_DECLARATIONS = ("GLOBAL_OPTIONS_WITH_VALUE", "GLOBAL_FLAGS")

# `_readonly_verdict` の戻り値。
_PROVEN = "proven"  # option まで含めて READONLY と言える
_UNAUDITED = "unaudited"  # READONLY の形だが宣言外の option が付いている


def _with_value(service) -> frozenset[str]:
    """値を取る option の名前 (分離形の値 token を消費するために使う)。

    `find_context_options` と同じく context option も値を取る側に含める
    (どちらも `--opt value` の形を取りうるため)。
    """
    return frozenset(
        set(getattr(service, "GLOBAL_OPTIONS_WITH_VALUE", frozenset()))
        | set(getattr(service, "CONTEXT_OPTIONS", None) or {})
    )


def _declared_option_names(service) -> frozenset[str]:
    """service が既に宣言している global / context option の名前。

    これらは「CLI 名直後に置ける global option」「照合先を変える option」として
    別途審査済み (dispatcher が剥がす / 値を verify に渡す) なので、READONLY の
    option 審査では宣言外として数えない。数えると
    `kubectl config view --context x` のような日常形が毎回 QUERY に降格して
    警告を出すことになる。

    代償: 宣言済み global option のどれかが将来「開示する option」になった場合、
    この審査では検出できない (`DISCLOSING` 側に書く必要がある)。
    """
    names: set[str] = set()
    for attr in _GLOBAL_DECLARATIONS:
        names |= set(getattr(service, attr, frozenset()))
    names |= set(getattr(service, "CONTEXT_OPTIONS", None) or {})
    return frozenset(names)


def _option_names(form: str, service) -> frozenset[str]:
    return cli_options.find_option_names(form, _with_value(service))


def _is_disclosing(forms: tuple[str, ...], service) -> bool:
    """認証情報を出力する形 / option に当たれば True (READONLY / QUERY を取り消す)。

    `DISCLOSING` の各エントリは (コマンド形 regex, その形で開示に化ける option 名集合)。
    option 名集合が空なら**形そのものが開示**
    (`aws configure export-credentials` / `kubectl cluster-info dump`)。
    """
    for pattern, option_names in getattr(service, "DISCLOSING", ()):
        for form in forms:
            if not re.search(pattern, form):
                continue
            if not option_names:
                return True
            if set(option_names) & _option_names(form, service):
                return True
    return False


def _readonly_verdict(forms: tuple[str, ...], service):
    """READONLY 判定の 3 値: `_PROVEN` / `_UNAUDITED` / None (READONLY ではない)。

    regex に当たった形ごとに、その形の option を審査する。同じ形が複数の
    READONLY エントリに当たることがあるので、**どれか 1 つでも安全と言えれば
    `_PROVEN`**。1 つも言えず、しかし形としては当たっている場合が `_UNAUDITED`。
    """
    safe_map = getattr(service, "READONLY_SAFE_OPTIONS", None) or {}
    declared = _declared_option_names(service)
    unaudited = False
    for pattern in getattr(service, "READONLY", ()):
        allowed = safe_map.get(pattern)
        for form in forms:
            if not re.search(pattern, form):
                continue
            if allowed is None:
                # 宣言の無いエントリは従来どおり option 無審査。
                return _PROVEN
            if _option_names(form, service) <= (set(allowed) | declared):
                return _PROVEN
            unaudited = True
    # regex で表せない readonly (github の「鍵操作を伴わない gh auth login」) は
    # regex 審査の後で見る。先に見ると、宣言済みエントリの option 審査に落ちた形が
    # `is_readonly()` の判定機会を失う。判定中の例外は安全側 (READONLY にしない)。
    fn = getattr(service, "is_readonly", None)
    if fn is not None:
        try:
            if any(fn(form) for form in forms):
                return _PROVEN
        except Exception:
            pass
    return _UNAUDITED if unaudited else None


def _is_query(forms: tuple[str, ...], service) -> bool:
    """リモート read と証明できる形なら True。判定中の例外は安全側 (WRITE)。"""
    patterns = getattr(service, "QUERY", ())
    if any(re.search(p, form) for p in patterns for form in forms):
        return True
    fn = getattr(service, "is_query", None)
    if fn is None:
        return False
    try:
        return any(fn(form) for form in forms)
    except Exception:
        return False


def classify(forms: tuple[str, ...], service) -> str:
    """候補セグメントの tier を返す (`READONLY` / `QUERY` / `WRITE`)。

    `forms` は dispatcher が作る「元の形 + CLI 名直後の global option を剥がした形」。
    どちらかで証明できれば採用する (anchored pattern は剥がした形を前提に書かれて
    いるが、`aws --version` のように剥がすと CLI 名だけになる形もあるため)。
    """
    if _is_disclosing(forms, service):
        return WRITE
    verdict = _readonly_verdict(forms, service)
    if verdict == _PROVEN:
        return READONLY
    if verdict == _UNAUDITED:
        return QUERY
    return QUERY if _is_query(forms, service) else WRITE


def _normalize(raw) -> tuple[str | None, bool]:
    """(policy, invalid) を返す。空 / 未指定は (None, False) = 未設定扱い。"""
    if not isinstance(raw, str):
        return None, raw is not None
    value = raw.strip().lower()
    if not value:
        return None, False
    if value in VALID_POLICIES:
        return value, False
    return None, True


def policy_from_accounts(accounts) -> tuple[str, str | None]:
    """accounts.local.json の `"$readonly"` から policy を読む。`(policy, invalid_note)`。

    既定は `warn` (QUERY 不一致は通知のみ)。不正な値は **`deny` に倒す**
    (fail-closed = 検証を消す方向ではなく止める方向) が、黙って倒すと
    「`$readonly` を書いたのに警告にならない」の理由が分からないため note を返す。

    `"$mode"` と同じ制約が 2 つある: ファイルを**読めたときだけ**参加する
    (未設定 / JSON 破損 / パス競合の判定はファイルを読む前に確定する) 点と、
    グローバル既定のファイルに書いた場合は accounts.local.json を持たない
    プロジェクトにしか効かない点。
    """
    if not isinstance(accounts, dict) or POLICY_KEY not in accounts:
        return POLICY_WARN, None
    policy, invalid = _normalize(accounts[POLICY_KEY])
    if invalid:
        return POLICY_DENY, (
            f'accounts.local.json の "{POLICY_KEY}" の値が不正です '
            f"({' | '.join(VALID_POLICIES)} のいずれか)。{POLICY_DENY} として"
            "扱いました (読み取り専用コマンドの不一致も deny します)。"
        )
    return policy or POLICY_WARN, None
