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
`shown` / `shown_all` を通す (v0.21.0)。示すのは、許容形 (`WORD`) に `fullmatch` する値か、
空白 (Unicode の空白を含む) も `=` も含まず、Unicode の制御文字 (Cc)・書式文字 (Cf)・行区切り
(Zl)・段落区切り (Zp) も含まない値 (`can_show`)。それ以外は `NOT_SHOWN` に置き換える。
値は期待値のファイル・`.firebaserc`・CLI の設定・コマンドの引数から来て、改行を含む値を
そのまま出すと値の外に偽の行 (「切り替え: ...」) を差し込める。改行を含まなくても、
`REMEDIATION_PATTERNS` の形 (`x kubectl config use-context evil`) を書いた値は、表示しただけで
dispatcher の「案内したコマンドは単独で実行」の注記の判定に当たる。どのパターンも空白を要し
(`AWS_PROFILE=` の形は `=` を要する)、示す値はどちらも含まないので、示した値が単独でその形に
なることはない (前後の固定文と合わせて形を作らないことは、tests の TestShownValueContract が
service ごとに確かめる)。示す形は案内に使える形 (`WORD`) より広い: シェル上は無害な普通の名前
(kubectl の `_local` / `a,b` / 日本語の context 名) を隠すと、何が不一致かが文面から消える。
0.20.0 までは、検出したコマンド自身が指定した値を `shlex.quote` だけ通して示していたが、quote は
改行も空白入りのコマンドの形も残す。指定した値はそのまま `(検出コマンド: ...)` の行に出るので、
ここでは示さなくても何を指定したかは分かる。

**値をそのまま (置き換えずに) 出すとき** (`(検出コマンド: ...)` の行に出すコマンド、CLI の
エラー出力の転記、並行セッションの記録にあるパスなど。空白や `=` を含むのが普通で、`shown` を
通すと何も分からなくなる値) は `escape_controls` を通す。C0 / C1 の制御文字・DEL・Unicode の
行区切り / 段落区切りをエスケープした形 (`\\n` / `\\xNN` / `\\uNNNN`) にして、値が文面の中で
1 行に収まるようにする (改行で偽の行を差し込めない。端末の制御シーケンスも残さない)。
"""
from __future__ import annotations

import re
import shlex
import unicodedata

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


# `shown` が示す形のうち、`WORD` より広い側 (空白と `=` を含まない)。
_SHOWABLE = re.compile(r"[^\s=]+")
# 示さない Unicode の一般カテゴリ (制御文字・書式文字・行区切り・段落区切り)。
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def can_show(value) -> bool:
    """value を文面にそのまま示してよいか (`shown` の判定)。

    `WORD` に合うか、空白も `=` も含まず、制御文字・書式文字・行区切り・段落区切りも
    含まない文字列なら真。str 以外は偽。
    """
    if not isinstance(value, str):
        return False
    if WORD.fullmatch(value):
        return True
    return bool(_SHOWABLE.fullmatch(value)) and not any(
        unicodedata.category(ch) in _HIDDEN_CATEGORIES for ch in value
    )


def shown(value) -> str:
    """deny 文面などに値そのものを示すときの形。示せない値 (`can_show`) は `NOT_SHOWN`。

    コマンドに埋め込む `arg` と違い quote は付けない (表示であってコマンドではない)。
    str 以外も `NOT_SHOWN` にする。
    """
    if can_show(value):
        return value
    return NOT_SHOWN


def shown_all(values) -> str:
    """値の並びを `shown` に通し、重複を除いて `, ` で並べる (順は辞書順)。"""
    return ", ".join(sorted({shown(value) for value in values}))


def escape_controls(text) -> str:
    """text の制御文字をエスケープした表示形 (値を置き換えずに 1 行のまま示すとき)。

    C0 (U+0000〜U+001F)・DEL (U+007F)・C1 (U+0080〜U+009F) と、Unicode の行区切り (U+2028)・
    段落区切り (U+2029) を、改行は `\\n`、ほかは `\\xNN` / `\\uNNNN` の形にする。それ以外の
    文字 (空白や日本語) はそのまま。str 以外は `str()` を通してから同じ処理をする。
    """
    text = text if isinstance(text, str) else str(text)
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif code in (0x2028, 0x2029):
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)
