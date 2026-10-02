"""案内するコマンドに、値をシェルの 1 語として埋め込む (v0.17.1)。

**なぜ必要か**: deny 文面の切替案内 (`kubectl config use-context <期待値>` 等) や
builder の `pin-env` が出す `firebase use <x>` は、Claude がそのまま実行しがちな
「次に打つコマンド」。値は accounts.local.json の期待値、リポジトリの `.firebaserc`、
CLI の設定 (AWS config の profile 名、gh の host 名) から来て、期待値のファイルや
`.firebaserc` はリポジトリに置かれうる (clone しただけのリポジトリでも中身を決められる)。
値に `;` / `$()` / 空白 / 改行などのシェルの構文や、option と解釈される先頭の `-` が
あると、案内どおりに打ったコマンドが別のコマンドや option として走る。

**どう埋め込むか**: 許容形に `fullmatch` した値だけをコマンドに出し、`shlex.quote` も
通す。許容形から外れた値はコマンドの形で案内せず、呼び出し側が「手で確認してください」
の文に落とす (`UNSAFE` を添える)。許容形の文字は `shlex.quote` がクォートを付けない
範囲に収めてあるので、普通の値の文面 (と、案内したコマンドを通す
`is_self_remediation` / 注記を付ける `REMEDIATION_PATTERNS` が見る形) は変わらない。
quote は許容形を将来緩めたときの二重化。

案内ではなく、**検出したコマンド自身が指定した値** (`--context` / `--profile` 等) を
文面に示すときは、検証せずに `shlex.quote` だけを通す (そのコマンドの引数をそのまま
示すためで、外れた値を隠すと何を指定したかが分からなくなる)。
"""
from __future__ import annotations

import re
import shlex

# 英数字で始まり、英数字と `.` `_` `-` だけからなる名前。firebase の alias /
# project ID (deny 文面の `firebase use` / `--project` と builder の `pin-env`)。
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
NAME_RULE = "使えるのは英数字で始まり、英数字と . _ - だけからなる名前"

# NAME に `:` `/` `@` `+` を足したもの。kubectl の context 名 (EKS の
# `arn:aws:eks:<region>:<account>:cluster/<name>`、kubeadm の
# `kubernetes-admin@kubernetes`)、gcloud の account (メールアドレス) とドメイン付きの
# project ID (`example.com:my-project`)、AWS の profile 名、gh の host 名 / アカウント名。
# 足した 4 文字はシェルで特別な意味を持たず、`shlex.quote` もクォートを付けない
# (`~` / `=` / `%` / `,` は入れない。`~` と `=` は語の先頭で展開されうる)。
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")

# 許容形から外れた値の代わりに置く文 (コマンドの形をとらない。「〜は」に続ける)。
UNSAFE = "シェルの構文や option として解釈されうる文字を含むため、コマンドの形では案内しません"


def arg(value, pattern: re.Pattern = WORD) -> str | None:
    """value を案内コマンドの 1 引数にした文字列。許容形でなければ None。

    照合は `fullmatch` で行う (`$` は末尾の改行の前でも一致するため)。
    """
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return None
    return shlex.quote(value)
