"""CLI 名直後の global option を剥がす (service 共通ヘルパー)。

`aws [global options] <command> <subcommand>` のように、多くの CLI はサブコマンドの
前に global option を置ける (`aws --profile prod sso login` /
`gcloud --project x config set ...` / `kubectl --context x config use-context ...` /
`firebase -P x use ...`)。READONLY / STATE_CHANGING / self-remediation の anchored
pattern (`^aws\\s+sso\\s+login`) はこの形に一致しないため、dispatcher は判定の前に
option を剥がして `aws sso login` に正規化する (元の形も併せて判定する)。

剥がした option は dict で返す。`--profile` / `--project` / `--context` の値は
将来の flag 照合 (bd_092a232e-629.4) で検証に反映するために捨てない。
各 service は `GLOBAL_OPTIONS_WITH_VALUE` (値を取る option) / `GLOBAL_FLAGS`
(値を取らない option) を宣言する。`--help` / `--version` は含めない (剥がすと
`aws --version` が `aws` になり readonly 判定から外れるため、元の形で判定させる)。
"""
from __future__ import annotations

import shlex
from collections.abc import Collection

_TRUE_VALUES = frozenset({"true", "t", "yes", "y", "1"})
_FALSE_VALUES = frozenset({"false", "f", "no", "n", "0"})


def _flag_value(value: str) -> str | bool:
    """`--flag=<value>` の値を bool として解釈する (できなければ生文字列のまま)。

    **剥がすかどうかの判定には使わない** (値に関わらず剥がす)。将来の flag 照合
    (bd_092a232e-629.4) のために書かれた形を保つだけ。
    """
    v = value.strip().lower()
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    return value


def strip_leading_options(
    candidate: str,
    with_value: Collection[str],
    flags: Collection[str],
) -> tuple[str, dict[str, str | bool]]:
    """候補の CLI 名直後に並ぶ global option を剥がし (正規化後の候補, option dict) を返す。

    - `--opt value` / `--opt=value` (with_value) と `--flag` / `--flag=<bool>` (flags)
      を先頭から順に消費する。`--` は option の終端 (それ自体も消費する)。
      boolean option の値は**剥がすかどうかに影響しない** (下記 `_flag_value`)
    - 剥がすのは CLI 名直後の option だけで、その後ろの subcommand 列はそのまま
      残る。つまり正規化しても「どの操作が走るか」は変わらないため、剥がし過ぎが
      別の操作への誤判定になることはない
    - 未知の option (どちらの集合にも無い) や値の欠けた option に当たったら、値を取るか
      分からないのでそこで打ち切り **候補を変更せず**に返す (保守的: readonly / 切替
      判定に乗らず通常検証される)
    - option を 1 つも剥がさなかった場合は元の文字列をそのまま返す (quote を保つ)。
      剥がした場合は `shlex.join` で再構成する (quote 付き引数は再分割可能な形で保つ)
    """
    try:
        tokens = shlex.split(candidate)
    except ValueError:
        return candidate, {}
    if len(tokens) < 2:
        return candidate, {}
    opts: dict[str, str | bool] = {}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if not tok.startswith("-") or tok == "-":
            break
        name, eq, value = tok.partition("=")
        if name in with_value:
            if eq:
                opts[name] = value
                i += 1
                continue
            if i + 1 >= len(tokens):
                return candidate, {}
            opts[name] = tokens[i + 1]
            i += 2
            continue
        if name in flags:
            # boolean option は `--flag=true` / `--flag=false` の値付き形も取りうる
            # (Go の pflag / cobra は bool に分離形 `--flag value` を許さず `=` 形のみ)。
            # **値の真偽で剥がすかどうかを変えない** — 剥がす目的は後続の subcommand を
            # 見つけることであって、flag の実効値は「どの操作が走るか」に影響しない。
            # (Codex R5 P1-A: `kubectl --insecure-skip-tls-verify=true config
            # use-context other` が剥がせず STATE_CHANGING に当たらず、切替後も
            # 古い成功 cache が TTL 分残っていた)
            opts[name] = _flag_value(value) if eq else True
            i += 1
            continue
        return candidate, {}
    if i == 1:
        return candidate, {}
    return shlex.join([tokens[0], *tokens[i:]]), opts
