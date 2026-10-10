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
の文に落とす (`UNSAFE` を添える)。dispatcher は `UNSAFE` を含む deny に「案内した
コマンドは単独で実行」の注記を付けない (`期待=...` の表示がコマンドの形に見えても、
それを案内したことにしない。外れた名前を文に置き換えたうえで必ず案内を出す aws は
除く)。許容形の文字は `shlex.quote` がクォートを付けない
範囲に収めてあるので、普通の値の文面 (と、案内したコマンドを通す
`is_self_remediation` / 注記を付ける `REMEDIATION_PATTERNS` が見る形) は変わらない。
quote は許容形を将来緩めたときの二重化。

**値を文面に示すとき** (`現在=` / `期待=` / コマンド指定 / alias の行き先 / host など。
コマンドの形で案内するのではなく、値そのものを見せる表示) は、出どころに関わらず
`shown` / `shown_all` を通す (v0.21.0)。許容形 (`WORD`) に `fullmatch` した値だけを示し、
それ以外は `NOT_SHOWN` に置き換える。値は期待値のファイル・`.firebaserc`・CLI の設定・
コマンドの引数から来て、改行を含む値をそのまま出すと値の外に偽の行 (「切り替え: ...」) を
差し込める。改行を含まなくても、`REMEDIATION_PATTERNS` の形 (`x kubectl config
use-context evil`) を書いた値は、表示しただけで dispatcher の「案内したコマンドは単独で
実行」の注記の判定に当たる。`WORD` は空白も `=` も含まないので、示した値が単独でその形に
なることはない (前後の固定文と合わせて形を作らないことは、tests の
TestShownValueContract が service ごとに確かめる)。0.20.0 までは、検出したコマンド
自身が指定した値を `shlex.quote` だけ通して示していたが、quote は改行も空白入りの
コマンドの形も残す。指定した値はそのまま `(検出コマンド: ...)` の行に出るので、
ここでは示さなくても何を指定したかは分かる。
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
# project ID (`example.com:my-project`)、AWS の profile 名、gh の host 名 / アカウント名、
# firebase の dict の案内行で `#` の後ろに置く project (コメントなので alias より広くてよい)。
# 足した 4 文字はシェルで特別な意味を持たず、`shlex.quote` もクォートを付けない
# (`~` / `=` / `%` / `,` は入れない。`~` と `=` は語の先頭で展開されうる)。
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")

# 許容形から外れた値の代わりに置く文 (コマンドの形をとらない。「〜は」「〜が」に続ける)。
# 「危険な文字を含む」とは言わない: 許容形は狭く、シェル上は無害な値 (kubectl の
# `_local` / `a,b` / 日本語の context 名、`firebase use` に渡す `example.com:proj` など)
# も外れる。言えるのは「案内に使える形ではない」という事実だけ。
UNSAFE = (
    "案内に使える形 (英数字で始まり、英数字と一部の記号だけからなる形) ではないため、"
    "コマンドの形では案内しません"
)


def arg(value, pattern: re.Pattern = WORD) -> str | None:
    """value を案内コマンドの 1 引数にした文字列。許容形でなければ None。

    照合は `fullmatch` で行う (`$` は末尾の改行の前でも一致するため)。
    """
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return None
    return shlex.quote(value)


# 文面に示せない値の代わりに置く語 (`shown`)。コマンドの形も `UNSAFE` の文も含まない。
NOT_SHOWN = "(表示しない値)"


def shown(value) -> str:
    """deny 文面などに値そのものを示すときの形。許容形 (`WORD`) でなければ `NOT_SHOWN`。

    コマンドに埋め込む `arg` と違い quote は付けない (`WORD` の値は quote しても変わらない)。
    str 以外も `NOT_SHOWN` にする。
    """
    if isinstance(value, str) and WORD.fullmatch(value):
        return value
    return NOT_SHOWN


def shown_all(values) -> str:
    """値の並びを `shown` に通し、重複を除いて `, ` で並べる (順は辞書順)。"""
    return ", ".join(sorted({shown(value) for value in values}))
